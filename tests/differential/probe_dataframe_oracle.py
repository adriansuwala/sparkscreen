"""Can the DataFrame API be differentially tested against a real Spark?

I claimed the DataFrame path has no oracle. That was too strong. We have a working
PySpark 3.5.1 install and a JVM, so the honest question is whether the *effect* of a
DataFrame call is observable, not whether it is theoretically inspectable.

This script runs real write operations in local mode against a scratch warehouse and
reports what actually happened on disk/catalog -- the ground truth a screener would need.

Run:
    JAVA_HOME=... .venv-pyspark/bin/python tests/differential/probe_dataframe_oracle.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

if os.environ.get("JAVA_HOME") is None and os.environ.get("JRE_HOME") is None:
    sys.exit("set JAVA_HOME")

WAREHOUSE = tempfile.mkdtemp(prefix="sparkscreen-oracle-")

from pyspark.sql import SparkSession  # noqa: E402
from pyspark.errors import AnalysisException  # noqa: E402

spark = (
    SparkSession.builder
    .master("local[1]")
    .appName("sparkscreen-oracle")
    .config("spark.ui.enabled", "false")
    .config("spark.sql.warehouse.dir", WAREHOUSE)
    .config("spark.sql.catalogImplementation", "in-memory")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("ERROR")


def table_exists(name: str) -> bool:
    return name.lower() in {
        r.name.lower() for r in spark.catalog.listTables()
    }


def row_count(name: str) -> int:
    return spark.table(name).count()


def probe(label: str, fn, *, table: str | None = None):
    """Run one operation and report the observable effect."""
    before_exists = table_exists(table) if table else None
    before_rows = row_count(table) if table and before_exists else None
    try:
        fn()
        outcome = "executed"
    except AnalysisException as e:
        outcome = f"AnalysisException: {str(e).splitlines()[0][:60]}"
    except Exception as e:
        outcome = f"{type(e).__name__}: {str(e).splitlines()[0][:60]}"

    after_exists = table_exists(table) if table else None
    after_rows = row_count(table) if table and after_exists else None

    effect = []
    if table:
        if before_exists and not after_exists:
            effect.append("TABLE DROPPED")
        elif not before_exists and after_exists:
            effect.append("TABLE CREATED")
        elif before_rows is not None and after_rows is not None and before_rows != after_rows:
            effect.append(f"ROWS {before_rows} -> {after_rows}")
    if outcome != "executed":
        effect.append("no data change")

    print(f"  {label:46} {outcome:34} {'; '.join(effect) or '-'}")


def main() -> int:
    try:
        base = spark.createDataFrame([(1,), (2,)], "a int")
        base.createOrReplaceTempView("base")
        print("=== writes to a TABLE ===")
        for mode in ("errorifexists", "overwrite", "append"):
            probe(
                f"write.mode('{mode}').saveAsTable('t_{mode}')",
                lambda m=mode: (
                    base.write.mode(m).saveAsTable(f"t_{m}")
                    if m != "errorifexists"
                    else base.write.mode("errorifexists").saveAsTable("t_new_missing")
                ),
                table=f"t_{mode}" if mode != "errorifexists" else "t_new_missing",
            )

        print("\n=== default mode (no .mode() call) ===")
        probe("write.saveAsTable('t_default')", lambda: base.write.saveAsTable("t_default"),
              table="t_default")
        probe("second write.saveAsTable('t_default') (should error)",
              lambda: base.write.saveAsTable("t_default"), table="t_default")

        print("\n=== row counts: does overwrite actually clobber? ===")
        spark.sql("CREATE TABLE t_probe (a int) USING parquet")
        spark.sql("INSERT INTO t_probe VALUES (10), (20)")
        print(f"    before: {row_count('t_probe')} rows")
        probe("write.mode('overwrite').saveAsTable('t_probe')",
              lambda: base.write.mode("overwrite").saveAsTable("t_probe"), table="t_probe")
        print(f"    after : {row_count('t_probe')} rows  <- 2 means clobbered, 4 means appended")

        print("\n=== writes to a PATH (no catalog) ===")
        path = Path(WAREHOUSE) / "out_parquet"
        probe("write.mode('overwrite').save(path)",
              lambda: base.write.mode("overwrite").save(str(path)))
        probe("write.mode('overwrite').save(path) again",
              lambda: base.write.mode("overwrite").save(str(path)))
        print(f"    files at path: {len(list(path.glob('*'))) if path.exists() else 0}")

        print("\n=== external systems ===")
        probe("write.jdbc(...) to a bogus postgres",
              lambda: base.write.mode("overwrite").jdbc(
                  "jdbc:postgresql://127.0.0.1:1/none", "t", batchsize=1))

        print("\n=== does an unresolvable target stay an effect? ===")
        probe("write.mode('overwrite').saveAsTable(<dynamic>)",
              lambda: base.write.mode("overwrite").saveAsTable(name_from_env()))

        return 0
    finally:
        spark.stop()
        shutil.rmtree(WAREHOUSE, ignore_errors=True)


def name_from_env() -> str:
    return os.environ.get("TARGET_TABLE", "t_from_env")


if __name__ == "__main__":
    raise SystemExit(main())