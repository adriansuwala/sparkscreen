"""Recover SQL text from Python source, or admit we cannot.

Static screening of `spark.sql(...)` only works if the SQL string can be recovered
before execution. In agent-written PySpark it usually is not a literal -- it is
typically an f-string or concatenation over module-level constants. Regex-over-source
misses those entirely; constant propagation does not.

`StringFolder` is a deliberately narrow abstract interpreter: it tracks bindings whose
value is a compile-time constant string (or int/float for format specs) and folds
string-building expressions over them. It handles f-strings (including nested format
specs and `!r`/`!a`/`!s` conversions), `+`, `%` formatting with scalar and tuple
arguments, `str.format`, `"".join`, and `.upper()`/`.lower()`/`.strip()`.

What it deliberately does NOT do: track types, model exceptions, or evaluate arbitrary
calls. When it cannot prove a value, it returns `None` -- and `None` flows into the
UNKNOWN verdict, never into "safe".

A known gap, recorded rather than papered over: this folds module-level constant
bindings only. It does not thread constants into or out of function bodies, so
`def run(tbl): spark.sql(f"drop table {tbl}")` is unresolved even when every caller
passes a literal. That is the correct fail-closed outcome, but it means a policy written
against heavily-factored agent code will see more UNKNOWN than a human reviewer expects.
Interprocedural constant propagation is the obvious next step.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

MAX_FOLD_DEPTH = 24
MAX_CONST_STRING = 1 << 20  # 1 MiB; guards against pathological `"a" * 10**9`


@dataclass(frozen=True)
class FoldFailure:
    """Why a SQL argument could not be reduced to a constant."""

    reason: str
    expression: str

    def __str__(self) -> str:
        return f"{self.reason}: {self.expression}"


class StringFolder(ast.NodeVisitor):
    """Fold string-building expressions to constants, tracking module bindings."""

    def __init__(self) -> None:
        self.consts: dict[str, object] = {}
        #: line number -> resolved SQL text, for every sink found
        self.resolved: dict[int, str] = {}
        #: line number -> FoldFailure, for sinks we could not resolve
        self.unresolved: dict[int, FoldFailure] = {}

    # -- constant folding ---------------------------------------------------

    def _scalar(self, node: ast.AST) -> object | None:
        """A literal str/int/float/bool value, or None."""
        if isinstance(node, ast.Constant) and isinstance(
            node.value, (str, int, float, bool)
        ):
            return node.value
        if isinstance(node, ast.Name):
            return self.consts.get(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            inner = self._scalar(node.operand)
            if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                return -inner if isinstance(node.op, ast.USub) else inner
        return None

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

    def fold(self, node: ast.AST, depth: int = 0) -> str | None:
        """Reduce `node` to a concrete string, or None if not provable."""
        if depth > MAX_FOLD_DEPTH:
            return None

        s = self._scalar(node)
        if s is not None:
            return s if isinstance(s, str) else str(s)

        if isinstance(node, ast.JoinedStr):
            return self._fold_fstring(node, depth)
        if isinstance(node, ast.BinOp):
            return self._fold_binop(node, depth)
        if isinstance(node, ast.Call):
            return self._fold_call(node, depth)
        if isinstance(node, ast.IfExp):
            # only safe when both branches agree
            b = self.fold(node.body, depth + 1)
            o = self.fold(node.orelse, depth + 1)
            return b if (b is not None and b == o) else None
        if isinstance(node, (ast.List, ast.Tuple)):
            parts = [self.fold(e, depth + 1) for e in node.elts]
            if any(p is None for p in parts):
                return None
            return "".join(parts) if isinstance(node, ast.List) else " ".join(parts)
        return None

    def _fold_fstring(self, node: ast.JoinedStr, depth: int) -> str | None:
        out: list[str] = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                out.append(str(v.value))
            elif isinstance(v, ast.FormattedValue):
                raw = self._scalar(v.value)
                inner = self.fold(v.value, depth + 1)
                if raw is None or inner is None:
                    return None
                fspec = (
                    self._spec(v.format_spec, depth + 1)
                    if v.format_spec is not None
                    else ""
                )
                if fspec is None:
                    return None
                conv = v.conversion
                if conv == 114:      # !r
                    text = repr(inner)
                elif conv == 97:     # !a
                    text = ascii(inner)
                else:                # !s or none
                    text = inner
                if fspec:
                    # format against the *native* value so {n:04d} works
                    try:
                        text = format(raw, fspec[1:] if fspec.startswith(":") else fspec)
                    except Exception:
                        return None
                out.append(text)
            else:
                return None
        result = "".join(out)
        return result if len(result) <= MAX_CONST_STRING else None

    def _fold_binop(self, node: ast.BinOp, depth: int) -> str | None:
        left = self.fold(node.left, depth + 1)
        if left is None:
            return None
        if isinstance(node.op, ast.Add):
            right = self.fold(node.right, depth + 1)
            return None if right is None else left + right
        if isinstance(node.op, ast.Mod):
            if isinstance(node.right, (ast.Tuple, ast.List)):
                args = [self._scalar(e) for e in node.right.elts]
                if any(a is None for a in args):
                    return None
                try:
                    result = left % tuple(args)
                except Exception:
                    return None
                return result if len(result) <= MAX_CONST_STRING else None
            right = self.fold(node.right, depth + 1)
            if right is None:
                return None
            try:
                result = left % right
            except Exception:
                return None
            return result if len(result) <= MAX_CONST_STRING else None
        if isinstance(node.op, ast.Mult) and isinstance(node.right, ast.Constant):
            n = node.right.value
            if isinstance(n, int) and 0 <= n * len(left) <= MAX_CONST_STRING:
                return left * n
        return None

    def _fold_call(self, node: ast.Call, depth: int) -> str | None:
        f = node.func
        # "...".format(...) and "...".format(a, b)
        if isinstance(f, ast.Attribute) and f.attr == "format" and isinstance(
            f.value, ast.Constant
        ):
            tmpl = self.fold(f.value, depth + 1)
            if tmpl is None:
                return None
            args = []
            for a in node.args:
                v = self._scalar(a)
                if v is None:
                    return None
                args.append(v)
            try:
                return tmpl.format(*args)
            except Exception:
                return None
        # sep.join([...])
        if isinstance(f, ast.Attribute) and f.attr == "join":
            sep = self.fold(f.value, depth + 1)
            if sep is None or not node.args:
                return None
            arg = node.args[0]
            if isinstance(arg, (ast.List, ast.Tuple)):
                parts = [self.fold(e, depth + 1) for e in arg.elts]
            elif isinstance(arg, (ast.GeneratorExp, ast.ListComp)):
                return None  # comprehension contents may depend on runtime state
            else:
                return None
            if any(p is None for p in parts):
                return None
            return sep.join(parts)
        # str methods on a folded receiver
        if isinstance(f, ast.Attribute) and f.attr in (
            "upper", "lower", "strip", "lstrip", "rstrip", "title", "capitalize"
        ):
            recv = self.fold(f.value, depth + 1)
            if recv is None:
                return None
            return getattr(recv, f.attr)()
        if isinstance(f, ast.Attribute) and f.attr == "replace":
            recv = self.fold(f.value, depth + 1)
            if recv is None or len(node.args) != 2:
                return None
            a, b = self._scalar(node.args[0]), self._scalar(node.args[1])
            if a is None or b is None:
                return None
            return recv.replace(a, b)
        return None

    # -- binding tracking ---------------------------------------------------

    def _bind(self, name: str, value: ast.AST) -> None:
        # A binding is constant only if the whole right-hand side folds. This
        # deliberately misses `"".join(x for x in y)` and dict lookups -- those
        # become UNKNOWN, which is the correct outcome.
        v = self._scalar(value)
        if v is not None:
            self.consts[name] = v
        else:
            self.consts.pop(name, None)

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._bind(node.targets[0].id, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._bind(node.target.id, node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        sql, line = self._sink_sql(node)
        if line is not None:
            if sql is None:
                self.unresolved[line] = FoldFailure(
                    "sql argument is not a compile-time constant",
                    _src(node.args[0]),
                )
            else:
                self.resolved[line] = sql
        self.generic_visit(node)

    def _sink_sql(self, node: ast.Call) -> tuple[str | None, int | None]:
        """(sql_text, lineno) if this call is a SQL sink, else (None, None)."""
        f = node.func
        is_sink = False
        if isinstance(f, ast.Attribute) and f.attr in SQL_METHODS:
            is_sink = True
        elif isinstance(f, ast.Name) and f.id in SQL_FUNCS:
            is_sink = True
        if not is_sink or not node.args:
            return None, None
        return self.fold(node.args[0]), node.lineno


def _src(node: ast.AST, limit: int = 120) -> str:
    try:
        text = ast.unparse(node)
    except Exception:
        text = "<expr>"
    return text if len(text) <= limit else text[: limit - 3] + "..."


#: DataFrame/SparkSession methods whose first string argument is SQL.
SQL_METHODS = frozenset({"sql", "sqlQuery"})

#: module-level function names treated as SQL sinks.
SQL_FUNCS = frozenset({"sql", "sqlQuery"})


def fold_sinks(tree: ast.AST) -> StringFolder:
    """Run the folder over a parsed module and collect resolved/unresolved sinks."""
    folder = StringFolder()
    folder.visit(tree)
    return folder