"""Ground truth: does Spark 3.5.1 really reject lowercase identifiers?

The vendored 3.5.1 grammar has `fragment LETTER : [A-Z]` and no caseInsensitive
option anywhere in its Maven config, which would imply `DROP TABLE prod.users` cannot
lex. That contradicts everyday Spark behaviour, so check against the real thing rather
than reasoning about it.

Prints, for a corpus of statements, whether real `spark.sql()` raises a *parse* error,
so the result is comparable with our parser's accept/reject decision.
"""
import sys

from pyspark.sql import SparkSession
from pyspark.errors import ParseException

CASES = [
    "DROP TABLE prod.users",
    "SELECT * FROM prod.t",
    "SELECT a FROM t1 WHERE a > 1",
    "INSERT OVERWRITE TABLE t SELECT 1",
    "TRUNCATE TABLE t",
    "SELCT 1",
    "SELECT $$abc$$",
    "INSERT INTO t SELECT * FROM",
    "ADD JAR /tmp/x.jar",
    "SELECT 1 |> SELECT 2",
]

spark = (
    SparkSession.builder
    .master("local[1]")
    .appName("sparkscreen-differential")
    .config("spark.ui.enabled", "false")
    .config("spark.sql.warehouse.dir", "/tmp/sparkscreen-warehouse")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("ERROR")

try:
    for sql in CASES:
        try:
            spark.sql(sql).collect()
            verdict = "ACCEPTS"
        except ParseException as e:
            verdict = "PARSE_ERROR"
        except Exception as e:
            # analysis failures (missing table, etc.) mean parsing succeeded
            verdict = f"ANALYSIS({type(e).__name__})"
        print(f"{verdict:28} {sql}")
finally:
    spark.stop()