"""Policy: declarative rules over parse-tree facts.

The point of a real parser here is that rules key on ANTLR labeled alternatives
(`DropTable`, `InsertOverwriteTable`, `MergeIntoTable`, `LoadData`, `ManageResource`,
`Call`, ...) rather than on SQL substrings. That is what makes ``"DR"+"OP TABLE t"``
and ``DROP /* comment */ TABLE t`` behave the same as ``DROP TABLE t``: there is no text
to evade.

A policy is a set of rules plus limits. Rules match on the statement label, on targets,
and on extracted string literals. Everything is data -- a company can add its own
protected namespaces without forking this package.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .model import Finding, Reason, Severity, Verdict
from .analysis.treewalk import (
    NamespaceRef,
    extract_namespaces,
    extract_string_literals,
    statement_label,
)


@dataclass
class Rule:
    """One policy rule.

    `labels` matches top-level ANTLR statement labels (`DropTable`, ...).
    `deny_labels` is the inverse list used for deny-all-destructive: rather than
    enumerating safe statements, we enumerate destructive ones and deny by default.
    """

    id: str
    verdict: Verdict
    reason: Reason
    message: str
    severity: Severity = Severity.MEDIUM
    labels: tuple[str, ...] = ()
    #: If set, the rule only fires when one of these dotted patterns matches a target.
    target_patterns: tuple[str, ...] = ()
    #: If set, the rule only fires when a string literal equals/startswith one of these.
    literal_prefixes: tuple[str, ...] = ()
    #: Match only these statement labels (used by the allow-list default).
    description: str = ""

    def applies_to(self, label: str) -> bool:
        return not self.labels or label in self.labels


#: ANTLR labels for statements that mutate or destroy data. This is the core
#: destructive set; it is intentionally broad and lives in data, not code, so it can
#: be extended without touching the screener.
DESTRUCTIVE_LABELS: tuple[str, ...] = (
    "DropTable",
    "DropView",
    "DropNamespace",
    "DropIndex",
    "DropFunction",
    "DropTablePartitions",
    "DropTableColumns",
    "DropTableConstraint",
    "TruncateTable",
    "InsertOverwriteTable",
    "InsertOverwriteHiveDir",
    "InsertOverwriteDir",
    "ReplaceTable",
    "RenameTable",
    "RenameTableColumn",
    "RenameTablePartition",
    "SetTableLocation",
    "AddTablePartition",
    "LoadData",
    "ManageResource",          # ADD JAR / LIST JAR / ADD FILE
    "SetConfiguration",        # SET spark.* -- can change runtime behaviour
    "SetQuotedConfiguration",
    "ResetConfiguration",
    "ResetQuotedConfiguration",
    "SetPath",
    "CreateFunction",          # CREATE FUNCTION ... USING JAR
    "DropVariable",
    "CacheTable",
    "UncacheTable",
    "ClearCache",
    "CreateIndex",
    "DropVariable",
    "SetNamespaceLocation",
    "SetNamespaceProperties",
    "UnsetNamespaceProperties",
    "SetTableProperties",
    "UnsetTableProperties",
    "AlterTableAlterColumn",
    "AddTableColumns",
    "RepairTable",
    "AlterViewQuery",
    "AlterViewSchemaBinding",
    "AlterClusterBy",
    "SetTableSerDe",
    "SetTableCollation",
    "AlterTableCollation",
    "AddTableConstraint",
    "HiveChangeColumn",
    "HiveReplaceColumns",
)

#: Statements that are inherently read-only, keyed on the *resolved* statement label
#: (i.e. after `effective_label` has descended through wrappers). Anything not listed
#: here and not matched by a rule is UNKNOWN, which is the fail-closed default.
READ_ONLY_LABELS: tuple[str, ...] = (
    "StatementDefault",       # a plain query
    "Use",
    "UseNamespace",
    "SetCatalog",
    "CreateNamespace",
    "CreateTable",
    "CreateTableLike",
    "CreateTempViewUsing",
    "CreateView",
    "ShowTables",
    "ShowDatabases",
    "ShowNamespaces",
    "ShowColumns",
    "ShowViews",
    "ShowPartitions",
    "ShowFunctions",
    "ShowProcedures",
    "ShowCreateTable",
    "ShowCurrentNamespace",
    "ShowCatalogs",
    "ShowCollation",
    "DescribeRelation",
    "DescribeQuery",
    "DescribeFunction",
    "DescribeProcedure",
    "DescribeNamespace",
    "CommentTable",
    "CommentNamespace",
    "CommentColumn",
    "RefreshTable",
    "RefreshFunction",
    "RefreshResource",
    "Analyze",
    "AnalyzeTables",
    "Explain",
    "Call",
    "Execute",
    "CreateVariable",
    "Fetch",
    "Open",
    "Close",
)


@dataclass
class Limits:
    """Hard caps on what we will analyze.

    `max_code_chars` is a review-budget control, not a safety control: the point is
    that a human should not be asked to eyeball five screens of code, while still
    letting automation wave through short, routine transformations. Exceeding it is a
    DENY (the reviewer's explicit intent) rather than UNKNOWN.
    """

    #: Reject snippets longer than this before any analysis.
    max_code_chars: int = 20_000
    #: Reject SQL statements longer than this.
    max_sql_chars: int = 10_000
    #: Cap on statements analyzed per snippet, to bound runtime.
    max_statements: int = 200
    #: Cap on string literals extracted per statement.
    max_literals: int = 100
    #: Cap on table references extracted per statement.
    max_targets: int = 100


@dataclass
class Policy:
    """A named, configurable set of rules and limits."""

    name: str = "default"
    rules: list[Rule] = field(default_factory=list)
    limits: Limits = field(default_factory=Limits)
    #: Namespaces whose tables may be written to / dropped, dotted with `*` wildcards.
    writable_namespaces: tuple[str, ...] = ()
    #: Any statement touching a namespace outside this set is UNKNOWN (needs a human),
    #: even if otherwise read-only. Empty means "no restriction".
    readable_namespaces: tuple[str, ...] = ()

    def rule_for_label(self, label: str) -> Rule | None:
        for r in self.rules:
            if r.applies_to(label):
                return r
        return None

    # -- evaluation ---------------------------------------------------------

    def evaluate_statement(
        self,
        label: str,
        targets: Iterable[NamespaceRef],
        literals: Iterable[str],
        *,
        sql: str | None = None,
        line: int | None = None,
    ) -> list[Finding]:
        """Evaluate one parsed statement. Returns zero or more findings."""
        findings: list[Finding] = []
        targets = list(targets)
        literals = list(literals)

        if len(targets) > self.limits.max_targets:
            return [Finding(
                verdict=Verdict.UNKNOWN,
                reason=Reason.RESOURCE_LIMIT,
                message=f"statement references {len(targets)} objects, over the "
                        f"limit of {self.limits.max_targets}; not analyzed",
                line=line, sql=sql, statement=label,
            )]
        if len(literals) > self.limits.max_literals:
            return [Finding(
                verdict=Verdict.UNKNOWN,
                reason=Reason.RESOURCE_LIMIT,
                message=f"statement contains {len(literals)} string literals, over "
                        f"the limit of {self.limits.max_literals}; not analyzed",
                line=line, sql=sql, statement=label,
            )]

        rule = self.rule_for_label(label)
        if rule is not None:
            verdict, reason = rule.verdict, rule.reason
            message, severity = rule.message, rule.severity
        elif label not in READ_ONLY_LABELS:
            # Not destructive, but we have no positive rule for it. This is the
            # fail-closed path for statements the policy has not been taught.
            return [Finding(
                verdict=Verdict.REVIEW,
                reason=Reason.UNSUPPORTED_STATEMENT,
                message=f"statement type {label!r} has no policy rule; "
                        "needs human review",
                line=line, sql=sql, statement=label,
                targets=tuple(t.name for t in targets),
            )]
        else:
            verdict = Verdict.ALLOW
            reason = Reason.NO_MATCHING_RULE
            message = f"{label} is read-only"
            severity = Severity.INFO

        # A literal-prefix rule (e.g. ADD JAR, LOAD DATA INPATH) escalates the verdict.
        if rule is not None and rule.literal_prefixes:
            for lit in literals:
                if any(lit.startswith(p) for p in rule.literal_prefixes):
                    verdict = Verdict.DENY
                    reason = rule.reason
                    message = f"{rule.message} (matched {lit!r})"
                    break

        # Namespace allowlists. These are two independent checks, not alternatives:
        # a statement can be writable in a namespace it is not allowed to read from, and
        # "writable here, readable there" is the normal shape of a real policy. They were
        # previously joined with `elif`, so setting both lists silently disabled the
        # readable check for every destructive statement -- the check that fires first
        # ate the other one.
        if self.writable_namespaces and label in DESTRUCTIVE_LABELS:
            for t in targets:
                if not any(t.matches(pat) for pat in self.writable_namespaces):
                    findings.append(Finding(
                        verdict=Verdict.REVIEW,
                        reason=Reason.OUTSIDE_ALLOWLIST,
                        message=f"{t.name} is outside the writable namespaces "
                                f"{list(self.writable_namespaces)}; needs review",
                        line=line, sql=sql, statement=label,
                        rule=rule.id if rule else None,
                        targets=(t.name,),
                    ))
        if self.readable_namespaces:
            for t in targets:
                if not any(t.matches(pat) for pat in self.readable_namespaces):
                    findings.append(Finding(
                        verdict=Verdict.REVIEW,
                        reason=Reason.OUTSIDE_ALLOWLIST,
                        message=f"{t.name} is outside the readable namespaces "
                                f"{list(self.readable_namespaces)}; needs review",
                        line=line, sql=sql, statement=label,
                        rule=rule.id if rule else None,
                        targets=(t.name,),
                    ))

        findings.append(Finding(
            verdict=verdict,
            reason=reason,
            message=message,
            severity=severity,
            line=line, sql=sql, statement=label,
            rule=rule.id if rule else None,
            matched_label=rule.id if rule else None,
            targets=tuple(t.name for t in targets),
        ))
        return findings


# ---------------------------------------------------------------------------
# built-in policies
# ---------------------------------------------------------------------------

def policy_label_drift(policy: "Policy | None" = None) -> dict[str, list[str]]:
    """Report labels that appear in exactly one of the two label sets.

    `DESTRUCTIVE_LABELS` and the rules' `labels` are maintained separately, which is a
    duplication bug waiting to happen. The two failure directions are not equally
    serious, so they are reported separately:

    * `destructive_only` -- listed as destructive, but no rule acts on it. Always a bug:
      the listing is dead, and whoever wrote it thought the statement was handled.
    * `deny_rules_only` -- matched by a rule that DENIES, but absent from
      DESTRUCTIVE_LABELS. Also always a bug: the namespace allowlist only tightens
      labels in DESTRUCTIVE_LABELS, so this statement is exempt from the allowlist.
    * `review_rules_only` -- matched by an UNKNOWN/review rule only. Expected, and not a
      bug: a read-only query, a CALL, or a row mutation is deliberately not
      namespace-destructive, so the allowlist has nothing to say about it.

    Both real categories already happened while this was being written, which is why the
    split exists rather than a single "drift" list: a one-directional check would either
    have hidden the first bug or cried wolf about the third.
    """
    rules = (policy or default_policy()).rules
    all_labels: set[str] = set()
    deny_labels: set[str] = set()
    for rule in rules:
        all_labels |= set(rule.labels)
        if rule.verdict is Verdict.DENY:
            deny_labels |= set(rule.labels)
    destructive = set(DESTRUCTIVE_LABELS)
    return {
        "destructive_only": sorted(destructive - all_labels),
        "deny_rules_only": sorted(deny_labels - destructive),
        "review_rules_only": sorted((all_labels - deny_labels) - destructive),
    }


def default_policy() -> Policy:
    """Deny-by-default for destructive statements; UNKNOWN for unrecognised ones."""
    rules = [
        Rule(
            id="deny.drop",
            verdict=Verdict.DENY,
            reason=Reason.DENY_RULE,
            message="drops or truncates a table/view/namespace",
            severity=Severity.CRITICAL,
            labels=(
                "DropTable", "DropView", "DropNamespace", "DropIndex",
                "DropFunction", "DropTablePartitions", "TruncateTable",
                "DropTableColumns", "DropTableConstraint",
            ),
        ),
        Rule(
            id="deny.overwrite",
            verdict=Verdict.DENY,
            reason=Reason.DESTRUCTIVE_STATEMENT,
            message="overwrites existing data (INSERT OVERWRITE / REPLACE)",
            severity=Severity.CRITICAL,
            labels=(
                "InsertOverwriteTable", "InsertOverwriteHiveDir",
                "InsertOverwriteDir", "ReplaceTable",
            ),
        ),
        Rule(
            id="deny.destructive-ddl",
            verdict=Verdict.DENY,
            reason=Reason.DESTRUCTIVE_STATEMENT,
            message="mutates table or namespace structure",
            severity=Severity.HIGH,
            labels=(
                "RenameTable", "RenameTableColumn", "RenameTablePartition",
                "SetTableLocation", "SetNamespaceLocation", "AddTablePartition",
                "AlterTableAlterColumn", "AddTableColumns", "HiveChangeColumn",
                "HiveReplaceColumns", "SetTableProperties", "UnsetTableProperties",
                "SetTableSerDe", "AlterTableCollation", "AddTableConstraint",
                "AlterViewQuery", "AlterViewSchemaBinding", "RepairTable",
                "SetNamespaceProperties", "UnsetNamespaceProperties",
                "SetTableCollation", "AlterClusterBy",
            ),
        ),
        Rule(
            id="deny.load-local-data",
            verdict=Verdict.DENY,
            reason=Reason.DENY_RULE,
            message="LOAD DATA reads from a local path",
            severity=Severity.CRITICAL,
            labels=("LoadData",),
        ),
        Rule(
            id="deny.add-resource",
            verdict=Verdict.DENY,
            reason=Reason.DENY_RULE,
            message="adds a JAR/file resource or creates a function from one; "
                    "this is arbitrary code loading",
            severity=Severity.CRITICAL,
            labels=("ManageResource", "CreateFunction"),
        ),
        Rule(
            id="review.config",
            verdict=Verdict.REVIEW,
            reason=Reason.UNSUPPORTED_STATEMENT,
            message="changes Spark configuration or cache state; needs review",
            severity=Severity.MEDIUM,
            labels=(
                "SetConfiguration", "SetQuotedConfiguration",
                "ResetConfiguration", "ResetQuotedConfiguration", "SetPath",
                "CacheTable", "UncacheTable", "ClearCache",
            ),
        ),
        Rule(
            id="review.row-mutation",
            verdict=Verdict.REVIEW,
            reason=Reason.DESTRUCTIVE_STATEMENT,
            message="mutates rows in place (DELETE / UPDATE / MERGE / INSERT); "
                    "needs review to confirm the WHERE clause",
            severity=Severity.HIGH,
            # Resolved labels, not the `DmlStatement` wrapper: effective_label() descends
            # through the wrapper, so keying on it would make this rule dead code and let
            # DELETE/UPDATE/MERGE fall through to the generic UNKNOWN -- right verdict,
            # wrong reason, and no operator-visible explanation.
            labels=(
                "DeleteFromTable", "UpdateTable", "MergeIntoTable",
                "InsertIntoTable", "InsertIntoPartition",
            ),
        ),
        Rule(
            id="review.call",
            verdict=Verdict.REVIEW,
            reason=Reason.UNSUPPORTED_STATEMENT,
            message="CALL may invoke a procedure with side effects; needs review",
            severity=Severity.MEDIUM,
            labels=("Call",),
        ),
        Rule(
            id="review.structural-index",
            verdict=Verdict.REVIEW,
            reason=Reason.DESTRUCTIVE_STATEMENT,
            message="creates or drops an index or session variable; needs review",
            severity=Severity.MEDIUM,
            labels=("CreateIndex", "DropVariable"),
        ),
        Rule(
            id="allow.query",
            verdict=Verdict.ALLOW,
            reason=Reason.NO_MATCHING_RULE,
            message="read-only query",
            severity=Severity.INFO,
            labels=("StatementDefault",),
        ),
    ]
    return Policy(name="default", rules=rules)


def read_only_policy() -> Policy:
    """Strict policy: only queries are allowed, everything else needs review."""
    p = default_policy()
    for r in p.rules:
        if r.id in ("allow.query",):
            continue
        r.verdict = Verdict.UNKNOWN
        r.reason = Reason.UNSUPPORTED_STATEMENT
        if r.severity is Severity.INFO:
            r.severity = Severity.LOW
    p.name = "read-only"
    return p


# ---------------------------------------------------------------------------
# (de)serialization
# ---------------------------------------------------------------------------

def policy_from_dict(data: dict[str, Any]) -> Policy:
    rules = []
    for r in data.get("rules", []):
        rules.append(Rule(
            id=r["id"],
            verdict=Verdict(r.get("verdict", "deny")),
            reason=Reason(r.get("reason", "deny_rule")),
            message=r.get("message", r["id"]),
            severity=Severity(r.get("severity", "medium")),
            labels=tuple(r.get("labels", ())),
            target_patterns=tuple(r.get("target_patterns", ())),
            literal_prefixes=tuple(r.get("literal_prefixes", ())),
            description=r.get("description", ""),
        ))
    lim = data.get("limits", {})
    limits = Limits(**{k: v for k, v in lim.items()
                       if k in Limits.__dataclass_fields__})
    return Policy(
        name=data.get("name", "custom"),
        rules=rules,
        limits=limits,
        writable_namespaces=tuple(data.get("writable_namespaces", ())),
        readable_namespaces=tuple(data.get("readable_namespaces", ())),
    )


def load_policy(path: str | Path) -> Policy:
    data = json.loads(Path(path).read_text())
    return policy_from_dict(data)