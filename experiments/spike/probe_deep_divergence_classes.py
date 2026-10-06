"""Probe: engine error classes for the deep-sweep's new divergence members (F27 follow-up).

Classifies each: grammar-level PARSE_SYNTAX_ERROR (port drift -- serious) vs an
AST-builder error class (the F27 shape: grammar accepts, engine rejects later).
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pyspark.sql import SparkSession

CASES = [
    "drop index ion  t",
    "drop index i on t limit 1",
    "drop index i (on t)",
    "add jarjar /tmp/x.jar",
    "add deletejar /tmp/x.jar",
    "alter table t replace columns (a int)int)",
    "alter table t replace columns (aint)",
    "alter table t replace columns (a",
    "ALTER TABLE t ADD COLUMN b INTINT",
    "replace table t (a fromint) using parquet",
    "SET( spark.sql.shuffle.partitions=200)",
    "set (spark.sql.shuffle.partitions=200)",
]

session = (
    SparkSession.builder.master("local[1]")
    .appName("sparkscreen-fuzz-probe2")
    .config("spark.ui.enabled", "false")
    .config("spark.sql.warehouse.dir", tempfile.mkdtemp(prefix="sparkscreen-fuzz-wh-probe2-"))
    .getOrCreate()
)
session.sparkContext.setLogLevel("ERROR")
jparser = session._jsparkSession.sessionState().sqlParser()

for sql in CASES:
    try:
        jparser.parsePlan(sql)
        print(f"ACCEPT  {sql!r}")
    except Exception as e:
        msg = str(e)
        # Error class is the [BRACKETED] marker in Spark 4 ParseException text.
        cls = msg.split("[", 1)[-1].split("]", 1)[0] if "[" in msg else "?"
        print(f"REJECT  {sql!r}\n        class={cls} :: {msg.replace(chr(10), ' | ')[:200]}")

session.stop()