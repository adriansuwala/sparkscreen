"""sparkscreen -- static safety screening for agent-written PySpark.

The premise: whether a piece of agent-generated PySpark is dangerous should be
decided by real parsers, not by pattern-matching source text. Python code is analysed
with `ast`; the SQL inside `spark.sql(...)` is parsed with Apache Spark's own ANTLR
grammar (ported to a Python target), so a statement is identified by its parse-tree
structure rather than by how it was spelled.

The API is one function:

    from sparkscreen import screen
    report = screen(source_code)
    if report.ok:
        ...

`report.ok` is true only when everything was analyzed and nothing dangerous was
found. Unanalyzable input produces UNKNOWN, never a false "ok".
"""

from .model import (
    Effect,
    Finding,
    Reason,
    Report,
    Severity,
    UNKNOWN_REASONS,
    Verdict,
    effect_names,
)
from .policy import (
    Limits,
    Policy,
    Rule,
    default_policy,
    load_policy,
    policy_from_dict,
    read_only_policy,
)
from .screen import screen
from .grammar.spec import SPECS, get_spec, spec_for_spark_version

try:  # installed: the packaging metadata is authoritative
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("sparkscreen")
except Exception:  # pragma: no cover - source checkout without an install
    __version__ = "0.9.0"

#: Why this is not 1.0, stated once so it is not re-litigated.
#:
#: The screener is functionally complete and fail-closed, but 1.0 means "the interface
#: will not break you", and two things can still break it:
#:
#:   * `Verdict` gained a member three commits ago. A downstream exhaustive `match` on
#:     the verdict would have raised. That is a 0.x-style break, not a 1.0-style one.
#:   * It has never been installed by anyone but its own author. There is one pair of
#:     hands on it, so "works on my machine" and "works" are currently the same claim.
#:
#: Promoted to 1.0 once: the policy JSON shape has not changed across a release, and the
#: differential suite has run against two real Spark versions on release day.
VERSION_NOTES = (
    "Pre-1.0. Verdict is not yet API-stable; exit codes (0/1/2) are stable. "
    "Scope is Spark SQL and DataFrame writes; general Python screening is out of scope."
)

__all__ = [
    "screen",
    "Finding",
    "Report",
    "Verdict",
    "Effect",
    "effect_names",
    "Reason",
    "Severity",
    "UNKNOWN_REASONS",
    "Policy",
    "Rule",
    "Limits",
    "default_policy",
    "read_only_policy",
    "load_policy",
    "policy_from_dict",
    "SPECS",
    "get_spec",
    "spec_for_spark_version",
    "__version__",
    "VERSION_NOTES",
]