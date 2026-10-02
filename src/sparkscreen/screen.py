"""Screen Python source: fold, parse, evaluate.

`screen(source, policy)` is the entry point. The pipeline is deliberately ordered so
each stage's failure modes land in the right verdict:

1. length check           -> DENY (explicit reviewer intent, not uncertainty)
2. Python syntax check    -> UNKNOWN
3. find SQL sinks         -> (none) means ALLOW with no findings
4. fold SQL arguments     -> UNKNOWN per unresolved sink
5. length check on SQL    -> DENY
6. strict ANTLR parse     -> UNKNOWN per unparseable statement
7. policy evaluation      -> ALLOW / DENY / UNKNOWN

Steps 4 and 6 are where the real work is, and both fail closed.
"""

from __future__ import annotations

import ast

from .analysis.calls import DataFrameWrite, find_dataframe_writes
from .analysis.effects import effects_for_label
from .analysis.folding import fold_sinks
from .analysis.treewalk import (
    extract_namespaces,
    extract_string_literals,
    executed_immediate_sql,
    statement_label,
    top_level_statement_contexts,
)
from .grammar.parser import SqlParser, SqlSyntaxError, get_parser
from .grammar.spec import GrammarSpec, get_spec
from .model import Finding, Reason, Report, Severity, Verdict
from .policy import Policy, default_policy


def screen(
    source: str,
    policy: Policy | None = None,
    *,
    spec: GrammarSpec | str | None = None,
) -> Report:
    """Screen Python source for dangerous PySpark operations."""
    policy = policy or default_policy()
    try:
        spec = spec if isinstance(spec, GrammarSpec) else get_spec(spec)
    except KeyError as e:
        rep = Report(policy=policy.name)
        rep.add(Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNSUPPORTED_SPARK_VERSION,
            message=str(e),
            severity=Severity.HIGH,
        ))
        return rep

    report = Report(policy=policy.name, grammar=spec.key)

    # 1. review-budget cap. This is a deliberate DENY, not an UNKNOWN: the caller
    #    asked for a snippet short enough to eyeball and this isn't one.
    n = len(source)
    if n > policy.limits.max_code_chars:
        report.add(Finding(
            verdict=Verdict.DENY,
            reason=Reason.CODE_LENGTH_EXCEEDED,
            message=f"code is {n} characters, over the limit of "
                    f"{policy.limits.max_code_chars}; split it before screening",
            severity=Severity.MEDIUM,
        ))
        return report

    # 2. Python syntax
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        report.add(Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.ANALYSIS_ERROR,
            message=f"python syntax error at line {e.lineno}: {e.msg}",
            severity=Severity.MEDIUM,
            line=e.lineno,
        ))
        return report

    report.lines = len(source.splitlines())

    # 3-4. find and fold SQL sinks
    folder = fold_sinks(tree)

    # DataFrame writes are found from the same tree and the same folder, but they are a
    # separate pass: they never become SQL text, so there is nothing to parse and no
    # statement label to classify. Their effect comes from the method and the save mode
    # instead. Run before the SQL early-return below, because a file can contain only
    # DataFrame writes and must not be reported as having nothing in it.
    for write in find_dataframe_writes(tree, folder):
        report.add(_eval_write(policy, write))

    if not folder.resolved and not folder.unresolved:
        return report

    parser = get_parser(spec)

    # `resolved`/`unresolved` are keyed by a per-sink SinkKey, not by line: two sinks
    # can share a line, and keying by line silently dropped one of them -- which is how
    # `spark.sql("DROP TABLE prod.users"); spark.sql("select 1")` screened as ALLOW.
    for key, sql in sorted(folder.resolved.items()):
        line = key.line
        if len(sql) > policy.limits.max_sql_chars:
            report.add(Finding(
                verdict=Verdict.DENY,
                reason=Reason.CODE_LENGTH_EXCEEDED,
                message=f"SQL statement is {len(sql)} characters, over the limit "
                        f"of {policy.limits.max_sql_chars}",
                severity=Severity.MEDIUM,
                line=line, sql=sql[:200] + "...",
            ))
            continue

        # 6. strict parse
        try:
            parsed = parser.parse(sql)
        except SqlSyntaxError as e:
            report.add(Finding(
                verdict=Verdict.UNKNOWN,
                reason=Reason.UNPARSEABLE_SQL,
                message=f"SQL did not parse, so it was not analyzed: {e}",
                severity=Severity.HIGH,
                line=line, sql=sql,
                sql_line=e.line, sql_column=e.column,
            ))
            continue

        # 7. evaluate each statement in the script
        statements = parsed.statements
        if len(statements) > policy.limits.max_statements:
            report.add(Finding(
                verdict=Verdict.UNKNOWN,
                reason=Reason.RESOURCE_LIMIT,
                message=f"script contains {len(statements)} statements, over the "
                        f"limit of {policy.limits.max_statements}; not analyzed",
                severity=Severity.MEDIUM,
                line=line, sql=sql, statement=parsed.label,
            ))
            continue

        for stmt in statements:
            report.add(_eval_one(policy, stmt.label, stmt.tree, sql, line, spec.key))

        # EXECUTE IMMEDIATE hides real statements from the top level.
        _check_execute_immediate(report, policy, parser, parsed.tree, sql, line)

    for key, failure in sorted(folder.unresolved.items()):
        report.add(Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNRESOLVED_DYNAMIC_SQL,
            message=f"{failure}; SQL not analyzed, needs human review",
            severity=Severity.HIGH,
            line=key.line,
        ))

    return report


def _ref(name: str):
    """Build a NamespaceRef from a dotted name.

    A DataFrame destination is a plain string, not a parse tree, so this reconstructs
    the shape `treewalk.NamespaceRef` produces. Splitting on "." is right for the table
    names the DataFrame API takes (`prod.users`); a path like `s3://bucket/x` is not a
    namespace and is handled before it reaches here.
    """
    from .analysis.treewalk import NamespaceRef

    return NamespaceRef(parts=tuple(p for p in name.split(".") if p))


def _eval_write(policy: Policy, write: DataFrameWrite) -> Finding:
    """Turn one DataFrame write into a Finding.

    The shape deliberately mirrors the SQL path -- namespace allowlists first, then a
    verdict, with the effect attached -- so a report reads the same way whichever kind of
    write produced it. But the reasoning is not identical, and the differences are the
    point:

    * The effect is always populated. A DataFrame write does something knowable even
      when its target is not, which is the opposite of `spark.sql(q)` with an
      unresolvable `q`. An unknown *target* is not an analysis failure here.

    * An unreadable save *mode* is a different failure. `df.write.mode(x)` where `x` is a
      runtime value could be an overwrite, so we cannot say the write is safe, and it
      reports UNKNOWN. This is the one case where the DataFrame path degrades, and it
      degrades to UNKNOWN rather than to ALLOW.

    * `denies_regardless_of_namespace` is the same predicate the effect axis applies to
      SQL labels. Reusing it is what makes "overwrite is not waivable by namespace" a
      property of the policy engine rather than of the SQL parser.
    """
    from .analysis.effects import denies_regardless_of_namespace

    where = f"{write.chain}.{write.operation}"
    targets = (write.target,) if write.target_known and write.target else ()

    finding: Finding | None = None
    # An unreadable destination defeats the allowlists, but only the allowlists -- the
    # effect is already known, so this is a review trigger rather than a blind spot.
    if not write.target_known:
        finding = Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNRESOLVED_DYNAMIC_SQL,
            message=f"{where} writes to a destination we cannot resolve, so its "
                    f"namespace was not checked; effect is still known",
            severity=Severity.MEDIUM,
            line=write.key.line,
            statement=f"DataFrameWriter.{write.operation}",
        )
    elif denies_regardless_of_namespace(write.effects):
        finding = Finding(
            verdict=Verdict.DENY,
            reason=Reason.DESTRUCTIVE_STATEMENT,
            message=f"{where} replaces existing data"
                    if write.overwrites
                    else f"{where} is a data write",
            severity=Severity.CRITICAL if write.overwrites else Severity.HIGH,
            line=write.key.line,
            statement=f"DataFrameWriter.{write.operation}",
            targets=targets,
        )
    else:
        # A plain append. Namespace still applies: writing prod is not the same as
        # writing staging. The two checks mirror `Policy.evaluate_statement` -- an
        # independent writable check and an independent readable check, not an
        # either/or -- because the bug that made the readable one dead was exactly
        # joining them (see D-notes in docs/findings.md, F8).
        notes: list[str] = []
        refs = [_ref(t) for t in targets]
        # No writable list configured is not permission to write. With no allowlist we
        # have no evidence the destination is in bounds, and the SQL path behaves the
        # same way: a Policy with no rules produces UNSUPPORTED_STATEMENT -> UNKNOWN,
        # never ALLOW. An append that lands here has been screened and cleared, which
        # is exactly the "confident wrong answer" this tool exists to avoid -- a policy
        # that forgets to set writable_namespaces would otherwise auto-approve every
        # DataFrame append in the codebase.
        if not policy.writable_namespaces:
            notes.append(
                "the policy sets no writable_namespaces, so the destination could "
                "not be confirmed as in bounds"
            )
        else:
            for r in refs:
                if not any(r.matches(pat) for pat in policy.writable_namespaces):
                    notes.append(
                        f"{r.name} is outside the writable namespaces "
                        f"{list(policy.writable_namespaces)}"
                    )
        if policy.readable_namespaces:
            for r in refs:
                if not any(r.matches(pat) for pat in policy.readable_namespaces):
                    notes.append(
                        f"{r.name} is outside the readable namespaces "
                        f"{list(policy.readable_namespaces)}"
                    )
        if notes:
            finding = Finding(
                verdict=Verdict.UNKNOWN,
                reason=Reason.OUTSIDE_ALLOWLIST,
                message=f"{where} writes data outside the permitted namespaces: "
                        + "; ".join(notes),
                severity=Severity.MEDIUM,
                line=write.key.line,
                statement=f"DataFrameWriter.{write.operation}",
                targets=targets,
            )
        else:
            finding = Finding(
                verdict=Verdict.ALLOW,
                reason=Reason.WITHIN_ALLOWLIST,
                message=f"{where} appends within the permitted namespaces",
                severity=Severity.LOW,
                line=write.key.line,
                statement=f"DataFrameWriter.{write.operation}",
                targets=targets,
            )

    # An unreadable mode is the one thing that can turn a benign append into an
    # unexamined overwrite, so it is checked last and overrides a permissive verdict.
    if not write.mode_known:
        finding = Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNRESOLVED_DYNAMIC_SQL,
            message=f"{where} sets a save mode we cannot read; it may be an overwrite, "
                    f"so it needs review",
            severity=Severity.HIGH,
            line=write.key.line,
            statement=f"DataFrameWriter.{write.operation}",
            targets=targets,
        )

    finding.effect = write.effects
    return finding


def _eval_one(
    policy: Policy,
    label: str,
    tree,
    sql: str,
    line: int | None,
    grammar_key: str | None = None,
) -> Finding:
    targets = extract_namespaces(tree, grammar_key=grammar_key)
    literals = extract_string_literals(tree)
    findings = policy.evaluate_statement(label, targets, literals, sql=sql, line=line)
    # callers want one finding per statement; policy returns allowlist notes plus the
    # main verdict, so keep them all but ensure at least one exists
    if not findings:
        findings = [Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNSUPPORTED_STATEMENT,
            message=f"no policy decision produced for {label}",
            line=line, sql=sql, statement=label,
        )]
    # Attach the effect axis. This is additive and cannot change a verdict: the
    # classification is a pure function of the statement label, and a statement we
    # parsed successfully either has an entry or is a bug in the table.
    #
    # `effects_for_label` raises on an unmapped label rather than returning an empty
    # set, and that error is deliberately NOT caught here. Swallowing it would leave
    # the finding with a default `frozenset()` -- indistinguishable from "this
    # statement does nothing", which is a fail-open reading of a DROP. `tests/
    # test_effects.py` asserts the table is total against the generated parsers, so
    # this can only fire when a grammar has gained a statement the table has not
    # caught up with, and crashing is the correct outcome: a screener that cannot
    # classify what it just parsed must not report a result.
    effects = effects_for_label(label)
    for f in findings:
        f.effect = effects
    return findings[0] if len(findings) == 1 else _combine(findings)


def _combine(findings: list[Finding]) -> Finding:
    """Fold allowlist notes into the primary finding without losing information."""
    from .model import UNKNOWN_REASONS

    if any(f.is_unknown for f in findings):
        primary = next(f for f in findings if f.is_unknown)
    else:
        primary = findings[-1]
    extra = [f for f in findings if f is not primary]
    if extra:
        details = "; ".join(f.message for f in extra)
        primary.message = f"{primary.message} ({details})"
    return primary


def _check_execute_immediate(
    report: Report,
    policy: Policy,
    parser: SqlParser,
    tree,
    sql: str,
    line: int | None,
) -> None:
    """Recursively analyze SQL hidden inside EXECUTE IMMEDIATE '...'."""
    for hidden in executed_immediate_sql(tree):
        try:
            inner = parser.parse(hidden)
        except SqlSyntaxError as e:
            report.add(Finding(
                verdict=Verdict.UNKNOWN,
                reason=Reason.UNPARSEABLE_SQL,
                message=f"SQL inside EXECUTE IMMEDIATE did not parse: {e}",
                severity=Severity.HIGH,
                line=line, sql=hidden,
            ))
            continue
        for stmt in inner.statements:
            report.add(_eval_one(policy, stmt.label, stmt.tree, hidden, line,
                                 parser.spec.key))