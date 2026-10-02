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
    if not folder.resolved and not folder.unresolved:
        return report

    parser = get_parser(spec)

    for line, sql in sorted(folder.resolved.items()):
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

    for line, failure in sorted(folder.unresolved.items()):
        report.add(Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNRESOLVED_DYNAMIC_SQL,
            message=f"{failure}; SQL not analyzed, needs human review",
            severity=Severity.HIGH,
            line=line,
        ))

    return report


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
        return Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNSUPPORTED_STATEMENT,
            message=f"no policy decision produced for {label}",
            line=line, sql=sql, statement=label,
        )
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