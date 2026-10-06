"""Probe: the complete divergence set of the promoted differential sweep, per engine.

The sweep in tests/differential/test_fuzz_against_real_spark.py is deterministic (fixed
seed), so its divergence set is a well-defined fact per engine. This probe computes it
by importing the test module itself and running the same case list through parsePlan, so
the recorded table in corpus.py can be built from the actual sweep rather than patched
case by case from pytest failures.

    JAVA_HOME=... <engine-venv>/bin/python experiments/spike/probe_sweep_divergences.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "differential"))

from pyspark.sql import SparkSession  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "fuzz_module", ROOT / "tests" / "differential" / "test_fuzz_against_real_spark.py"
)
fuzz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fuzz)

from sparkscreen.grammar.parser import SqlSyntaxError, get_parser  # noqa: E402

version = __import__("pyspark").__version__
key = "spark-3.5.1" if version.startswith("3.5") else (
    "spark-4.1" if version.startswith("4.1") else "spark-4.2"
)

session = (
    SparkSession.builder.master("local[1]")
    .appName("sparkscreen-sweep-divergence-probe")
    .config("spark.ui.enabled", "false")
    .config("spark.sql.warehouse.dir",
            os.environ.get("SPARKSCREEN_FUZZ_WAREHOUSE_DIR")
            or tempfile.mkdtemp(prefix=f"sparkscreen-fuzz-wh-{key}-"))
    .getOrCreate()
)
session.sparkContext.setLogLevel("ERROR")
jparser = session._jsparkSession.sessionState().sqlParser()

cases = fuzz._cases()
divs, notes = [], []
try:
    for sql in cases:
        try:
            jparser.parsePlan(sql)
            engine = True
        except Exception:
            engine = False
        try:
            ours = get_parser(key).parse(sql) is not None
        except SqlSyntaxError:
            ours = False
        if ours and not engine:
            divs.append(sql)
        elif engine and not ours:
            notes.append(sql)
finally:
    session.stop()

print(f"== {key} ({version}): {len(cases)} cases")
print("DIV we-accept/engine-rejects:")
for s in divs:
    print("   ", repr(s))
print("NOTE we-reject/engine-accepts:", [repr(s) for s in notes])