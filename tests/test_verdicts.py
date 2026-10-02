"""Verdict aggregation -- the fail-closed contract.

This module is short and load-bearing. Each test here guards a way the screener could
report "allowed" for something it should not, which is the only failure mode that makes
the tool worse than useless.
"""
import pytest

from sparkscreen import Reason, screen, Verdict
from sparkscreen.model import Finding, Report, UNKNOWN_REASONS

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

    Regression test. `Report.verdict` used to test `f.reason in UNKNOWN_REASONS`
    instead of `f.verdict is UNKNOWN`, so a policy rule of
    `verdict=UNKNOWN, reason=DESTRUCTIVE_STATEMENT` aggregated to ALLOW. The rules
    carrying DELETE/UPDATE/MERGE/INSERT are exactly that shape, so every one of them
    was silently waved through.
    """
    report = Report(findings=[_finding(verdict, reason)])
    assert report.verdict is verdict
    assert report.ok is (verdict is Verdict.ALLOW)


@pytest.mark.parametrize("verdict,reason", PAIRS)
def test_analysis_failure_classification_is_independent(verdict, reason):
    """`is_analysis_failure` classifies; it must not influence the verdict."""
    finding = _finding(verdict, reason)
    assert finding.is_analysis_failure is (reason in UNKNOWN_REASONS)
    assert finding.is_unknown is (verdict is Verdict.UNKNOWN)


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
    assert report.verdict in (Verdict.DENY, Verdict.UNKNOWN)


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
