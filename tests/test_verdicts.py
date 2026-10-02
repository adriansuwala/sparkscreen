"""Verdict aggregation -- the fail-closed contract.

This module is short and load-bearing. Each test here guards a way the screener could
report "allowed" for something it should not, which is the only failure mode that makes
the tool worse than useless.
"""
import pytest

from sparkscreen import Reason, screen, Verdict
from sparkscreen.model import Finding, Report, ANALYSIS_FAILURE_REASONS

#: Verdict/reason pairs that are legal but were historically mishandled. `verdict` and
#: `reason` are independent axes: a rule may be UNKNOWN with a non-UNKNOWN reason, and a
#: DENY may carry a reason that looks like a review.
PAIRS = [
    (Verdict.UNKNOWN, Reason.DESTRUCTIVE_STATEMENT),
    (Verdict.UNKNOWN, Reason.UNSUPPORTED_STATEMENT),
    (Verdict.UNKNOWN, Reason.UNRESOLVED_DYNAMIC_SQL),
    (Verdict.UNKNOWN, Reason.UNPARSEABLE_SQL),
    (Verdict.DENY, Reason.DENY_RULE),
    (Verdict.DENY, Reason.CODE_LENGTH_EXCEEDED),
    (Verdict.ALLOW, Reason.NO_MATCHING_RULE),
    (Verdict.ALLOW, Reason.WITHIN_ALLOWLIST),
]


def _finding(verdict, reason):
    return Finding(verdict=verdict, reason=reason, message="x")


@pytest.mark.parametrize("verdict,reason", PAIRS)
def test_aggregation_follows_verdict_not_reason(verdict, reason):
    """A single finding's verdict must drive the report verdict.

    Regression test. `Report.verdict` used to test `f.reason in ANALYSIS_FAILURE_REASONS`
    instead of `f.verdict is UNKNOWN`, so a policy rule of
    `verdict=UNKNOWN, reason=DESTRUCTIVE_STATEMENT` aggregated to ALLOW. The rules
    carrying DELETE/UPDATE/MERGE/INSERT are exactly that shape, so every one of them
    was silently waved through.
    """
    report = Report(findings=[_finding(verdict, reason)])
    assert report.verdict is verdict
    assert report.ok is (verdict is Verdict.ALLOW)


@pytest.mark.parametrize("verdict,reason", PAIRS)
def test_analysis_failure_and_review_track_the_verdict(verdict, reason):
    """Both classifications follow the verdict, never the reason.

    This used to assert `is_analysis_failure is (reason in ANALYSIS_FAILURE_REASONS)` -- i.e.
    that the two were computed from *different* axes, which was true when the verdict
    scale could not express the distinction. It cannot any more, and asserting they
    disagree would now be asserting a bug: the whole point of the REVIEW/UNKNOWN split
    is that the verdict carries the distinction, so a reason-keyed test would contradict
    it for exactly the cases that motivated the split.

    `UNSUPPORTED_STATEMENT` is the case that proves it. It reads like a failure and
    used to be classified as one, but we parse the statement and know its label and
    targets -- we only lack a policy rule. That is a review.
    """
    finding = _finding(verdict, reason)
    assert finding.is_analysis_failure is (verdict is Verdict.UNKNOWN)
    assert finding.is_unknown is (verdict is Verdict.UNKNOWN)
    assert finding.needs_review is (verdict is Verdict.REVIEW)
    # A REVIEW must never also read as a failure, or a dashboard double-counts it.
    assert not (finding.is_analysis_failure and finding.needs_review)


def test_deny_outranks_unknown():
    report = Report(findings=[
        _finding(Verdict.UNKNOWN, Reason.UNRESOLVED_DYNAMIC_SQL),
        _finding(Verdict.DENY, Reason.DENY_RULE),
    ])
    assert report.verdict is Verdict.DENY


def test_unknown_outranks_allow():
    report = Report(findings=[
        _finding(Verdict.ALLOW, Reason.NO_MATCHING_RULE),
        _finding(Verdict.UNKNOWN, Reason.UNRESOLVED_DYNAMIC_SQL),
    ])
    assert report.verdict is Verdict.UNKNOWN
    assert not report.ok


def test_empty_report_is_allow():
    """No findings means no SQL, which is genuinely allow.

    Not the same as a report where analysis failed -- that has a finding, and it is
    UNKNOWN.
    """
    assert Report().verdict is Verdict.ALLOW
    assert Report().ok


# ---------------------------------------------------------------------------
# End-to-end fail-closed checks
# ---------------------------------------------------------------------------

#: Things that must never come back ALLOW. Each is either destructive or
#: unanalyzable, and both are reasons to stop.
MUST_NOT_ALLOW = [
    "spark.sql('delete from prod.users')",
    "spark.sql('update prod.t set a = 1')",
    "spark.sql('merge into prod.t using s on t.id=s.id when matched then delete')",
    "spark.sql('insert into prod.t values (1)')",
    "spark.sql('add jar /tmp/x.jar')",
    "spark.sql('load data local inpath \'/etc/passwd\' into table t')",
    "spark.sql('create function f as \'x\' using jar /tmp/x.jar')",
    "tbl = input()\nspark.sql(f'drop table {tbl}')",
    "spark.sql('SELCT 1')",
    "for t in tables:\n    spark.sql(f'truncate table {t}')",
    "spark.sql(query)",
    # two sinks on one line: the destructive one was dropped entirely, ALLOW
    'spark.sql("DROP TABLE prod.users"); spark.sql("select 1")',
    # keyword-argument sinks were ignored by the folder and vanished
    'spark.sql(query="DROP TABLE prod.users")',
    'spark.sql("DROP TABLE prod.users", args={})',
    'spark.sql(sql="select 1"); spark.sql(query="DROP TABLE prod.users")',
    # a rebound name was folded to the pre-rebind value: a specific, wrong finding
    "t = 'safe_table'\nfor t in ['a', 'b']:\n    spark.sql(f'drop table {t}')",
    "tbl = 'safe'\ndef f(tbl):\n    return spark.sql(f'drop table {tbl}')",
    "q = 'DROP TABLE prod.users'\nq += ' WHERE x=1'\nspark.sql(q)",
    # genuine Python syntax error -- must be ANALYSIS_ERROR, not a silent allow
    "def broken(:\n    pass",
    "spark.sql('select 1' +",
]


@pytest.mark.parametrize("source", MUST_NOT_ALLOW)
@pytest.mark.parametrize("spec_key", ["spark-4.0", "spark-3.5.1"])
def test_never_allows_dangerous_or_unanalyzable(spec_key, source):
    report = screen(source, spec=spec_key)
    assert not report.ok, (
        f"{source!r} was allowed under {spec_key}: "
        f"{[f.to_dict() for f in report.findings]}"
    )
    # All three refusing verdicts. ALLOW is the only thing that must never appear
    # here, and the explicit list is the point: adding a fifth verdict later has to
    # update this line deliberately rather than slip past it.
    assert report.verdict in (Verdict.DENY, Verdict.UNKNOWN, Verdict.REVIEW), (
        f"{source!r} came back {report.verdict.value!r}, which is neither a refusal "
        f"nor an examination"
    )


@pytest.mark.parametrize("source", MUST_NOT_ALLOW)
def test_unresolvable_never_reports_ok(source):
    """`ok` is the property a caller is most likely to branch on. It must be strict."""
    assert screen(source).ok is False


def test_allow_requires_no_unknown_findings():
    """A single UNKNOWN anywhere poisons the report, even alongside ALLOWs."""
    report = Report(findings=[
        _finding(Verdict.ALLOW, Reason.NO_MATCHING_RULE),
        _finding(Verdict.ALLOW, Reason.NO_MATCHING_RULE),
        _finding(Verdict.UNKNOWN, Reason.UNPARSEABLE_SQL),
    ])
    assert report.verdict is Verdict.UNKNOWN
    assert not report.ok


def test_analysis_failures_separated_from_review_verdicts():
    report = screen("spark.sql('select 1')\nspark.sql('SELCT 1')")
    assert report.verdict is Verdict.UNKNOWN
    # one clean query, one that failed to parse
    assert len(report.analysis_failures) == 1
    assert report.analysis_failures[0].reason is Reason.UNPARSEABLE_SQL

@pytest.mark.parametrize(
    "source",
    [
        "def broken(:\n    pass",
        "spark.sql('select 1' +",
        "class ???:\n  bad",
    ],
)
def test_python_syntax_error_is_unknown_not_allow(source):
    """Unparseable *Python* must fail closed too.

    The parser test suite covers unparseable SQL. This is the other half: if we cannot
    even parse the surrounding Python, we cannot claim there is nothing in it.
    """
    report = screen(source)
    assert report.verdict is Verdict.UNKNOWN
    assert not report.ok
    assert any(f.reason is Reason.ANALYSIS_ERROR for f in report.findings)


def test_valid_python_with_no_sql_is_allow():
    """The control case: not every snippet is a problem."""
    assert screen("this is not python").ok          # a valid expression
    assert screen("x = 1\ndef f(a):\n    return a + x\n").ok


# ---------------------------------------------------------------------------
# Policy-shape traps
# ---------------------------------------------------------------------------

def test_both_allowlists_apply_independently():
    """`readable_namespaces` must not be shadowed by `writable_namespaces`.

    Regression test. The two checks were joined with `elif`, so a policy setting both
    -- "you may write staging, you may read prod", the ordinary shape of a real policy
    -- silently checked only the writable half. `staging.x` is writable and plainly not
    readable, and nothing objected.
    """
    from sparkscreen.policy import Policy, default_policy

    base = default_policy()
    policy = Policy(
        name="both", rules=base.rules,
        writable_namespaces=("staging.*",), readable_namespaces=("prod.*",),
    )
    # REVIEW, not UNKNOWN: the allowlist check ran and came back negative. The
    # statement was analysed perfectly well; it simply is not permitted.
    writable_but_unreadable = screen('spark.sql("DROP TABLE staging.x")', policy)
    assert writable_but_unreadable.verdict is Verdict.REVIEW
    assert any(f.reason is Reason.OUTSIDE_ALLOWLIST
               for f in writable_but_unreadable.findings)

    fully_allowed = screen('spark.sql("SELECT * FROM prod.x")', policy)
    assert fully_allowed.verdict is Verdict.ALLOW

    fully_denied = screen('spark.sql("SELECT * FROM other.x")', policy)
    assert fully_denied.verdict is Verdict.REVIEW


def test_outside_allowlist_is_a_review_not_an_analysis_failure():
    """OUTSIDE_ALLOWLIST means "we looked and it's outside policy", not "we couldn't look".

    This used to be a subtle assertion: the verdict was UNKNOWN and the reason was
    carefully kept out of ANALYSIS_FAILURE_REASONS so a dashboard could still tell the two
    apart. The split verdict scale states it directly -- the verdict IS REVIEW, which
    is a different value on the axis a dashboard already reads. The reason set is kept
    as a second, independent route to the same fact.
    """
    from sparkscreen.policy import Policy, default_policy

    base = default_policy()
    policy = Policy(name="r", rules=base.rules, readable_namespaces=("prod.*",))
    report = screen('spark.sql("SELECT * FROM secret.s")', policy)
    assert report.verdict is Verdict.REVIEW
    assert Reason.OUTSIDE_ALLOWLIST not in ANALYSIS_FAILURE_REASONS
    assert not report.analysis_failures
    assert report.findings[0].needs_review


def test_empty_policy_is_not_allow_everything():
    """`Policy()` with no rules still allows queries -- and that is worth knowing.

    The obvious way to write an allow-nothing policy does not do that: with no rules,
    anything in READ_ONLY_LABELS is ALLOW. So `Policy()` is closer to
    "allow read-only, escalate everything else" than to "allow nothing". Pinned here so
    the behaviour is a decision rather than an accident.
    """
    from sparkscreen.policy import Policy

    assert screen("spark.sql('select 1')", Policy()).ok
    # ...but nothing destructive slips through it. REVIEW, because the statement was
    # analysed and classified; what the empty policy lacks is an opinion about it.
    assert screen("spark.sql('drop table t')", Policy()).verdict is Verdict.REVIEW
    assert screen("spark.sql('insert overwrite table t select 1')", Policy()).verdict \
        is Verdict.REVIEW
