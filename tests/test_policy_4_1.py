"""The shipped Spark 4.1 default policy.

`default-spark-3.5.1.jsonc` is exercised through the loader in
`test_jsonc_policy_loading.py`; the questions this module asks are about the 4.1
artefact itself, and they are asked in three registers on purpose:

* structural, against the label universe derived from the generated spark-4.1 parser
  (`labels_for_grammar`) and the effect table (`effects_for_label`) -- no JVM needed,
  because the parsers are committed;
* behavioural, through `screen()` with the loaded file, including the two 4.1 shapes
  that differ from 3.5.1: BEGIN...END scripts, and the statement a CASE body hides;
* differential, against a real Spark 4.1.3 engine, asserting that the statements the
  file denies are statements the engine actually parses and runs. A deny rule aimed at
  SQL the engine rejects would be theatre.

The differential test follows the tests/differential/ gate: importorskip on pyspark,
skip without a JVM, marked slow. Run it with:

    export JAVA_HOME=$(ls -d /opt/data/home/.jre/*)
    PATH="$JAVA_HOME/bin:$PATH" <venv with pyspark 4.1.3>/bin/python \\
        -m pytest tests/test_policy_4_1.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from sparkscreen import screen
from sparkscreen.analysis.effects import effects_for_label
from sparkscreen.analysis.label_universe import labels_for_grammar
from sparkscreen.model import Verdict
from sparkscreen.grammar.parser import SqlSyntaxError, get_parser
from sparkscreen.policy import load_policy

REPO = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO / "src" / "sparkscreen" / "policies" / "default-spark-4.1.jsonc"

#: Labels the tree walk descends *through*. A rule naming one would be dead code, and a
#: dead deny rule is a fail-open: the statement it was written for falls through to the
#: built-in defaults with no operator-visible explanation.
WRAPPER_LABELS = ("DmlStatement", "SingleInsertQuery", "MultiInsertQuery")

#: The script control-flow labels. They hide their bodies from the tree walk (see the
#: policy file's header), so they must never be allowed by any rule.
CONTROL_FLOW_LABELS = ("SearchedCaseStatement", "SimpleCaseStatement")


@pytest.fixture(scope="module")
def policy_4_1():
    return load_policy(POLICY_PATH)


# ---------------------------------------------------------------------------
# Structure: the file against the 4.1 label universe
# ---------------------------------------------------------------------------


def test_loads_through_the_public_loader(policy_4_1):
    """`jsonc` must load via `load_policy`, which switched on the suffix at 0091278."""
    assert policy_4_1.name == "sparkscreen-defaults-4.1"
    assert len(policy_4_1.rules) > 0


def test_every_label_exists_in_the_4_1_universe_and_maps_to_effects(policy_4_1):
    """Each rule names labels the spark-4.1 grammar can actually emit, and the effect
    table can classify. A label outside the universe matches nothing (dead rule); a
    label with no effect entry would raise `UnmappedLabelError` at screen time."""
    universe = labels_for_grammar("spark-4.1")
    for rule in policy_4_1.rules:
        for label in rule.labels:
            assert label in universe, f"{rule.id} names {label!r}, not in the spark-4.1 universe"
            effects_for_label(label)  # raises on a miss; never returns empty


def test_no_label_appears_in_two_rules(policy_4_1):
    """First match wins, so a label in two rules makes the later one dead."""
    owner: dict[str, str] = {}
    for rule in policy_4_1.rules:
        for label in rule.labels:
            assert label not in owner, (
                f"{label!r} is matched by both {owner[label]} and {rule.id}; "
                "the second rule is dead because first-match-wins"
            )
            owner[label] = rule.id


def test_verdicts_are_only_deny_review_allow(policy_4_1):
    """`unknown` is reserved for statements the screener could not analyse, and no rule
    may hand it out."""
    for rule in policy_4_1.rules:
        assert rule.verdict in (Verdict.DENY, Verdict.REVIEW, Verdict.ALLOW), rule.id


def test_no_wrapper_label_is_named_by_any_rule(policy_4_1):
    """`effective_label` descends through the wrappers, so a rule keyed on one never
    fires. This is asserted structurally, not just in the file's own comments."""
    named = {label for rule in policy_4_1.rules for label in rule.labels}
    assert not named & set(WRAPPER_LABELS)


def test_no_allow_rule_names_a_control_flow_label(policy_4_1):
    """A CASE/WHILE body's statements never surface as labels (verified against the
    shipped 4.1 parser: `BEGIN CASE WHEN TRUE THEN DROP TABLE t; END CASE; END`
    reports only SearchedCaseStatement), so ALLOWing the enclosing label would ALLOW
    the hidden DROP."""
    for rule in policy_4_1.rules:
        if rule.verdict is not Verdict.ALLOW:
            continue
        assert not set(rule.labels) & set(CONTROL_FLOW_LABELS), rule.id


def test_the_deny_rules_name_the_canonical_destructive_labels(policy_4_1):
    """The two labels the differential expectation checks by name."""
    deny_labels: set[str] = set()
    for rule in policy_4_1.rules:
        if rule.verdict is Verdict.DENY:
            deny_labels |= set(rule.labels)
    assert "DropTable" in deny_labels
    assert "ManageResource" in deny_labels       # ADD JAR / ADD FILE / LIST JAR
    assert "CreateFunction" in deny_labels       # CREATE FUNCTION ... USING resource


# ---------------------------------------------------------------------------
# Behaviour: screen() with the shipped file, no JVM needed
# ---------------------------------------------------------------------------


def _verdict_for(code: str, policy_4_1) -> Verdict:
    return screen(code, policy_4_1, spec="spark-4.1").verdict


@pytest.mark.parametrize("code", [
    "spark.sql('DROP TABLE prod.users')",
    "spark.sql('drop table prod.users')",          # case-insensitive engine, same verdict
    "spark.sql('DR' + 'OP TABLE prod.users')",     # folded before parsing; no text to evade
    "spark.sql('BEGIN DROP TABLE prod.users; END')",  # 4.x scripts: rules still match
])
def test_drop_table_denies(policy_4_1, code):
    assert _verdict_for(code, policy_4_1) is Verdict.DENY


@pytest.mark.parametrize("code", [
    "spark.sql('ADD JAR /tmp/x.jar')",
    "spark.sql('LIST JAR /tmp/x.jar')",
    "spark.sql(\"CREATE FUNCTION f AS 'com.x.Y' USING jar '/tmp/x.jar'\")",
])
def test_add_jar_and_class_loading_denies(policy_4_1, code):
    assert _verdict_for(code, policy_4_1) is Verdict.DENY


def test_a_statement_the_case_body_hides_is_never_allowed(policy_4_1):
    """`SearchedCaseStatement` has no rule, so the script reports REVIEW -- the
    fail-closed answer for a body the screener cannot see. Asserted as NOT ALLOW
    because the interesting failure is a future rule letting it through."""
    verdict = _verdict_for(
        "spark.sql('BEGIN CASE WHEN TRUE THEN DROP TABLE t; END CASE; END')", policy_4_1)
    assert verdict is Verdict.REVIEW


def test_unparseable_sql_is_unknown_not_allowed(policy_4_1):
    """`!cmd` has no shell-escape alternative in the 4.1 grammar, so it fails to parse
    and reports UNKNOWN -- the fail-closed outcome, and the reason the file has no rule
    for a shell escape."""
    assert _verdict_for("spark.sql('!ls /')", policy_4_1) is Verdict.UNKNOWN


# ---------------------------------------------------------------------------
# Differential: a real Spark 4.1.3 engine
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def spark_session():
    if not (os.environ.get("JAVA_HOME") or os.environ.get("JRE_HOME")):
        pytest.skip("differential test needs a JVM (set JAVA_HOME)")
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder
        .master("local[1]")
        .appName("sparkscreen-policy-4-1")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", "/tmp/sparkscreen-policy-41")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    try:
        yield session
    finally:
        session.stop()


@pytest.fixture(scope="module")
def differential_gate():
    """The engine under test must be the release this policy names.

    `grammar_key_for_engine` resolves by measured version and raises on an unmapped one,
    so a mismatched install fails here rather than screening one engine with another
    engine's policy. `ENGINE_TO_GRAMMAR` maps exactly 4.1.3 -> spark-4.1.
    """
    pyspark = pytest.importorskip("pyspark", reason="differential test needs pyspark")
    sys.path.insert(0, str(REPO / "tests"))
    from differential.engine_matrix import grammar_key_for_engine

    key = grammar_key_for_engine(pyspark.__version__)
    if key != "spark-4.1":
        pytest.skip(f"this leg screens spark-4.1; running engine is {key}")
    return pyspark.__version__


@pytest.fixture(scope="module")
def drop_target(spark_session):
    """A real table the engine can drop, seeded via the DataFrame API.

    The in-memory catalog does not support REPLACE TABLE, and `CREATE TABLE ... AS
    SELECT` on a bare name lands in the default warehouse; saveAsTable is the shape the
    differential suite itself uses. The statement is executed BEFORE the screener sees
    it, so the DENY we assert was measured against a table the engine demonstrably had.
    """
    session = spark_session
    session.range(1).write.mode("overwrite").saveAsTable("sparkscreen_policy_41_target")
    return "sparkscreen_policy_41_target"


def test_real_engine_runs_drop_table_and_the_policy_denies_it(
        differential_gate, spark_session, drop_target, policy_4_1):
    from differential.corpus import real_spark_verdict

    sql = f"DROP TABLE {drop_target}"
    # The engine accepts and executes it -- the deny is not aimed at air.
    assert real_spark_verdict(spark_session, sql), (
        f"Spark {differential_gate} could not run {sql!r}; the expectation below is void"
    )
    # Our parser agrees it is well-formed 4.1 SQL...
    assert get_parser("spark-4.1").parse(sql) is not None
    # ...and the shipped policy refuses it.
    assert _verdict_for(f"spark.sql({sql!r})", policy_4_1) is Verdict.DENY


def test_real_engine_runs_add_jar_and_the_policy_denies_it(
        differential_gate, spark_session, policy_4_1):
    import pyspark
    from differential.corpus import real_spark_verdict

    assert pyspark.__file__ is not None
    pyspark_jars = Path(pyspark.__file__).parent / "jars"
    jars = sorted(pyspark_jars.glob("*.jar"))
    assert jars, f"no jars found under {pyspark_jars}; cannot exercise ADD JAR against the engine"
    sql = f"ADD JAR {jars[0]}"
    assert real_spark_verdict(spark_session, sql), (
        f"Spark {differential_gate} rejected {sql!r}; the expectation below is void"
    )
    assert get_parser("spark-4.1").parse(sql) is not None
    assert _verdict_for(f"spark.sql({sql!r})", policy_4_1) is Verdict.DENY
