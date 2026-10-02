"""Verify every factual claim in docs/user-docs/usage.md.

User documentation that lies is worse than none, and several claims in that file are the
kind that rot silently -- a renamed enum member, a changed default, a default that was
tweaked and the doc not updated.

Run: PYTHONPATH=src .venv/bin/python _verify_docs.py
Not part of the test suite: this is a documentation audit, run by hand when usage.md
changes. Promote it to a real test if the docs start drifting.
"""
from __future__ import annotations

import sys

from sparkscreen import Verdict, read_only_policy, screen
from sparkscreen.model import Reason
from sparkscreen.policy import Limits

failures: list[str] = []


def check(label: str, got, want) -> None:
    good = got == want
    if not good:
        failures.append(f"{label}: got={got!r} want={want!r}")
    print(f"  {'OK  ' if good else 'FAIL'} {label:44} {got!r}")


def main() -> int:
    # --- quick start / three verdicts ---
    check("clean query -> ALLOW",
          screen("spark.sql('select 1')").verdict, Verdict.ALLOW)
    check("DROP -> DENY",
          screen("spark.sql('DROP TABLE prod.t')").verdict, Verdict.DENY)
    check("dynamic SQL -> UNKNOWN",
          screen("spark.sql(q)").verdict, Verdict.UNKNOWN)

    # --- library API surface ---
    report = screen("spark.sql('DROP TABLE prod.t')")
    check("report.ok is False on DENY", report.ok, False)
    check("report.findings is a list", isinstance(report.findings, list), True)
    finding = report.findings[0]
    check("finding.statement", finding.statement, "DropTable")
    check("finding.targets", finding.targets, ("prod.t",))
    check("finding.severity", finding.severity.value, "critical")
    check("summary() shape", report.summary(),
          "DENY: 1 deny, 0 unknown, 0 allow")

    # The strictness claim in usage.md: one unanalysable sink poisons report.ok.
    mixed = screen("spark.sql('select 1')\nspark.sql(q)")
    check("unresolved anywhere -> ok False", mixed.ok, False)
    check("unresolved anywhere -> UNKNOWN", mixed.verdict, Verdict.UNKNOWN)

    # --- read_only_policy ---
    check("read_only: query allowed",
          screen("spark.sql('select 1')", read_only_policy()).verdict, Verdict.ALLOW)
    check("read_only: drop needs review",
          screen("spark.sql('drop table t')", read_only_policy()).verdict,
          Verdict.UNKNOWN)

    # --- limits table ---
    check("Limits.max_code_chars", Limits().max_code_chars, 20_000)
    check("Limits.max_sql_chars", Limits().max_sql_chars, 10_000)

    # --- every Reason named in the troubleshooting table must exist ---
    for name in ("unresolved_dynamic_sql", "unparseable_sql", "unsupported_statement",
                 "resource_limit", "outside_allowlist"):
        check(f"Reason.{name}", getattr(Reason, name.upper()).value, name)

    # --- analysis_failures is documented as separating "could not look" ---
    report = screen("spark.sql('select 1')\nspark.sql('SELCT 1')")
    check("analysis_failures excludes clean queries",
          len(report.analysis_failures), 1)

    # --- keyword forms and folding are documented as supported ---
    check("keyword sink detected",
          screen("spark.sql(query='DROP TABLE prod.t')").verdict, Verdict.DENY)
    check("f-string folding detected",
          screen("t='prod.t'\nspark.sql(f'DROP TABLE {t}')").verdict, Verdict.DENY)

    # --- scope shadowing fails closed, as usage.md claims ---
    check("shadowed name -> UNKNOWN",
          screen("t='prod.t'\nfor t in xs:\n    spark.sql(f'DROP TABLE {t}')").verdict,
          Verdict.UNKNOWN)

    print()
    if failures:
        print(f"{len(failures)} DOC CLAIM(S) STALE:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all documented claims hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())