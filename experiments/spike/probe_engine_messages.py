"""Probe: what exactly does the live engine say about the differential-fuzz FAIL cases?

parsePlan is the grammar-level oracle, but the exception message tells us WHERE it
rejected: a PARSE_SYNTAX_ERROR at the grammar is different from a rejection raised by
the AST builder, which is different again from config-gated syntax. Record the message.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pyspark.sql import SparkSession

CASES = [
    "set( spark.sql.shuffle.partitions=200)",
    "alter table t add column b intunion",
    "alter table t add column b partitionint",
]

session = (
    SparkSession.builder.master("local[1]")
    .appName("sparkscreen-fuzz-probe")
    .config("spark.ui.enabled", "false")
    .config("spark.sql.warehouse.dir", "/opt/data/cache/scratch/ps-fuzz-wh-probe")
    .getOrCreate()
)
session.sparkContext.setLogLevel("ERROR")
jparser = session._jsparkSession.sessionState().sqlParser()

for sql in CASES:
    try:
        jparser.parsePlan(sql)
        print(f"ACCEPT  {sql!r}")
    except Exception as e:
        msg = str(e).replace("\n", " | ")[:300]
        print(f"REJECT  {sql!r}\n        {type(e).__name__}: {msg}")

session.stop()