"""The finding model: what the screener concluded, and how sure it is.

Three verdicts, not two. The third is the point of the whole design.

    ALLOW       we resolved the SQL, parsed it, and no policy matched
    DENY        we resolved the SQL, parsed it, and a deny rule matched
    UNKNOWN     we could not determine -- unparseable SQL, an unresolved dynamic
                string, an unsupported construct, or a resource limit

`UNKNOWN` exists because the alternative is worse than useless. A screener that
reports "no issues found" on code it could not analyze is a false assurance with a
confident voice, and that is precisely how a dangerous tool gets trusted. Every
internal failure mode maps here. Nothing in this package has a path from
"something went wrong" to `ALLOW`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


class Reason(str, Enum):
    """Why a finding has the verdict it does.

    The UNKNOWN_* reasons are load-bearing: each names a specific way analysis can
    fail, so an operator reading a report can tell "we couldn't parse this" apart from
    "we parsed it and it's fine".
    """

    # ALLOW
    NO_MATCHING_RULE = "no_matching_rule"
    WITHIN_ALLOWLIST = "within_allowlist"

    # DENY
    DENY_RULE = "deny_rule"
    DESTRUCTIVE_STATEMENT = "destructive_statement"
    OUTSIDE_ALLOWLIST = "outside_allowlist"
    CODE_LENGTH_EXCEEDED = "code_length_exceeded"
    PYTHON_DANGEROUS_CALL = "dangerous_python_call"

    # UNKNOWN -- the interesting half
    UNPARSEABLE_SQL = "unparseable_sql"
    UNRESOLVED_DYNAMIC_SQL = "unresolved_dynamic_sql"
    UNSUPPORTED_STATEMENT = "unsupported_statement"
    RESOURCE_LIMIT = "resource_limit"
    UNSUPPORTED_SPARK_VERSION = "unsupported_spark_version"
    ANALYSIS_ERROR = "analysis_error"


#: Reasons that mean "we could not analyze this". Anything else is a real verdict.
UNKNOWN_REASONS = frozenset({
    Reason.UNPARSEABLE_SQL,
    Reason.UNRESOLVED_DYNAMIC_SQL,
    Reason.UNSUPPORTED_STATEMENT,
    Reason.RESOURCE_LIMIT,
    Reason.UNSUPPORTED_SPARK_VERSION,
    Reason.ANALYSIS_ERROR,
})


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class Finding:
    """One observation about one piece of code."""

    verdict: Verdict
    reason: Reason
    message: str
    severity: Severity = Severity.MEDIUM
    #: Line in the Python source, when the finding came from a code sink.
    line: int | None = None
    #: Line/column within the SQL text, when the finding came from inside SQL.
    sql_line: int | None = None
    sql_column: int | None = None
    #: The SQL we actually parsed, when we got that far.
    sql: str | None = None
    #: Top-level ANTLR statement label, e.g. "DropTable".
    statement: str | None = None
    #: Policy rule id that produced this finding, if any.
    rule: str | None = None
    #: Tables/namespaces the statement touches.
    targets: tuple[str, ...] = ()
    #: Rule name for a matched policy rule, for human-readable reports.
    matched_label: str | None = None

    @property
    def is_unknown(self) -> bool:
        return self.reason in UNKNOWN_REASONS

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason.value,
            "message": self.message,
            "severity": self.severity.value,
            "line": self.line,
            "sql": self.sql,
            "statement": self.statement,
            "rule": self.rule,
            "matched_label": self.matched_label,
            "targets": list(self.targets),
        }


@dataclass
class Report:
    """Result of screening one snippet of code."""

    findings: list[Finding] = field(default_factory=list)
    #: Lines of Python analyzed.
    lines: int = 0
    #: Policy name applied.
    policy: str = ""
    #: Grammar key used.
    grammar: str = ""

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def verdict(self) -> Verdict:
        """Worst verdict present. UNKNOWN outranks ALLOW but not DENY.

        UNKNOWN outranks ALLOW deliberately: a report with one unanalyzable sink
        cannot be summarised as "allowed", because we do not actually know.
        """
        if any(f.verdict is Verdict.DENY for f in self.findings):
            return Verdict.DENY
        if any(f.is_unknown for f in self.findings):
            return Verdict.UNKNOWN
        return Verdict.ALLOW

    @property
    def ok(self) -> bool:
        """True only when the verdict is ALLOW with no unknowns."""
        return self.verdict is Verdict.ALLOW

    def by_verdict(self, verdict: Verdict) -> list[Finding]:
        return [f for f in self.findings if f.verdict is verdict]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "policy": self.policy,
            "grammar": self.grammar,
            "lines": self.lines,
            "findings": [f.to_dict() for f in self.findings],
        }

    def summary(self) -> str:
        counts = {v: len(self.by_verdict(v)) for v in Verdict}
        return (
            f"{self.verdict.value.upper()}: "
            f"{counts[Verdict.DENY]} deny, "
            f"{counts[Verdict.UNKNOWN]} unknown, "
            f"{counts[Verdict.ALLOW]} allow"
        )


class ScreenError(Exception):
    """Raised only for programmer errors / misconfiguration, never for policy outcomes.

    Ordinary "could not analyze" outcomes are Findings with an UNKNOWN verdict.
    """