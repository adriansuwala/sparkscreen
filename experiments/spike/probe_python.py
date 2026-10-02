"""Probe: what does the Python-AST layer actually have to deal with?

Walks real-world-ish agent-written PySpark and reports, per call site, whether the
SQL argument is a compile-time constant or dynamically built. The dynamic case is
the one that decides whether a static screener is viable at all.
"""
import ast
from collections import Counter

SQL_SINKS = {"sql", "sqlQuery", "collect_sql", "table", "sql_expr"}


class SinkScan(ast.NodeVisitor):
    def __init__(self):
        self.found = []

    def _is_sink(self, node):
        # spark.sql(...) / spark.sqlQuery(...) / SparkSession.sql
        if not isinstance(node, ast.Call):
            return None
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in ("sql", "sqlQuery"):
            return f.attr
        if isinstance(f, ast.Name) and f.id in ("sql", "sqlQuery"):
            return f.id
        return None

    def visit_Call(self, node):
        sink = self._is_sink(node)
        if sink and node.args:
            arg = node.args[0]
            kind = "literal"
            preview = None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                preview = arg.value
            elif isinstance(arg, ast.JoinedStr):
                kind = "f-string"
                preview = ast.unparse(arg)[:60]
            elif isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
                kind = "concat"
                preview = ast.unparse(arg)[:60]
            elif isinstance(arg, ast.Name):
                kind = "variable"
                preview = arg.id
            elif isinstance(arg, ast.Call):
                kind = "call-result"
                preview = ast.unparse(arg)[:60]
            elif isinstance(arg, (ast.List, ast.Tuple)):
                kind = "collection"
                preview = ast.unparse(arg)[:60]
            self.found.append((node.lineno, sink, kind, preview))
        self.generic_visit(node)


SAMPLES = {
    "literal": 'spark.sql("SELECT * FROM t")',
    "f-string": 'spark.sql(f"SELECT * FROM {tbl} WHERE d = \'{day}\'")',
    "concat": 'spark.sql("SELECT * FROM " + tbl + " WHERE x=1")',
    "variable": 'spark.sql(query)',
    "percent": 'spark.sql("SELECT * FROM %s" % tbl)',
    "format": 'spark.sql("SELECT * FROM {}".format(tbl))',
    "join": 'spark.sql(" ".join(["SELECT *", "FROM", tbl]))',
    "loop": 'for t in tables:\n    spark.sql(f"TRUNCATE TABLE {t}")',
    "read-side": 'df = spark.table("prod.t")\nspark.sql(f"SELECT * FROM {other} JOIN prod.t USING (id)")',
}

print(f"{'pattern':12} {'kind':14} sql-arg")
print("-" * 78)
counts = Counter()
for name, src in SAMPLES.items():
    tree = ast.parse(src)
    s = SinkScan()
    s.visit(tree)
    for lineno, sink, kind, preview in s.found:
        counts[kind] += 1
        print(f"{name:12} {kind:14} {preview}")
    if not s.found:
        print(f"{name:12} {'(no sink)':14} -")
print()
print("dynamic-vs-literal ratio in this sample:",
      f"{sum(v for k,v in counts.items() if k!='literal')}/{sum(counts.values())}")