"""Can a constant-propagation pass recover the SQL text for agent-style code?

Walks the module, tracks string-valued bindings, and tries to fold each SQL sink
argument into a concrete string. Reports what was recovered vs. left unknown.
"""
import ast


class Folder(ast.NodeVisitor):
    def __init__(self):
        self.consts = {}   # name -> str
        self.results = []

    def _const(self, node):
        """Return a concrete str or a concrete non-str scalar (int/float/bool)."""
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool)):
            return node.value
        if isinstance(node, ast.Name):
            return self.consts.get(node.id)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            inner = self._const(node.operand)
            if isinstance(inner, (int, float)) and not isinstance(inner, bool):
                return -inner if isinstance(node.op, ast.USub) else inner
        return None

    def _fold(self, node, depth=0):
        """Return a concrete string, or None if unknown."""
        if depth > 12:
            return None
        folded_const = self._const(node)
        if folded_const is not None:
            return folded_const if isinstance(folded_const, str) else str(folded_const)
        if isinstance(node, ast.JoinedStr):
            out = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    out.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    # keep the native type so numeric format specs like {n:04d} work
                    raw = self._const(v.value)
                    inner = self._fold(v.value, depth + 1)
                    if raw is None or inner is None:
                        return None
                    if v.format_spec is not None:
                        fspec = self._fold_spec(v.format_spec, depth + 1)
                        if fspec is None:
                            return None
                    else:
                        fspec = ""
                    conv = v.conversion
                    if conv == 114:
                        inner = repr(inner)
                    elif conv == 115:
                        inner = inner
                    elif conv == 97:
                        inner = ascii(inner)
                    if fspec:
                        try:
                            inner = format(raw, fspec[1:] if fspec.startswith(":") else fspec)
                        except Exception:
                            return None
                    out.append(inner)
                else:
                    return None
            return "".join(out)
        if isinstance(node, ast.BinOp):
            left = self._fold(node.left, depth + 1)
            right = self._fold(node.right, depth + 1)
            if left is None:
                return None
            if isinstance(node.op, ast.Add):
                return None if right is None else left + right
            if isinstance(node.op, ast.Mod):
                # right is the format spec when constant, else a tuple of args
                if isinstance(node.right, (ast.Tuple, ast.List)):
                    args = []
                    for e in node.right.elts:
                        c = self._const(e)
                        if c is None:
                            return None
                        args.append(c)
                    try:
                        return left % tuple(args)
                    except Exception:
                        return None
                if right is None:
                    return None
                try:
                    return left % right
                except Exception:
                    return None
        if isinstance(node, (ast.For, ast.While)):
            self.generic_visit(node)
            return
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "format" and isinstance(f.value, ast.Constant):
                try:
                    return f.value.value.format(*[self._fold(a, depth + 1) or "{}" for a in node.args])
                except Exception:
                    return None
            if isinstance(f, ast.Attribute) and f.attr == "join":
                sep = self._fold(f.value, depth + 1)
                if sep is None:
                    return None
                parts = [self._fold(e, depth + 1) for e in node.args[0].elts] \
                    if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)) else []
                if any(p is None for p in parts):
                    return None
                return sep.join(parts)
        if isinstance(node, ast.IfExp):
            b = self._fold(node.body, depth + 1)
            o = self._fold(node.orelse, depth + 1)
            return b if b is not None and b == o else None
        return None

    def _fold_spec(self, node, depth):
        """Format specs can contain nested replacement fields: f"{x:{w}d}"."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            out = []
            for v in node.values:
                if isinstance(v, ast.Constant):
                    out.append(str(v.value))
                elif isinstance(v, ast.FormattedValue):
                    inner = self._fold(v.value, depth + 1)
                    if inner is None:
                        return None
                    out.append(inner)
            return "".join(out)
        return None

    def visit_Assign(self, node):
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            self._bind(node.targets[0].id, node.value)
        self.generic_visit(node)

    def _bind(self, name, value_node):
        c = self._const(value_node)
        if c is not None:
            self.consts[name] = c
        else:
            self.consts.pop(name, None)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._bind(node.target.id, node.value)
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        sink = None
        if isinstance(f, ast.Attribute) and f.attr in ("sql", "sqlQuery"):
            sink = f.attr
        if sink and node.args:
            folded = self._fold(node.args[0])
            self.results.append((node.lineno, sink, folded))
        self.generic_visit(node)


CASES = {
    "literal": 'spark.sql("SELECT * FROM prod.t")',
    "f-string, both consts": 'tbl = "prod.t"\nday = "2026-01-01"\nspark.sql(f"SELECT * FROM {tbl} WHERE d = \'{day}\'")',
    "concat": 'tbl = "prod.t"\nspark.sql("SELECT * FROM " + tbl + " WHERE x=1")',
    "percent": 'tbl = "prod.t"\nspark.sql("SELECT * FROM %s" % tbl)',
    "percent tuple": 'a="prod.t"\nb=3\nspark.sql("SELECT * FROM %s LIMIT %d" % (a,b))',
    "format": 'tbl = "prod.t"\nspark.sql("SELECT * FROM {}".format(tbl))',
    "join": 'tbl = "prod.t"\nspark.sql(" ".join(["SELECT *", "FROM", tbl]))',
    "loop over literal list": 'for t in ["a","b"]:\n    print(f"TRUNCATE TABLE {t}")',
    "format spec": 'n = 3\nspark.sql(f"SELECT * FROM t LIMIT {n:04d}")',
    "repr conversion": 'tbl = "prod.t"\nspark.sql(f"SELECT * FROM {tbl!r}")',
    "UNKNOWN: user input": 'tbl = input()\nspark.sql(f"SELECT * FROM {tbl}")',
    "UNKNOWN: loop var": 'for t in tables:\n    spark.sql(f"TRUNCATE TABLE {t}")',
    "UNKNOWN: function arg": 'def q(spark, tbl):\n    return spark.sql(f"SELECT * FROM {tbl}")',
    "UNKNOWN: dict lookup": 'tbl = cfg["t"]\nspark.sql(f"SELECT * FROM {tbl}")',
}

for name, src in CASES.items():
    f = Folder()
    f.visit(ast.parse(src))
    for lineno, sink, folded in f.results:
        if folded is None:
            print(f"{name:24} line {lineno}: UNRESOLVED  (cannot statically recover)")
        else:
            print(f"{name:24} line {lineno}: {folded!r}")