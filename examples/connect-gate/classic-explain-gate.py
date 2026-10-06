"""EXPLAIN gate for a CLASSIC SparkSession (spark:// master or local[...]).

A classic session has no client-side plan: the plan exists only inside the JVM,
and Python never sees it. The plan-level option there is tier 3 from T8 -- ask the
engine to plan the statement (`EXPLAIN <stmt>`), parse the plan text, decide, and
only then run the real statement. This costs one extra RPC per statement and a
JVM, both of which a classic session already has.

This module is intentionally NOT imported by the startup hook by default: the
plan-text matcher is pinned to one engine's rendering (`DropTable V2SessionCatalog
..., Execute InsertIntoHadoopFsRelationCommand ..., Overwrite`), and enabling it
is a per-deployment decision recorded in T8. Verify the shapes against YOUR engine
before trusting the table below; `tests/differential/probe_explain.py` shows how.

Properties verified against live engines (docs/threads.md T8):
- `EXPLAIN <stmt>` executes nothing (post-state checked on 3.5.1/4.1.3/4.2.0).
- Plan text carries target + mode for the destructive commands.
- 4.x returns an EMPTY message on planning failure; never parse error text.
- identifier casing is NOT canonicalized (uppercase stays uppercase in DropTable);
  normalize case before allowlist matching, the same lesson as F6.
"""

from __future__ import annotations

import re

#: Plan-text shapes -> the effect classification they imply. Pinned per engine
#: release; treat a mismatch as UNKNOWN, never as a guessed effect.
_EXPLAIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # DropTable V2SessionCatalog(...), default.scratch_t, false, false, <lambda>
    #   flags: [if_exists, is_view]
    (
        "DropTable",
        re.compile(r"^\s*DropTable\s+.*?,\s*([^\s,]+(?:\.[^\s,]+)*)\s*,", re.IGNORECASE),
    ),
    (
        "TruncateTable",
        re.compile(r"TruncateTable(?:Command)?\s+.*?([^\s`]+)`?\.`?([^\s`]+)", re.IGNORECASE),
    ),
    (
        "InsertInto",
        re.compile(
            r"InsertIntoHadoopFsRelationCommand\s+\S+,\s*\w+,\s*\w+,\s*\[[^\]]*\],\s*(\w+),"
            r"\s*`?([^`]+)`?",
            re.IGNORECASE,
        ),
    ),
    (
        "SetCommand",
        re.compile(r"^\s*(?:Execute\s+)?SetCommand\b", re.IGNORECASE),
    ),
)


class ExplainGate:
    """Screen-then-run for a classic session's `spark.sql`."""

    def __init__(self, spark, policy=None):
        import sparkscreen

        self._spark = spark
        self._policy = policy or sparkscreen.default_policy()

    def sql(self, statement: str):
        """Plan the statement, decide, then run it -- or raise."""
        verdict, effects, target = self._screen_text(statement)
        if verdict == "DENY":
            raise PermissionError(
                f"sparkscreen refused this statement ({effects}): {target or statement!r}"
            )
        if verdict == "UNKNOWN":
            raise PermissionError(
                "sparkscreen could not classify this statement from the plan; "
                "it was not run"
            )
        return self._spark.sql(statement)

    def _screen_text(self, statement: str) -> tuple[str, str, str | None]:
        """(verdict, effects-label, target) from the plan text of `statement`."""
        plan = self._explain(statement)
        if plan is None:
            return "UNKNOWN", "unplannable", None
        for label, pattern in _EXPLAIN_PATTERNS:
            m = pattern.match(plan) or pattern.search(plan)
            if m is None:
                continue
            groups = [g for g in m.groups() if g is not None]
            target = groups[-1] if groups else None
            # Casing is not canonicalized by the engine (T8); normalize here.
            target = target.lower().strip("`") if target else None
            destructive = label in ("DropTable", "TruncateTable") or (
                label == "InsertInto" and "OVERWRITE" == m.group(1).upper()
            )
            return ("DENY" if destructive else "REVIEW"), label, target
        return "UNKNOWN", "unrecognized plan shape", None

    def _explain(self, statement: str) -> str | None:
        """The plan text for `statement`, without executing it. None on failure."""
        try:
            row = self._spark.sql("EXPLAIN " + statement).collect()
        except Exception:
            # Planning failed (bad SQL, unsupported command). The statement would
            # have failed for real too, but the honest verdict here is UNKNOWN:
            # we did not classify it, we just failed to plan it.
            return None
        text = row[0][0] if row else ""
        # 4.x: "Error occurred during query planning: " with an empty message (T8).
        # Treat any embedded error as unplannable rather than trying to parse it.
        if "Error occurred during query planning" in text or "AnalysisException" in text:
            return None
        return text
