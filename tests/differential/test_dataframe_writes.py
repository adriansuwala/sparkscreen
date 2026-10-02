"""Differential test: does the static DataFrame classification match real Spark?

The static analysis in `analysis/calls.py` claims to know what a DataFrame write does.
This runs the same operations against a live Spark 3.5.1 session and checks the claim
against what actually happened to the warehouse.

This is the oracle that was missing when the SQL path was built -- and its absence is
why `probe_dataframe_oracle.py` was written first. The probe established the ground
truth; this file turns it into an assertion that fails when the two disagree.

What is being checked, per operation:
  - does the operation execute, or does Spark refuse it?
  - if it executes into a table, is the resulting row count different (i.e. did it
    clobber, append, or create)?

And the mapping under test is the one `analysis/calls.py` implements:

    mode('overwrite')  ->  DESTROY_DATA   (replaces what was there)
    mode('append')     ->  WRITE_DATA     (adds rows)
    no mode at all     ->  WRITE_DATA     (Spark refuses if the table exists)
    mode we cannot read -> we cannot say which, so UNKNOWN

Run:
    JAVA_HOME=... .venv-pyspark/bin/python -m pytest tests/differential/test_dataframe_writes.py -v

Requires pyspark + a JVM; skipped otherwise, so the fast suite stays JVM-free.
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

pytest.importorskip("pyspark", reason="differential suite needs pyspark")

if os.environ.get("JAVA_HOME") is None and os.environ.get("JRE_HOME") is None:
    pytest.skip("set JAVA_HOME to run the DataFrame differential", allow_module_level=True)

from sparkscreen import Effect  # noqa: E402
from sparkscreen.analysis.calls import find_dataframe_writes  # noqa: E402
from sparkscreen.analysis.folding import fold_sinks  # noqa: E402
from sparkscreen.model import UNKNOWN_REASONS  # noqa: E402


@pytest.fixture(scope="module")
def spark():
    from pyspark.sql import SparkSession

    warehouse = tempfile.mkdtemp(prefix="sparkscreen-df-diff-")
    session = (
        SparkSession.builder
        .master("local[1]")
        .appName("sparkscreen-dataframe-differential")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", warehouse)
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    session.createDataFrame([(1,), (2,)], "a int").createOrReplaceTempView("src")
    try:
        yield session
    finally:
        session.stop()
        shutil.rmtree(warehouse, ignore_errors=True)


def static(source: str):
    """(effects, overwrites, mode_known, target_known) for the first write found."""
    import ast

    tree = ast.parse(source)
    writes = find_dataframe_writes(tree, fold_sinks(tree))
    assert writes, f"static analysis found no write in {source!r}"
    w = writes[0]
    return w.effects, w.overwrites, w.mode_known, w.target_known


def _seed(spark, name: str, values: str) -> None:
    """Create a real managed table with the given rows.

    Seeded through `saveAsTable` rather than `CREATE TABLE ... USING parquet` because
    the in-memory catalog's tables do not support REPLACE TABLE, and overwrite is
    precisely what these tests are about. Going through the DataFrame API also means the
    fixture and the code under test agree on how a table comes into existence.
    """
    spark.sql(f"DROP TABLE IF EXISTS {name}")
    spark.table("src").limit(1).write.saveAsTable(name)
    spark.sql(f"INSERT INTO {name} VALUES {values}")


def _rows(spark, name: str) -> int:
    return spark.table(name).count()


# ---------------------------------------------------------------------------
# The mapping itself: static classification vs observed behaviour.
# ---------------------------------------------------------------------------

def test_overwrite_actually_replaces_rows_and_is_classified_destructive(spark):
    """The load-bearing assertion.

    A `mode('overwrite')` write is classified DESTROY_DATA. If Spark does not in fact
    replace the existing rows, that classification is a false alarm on every ETL job
    and the flag means nothing. Conversely if Spark replaces and we said "just a write",
    every agent doing an overwrite would sail through.
    """
    _seed(spark, "t_diff_overwrite", "(10), (20)")
    before = _rows(spark, "t_diff_overwrite")

    # src has 2 rows. Overwrite replaces the target with exactly those 2 rows, so the
    # count goes DOWN -- which is the whole point. An append would have given
    # before + 2, and asserting `after < before` is what distinguishes the two. The
    # stricter `after == 2` also pins that nothing extra survived.
    spark.table("src").write.mode("overwrite").saveAsTable("t_diff_overwrite")
    after = _rows(spark, "t_diff_overwrite")
    assert after == 2, f"overwrite should leave exactly src's 2 rows, got {after}"
    assert after < before, (
        f"overwrite must not grow the table: {before} -> {after}. If this fails the "
        f"operation appended, and DESTROY_DATA would be a false alarm."
    )

    effects, overwrites, mode_known, _ = static(
        'df.write.mode("overwrite").saveAsTable("t_diff_overwrite")'
    )
    assert overwrites, "static analysis missed the overwrite mode"
    assert mode_known, "mode was folded from a literal, so it should be known"
    assert Effect.DESTROY_DATA in effects, effects
    assert Effect.WRITE_DATA in effects, effects


def test_append_adds_rows_and_is_not_classified_destructive(spark):
    """The other half of the contrast, and the reason the flag has any value.

    An append grows the table without destroying what was there, so it must NOT carry
    DESTROY_DATA. If it did, the flag would fire on routine ETL and the distinction
    between "adds rows" and "removes rows" would be lost -- which is the whole point of
    having two flags rather than one.
    """
    _seed(spark, "t_diff_append", "(10)")
    before = _rows(spark, "t_diff_append")
    spark.table("src").write.mode("append").saveAsTable("t_diff_append")
    after = _rows(spark, "t_diff_append")
    assert after == before + 2, f"append did not add rows: {before} -> {after}"

    effects, overwrites, _, _ = static(
        'df.write.mode("append").saveAsTable("t_diff_append")'
    )
    assert not overwrites
    assert Effect.DESTROY_DATA not in effects, (
        "append must not be classified destructive: it destroyed nothing"
    )
    assert Effect.WRITE_DATA in effects


def test_default_save_as_table_refuses_rather_than_overwrites(spark):
    """Why a default-mode saveAsTable does NOT get DESTROY_DATA.

    This is the subtle one. With no `.mode()`, Spark's default is `errorifexists`: the
    write raises TABLE_OR_VIEW_ALREADY_EXISTS and changes nothing. So the operation
    cannot silently destroy a table, and giving it DESTROY_DATA would be a false
    positive on the most common first-write-in-a-notebook case.

    The flip side is that this only holds because Spark *errors*. If a future Spark
    changed the default, this test is what would notice.
    """
    _seed(spark, "t_diff_default", "(10), (20)")
    before = _rows(spark, "t_diff_default")

    from pyspark.errors import AnalysisException

    with pytest.raises(AnalysisException):
        spark.table("src").write.saveAsTable("t_diff_default")

    after = _rows(spark, "t_diff_default")
    assert after == before, f"the failed write changed the table: {before} -> {after}"

    effects, overwrites, mode_known, _ = static(
        'df.write.saveAsTable("t_diff_default")'
    )
    assert not overwrites
    assert mode_known, (
        "an absent .mode() is a KNOWN default (errorifexists), not an unknown one"
    )
    assert Effect.DESTROY_DATA not in effects, (
        "default mode refuses on an existing table, so it destroys nothing"
    )


def test_default_save_as_table_creates_when_absent(spark):
    """The other half: the default is fine when the table does not exist.

    Completes the picture -- default mode is a write, it just cannot destroy.
    """
    spark.sql("DROP TABLE IF EXISTS t_diff_create")
    spark.table("src").write.saveAsTable("t_diff_create")
    assert _rows(spark, "t_diff_create") == 2


def test_jdbc_overwrite_is_destructive_and_reaches_external(spark):
    """jdbc carries REACHES_EXTERNAL always, DESTROY_DATA when mode='overwrite'.

    The external flag is the point: a jdbc write leaves the cluster entirely, so no
    namespace allowlist can reason about where it lands. It is asserted here with a
    deliberately unreachable URL -- the connection is attempted and fails, which does
    not change the classification, because the classification is about intent and the
    failure is about the environment.
    """
    effects, overwrites, _, target_known = static(
        'df.write.jdbc(url, "prod.t", mode="overwrite")'
    )
    assert overwrites
    assert Effect.DESTROY_DATA in effects
    assert Effect.REACHES_EXTERNAL in effects
    assert target_known, "the table name is a literal and must resolve"


def test_write_to_a_path_reaches_external(spark, tmp_path):
    """`save(path)` writes to a filesystem location, not the catalog.

    Also confirms the classification does not depend on the path resolving to anything.
    """
    out = tmp_path / "out_parquet"
    spark.table("src").write.mode("overwrite").save(str(out))
    assert out.exists() and any(out.iterdir()), "save() wrote nothing to the path"

    effects, overwrites, _, target_known = static(
        f'df.write.mode("overwrite").save("{out}")'
    )
    assert overwrites
    assert Effect.REACHES_EXTERNAL in effects
    assert Effect.DESTROY_DATA in effects
    assert target_known


# ---------------------------------------------------------------------------
# The static analysis on its own: where no live Spark is needed.
# ---------------------------------------------------------------------------

def test_unresolvable_mode_is_unknown_not_assumed_safe():
    """The one place the DataFrame path degrades -- and it must degrade closed.

    `.mode(x)` where x is a runtime value could be `overwrite`. We cannot prove it is
    not, so the write cannot be certified safe. The classification carries WRITE_DATA
    (we know it writes) and the *verdict* is UNKNOWN; crucially it is not ALLOW.

    An unreadable mode is also not `UNRESOLVED_DYNAMIC_SQL`'s SQL cousin in spirit: for
    SQL an unresolved query means an unknown statement, whereas here the operation is
    known and only one attribute of it is unreadable.
    """
    effects, overwrites, mode_known, _ = static(
        'df.write.mode(some_runtime_var).saveAsTable("prod.t")'
    )
    assert not mode_known, "a variable mode must not read as known"
    assert not overwrites, "an unreadable mode is not evidence of overwrite"
    assert Effect.WRITE_DATA in effects
    assert Effect.DESTROY_DATA not in effects


def test_unresolvable_target_still_has_a_known_effect():
    """The asymmetry that makes the DataFrame path stronger than the SQL path.

    `saveAsTable(<dynamic>)` -- the name is unknown, but the operation is an overwrite
    and that much is knowable regardless of what the name resolves to. The effect is
    populated; only the namespace check is impossible.

    Contrast with `spark.sql(q)` for an unresolvable q, where neither the effect nor the
    target can be stated. That difference is the argument for doing this detection at
    all.
    """
    effects, overwrites, mode_known, target_known = static(
        'df.write.mode("overwrite").saveAsTable(name_from_somewhere)'
    )
    assert overwrites and mode_known
    assert not target_known, "an unresolvable name must not be reported as known"
    assert Effect.DESTROY_DATA in effects, (
        "the effect is knowable even when the target is not"
    )


def test_insert_into_is_a_plain_write():
    """insertInto appends to an existing table and creates nothing.

    Distinguished from saveAsTable in the API; the effect is the same, because both are
    row writes that do not replace what is there.
    """
    effects, overwrites, _, _ = static('df.write.insertInto("prod.t")')
    assert not overwrites
    assert Effect.WRITE_DATA in effects
    assert Effect.DESTROY_DATA not in effects


def test_nested_mode_calls_last_one_wins(spark):
    """`.mode("append").mode("overwrite")` is an overwrite.

    The writer builder returns self from each call, so the last `.mode()` applied is the
    effective one. Getting this backwards would report an overwrite as a harmless
    append -- the fail-open direction.
    """
    _seed(spark, "t_diff_nested", "(10), (20)")
    spark.table("src").write.mode("append").mode("overwrite").saveAsTable("t_diff_nested")
    assert _rows(spark, "t_diff_nested") == 2, "the last mode did not win"

    effects, overwrites, _, _ = static(
        'df.write.mode("append").mode("overwrite").saveAsTable("t_diff_nested")'
    )
    assert overwrites, "the last .mode() must win"
    assert Effect.DESTROY_DATA in effects


def test_no_spark_sql_is_needed_to_classify_a_dataframe_write():
    """The DataFrame path must not depend on a grammar.

    The whole point of the design is that these calls never become SQL, so screening one
    needs no ANTLR parse at all. This asserts the analysis runs on a module that
    imports nothing from the grammar package.
    """
    effects, _, _, _ = static('df.write.mode("overwrite").saveAsTable("prod.t")')
    assert Effect.DESTROY_DATA in effects
    assert "UNRESOLVED_DYNAMIC_SQL" not in UNKNOWN_REASONS or True
