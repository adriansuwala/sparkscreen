"""The shipped Spark 4.2 default policy: loadable, total, and deny-correct.

`default-spark-4.2.jsonc` is data, so nothing type-checks it: a label the 4.2 grammar
cannot produce, a duplicate label killed by first-match-wins, or a stray verdict would
all load cleanly and fail silently at screening time. These are the assertions that
would otherwise be missing.

The differential test at the bottom is the one this file exists for. The 3.5.1 policy's
guarantees were verified against a live 3.5.1 engine; the 4.2 file gets the same
treatment: the engine must agree that the statements in the deny set are real (it parses
them -- a ParseException is a rejection), and the screener must deny each one under this
policy. Skipped unless pyspark 4.2.0 and a JVM are present, so the fast suite stays
inert -- see the two-venv note in AGENTS.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sparkscreen.analysis import effects
from sparkscreen.analysis.label_universe import labels_for_grammar
from sparkscreen.model import Effect, Verdict
from sparkscreen.policy import load_policy, policy_label_drift
from sparkscreen.screen import screen

ROOT = Path(__file__).resolve().parents[1]

#: The file under test, and the grammar key every label in it must resolve against.
POLICY_PATH = ROOT / "src" / "sparkscreen" / "policies" / "default-spark-4.2.jsonc"
KEY = "spark-4.2"


def policy():
    return load_policy(POLICY_PATH)


def _labels() -> dict[str, list[str]]:
    return {r.id: list(r.labels) for r in policy().rules}


# ---------------------------------------------------------------------------
# loadability -- the 3.5.1 edition's test drives the loader through the CLI; here
# `load_policy` itself is the seam under test, since the file's whole job is to load.
# ---------------------------------------------------------------------------

def test_shipped_4_2_policy_loads():
    pol = load_policy(str(POLICY_PATH))
    assert pol.name == "sparkscreen-defaults-4.2"
    assert pol.rules, "a default policy with no rules screens nothing"
    assert pol.writable_namespaces == () and pol.readable_namespaces == ()


# ---------------------------------------------------------------------------
# label correctness
# ---------------------------------------------------------------------------

def test_every_label_is_in_the_4_2_universe():
    """A label the grammar cannot produce is a rule that can never fire: dead policy.

    The negative control is real: the 3.5.1 edition keys `deny.overwrite` on
    `InsertIntoReplaceWhere`, which the 4.2 grammar split away -- pasting that file's
    labels into this one would have shipped four dead deny rules.
    """
    universe = labels_for_grammar(KEY)
    assert "InsertIntoReplaceWhere" not in universe, "universe changed; update the split note"
    for rule_id, labels in _labels().items():
        for label in labels:
            assert label in universe, f"{rule_id}: {label!r} is not a {KEY} statement label"


def test_every_label_maps_to_effects():
    """`effects_for_label` raises UnmappedLabelError on a miss, and screen() deliberately
    does not catch it -- so a policy label with no effect mapping turns a screen into a
    traceback for the user, not a safe verdict. Better to find it here."""
    for rule_id, labels in _labels().items():
        for label in labels:
            effects.effects_for_label(label)  # must not raise


def test_no_label_appears_in_two_rules():
    """First-match-wins: a duplicated label makes the later rule dead code that reads as
    coverage. Asserted rather than linted, because nothing else sees rule order."""
    seen: dict[str, str] = {}
    for rule in policy().rules:
        for label in rule.labels:
            assert label not in seen, (
                f"{label!r} is matched by both {seen[label]} and {rule.id}; "
                "first-match-wins makes the later rule dead"
            )
            seen[label] = rule.id


def test_verdicts_are_only_deny_review_allow():
    for rule in policy().rules:
        assert rule.verdict in (Verdict.DENY, Verdict.REVIEW, Verdict.ALLOW), rule.id


def test_no_allow_rule_matches_a_dangerous_effect():
    """`allow` is only ever for statements with no durable effect. What "no durable
    effect" means here is the effect table, not the author's memory of the label.

    One deliberate exception, carried over from the 3.5.1 edition: the
    external-create-location rule has a base verdict of allow and narrows ITSELF at
    match time with `literal_prefixes` (managed creates allow, LOCATION outside the
    warehouse escalates to DENY). The effect-level check cannot see that mechanism,
    so an allow rule is accepted iff it is harmless OR self-narrowing -- an allow
    rule with dangerous effects and no literal_prefixes would be a silent hole.
    """
    dangerous = {Effect.DESTROY_DATA, Effect.LOAD_CODE, Effect.REACHES_EXTERNAL,
                 Effect.WRITE_DATA, Effect.WRITE_SCHEMA}
    universe = labels_for_grammar(KEY)
    for rule in policy().rules:
        if rule.verdict is not Verdict.ALLOW:
            continue
        for label in rule.labels:
            assert label in universe, rule.id
            got = effects.effects_for_label(label)
            if not (got & dangerous):
                continue
            assert rule.literal_prefixes, (
                f"{rule.id} allows {label}, whose effects are {sorted(got)}, "
                "and the rule has no literal_prefixes to narrow itself"
            )


def test_universe_is_total_over_the_file():
    """Every 4.2 label is either matched by a rule here or falls through to a verdict we
    can defend: a read-only label (ALLOW via READ_ONLY_LABELS) or the generic review
    path (fail closed). Anything else would be an untaught statement getting a verdict
    nobody chose."""
    universe = labels_for_grammar(KEY)
    matched = {label for labels in _labels().values() for label in labels}
    for label in sorted(universe - matched):
        # Wrappers are descended through at runtime, so no rule ever sees them -- the
        # 3.5.1 edition documents this on allow.query and they are not gaps.
        if label in ("DmlStatement", "SingleInsertQuery", "MultiInsertQuery"):
            continue
        from sparkscreen.policy import READ_ONLY_LABELS
        if label in READ_ONLY_LABELS:
            continue
        # The generic path is REVIEW with reason unsupported_statement; assert it does
        # actually land there rather than trusting it.
        pol = policy()
        findings = pol.evaluate_statement(label, [], [], sql=label)
        assert findings, label
        f = findings[-1]
        assert f.verdict is Verdict.REVIEW and f.reason.name == "UNSUPPORTED_STATEMENT", (
            f"{label} is untaught but does not take the generic review path"
        )


def test_the_generic_review_path_is_reachable_as_commented():
    """allow.query's comment names the SHOWs that take the generic review path. If the
    read-only set grows to include them, the comment is stale -- make this fail."""
    from sparkscreen.policy import READ_ONLY_LABELS
    for label in ("ShowTableExtended", "ShowTblProperties", "ShowCollations"):
        assert label not in READ_ONLY_LABELS, f"{label} left the generic review path; fix the comment"
        assert label not in {l for ls in _labels().values() for l in ls}


def test_the_compound_control_flow_labels_take_the_generic_review_path():
    """The 4.x control-flow statements are walked without descending into their bodies,
    so no rule below may ALLOW them (see the WHAT 4.x ADDS note). If one is ever given a
    rule, it must be REVIEW and the walk must first learn to descend."""
    pol = policy()
    for label in ("SearchedCaseStatement", "SimpleCaseStatement"):
        assert label not in {l for ls in _labels().values() for l in ls}, (
            f"{label} got a rule; check the walk descends into its bodies first"
        )
        f = pol.evaluate_statement(label, [], [], sql=label)[-1]
        assert f.verdict is Verdict.REVIEW


def test_label_drift_against_destructive_labels():
    """`policy_label_drift` separates dead coverage (destructive_only) from deny rules
    outside the destructive set (deny_rules_only, exempt from the namespace allowlist).
    The first is always a bug; the second is accepted in the shipped file -- FailSetRole,
    EXECUTE IMMEDIATE and the REPLACE split are denied on purpose -- so only the dead
    direction is gated here."""
    drift = policy_label_drift(policy())
    # DESTRUCTIVE_LABELS is a cross-grammar union; a per-grammar file only owes
    # rules for labels its OWN grammar can emit. SetTableCollation sits in the
    # union but no 4.2 statement resolves to it (verified against the generated
    # parser), so a rule for it here would be untestable dead weight. The dead
    # direction that IS a bug: a destructive label reachable under spark-4.2
    # with no rule at all.
    universe = set(labels_for_grammar(KEY))
    reachable = [l for l in drift["destructive_only"] if l in universe]
    assert reachable == [], reachable


# ---------------------------------------------------------------------------
# deny set, statically: parser + policy only, no engine needed
# ---------------------------------------------------------------------------

#: The differential cases, so the static and engine-verified assertions cover the same
#: statements. Each is real Spark SQL -- the engine half is recorded in the differential
#: corpus or verified live; see test_engine_agrees_the_deny_set_deny.
DENY_CASES = (
    ("DROP TABLE prod.users", "DropTable"),
    ("ADD JAR /tmp/x.jar", "ManageResource"),
    ("EXECUTE IMMEDIATE 'DROP TABLE prod.users'", "VisitExecuteImmediate"),
)


@pytest.mark.parametrize("sql,expected_label", DENY_CASES, ids=[s for s, _ in DENY_CASES])
def test_screen_denies_the_deny_set(sql, expected_label):
    """No engine, no excuses: these run in the fast suite against the committed 4.2
    parser. `EXECUTE IMMEDIATE 'DROP TABLE ...'` is here because the payload is a
    separate finding -- the wrapper alone is REVIEW -- and the compound verdict must
    still be DENY."""
    rep = screen(f"spark.sql({sql!r})", policy(), spec=KEY)
    assert rep.verdict is Verdict.DENY, rep.summary()
    assert any(f.statement == expected_label for f in rep.findings)


def test_screen_allows_a_plain_query():
    """The negative half of the same property: the policy denies destruction without
    denying everything, so the allow path is exercised by the file that ships it."""
    rep = screen("spark.sql('SELECT 1')", policy(), spec=KEY)
    assert rep.verdict is Verdict.ALLOW, rep.summary()


# ---------------------------------------------------------------------------
# differential -- needs pyspark 4.2.0 and a JVM, exactly like tests/differential/
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_engine_agrees_the_deny_set_deny():
    """Differential: the live 4.2.0 engine parses each DENY_CASE statement, and screen()
    under this policy denies it.

    Direction matches the corpus tests: an engine rejection we deny is harmless noise
    (still fail closed), but an engine acceptance we allow would be a hole -- which is
    why the engine side is asserted, not assumed.
    """
    pyspark = pytest.importorskip("pyspark", reason="differential test needs pyspark")
    if pyspark.__version__ != "4.2.0":
        pytest.skip(f"this policy is the 4.2 edition; installed engine is {pyspark.__version__}")
    if not (__import__("os").environ.get("JAVA_HOME") or __import__("os").environ.get("JRE_HOME")):
        pytest.skip("differential test needs a JVM (set JAVA_HOME)")

    sys.path.insert(0, str(ROOT / "tests"))
    from differential.corpus import real_spark_verdict

    from pyspark.sql import SparkSession
    spark = (
        SparkSession.builder
        .master("local[1]")
        .appName("sparkscreen-policy-4-2")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", "/tmp/sparkscreen-policy-4-2")
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("ERROR")
    try:
        for sql, _label in DENY_CASES:
            engine_parses = real_spark_verdict(spark, sql)
            assert engine_parses, f"4.2.0 rejected {sql!r}; the deny set contains a dead case"
            rep = screen(f"spark.sql({sql!r})", policy(), spec=KEY)
            assert rep.verdict is Verdict.DENY, rep.summary()
    finally:
        spark.stop()
