"""Recover SQL text from Python source, or admit we cannot.

Static screening of `spark.sql(...)` only works if the SQL string can be recovered
before execution. In agent-written PySpark it usually is not a literal -- it is
typically an f-string or concatenation over module-level constants. Regex-over-source
misses those entirely; constant propagation does not.

`StringFolder` is a deliberately narrow abstract interpreter: it tracks bindings whose
value is a compile-time constant string (or int/float for format specs) and folds
string-building expressions over them. It handles f-strings (including nested format
specs, `!r`/`!a`/`!s` conversions and folded expressions as replacement fields), `+`,
`%` formatting with scalar and tuple arguments, `str.format`, `"".join`, and the
harmless `str` methods.

Two rules make it a security tool rather than a convenience:

1. Every call the folder recognises as a SQL sink produces exactly one entry, in
   `resolved` or in `unresolved` -- never neither, and never both. A sink dropped
   on the floor is a false ALLOW. The dicts are keyed by a per-sink ordinal, not by
   line number, because a line is not a sink identity: `spark.sql("DROP TABLE a");
   spark.sql("DROP TABLE b")` is one line and two sinks, and keying by line silently
   discarded one. `StringFolder.lines` maps the ordinal back to its line.
2. A binding is only a constant while nothing has invalidated it. A name rebound by
   a loop target, a `with`/`except` target, a function parameter, a comprehension
   variable, an augmented assignment, a `global` write, or `del` loses its value;
   a function or class body never contributes to the enclosing scope. A stale value
   is worse than no value: the operator reads a clean, specific finding for a
   statement that will never run that way.

What it deliberately does NOT do: track types, model exceptions, or evaluate arbitrary
calls. When it cannot prove a value, it returns `None` -- and `None` flows into the
UNKNOWN verdict, never into "safe".

Known gaps, recorded rather than papered over:

  * Constants are not threaded into or out of function bodies. `def run(tbl):
    spark.sql(f"drop table {tbl}")` is unresolved even when every caller passes a
    literal. That is the correct fail-closed outcome, but a policy written against
    heavily-factored agent code will see more UNKNOWN than a human reviewer expects.
    Interprocedural constant propagation is the obvious next step.
  * An f-string field with both a conversion and a format spec applies only the spec
    (`f"{x!r:>8}"` is folded as if `!r` were absent), because the converted value is
    no longer a native value that `format` accepts. Fail-closed in practice: such a
    field only occurs in code whose intent is already obscure, and the fold errs
    toward showing text the author can still eyeball.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

MAX_FOLD_DEPTH = 24
MAX_CONST_STRING = 1 << 20  # 1 MiB; guards against pathological `"a" * 10**9`
MAX_CONST_NUMBER = 1 << 53  # guards against pathological numeric folding


@dataclass(frozen=True)
class FoldFailure:
    """Why a SQL argument could not be reduced to a constant."""

    reason: str
    expression: str

    def __str__(self) -> str:
        return f"{self.reason}: {self.expression}"


@dataclass(frozen=True, order=True)
class SinkKey:
    """Identity of one SQL sink call.

    Not a line number, on purpose: a line is not a sink identity. `spark.sql("DROP
    TABLE a"); spark.sql("DROP TABLE b")` is one line and two sinks, and keying by
    line silently discarded one -- which is how `spark.sql("DROP TABLE prod.users");
    spark.sql("select 1")` came back ALLOW. The `ordinal` is a tiebreaker so the key
    is unique even in the pathological cases `col_offset` cannot separate, and so
    `sorted()` is deterministic.
    """

    line: int
    col: int
    ordinal: int


class _Missing:
    """Sentinel for "this expression has no provable value".

    Distinct from a folded `None`/empty string, and distinct from the public
    `None`-means-unresolved return of `fold`.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<missing>"

    def __bool__(self) -> bool:
        return False


MISSING = _Missing()


def _is_num(v: object) -> bool:
    """True for int/float, excluding bool (whose arithmetic surprises are not worth it)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class _Frame:
    """One lexical scope's constant table.

    `parent` is the enclosing *visible* scope, or None when this frame is a boundary
    (a function or class body sees only its own bindings -- see the module docstring).
    `globals` is shared by every frame belonging to one function body, so a
    `global x` declaration anywhere in that body routes the binding correctly.
    """

    __slots__ = ("table", "parent", "globals")

    def __init__(
        self,
        parent: "_Frame | None" = None,
        *,
        isolate: bool = False,
        globals_: set[str] | None = None,
    ) -> None:
        self.table: dict[str, object] = {}
        self.parent = None if (isolate or parent is None) else parent
        if globals_ is not None:
            self.globals = globals_
        elif parent is not None:
            self.globals = parent.globals
        else:
            self.globals = set()


class StringFolder(ast.NodeVisitor):
    """Fold string-building expressions to constants, tracking lexical scopes."""

    def __init__(self) -> None:
        self._frames: list[_Frame] = [_Frame()]
        #: sink key -> resolved SQL text, for every sink found
        self.resolved: dict[SinkKey, str] = {}
        #: sink key -> FoldFailure, for sinks we could not resolve
        self.unresolved: dict[SinkKey, FoldFailure] = {}

    @property
    def consts(self) -> dict[str, object]:
        """The module-level constant table. Sinks are found in every scope."""
        return self._frames[0].table

    # -- scopes ------------------------------------------------------------

    def _frame(self) -> _Frame:
        return self._frames[-1]

    def _push(self, frame: _Frame) -> None:
        self._frames.append(frame)

    def _pop(self) -> _Frame:
        return self._frames.pop()

    def _lookup(self, name: str) -> object:
        for frame in reversed(self._frames):
            if name in frame.table:
                return frame.table[name]
        return MISSING

    def _invalidate(self, target: ast.AST) -> None:
        """Drop any constant bound to the name(s) in `target`."""
        for name in target_names(target):
            self._frame().table.pop(name, None)

    def _bind(self, name: str, value: ast.AST) -> None:
        """Bind `name` to `value` if it folds, otherwise drop any previous binding.

        A binding is constant only if the whole right-hand side folds. This
        deliberately misses `"".join(x for x in y)` and dict lookups -- those
        become UNKNOWN, which is the correct outcome.
        """
        frame = self._frame()
        if name in frame.globals and frame is not self._frames[0]:
            # `global x` inside a function body: the write may never happen (the
            # function may never be called) and it may happen at an unknown time, so
            # the module binding is poisoned rather than overwritten. UNKNOWN beats a
            # confidently wrong value. `nonlocal` is handled the same way -- it can
            # only ever cost us a resolution, never give a wrong one.
            self._frames[0].table.pop(name, None)
            return
        v = self._value(value, 0)
        if v is MISSING:
            frame.table.pop(name, None)
        else:
            frame.table[name] = v

    def _block(self, body: list[ast.stmt], *, leak: bool = True) -> None:
        """Visit statements in a child frame.

        `leak=True` (if/for/while/with/try/match bodies): the body may never run, so
        every name it writes is invalidated in the parent afterwards. `leak=False`
        (comprehension scopes) is exact -- Python gives comprehensions their own
        scope and nothing inside one escapes it.
        """
        parent = self._frame()
        self._push(_Frame(parent))
        try:
            for stmt in body:
                self.visit(stmt)
        finally:
            child = self._pop()
        if leak:
            for name in child.table:
                self._invalidate_name(name, parent)

    def _invalidate_name(self, name: str, frame: _Frame) -> None:
        if name in frame.globals and frame is not self._frames[0]:
            self._frames[0].table.pop(name, None)
        else:
            frame.table.pop(name, None)

    def _body_scope(
        self, body: list[ast.stmt], args: ast.arguments | None = None
    ) -> None:
        """Enter a function/class/lambda body: isolated, parameters not constants."""
        declared: set[str] = set()
        nonlocal_names: set[str] = set()
        for node in body:
            _declared_globals(node, declared)
            _declared_nonlocals(node, nonlocal_names)
        # A `nonlocal q` rebinds `q` in an enclosing function frame. We cannot see
        # when that enclosing function runs relative to a sink in it, so any name
        # declared nonlocal anywhere below is poisoned in every enclosing frame -- a
        # wrong specific value is worse than UNKNOWN.
        for name in nonlocal_names:
            self._poison_enclosing(name, declared)
        frame = _Frame(self._frame(), isolate=True, globals_=declared)
        if args is not None:
            for name in argument_names(args):
                frame.table[name] = MISSING
        self._push(frame)
        try:
            self._block(body)
        finally:
            self._pop()

    def _poison_enclosing(self, name: str, own_globals: set[str]) -> None:
        """Invalidate `name` in every frame between here and the nearest module frame.

        Stops at the first frame that actually holds the name, or at the module frame:
        beyond that the name belongs to a different scope entirely.
        """
        for frame in reversed(self._frames):
            if frame.globals is own_globals:
                continue  # our own body's globals declaration, not an enclosing frame
            if frame.parent is None:
                frame.table.pop(name, None)
                return
            if name in frame.table:
                frame.table.pop(name, None)
                return

    # -- constant folding ---------------------------------------------------

    def _scalar(self, node: ast.AST) -> object | None:
        """A literal str/int/float/bool value, or None."""
        v = self._value(node, 0)
        return v if v is not MISSING else None

    def _value(self, node: ast.AST, depth: int) -> object:
        """Reduce `node` to its native Python value, or MISSING.

        Native types matter: `f"{a + b}"` with two strings concatenates, but with two
        ints it must add. Folding to a string and formatting that would report SQL the
        engine never sees, so arithmetic is only folded when both operand types are
        known.
        """
        if depth > MAX_FOLD_DEPTH:
            return MISSING

        if isinstance(node, ast.Constant) and isinstance(
            node.value, (str, int, float, bool)
        ):
            return node.value
        if isinstance(node, ast.Name):
            return self._lookup(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            inner = self._value(node.operand, depth + 1)
            if _is_num(inner):
                try:
                    return -inner if isinstance(node.op, ast.USub) else inner
                except Exception:  # pragma: no cover - defensive
                    return MISSING
            return MISSING
        if isinstance(node, ast.JoinedStr):
            return self._fold_fstring(node, depth)
        if isinstance(node, ast.BinOp):
            return self._fold_binop(node, depth)
        if isinstance(node, ast.Call):
            return self._fold_call(node, depth)
        if isinstance(node, ast.IfExp):
            # only safe when both branches agree, value *and* type
            b = self._value(node.body, depth + 1)
            o = self._value(node.orelse, depth + 1)
            if b is not MISSING and type(b) is type(o) and b == o:
                return b
            return MISSING
        return MISSING

    def fold(self, node: ast.AST, depth: int = 0) -> str | None:
        """Reduce `node` to a concrete string, or None if not provable."""
        v = self._value(node, depth)
        if v is MISSING:
            return None
        return v if isinstance(v, str) else str(v)

    def _spec(self, node: ast.AST, depth: int) -> str | None:
        """A format spec, which may itself contain a replacement field."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            out = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    out.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    inner = self.fold(v.value, depth + 1)
                    if inner is None:
                        return None
                    out.append(inner)
                else:
                    return None
            return "".join(out)
        return None

    def _fold_fstring(self, node: ast.JoinedStr, depth: int) -> object:
        out: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                raw = self._value(v.value, depth + 1)
                if raw is MISSING:
                    return MISSING
                conv = v.conversion
                if conv == 114:      # !r
                    text = repr(raw)
                elif conv == 97:     # !a
                    text = ascii(raw)
                else:                # !s or none
                    text = raw if isinstance(raw, str) else str(raw)
                if v.format_spec is not None:
                    fspec = self._spec(v.format_spec, depth + 1)
                    if fspec is None:
                        return MISSING
                    # format against the *native* value so {n:04d} works
                    try:
                        text = format(
                            raw, fspec[1:] if fspec.startswith(":") else fspec
                        )
                    except Exception:
                        return MISSING
                out.append(text)
            else:
                return MISSING
        result = "".join(out)
        return result if len(result) <= MAX_CONST_STRING else MISSING

    def _fold_binop(self, node: ast.BinOp, depth: int) -> object:
        left = self._value(node.left, depth + 1)
        if left is MISSING:
            return MISSING
        op = node.op

        if isinstance(op, ast.Mod):
            if isinstance(node.right, (ast.Tuple, ast.List)):
                args = [self._value(e, depth + 1) for e in node.right.elts]
                if any(a is MISSING for a in args):
                    return MISSING
                try:
                    result = left % tuple(args)
                except Exception:
                    return MISSING
                return _checked_str(result)
            right = self._value(node.right, depth + 1)
            if right is MISSING:
                return MISSING
            if _is_num(left) and _is_num(right):
                return _checked_num(left % right)
            try:
                result = left % right
            except Exception:
                return MISSING
            return _checked_str(result)

        if isinstance(op, ast.Add):
            right = self._value(node.right, depth + 1)
            if right is MISSING:
                return MISSING
            if isinstance(left, str) and isinstance(right, str):
                return _checked_str(left + right)
            if _is_num(left) and _is_num(right):
                return _checked_num(left + right)
            return MISSING

        if isinstance(op, ast.Mult):
            right = self._value(node.right, depth + 1)
            if right is MISSING:
                return MISSING
            if isinstance(left, str) and isinstance(right, int) and not isinstance(
                right, bool
            ):
                # A float multiplier is a TypeError at runtime, not a repeat count.
                if 0 <= right * len(left) <= MAX_CONST_STRING:
                    return left * right
                return MISSING
            if _is_num(left) and _is_num(right):
                return _checked_num(left * right)
            return MISSING

        if isinstance(op, (ast.Sub, ast.Div, ast.FloorDiv)):
            right = self._value(node.right, depth + 1)
            if right is MISSING or not (_is_num(left) and _is_num(right)):
                return MISSING
            try:
                result = {
                    ast.Sub: lambda: left - right,
                    ast.Div: lambda: left / right,
                    ast.FloorDiv: lambda: left // right,
                }[type(op)]()
            except Exception:
                return MISSING
            return _checked_num(result)

        return MISSING

    def _fold_call(self, node: ast.Call, depth: int) -> object:
        f = node.func
        # "...".format(...) and "...".format(a, b)
        if isinstance(f, ast.Attribute) and f.attr == "format" and isinstance(
            f.value, ast.Constant
        ):
            tmpl = self.fold(f.value, depth + 1)
            if tmpl is None:
                return MISSING
            args = []
            for a in node.args:
                v = self._value(a, depth + 1)
                if v is MISSING:
                    return MISSING
                args.append(v)
            try:
                return _checked_str(tmpl.format(*args))
            except Exception:
                return MISSING
        # sep.join([...])
        if isinstance(f, ast.Attribute) and f.attr == "join":
            sep = self.fold(f.value, depth + 1)
            if sep is None or not node.args:
                return MISSING
            arg = node.args[0]
            if isinstance(arg, (ast.List, ast.Tuple)):
                parts = [self.fold(e, depth + 1) for e in arg.elts]
            elif isinstance(arg, (ast.GeneratorExp, ast.ListComp)):
                return MISSING  # comprehension contents may depend on runtime state
            else:
                return MISSING
            if any(p is None for p in parts):
                return MISSING
            return _checked_str(sep.join(parts))
        # str methods on a folded receiver
        if isinstance(f, ast.Attribute) and f.attr in (
            "upper", "lower", "strip", "lstrip", "rstrip", "title", "capitalize"
        ):
            recv = self.fold(f.value, depth + 1)
            if recv is None:
                return MISSING
            return getattr(recv, f.attr)()
        if isinstance(f, ast.Attribute) and f.attr == "replace":
            recv = self.fold(f.value, depth + 1)
            if recv is None or len(node.args) != 2:
                return MISSING
            a = self._value(node.args[0], depth + 1)
            b = self._value(node.args[1], depth + 1)
            if a is MISSING or b is MISSING:
                return MISSING
            return _checked_str(recv.replace(a, b))
        return MISSING

    # -- bindings -----------------------------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._bind(target.id, node.value)
            else:
                # `a, b = ...` / `d[k] = ...` / `o.x = ...`: the name (if any) no
                # longer holds whatever it held before.
                self._invalidate(target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._bind(node.target.id, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # `q += " WHERE x=1"` rebinds `q` to something we do not model. Reporting the
        # pre-update value as the recovered SQL would be a confident wrong answer, so
        # the binding is dropped and the sink becomes UNKNOWN.
        self._invalidate(node.target)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        if isinstance(node.target, ast.Name):
            self._bind(node.target.id, node.value)
        self.visit(node.value)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._invalidate(target)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        self._frame().globals.update(node.names)

    def visit_Nonlocal(self, node: ast.Nonlocal) -> None:
        # Treated like `global`: we cannot see the binding it refers to, so the
        # name is poisoned. Fail-closed by construction.
        self._frame().globals.update(node.names)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._invalidate_name(alias.asname or alias.name.split(".")[0],
                                 self._frame())
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                continue
            self._invalidate_name(alias.asname or alias.name, self._frame())
        self.generic_visit(node)

    # -- control flow -------------------------------------------------------

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.target)
        self._invalidate(node.target)  # the loop variable outlives the loop
        self.visit(node.iter)
        self._block(node.body)
        self._block(node.orelse)

    visit_AsyncFor = visit_For

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self._block(node.body)
        self._block(node.orelse)

    def visit_While(self, node: ast.While) -> None:
        self.visit(node.test)
        self._block(node.body)
        self._block(node.orelse)

    def visit_With(self, node: ast.With) -> None:
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._invalidate(item.optional_vars)
        self._block(node.body)

    visit_AsyncWith = visit_With

    def visit_Try(self, node: ast.Try) -> None:
        self._block(node.body)
        for handler in node.handlers:
            if handler.name:
                self._invalidate_name(handler.name, self._frame())
            self._block(handler.body)
        self._block(node.orelse)
        self._block(node.finalbody)

    visit_TryStar = visit_Try

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            for name in pattern_names(case.pattern):
                self._invalidate_name(name, self._frame())
            self._block(case.body)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node, [node.elt], isolated=False)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node, [node.elt], isolated=False)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node, [node.key, node.value], isolated=False)

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        # A generator body is consumed later, possibly after module-level rebinding,
        # so it is treated as deferred: only its own bindings are visible.
        self._comprehension(node, [node.elt], isolated=True)

    def _comprehension(
        self, node: ast.AST, elements: list[ast.AST], *, isolated: bool
    ) -> None:
        frame = _Frame(self._frame(), isolate=isolated)
        self._push(frame)
        try:
            for gen in node.generators:  # type: ignore[attr-defined]
                for name in target_names(gen.target):
                    frame.table[name] = MISSING
                self.visit(gen.iter)
                for cond in gen.ifs:
                    self.visit(cond)
            for element in elements:
                self.visit(element)
        finally:
            self._pop()

    # -- scopes with deferred execution -------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._invalidate_name(node.name, self._frame())
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in _defaults(node.args):
            self.visit(default)
        self._body_scope(node.body, node.args)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for default in _defaults(node.args):
            self.visit(default)
        self._body_scope([ast.Expr(value=node.body)], node.args)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._invalidate_name(node.name, self._frame())
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for kw in node.keywords:
            self.visit(kw.value)
        self._body_scope(node.body)

    # -- sinks --------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        is_sink = (
            isinstance(f, ast.Attribute) and f.attr in SQL_METHODS
        ) or (
            isinstance(f, ast.Name) and f.id in SQL_FUNCS
        )
        if is_sink:
            key = SinkKey(node.lineno, node.col_offset, len(self.resolved)
                          + len(self.unresolved))
            sql, failure = self._sink_sql(node)
            if failure is not None:
                self.unresolved[key] = failure
            else:
                self.resolved[key] = sql  # type: ignore[arg-type]
        self.generic_visit(node)

    def _sink_sql(self, node: ast.Call) -> tuple[str | None, FoldFailure | None]:
        """(sql, None) when the call is a sink we recovered, else (None, failure).

        Every recognised sink lands in exactly one of the two outcomes. A call with no
        visible SQL argument is a failure, not a non-sink: `spark.sql(**kw)` and
        `spark.sql(query=q)` both execute SQL we cannot see.
        """
        if node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Starred):
                return None, FoldFailure(
                    "sql argument is unpacked, so the text is not visible", _src(arg)
                )
            sql = self.fold(arg)
            if sql is None:
                return None, FoldFailure(
                    "sql argument is not a compile-time constant", _src(arg)
                )
            return sql, None

        for keyword in node.keywords:
            if keyword.arg is None:
                return None, FoldFailure(
                    "sql is passed via **kwargs, so the text is not visible",
                    _src(node),
                )
            if keyword.arg in SQL_KEYWORD_ARGS:
                sql = self.fold(keyword.value)
                if sql is None:
                    return None, FoldFailure(
                        f"sql keyword argument {keyword.arg!r} is not a "
                        f"compile-time constant",
                        _src(keyword.value),
                    )
                return sql, None

        return None, FoldFailure(
            "no sql argument is visible on this call", _src(node)
        )


def _checked_str(result: Any) -> object:
    if isinstance(result, str) and len(result) <= MAX_CONST_STRING:
        return result
    return MISSING


def _checked_num(result: Any) -> object:
    if _is_num(result) and abs(result) <= MAX_CONST_NUMBER:
        return result
    return MISSING


def _defaults(args: ast.arguments) -> list[ast.expr]:
    return [d for d in list(args.defaults) + list(args.kw_defaults) if d is not None]


def argument_names(args: ast.arguments) -> list[str]:
    """Every name a call binds: positional, keyword-only, *args and **kwargs."""
    out = [a.arg for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)]
    for extra in (args.vararg, args.kwarg):
        if extra is not None:
            out.append(extra.arg)
    return out


def target_names(target: ast.AST) -> list[str]:
    """Names bound by an assignment/loop/with target, including tuple unpacking."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [n for e in target.elts for n in target_names(e)]
    if isinstance(target, ast.Starred):
        return target_names(target.value)
    return []


def pattern_names(pattern: ast.AST) -> list[str]:
    """Names a `match` pattern captures."""
    if isinstance(pattern, ast.MatchAs):
        return [pattern.name] if pattern.name else pattern_names(pattern.pattern)
    if isinstance(pattern, ast.MatchStar):
        return [pattern.name] if pattern.name else []
    if isinstance(pattern, ast.MatchMapping):
        return [pattern.rest] if pattern.rest else []
    if isinstance(pattern, ast.MatchClass):
        out: list[str] = []
        for sub in pattern.patterns:
            out.extend(pattern_names(sub))
        for sub in pattern.kwd_patterns:
            out.extend(pattern_names(sub))
        return out
    if isinstance(pattern, (ast.MatchOr, ast.MatchSequence)):
        out = []
        for p in pattern.patterns:
            out.extend(pattern_names(p))
        return out
    if isinstance(pattern, ast.MatchValue):
        return []
    return []


def _declared_globals(node: ast.AST, out: set[str]) -> None:
    """Collect `global`/`nonlocal` names declared in a function body.

    Not descending into nested scopes: an inner `global` belongs to that function.
    """
    _scan_declarations(node, out, (ast.Global, ast.Nonlocal))


def _declared_nonlocals(node: ast.AST, out: set[str]) -> None:
    """Collect `nonlocal` names declared in a function body."""
    _scan_declarations(node, out, (ast.Nonlocal,))


def _scan_declarations(
    node: ast.AST, out: set[str], kinds: tuple[type[ast.AST], ...]
) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                        ast.Lambda)):
        return
    if isinstance(node, kinds):
        out.update(node.names)
        return
    for child in ast.iter_child_nodes(node):
        _scan_declarations(child, out, kinds)


def _src(node: ast.AST, limit: int = 120) -> str:
    try:
        text = ast.unparse(node)
    except Exception:
        text = "<expr>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


#: DataFrame/SparkSession/SQLContext methods whose first string argument is SQL.
SQL_METHODS = frozenset({"sql", "sqlQuery"})

#: module-level function names treated as SQL sinks.
SQL_FUNCS = frozenset({"sql", "sqlQuery"})

#: Keyword spellings PySpark uses for the SQL text itself. `args`, `hiveCtx`,
#: `queryTimeout` and friends are *not* here: they are parameters of the SQL call, not
#: the statement. Being generous with the list is the safe direction -- an extra name
#: only ever produces an UNKNOWN, never a missed sink.
SQL_KEYWORD_ARGS = frozenset({
    "query", "sql", "sqlQuery", "sql_query", "sqlStr", "sql_str", "sqlText",
    "sql_text", "statement",
})


def fold_sinks(tree: ast.AST) -> StringFolder:
    """Run the folder over a parsed module and collect resolved/unresolved sinks."""
    folder = StringFolder()
    folder.visit(tree)
    return folder
