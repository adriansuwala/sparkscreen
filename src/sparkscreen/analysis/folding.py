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
   a loop target, a `with`/`except` target, an unresolved function parameter, a
   comprehension variable, an augmented assignment, a `global` write, or `del` loses
   its value; a function or class body never contributes to the enclosing scope. A
   stale value is worse than no value: the operator reads a clean, specific finding
   for a statement that will never run that way.

What it deliberately does NOT do: track types, model exceptions, or evaluate arbitrary
calls. When it cannot prove a value, it returns `None` -- and `None` flows into the
UNKNOWN verdict, never into "safe".

Two bounded reachability steps sit on top of the intra-procedural folder. Both exist
only to prove *more* values, and both refuse whenever they would have to guess:

  * **Loop unrolling.** `for t in ["prod.a", "prod.b"]: spark.sql(f"DROP {t}")` runs
    the body once per element, so it yields one entry per element -- the same rule as
    "two sinks on one line are two sinks", applied to two executions of one sink. The
    iterable must be a literal list/tuple of foldable elements, the body must contain
    no loop or loop control (`break`/`continue`/`return`/`yield`), and the run is
    charged against `MAX_LOOP_UNROLL` and `MAX_UNROLL_TOTAL`. A loop that does not
    qualify is visited exactly once, as before, and reports UNKNOWN.
  * **Parameter resolution.** A module-level `def` gets its parameters bound when
    *every* call site of that name in the file passes a literal, every call site
    agrees on every parameter's value and type, the defaults used are literals, and
    the name is nowhere else bound or used as a value. Then
    `def drop(t): spark.sql(f"DROP TABLE {t}")` with `drop("prod.users")` resolves.
    Any other shape leaves the parameters unresolved.

Known gaps, recorded rather than papered over:

  * Resolution is per-file and one hop deep. A function called only from another
    module, a function passed around as a value, a recursive or mutually recursive
    pair, a decorator, a closure, a generator, `*args`/`**kwargs` at either end, and
    any second-level call (`f(g("x"))`) are all UNKNOWN -- the argument value is not
    provable from what is visible here.
  * Unrolling is all-or-nothing. A loop over more than `MAX_LOOP_UNROLL` elements,
    or one that pushes the module past `MAX_UNROLL_TOTAL`, is visited exactly once
    and reports UNKNOWN for the whole loop -- truncating would report a prefix of the
    statements the loop issues and say nothing about the rest, which is the false
    ALLOW this module exists to prevent.
  * An f-string field with both a conversion and a format spec applies only the spec
    (`f"{x!r:>8}"` is folded as if `!r` were absent), because the converted value is
    no longer a native value that `format` accepts. Fail-closed in practice: such a
    field only occurs in code whose intent is already obscure, and the fold errs
    toward showing text the author can still eyeball.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from typing import Any, Sequence

MAX_FOLD_DEPTH = 24
MAX_CONST_STRING = 1 << 20  # 1 MiB; guards against pathological `"a" * 10**9`
MAX_CONST_NUMBER = 1 << 53  # guards against pathological numeric folding

#: Iterations unrolled from a single `for`. Bounds the work one loop can ask for,
#: so `[x] * 10_000` cannot turn screening into a timeout.
MAX_LOOP_UNROLL = 32

#: Iterations unrolled across one whole module, at any nesting depth. Nested literal
#: loops multiply, so the per-loop cap alone is not a bound on total work.
MAX_UNROLL_TOTAL = 256


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


@dataclass
class _SinkOutcome:
    """Every execution of one syntactic sink, and whether they agree.

    A sink inside an unrolled loop is executed once per element, and those executions
    do not necessarily send the same SQL. Storing them as a set -- rather than
    letting the last write win -- is what makes "one sink call is one finding" and
    "we never report a statement that will never run" both true at once:

      * they agree (one text, or every failure) -> exactly one entry, as today;
      * they disagree -> one entry per distinct outcome, because reporting only one
        of the statements the code really issues would be a specific, wrong finding,
        and dropping the others would be a false ALLOW.

    A sink that both resolved and failed is poisoned: it is both "we could not read
    it" and "we did", which is exactly the ambiguity a screener must not resolve in
    its own favour.
    """

    resolved: set[str] = field(default_factory=set)
    failures: list[FoldFailure] = field(default_factory=list)

    def record(self, sql: str | None, failure: FoldFailure | None) -> None:
        if failure is not None:
            self.failures.append(failure)
        elif sql is not None:
            self.resolved.add(sql)

    @property
    def poisoned(self) -> bool:
        return bool(self.resolved) and bool(self.failures)

    def entries(self) -> list[tuple[str | None, FoldFailure | None]]:
        """The (sql, failure) pairs this sink contributes, in a stable order.

        A poisoned sink contributes only its failure: the resolutions beside it
        cannot all be true, and UNKNOWN is the verdict that does not pick a winner.
        """
        if self.poisoned:
            return [(None, self.failures[0])]
        if self.resolved:
            return [(sql, None) for sql in sorted(self.resolved)]
        return [(None, self.failures[0])] if self.failures else []


def _is_num(v: object) -> bool:
    """True for int/float, excluding bool (whose arithmetic surprises are not worth it)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class _Frame:
    """One lexical scope's constant table.

    `parent` is the enclosing *visible* scope, or None when this frame is a boundary
    (a function or class body sees only its own bindings -- see the module docstring).
    `globals` is shared by every frame belonging to one function body, so a
    `global x` declaration anywhere in that body routes the binding correctly.

    `poisoned` holds names this frame invalidated that it did not itself hold. It
    exists because a child frame cannot see the parent's table: `t = "prod.a"` then
    `if c: del t` leaves the child's table empty, so without this the rebinding is
    invisible and the stale `prod.a` survives -- a stale binding reported as a clean,
    specific statement that will never run.
    """

    __slots__ = ("table", "parent", "globals", "poisoned")

    def __init__(
        self,
        parent: "_Frame | None" = None,
        *,
        isolate: bool = False,
        globals_: set[str] | None = None,
    ) -> None:
        self.table: dict[str, object] = {}
        self.poisoned: set[str] = set()
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
        #: sink key -> resolved SQL text, for every sink found. Populated by
        #: `_flush`, which runs once the whole module has been walked -- a sink
        #: reached by more than one execution (an unrolled loop iteration) must be
        #: reconciled before it becomes a finding.
        self.resolved: dict[SinkKey, str] = {}
        #: sink key -> FoldFailure, for sinks we could not resolve
        self.unresolved: dict[SinkKey, FoldFailure] = {}
        #: (line, col) -> what that one syntactic sink turned out to send
        self._sinks: dict[tuple[int, int], _SinkOutcome] = {}
        #: module-level defs whose parameters are provable from their call sites
        self._bindings: dict[ast.FunctionDef, dict[str, object]] = {}
        #: unroll iterations still allowed in this module
        self._unroll_budget = MAX_UNROLL_TOTAL

    def visit(self, node: ast.AST) -> None:
        """Walk, then reconcile sinks once the whole module has been seen.

        Reconciliation needs the whole module: whether an unrolled loop's body is
        resolvable can depend on a later iteration, and a parameter binding needs
        every call site, including ones below the definition.
        """
        if not isinstance(node, ast.Module):
            super().visit(node)
            return
        self._bindings = provable_parameter_bindings(node)
        super().visit(node)
        self._flush()

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
        """Drop any constant bound to the name(s) in `target`.

        The binding is dropped from the frame that actually holds it, not just from
        the frame doing the invalidating. Block bodies get a child frame, so
        `t = "prod.a"` then `if c: del t` used to leave `prod.a` visible for the rest
        of the block and resolve a statement the code cannot reach. Walking up until
        the scope boundary keeps the deletion honest without letting a function body
        reach into the module's table.
        """
        for name in target_names(target):
            self._drop_binding(name)
            self._frame().poisoned.add(name)

    def _drop_binding(self, name: str) -> None:
        """Remove `name`'s constant from the nearest frame that holds it."""
        frame: _Frame | None = self._frame()
        while frame is not None:
            frame.table.pop(name, None)
            # `parent is None` marks a scope boundary: a `del` inside a function
            # deletes a local, it does not unbind the module's name.
            frame = None if frame.parent is None else frame.parent

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
            frame.poisoned.add(name)
            return
        v = self._value(value, 0)
        if v is MISSING:
            # A right-hand side we cannot read may have rebound the name to something
            # else, so the previous constant is dropped from wherever it is held.
            self._drop_binding(name)
            frame.poisoned.add(name)
        else:
            frame.table[name] = v
            frame.poisoned.discard(name)

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
            for name in (*child.table, *child.poisoned):
                self._invalidate_name(name, parent)

    def _invalidate_name(self, name: str, frame: _Frame) -> None:
        if name in frame.globals and frame is not self._frames[0]:
            self._frames[0].table.pop(name, None)
        else:
            frame.table.pop(name, None)
        # An `import x` or a `match` capture inside a nested block rebinds `x` at the
        # enclosing scope, but this only touches one frame; recording it lets the
        # block's leak carry the fact outwards.
        frame.poisoned.add(name)

    def _body_scope(
        self, body: list[ast.stmt], args: ast.arguments | None = None,
        params: dict[str, object] | None = None,
    ) -> None:
        """Enter a function/class/lambda body: isolated, parameters not constants.

        `params` is the interprocedural binding, when `provable_parameter_bindings`
        proved one. It is a dict of parameter name -> value for *this* definition,
        and it is only ever passed when every call site in the file agrees; anything
        left out stays MISSING and so stays unresolved.
        """
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
            if params:
                for name, value in params.items():
                    if name in frame.table:
                        frame.table[name] = value
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
        if self._unroll(node):
            return
        self._block(node.body)
        self._block(node.orelse)

    visit_AsyncFor = visit_For

    def _unroll(self, node: ast.For) -> bool:
        """Run a literal-iterable loop body once per element. True if unrolled.

        Unrolling is the loop analogue of interprocedural propagation: the loop
        variable is as unknowable as a parameter until someone says what it holds,
        and a literal iterable says exactly that. It is refused -- leaving the
        existing single-visit behaviour, and therefore UNKNOWN -- unless *every*
        one of these holds:

          * the target is a single plain name (tuple targets need unpacking rules);
          * the iterable is a literal list/tuple of elements that all fold;
          * the body contains no `break`, `continue`, `return`, `yield` or nested
            loop, so the iteration count really is the element count;
          * the element count is within `MAX_LOOP_UNROLL` and the module's
            `MAX_UNROLL_TOTAL` budget.

        The budget is what stops nested literal loops (`for a in [..]: for b in [..]`)
        from multiplying into an unbounded walk, and what stops a 10,000-element
        literal from being materialised at all.
        """
        if not isinstance(node.target, ast.Name):
            return False
        if isinstance(node, ast.AsyncFor):
            # A literal list is not an async iterable: this raises at runtime rather
            # than iterating, so there is no execution to enumerate.
            return False
        elements = self._literal_elements(node.iter)
        if elements is None or not elements:
            return False
        if len(elements) > MAX_LOOP_UNROLL or len(elements) > self._unroll_budget:
            return False
        if _iterates_more_than_once(node.body):
            return False

        self._unroll_budget -= len(elements)
        for element in elements:
            frame = _Frame(self._frame())
            frame.table[node.target.id] = element
            self._push(frame)
            try:
                for stmt in node.body:
                    self.visit(stmt)
            finally:
                child = self._pop()
            # Every iteration may write a different thing, so the union of what any
            # of them bound is what has to be invalidated in the parent.
            for name in (*child.table, *child.poisoned):
                self._invalidate_name(name, self._frame())
        # `orelse` runs once, after the loop completes without `break` -- and the
        # body guard above excludes `break`, so it always runs.
        self._block(node.orelse)
        return True

    def _literal_elements(self, node: ast.AST) -> list[object] | None:
        """The folded elements of a literal list/tuple, or None if not provable.

        Elements go through the normal folder rather than a shortcut, so `for t in
        [a, b]` where `a` and `b` are module constants unrolls, while
        `for t in [input(), "x"]` does not: one unknown element poisons the whole
        iterable, because a partial unroll would report a specific statement for a
        loop that really iterates over something else.
        """
        if not isinstance(node, (ast.List, ast.Tuple)):
            return None
        elements: list[object] = []
        for element in node.elts:
            value = self._value(element, 0)
            if value is MISSING:
                return None
            elements.append(value)
        return elements

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
        # Only a module-level def can have a binding: a nested one is reached through
        # a call site we would have to resolve first, and this pass does not chase
        # call chains (a second-level call `f(g("x"))` is UNKNOWN by design).
        params = self._bindings.get(node) if self._frame() is self._frames[0] else None
        self._body_scope(node.body, node.args, params)

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
            sql, failure = self._sink_sql(node)
            outcome = self._sinks.setdefault(
                (node.lineno, node.col_offset), _SinkOutcome()
            )
            outcome.record(sql, failure)
        self.generic_visit(node)

    def _flush(self) -> None:
        """Turn collected sink outcomes into the public dicts, in source order.

        Ordinals are assigned here rather than at record time because one syntactic
        sink can contribute several entries, and they must still form a dense
        `0..n-1` sequence across both dicts -- the tiebreaker's contract.
        """
        self.resolved.clear()
        self.unresolved.clear()
        ordinal = 0
        for (line, col), outcome in sorted(self._sinks.items()):
            for sql, failure in outcome.entries():
                key = SinkKey(line, col, ordinal)
                ordinal += 1
                if failure is not None:
                    self.unresolved[key] = failure
                else:
                    self.resolved[key] = sql  # type: ignore[arg-type]

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


def _iterates_more_than_once(body: list[ast.stmt]) -> bool:
    """True if the statements can run a different number of times than once.

    `break`, `continue`, `return` and `yield` all cut the loop short or detach it
    from its iteration count, and a nested loop introduces an inner count we have
    not accounted for. Any of them and the element count is no longer the number of
    times the body runs, so unrolling would be reporting statements that will not
    execute -- or, worse, not reporting ones that will.
    """
    return _contains_loop_control(body)


def _contains_loop_control(nodes: Sequence[ast.AST]) -> bool:
    for node in nodes:
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.Break,
                             ast.Continue, ast.Return, ast.Yield, ast.YieldFrom)):
            return True
        if _contains_loop_control(list(ast.iter_child_nodes(node))):
            return True
    return False


def _literal_argument(node: ast.AST) -> tuple[bool, object]:
    """(is_literal, value) for a call-site argument.

    Only `ast.Constant` counts. A name that happens to hold a constant at the call
    site is deliberately *not* accepted: the value has to be visible in the argument
    itself, not inferred from a table that the call site may run before it is filled.
    """
    if isinstance(node, ast.Constant) and isinstance(
        node.value, (str, int, float, bool)
    ):
        return True, node.value
    return False, None


def provable_parameter_bindings(module: ast.Module) -> dict[ast.FunctionDef, dict[str, object]]:
    """`def` -> the parameter values every visible call site agrees on.

    A function gets a binding only when *all* of the following hold. Each one is a
    place where the analysis could otherwise report a specific SQL for a statement
    that will not run that way, which is the failure mode this module exists to
    prevent -- a wrong, confident finding is worse than no finding at all.

    1. The def is a module-level, non-async, undecorated `def`.
    2. The name is bound exactly once in the module (no redefinition, no later
       assignment, no `import x as name`, no `del`).
    3. Every appearance of the name is a direct call `name(...)`. Passing it as a
       value, storing it, or calling it as an attribute of something else means we
       cannot see who calls it or with what.
    4. There is at least one call site, and none of them is inside the function
       itself -- a recursive call re-enters the body with a value we would have to
       resolve again, and mutual recursion never terminates.
    5. Every argument at every call site is a literal (see `_literal_argument`), the
       call has no `*args`/`**kwargs` spread, and no parameter is passed twice.
    6. All call sites agree on every parameter's value *and* type. Two callers
       passing `"a"` and `"b"` is genuine ambiguity, and it stays UNKNOWN.
    7. A parameter left to its default binds only if the default is itself a literal.

    Anything not satisfying all seven gets no entry, which leaves the parameters
    MISSING -- i.e. exactly today's UNKNOWN behaviour. There is no partial credit:
    a binding is all-or-nothing per function, because a half-known parameter is a
    stale value waiting to be reported.
    """
    defs = _module_level_defs(module)
    if not defs:
        return {}

    bound: dict[ast.FunctionDef, dict[str, object]] = {}
    for name, node in defs.items():
        if node is None or _name_is_rebound(module, name):
            continue
        calls = _call_sites(module, name, node)
        if not calls:
            continue
        params = _agreed_parameters(node, calls)
        if params is not None:
            bound[node] = params
    return bound


def _module_level_defs(module: ast.Module) -> dict[str, ast.FunctionDef]:
    """Module-level `def`s, keyed by name. Anything disqualifying maps to None.

    A name that is redefined, async, or decorated is mapped to a value the caller
    will refuse -- ``_is_bindable`` is the single place that decides, so "can this
    definition have provable parameters" is answered in one spot.
    """
    out: dict[str, ast.FunctionDef] = {}
    for stmt in module.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_bindable(stmt):
            out[stmt.name] = None  # type: ignore[assignment]
        elif stmt.name in out:
            out[stmt.name] = None  # type: ignore[assignment]  # redefined
        else:
            out[stmt.name] = stmt  # type: ignore[assignment]
    return out


def _is_bindable(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if this definition could have provable parameters at all.

    An `async def` body does not run at the call, a decorator can swap the function
    object for something else entirely, a generator body does not run at the call
    either (it runs when the generator is iterated, possibly never), and PEP 695
    `type_params` is rare enough that refusing it costs nothing.
    """
    if isinstance(node, ast.AsyncFunctionDef):
        return False
    if node.decorator_list:
        return False
    if getattr(node, "type_params", []):
        return False
    return not _is_generator(node)


def _is_generator(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if calling this definition yields a generator instead of running it.

    The walk covers expressions as well as statements (`yield` usually sits inside an
    `Expr`), but stops at nested definitions: a `yield` inside a helper does not make
    the outer function a generator.
    """
    def scan(nodes: Sequence[ast.AST]) -> bool:
        for child in nodes:
            if isinstance(child, (ast.Yield, ast.YieldFrom)):
                return True
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef, ast.Lambda)):
                continue
            if scan(list(ast.iter_child_nodes(child))):
                return True
        return False

    return scan(node.body)


def _name_is_rebound(module: ast.Module, name: str) -> bool:
    """True if anything at module scope binds or frees `name` besides its def.

    Scope is deliberately module-only: a local `drop` inside an unrelated function
    does not shadow the module's `drop`, and refusing on it would throw away
    resolutions for no soundness gain.
    """
    for stmt in _module_scope_statements(module):
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                if (alias.asname or alias.name.split(".")[0]) == name:
                    return True
        elif isinstance(stmt, ast.ClassDef) and stmt.name == name:
            return True
        elif isinstance(stmt, ast.Assign):
            if any(name in target_names(target) for target in stmt.targets):
                return True
        elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
            if name in target_names(stmt.target):
                return True
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            if name in target_names(stmt.target):
                return True
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                if item.optional_vars is not None and name in target_names(
                    item.optional_vars
                ):
                    return True
        elif isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                if name in target_names(target):
                    return True
        elif isinstance(stmt, ast.Try):
            for handler in stmt.handlers:
                if handler.name == name:
                    return True
        elif isinstance(stmt, (ast.Global, ast.Nonlocal)) and name in stmt.names:
            return True
        elif isinstance(stmt, ast.NamedExpr) and name in target_names(stmt.target):
            return True
    return False


def _module_scope_statements(module: ast.Module) -> list[ast.stmt]:
    """Statements that execute at module scope: the top level, plus control flow.

    Function, class and comprehension bodies are *not* module scope, even when they
    are nested inside an `if`, so they are not descended into.
    """
    out: list[ast.stmt] = []

    def walk(body: list[ast.stmt]) -> None:
        for stmt in body:
            out.append(stmt)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef, ast.Lambda)):
                continue
            for field in ("body", "orelse", "finalbody"):
                nested = getattr(stmt, field, None)
                if isinstance(nested, list):
                    walk(nested)
            for handler in getattr(stmt, "handlers", []) or []:
                walk(handler.body)

    walk(module.body)
    return out


def _call_sites(
    module: ast.Module, name: str, node: ast.FunctionDef
) -> list[ast.Call] | None:
    """Every direct `name(...)` call, or None if the name is ever used as a value.

    Only a `Call`'s own function position counts. `handlers = [drop]` or
    `map(drop, xs)` means a caller we cannot see may pass anything, and
    `other.drop("x")` is a different function entirely -- both disqualifying.
    """
    direct_funcs: set[int] = set()
    calls: list[ast.Call] = []
    for stmt in ast.walk(module):
        if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Name) \
                and stmt.func.id == name:
            direct_funcs.add(id(stmt.func))
            calls.append(stmt)
    for stmt in ast.walk(module):
        if isinstance(stmt, ast.Name) and stmt.id == name and id(stmt) not in direct_funcs:
            return None
    # A call inside the body re-enters it with a value we would have to resolve
    # again; refuse rather than model recursion, direct or mutual.
    if any(_within(call, node) for call in calls):
        return None
    return calls


def _within(node: ast.AST, ancestor: ast.AST) -> bool:
    return any(stmt is node for stmt in ast.walk(ancestor))


def _agreed_parameters(
    node: ast.FunctionDef, calls: list[ast.Call]
) -> dict[str, object] | None:
    """The one value every call site passes for each parameter, or None."""
    names = [a.arg for a in list(node.args.posonlyargs) + list(node.args.args)
             + list(node.args.kwonlyargs)]
    posonly_names = {a.arg for a in node.args.posonlyargs}
    defaults = _defaults_by_name(node.args)

    agreed: dict[str, object] | None = None
    for call in calls:
        if call.keywords and any(k.arg is None for k in call.keywords):
            return None  # `f(**kw)`
        if any(isinstance(a, ast.Starred) for a in call.args):
            return None  # `f(*args)`
        positional = list(node.args.posonlyargs) + list(node.args.args)
        if len(call.args) > len(positional):
            return None
        supplied: dict[str, object] = {}
        for arg, param in zip(call.args, positional):
            ok, value = _literal_argument(arg)
            if not ok:
                return None
            supplied[param.arg] = value
        for keyword in call.keywords:
            if keyword.arg not in names or keyword.arg in supplied:
                return None
            if keyword.arg in posonly_names:
                # `def f(a, /, ...)` called as `f(a="x")` is a TypeError, not a call.
                return None
            ok, value = _literal_argument(keyword.value)
            if not ok:
                return None
            supplied[keyword.arg] = value
        for param in names:
            if param in supplied:
                continue
            default = defaults.get(param)
            if default is None:
                # Required and not supplied: the call raises TypeError at runtime,
                # so there is no execution to reason about.
                return None
            supplied[param] = default

        if agreed is None:
            agreed = supplied
            continue
        if len(agreed) != len(supplied):
            return None
        for name, value in agreed.items():
            other = supplied.get(name)
            # Type matters as much as value: `1` and `True` compare equal but render
            # differently in SQL text, and `"1"` is a third thing again.
            if other is None or type(other) is not type(value) or other != value:
                return None
    return agreed


def _defaults_by_name(args: ast.arguments) -> dict[str, object]:
    """Parameter name -> default value, for defaults that are literals.

    A non-literal default is simply absent from the map, which makes any call site
    that relies on it fall out of `_agreed_parameters`.
    """
    out: dict[str, object] = {}
    positional = list(args.posonlyargs) + list(args.args)
    pairs = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
    pairs += [
        (param, default)
        for param, default in zip(args.kwonlyargs, args.kw_defaults)
        if default is not None
    ]
    for param, default in pairs:
        ok, value = _literal_argument(default)
        if ok:
            out[param.arg] = value
    return out


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
