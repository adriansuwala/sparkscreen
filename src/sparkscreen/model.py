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
from enum import Enum, Flag
from typing import Any, Iterable


class Verdict(str, Enum):
    """The decision, on a four-value scale.

    The split between REVIEW and UNKNOWN is the whole point of the scale, and it exists
    because the old two-value-plus-one version could not answer "is the agent
    misbehaving, or is the screener failing?". Both of those used to be UNKNOWN.

      ALLOW    we resolved it, parsed it, and policy did not object
      DENY     policy has a rule that forbids it
      REVIEW   we know exactly what it does, and a person should decide. The screener
               worked. DELETE from prod, a write outside the allowlist, a statement
               type this policy has no rule for.
      UNKNOWN  we could not determine. The screener failed. Unparseable SQL, a string
               we could not fold, a resource limit, a Python syntax error.

    Both REVIEW and UNKNOWN are refusals to proceed -- neither is a quiet pass -- so the
    fail-closed property is unchanged. What changed is that a dashboard can now separate
    "the agent wanted something questionable" from "we have a bug", which is the
    difference between filing a finding against the agent and filing one against this
    tool.

    Ranking for aggregation is DENY > UNKNOWN > REVIEW > ALLOW. UNKNOWN outranks REVIEW
    because a report containing something we could not look at is the more urgent of
    the two: an unexamined statement is an unknown risk, whereas a REVIEW has been
    looked at and is waiting on a decision. Both still gate identically.
    """

    ALLOW = "allow"
    DENY = "deny"
    REVIEW = "review"
    UNKNOWN = "unknown"


class Effect(Flag):
    """What an operation *does*, independent of what the policy decided about it.

    Orthogonal to `Verdict` on purpose. The verdict axis answers "what did the policy
    conclude" -- which depends on the configured rules, the namespace allowlists and
    the operator's intent. This axis answers "what would Spark do if it ran", which
    depends only on the statement. Conflating the two was the original bug: a
    `DROP TABLE` is `DESTRUCTIVE_STATEMENT` under one policy and a reviewable
    `WRITE_SCHEMA` under another, but it destroys the table either way.

    A `Flag`, not a single-valued enum, because the axes are genuinely orthogonal and
    a single value cannot express the cases that matter:

        ALTER TABLE t DROP COLUMN a   -- schema change AND data destruction
        TRUNCATE TABLE t              -- data destruction, schema untouched
        LOAD DATA LOCAL INPATH '...'  -- reads the driver's filesystem AND writes rows

    A single-valued design was tried and rejected: it forced a choice between "it is
    a schema change" and "it destroys data", and whichever was picked lost the other.

    Note what is deliberately *not* here: there is no `Effect.UNKNOWN`. An effect set
    is a set of flags that are *known to apply*, so an empty set is ambiguous between
    "we analysed this and it does nothing durable" (`USE prod`) and "we could not
    analyse it at all" (unparseable SQL). Absence is the signal for the second, and it
    is carried by the verdict/reason axes -- `Finding.effect` is empty for both cases
    and the caller distinguishes them with `is_analysis_failure`, exactly as it already
    distinguishes UNKNOWN from DENY. Adding an `UNKNOWN` member here would destroy
    that distinction, which is why this type has none.

    Plain `Flag` rather than `str, Flag` on purpose: with a `str` mixin,
    `Effect.DESTROY_DATA == "destroy_data"`, so a bare string could silently enter an
    effect set and be compared as if it were the flag. Nothing in this package should
    be coercible into an effect by accident.
    """

    #: Changes structure: columns, constraints, indexes, tables, views, namespaces.
    WRITE_SCHEMA = 1
    #: Changes rows: INSERT / UPDATE / DELETE / MERGE.
    WRITE_DATA = 2
    #: Irreversibly loses data or a durable object. Cannot be undone by re-running.
    DESTROY_DATA = 4
    #: Reads table rows.
    READ_DATA = 8
    #: Reads the *driver's* local filesystem (`LOAD DATA LOCAL INPATH`).
    READ_LOCAL_FS = 16
    #: Loads code that will be executed (`ADD JAR` / `CREATE FUNCTION ... USING JAR`).
    LOAD_CODE = 32
    #: Touches a system outside the cluster: a filesystem path, a catalog, or a
    #: procedure whose body we cannot see.
    REACHES_EXTERNAL = 64
    #: Changes runtime or session state: `SET spark.*`, CACHE, SET ROLE, MSCK REPAIR.
    CHANGE_CONFIG = 128

    @classmethod
    def _ordered(cls) -> tuple["Effect", ...]:
        """Members in declaration order -- powers of two, so `sorted` on values."""
        return tuple(sorted(cls, key=lambda m: m.value))

    def __str__(self) -> str:
        """Render a combination as `WRITE_SCHEMA|DESTROY_DATA`, not `Effect.X|Y`.

        Combination flags inherit `Flag.__str__`, which prints the repr of every
        member. That is unreadable in a report and unreadable in an assertion failure,
        and this type exists to be printed.
        """
        names = [f.name for f in type(self)._ordered() if f and f in self]
        return "|".join(n for n in names if n)


def effect_names(effects: "frozenset[Effect] | Iterable[Effect]") -> list[str]:
    """Sorted member names, for JSON and reports.

    Composite values are expanded. Iterating a `Flag` yields one element per *member*,
    but a set can also hold a composite pseudo-member (`Effect(3)`), whose `.name` is
    the joined string `"WRITE_SCHEMA|WRITE_DATA"` -- a single list entry that no
    consumer can match against a member name. Expanding on the way out means callers
    always get individual flags, whatever shape went in.
    """
    out: set[str] = set()
    for e in effects:
        if not e.value:
            continue
        for member in Effect._ordered():
            if member and member in e:
                out.add(member.name)
    return sorted(n for n in out if n)


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

    # There is deliberately no PYTHON_DANGEROUS_CALL here. One existed and was never
    # raised, which meant the enum advertised screening of os.system / shutil.rmtree /
    # dbutils.fs.rm that the tool does not do -- worse than absence, because a reader
    # would reasonably assume the coverage existed. Removed 2026-10-02
    # (sparkscreen-znf): Python-level call screening is out of scope, the target
    # environment is an ephemeral pod, and the expensive failures there are wrong
    # warehouse writes, which the spark.sql() path already covers.

    # UNKNOWN -- the interesting half
    UNPARSEABLE_SQL = "unparseable_sql"
    UNRESOLVED_DYNAMIC_SQL = "unresolved_dynamic_sql"
    UNSUPPORTED_STATEMENT = "unsupported_statement"
    RESOURCE_LIMIT = "resource_limit"
    UNSUPPORTED_SPARK_VERSION = "unsupported_spark_version"
    ANALYSIS_ERROR = "analysis_error"


#: Reasons that mean "we could not clear this". Classification only -- it must never be
#: used to compute a verdict (see `Finding.is_unknown` and `Report.verdict`).
#:
#: Note this is a *subset* of the reasons that can accompany an UNKNOWN verdict, not the
#: definition of one. OUTSIDE_ALLOWLIST is the deliberate counter-example: a
#: fully-analysed statement that simply touches a namespace the policy does not permit is
#: UNKNOWN (a human should look) but is not an analysis failure (we did the analysis).
#: Keeping OUTSIDE_ALLOWLIST out of this set is what lets a dashboard say "we couldn't
#: look" separately from "we looked, and it's outside policy".
#: Reasons that mean *the screener could not analyse this*, as opposed to a reason that
#: means *it analysed it and a person should decide*.
#:
#: This used to double as "the reasons that produce Verdict.UNKNOWN", which conflated
#: the two and made the distinction unavailable to any consumer. With the four-value
#: verdict scale the distinction is carried by the verdict itself, and this set is the
#: definition of UNKNOWN rather than a proxy for it.
#:
#: `UNSUPPORTED_STATEMENT` was here and is deliberately not any more. We parsed it, we
#: know the label and the targets; what we lack is a policy rule. That is a review, not a
#: blind spot, and calling it a failure made every unfamiliar-but-benign statement look
#: like a tool bug.
ANALYSIS_FAILURE_REASONS = frozenset({
    Reason.UNPARSEABLE_SQL,
    Reason.UNRESOLVED_DYNAMIC_SQL,
    Reason.RESOURCE_LIMIT,
    Reason.UNSUPPORTED_SPARK_VERSION,
    Reason.ANALYSIS_ERROR,
})

#: Backwards-compatible alias. The name is now a slight misnomer -- it means "analysis
#: failure", not "every UNKNOWN verdict" -- but keeping it avoids breaking an import for
#: no benefit. Prefer `ANALYSIS_FAILURE_REASONS`.
UNKNOWN_REASONS = ANALYSIS_FAILURE_REASONS


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
    #: What the statement does, as a set of orthogonal `Effect` flags.
    #:
    #: Independent of `verdict`: a `DROP TABLE` is DESTROY_DATA here whichever policy
    #: ran, and is DENY or UNKNOWN depending on namespace allowlists.
    #:
    #: Empty means "no known effect", and is NOT the same as "harmless". A statement we
    #: could not analyse at all (unparseable SQL, an unresolved dynamic string, a
    #: resource limit) gets an empty set deliberately: we have no idea what it does,
    #: and inventing `READ_DATA` because that is the common case would be exactly the
    #: confident wrong answer this package exists to avoid. `is_analysis_failure` is
    #: what tells the two apart.
    effect: frozenset[Effect] = frozenset()

    def effect_flags(self) -> Effect:
        """The effect set collapsed into a single combinable flag value."""
        out = Effect(0)
        for e in self.effect:
            out |= e
        return out

    def has_effect(self, *effects: Effect) -> bool:
        """True if every flag in `effects` is present."""
        return all(e in self.effect for e in effects)

    @property
    def is_unknown(self) -> bool:
        """True when this finding's verdict is UNKNOWN.

        Note this asks the *verdict*, not the reason. The two are independent axes and
        conflating them is a fail-open bug: a policy rule can legitimately be
        `verdict=UNKNOWN` with `reason=DESTRUCTIVE_STATEMENT` (DELETE/MERGE are parsed
        fine -- we just want a human to confirm the WHERE clause). Aggregating on
        `reason in UNKNOWN_REASONS` instead of on the verdict made every such rule
        report as ALLOW, so `DELETE FROM prod.users` was waved through. See
        `Report.verdict`.
        """
        return self.verdict is Verdict.UNKNOWN

    @property
    def is_analysis_failure(self) -> bool:
        """True when *the screener could not analyse this*, as opposed to a review.

        Now equivalent to `is_unknown`, because that distinction moved from the reason
        axis onto the verdict axis. It is kept as a separate name because it says what
        the thing *is* rather than what it is called, and because `analysis_failures` on
        the report is the dashboard-facing form of it.

        The check is on the verdict, never on the reason. Deriving it from
        `reason in ANALYSIS_FAILURE_REASONS` would reintroduce exactly the bug that
        made DELETE/MERGE/INSERT report as ALLOW: those carry
        `verdict=REVIEW, reason=DESTRUCTIVE_STATEMENT`, and a reason-keyed test would
        read them as failures.
        """
        return self.verdict is Verdict.UNKNOWN

    @property
    def needs_review(self) -> bool:
        """True for a REVIEW finding: we know what it does, and a person must decide.

        Distinct from `is_analysis_failure` because the two call for different responses
        -- one is a finding against the code, the other is a finding against the tool.
        """
        return self.verdict is Verdict.REVIEW

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
            "effect": effect_names(self.effect),
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
        """Worst verdict present: DENY > UNKNOWN > REVIEW > ALLOW.

        Aggregated on each finding's *verdict*, never on its reason. The reason is
        metadata for a human reader; the verdict is the decision. Deriving one from the
        other is how DELETE/MERGE/INSERT silently became ALLOW -- the rules that carry
        them are `verdict=REVIEW, reason=DESTRUCTIVE_STATEMENT`, and the old code asked
        whether the reason was in UNKNOWN_REASONS.

        Neither UNKNOWN nor REVIEW may be summarised as "allowed", and the ordering
        between them says which to mention first. UNKNOWN outranks REVIEW because a
        statement nobody examined is a larger unknown than one that was examined and is
        waiting on a decision.
        """
        if any(f.verdict is Verdict.DENY for f in self.findings):
            return Verdict.DENY
        if any(f.verdict is Verdict.UNKNOWN for f in self.findings):
            return Verdict.UNKNOWN
        if any(f.verdict is Verdict.REVIEW for f in self.findings):
            return Verdict.REVIEW
        return Verdict.ALLOW

    @property
    def analysis_failures(self) -> list[Finding]:
        """Findings where analysis could not complete at all, as opposed to a review.

        Useful for dashboards: "we couldn't look" is a different problem from "we looked
        and a human should decide". Never affects the verdict.
        """
        return [f for f in self.findings if f.is_analysis_failure]

    @property
    def ok(self) -> bool:
        """True only when the verdict is ALLOW with no unknowns."""
        # Strict, and deliberately stricter than "no DENY": REVIEW and UNKNOWN both
        # mean this was not cleared. An absent REVIEW in the enum would be caught here,
        # since anything that is not ALLOW returns False.
        return self.verdict is Verdict.ALLOW

    @property
    def effects(self) -> frozenset[Effect]:
        """Every effect seen anywhere in the report, unioned across findings.

        A summary, not a verdict. It answers "how big is the blast radius of this
        snippet" without reference to policy -- "this code drops tables and loads jars"
        is true regardless of whether the configured policy denied it, allowed it, or
        never heard of it.

        Empty when every finding failed analysis. That is not "this snippet is
        harmless", and callers must not read it that way: the verdict and
        `analysis_failures` are what say whether anything was actually determined.
        """
        out: set[Effect] = set()
        for f in self.findings:
            out |= f.effect
        return frozenset(out)

    def by_effect(self, effect: Effect) -> list[Finding]:
        """Findings carrying `effect`. Union-aware: partial flags are not accepted."""
        return [f for f in self.findings if effect in f.effect]

    def by_verdict(self, verdict: Verdict) -> list[Finding]:
        return [f for f in self.findings if f.verdict is verdict]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "policy": self.policy,
            "grammar": self.grammar,
            "lines": self.lines,
            "effects": effect_names(self.effects),
            "findings": [f.to_dict() for f in self.findings],
        }

    def summary(self) -> str:
        counts = {v: len(self.by_verdict(v)) for v in Verdict}
        return (
            f"{self.verdict.value.upper()}: "
            f"{counts[Verdict.DENY]} deny, "
            f"{counts[Verdict.UNKNOWN]} unknown, "
            f"{counts[Verdict.REVIEW]} review, "
            f"{counts[Verdict.ALLOW]} allow"
        )


class ScreenError(Exception):
    """Raised only for programmer errors / misconfiguration, never for policy outcomes.

    Ordinary "could not analyze" outcomes are Findings with an UNKNOWN verdict.
    """