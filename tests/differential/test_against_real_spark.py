"""Differential testing against a real Spark installation.

This is the only test module that can catch a class of bug nothing else can: our ported
parser *disagreeing with the engine it screens for*. The case-insensitivity bug is the
motivating example -- both grammars rejected `select 1` while real Spark accepted it, and
the entire hand-written corpus missed it because the corpus was written in uppercase.

These tests need a PySpark install and a JVM, so they are skipped when either is absent.
Run them explicitly, using an interpreter that has both pyspark and the antlr runtime:

    uv pip install --python .venv-pyspark/bin/python pyspark==3.5.1 \
        "antlr4-python3-runtime==4.13.1" pytest
    JAVA_HOME=/path/to/jre PYTHONPATH=src:. .venv-pyspark/bin/python -m pytest \
        tests/differential/test_against_real_spark.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkscreen.grammar.parser import SqlSyntaxError, get_parser  # noqa: E402

from differential.corpus import (  # noqa: E402
    CORPUS,
    VERSION_SPECIFIC,
    VERSION_SPECIFIC_REJECTED,
)

pyspark = pytest.importorskip("pyspark", reason="differential tests need pyspark")
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def spark_session():
    if not (os.environ.get("JAVA_HOME") or os.environ.get("JRE_HOME")):
        pytest.skip("differential tests need a JVM (set JAVA_HOME)")
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder
        .master("local[1]")
        .appName("sparkscreen-differential")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", "/tmp/sparkscreen-warehouse")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    try:
        yield session
    finally:
        session.stop()


def _our_verdict(spec_key: str, sql: str) -> bool:
    """True when our parser accepted `sql`."""
    try:
        return get_parser(spec_key).parse(sql) is not None
    except SqlSyntaxError:
        return False


def _running_key() -> str:
    """The pinned grammar matching the installed pyspark."""
    return "spark-3.5.1" if pyspark.__version__.startswith("3.5") else "spark-4.2"


def _id(value):
    return value if isinstance(value, str) else ""


@pytest.mark.parametrize("sql,spark_accepts", CORPUS, ids=[_id(s) for s, _ in CORPUS])
def test_agrees_with_real_spark(sql, spark_accepts):
    """Our accept/reject decision must not contradict the engine's.

    Direction matters. We are *allowed* to reject what Spark accepts -- that yields a
    spurious UNKNOWN, which is safe, just noisy. We are NOT allowed to accept what Spark
    rejects, because that means analysing SQL the engine would never run.

    This test runs without a Spark session: the expectations in `corpus.py` were
    recorded from a real one (see `real_spark_verdict`), so re-running Spark here would
    add minutes per suite for no new information. `test_recorded_expectations_still_hold`
    below is the opt-in check that the recording is still accurate.
    """
    key = _running_key()
    actual = _our_verdict(key, sql)
    if actual and not spark_accepts:
        pytest.fail(
            f"we accept {sql!r} but real Spark {pyspark.__version__} rejects it; "
            "a screener must not analyse SQL the engine will not run"
        )


@pytest.mark.parametrize(
    "sql,accepting_key,observed",
    VERSION_SPECIFIC,
    ids=[s for s, _, _ in VERSION_SPECIFIC],
)
def test_version_specific_accepted(sql, accepting_key, observed):
    parser = get_parser(accepting_key)
    assert observed, f"{accepting_key} is recorded as accepting {sql!r}"
    assert parser.parse(sql) is not None


@pytest.mark.parametrize(
    "sql,rejecting_key", VERSION_SPECIFIC_REJECTED, ids=[s for s, _ in VERSION_SPECIFIC_REJECTED]
)
def test_version_specific_rejected(sql, rejecting_key):
    parser = get_parser(rejecting_key)
    with pytest.raises(SqlSyntaxError):
        parser.parse(sql)


@pytest.mark.slow
def test_recorded_expectations_still_hold(spark_session):
    """Re-verify the recorded corpus against the live engine.

    Slow: starts a JVM. Opt in with JAVA_HOME set. If Spark changes behaviour, this is
    what tells us -- and `corpus.py` needs updating, deliberately, by hand.
    """
    from differential.corpus import real_spark_verdict

    for sql, expected in CORPUS:
        actual = real_spark_verdict(spark_session, sql)
        assert actual == expected, (
            f"recorded expectation is stale for {sql!r}: "
            f"corpus says Spark {'accepts' if expected else 'rejects'}, "
            f"but it {'accepts' if actual else 'rejects'}. "
            f"Update tests/differential/corpus.py."
        )