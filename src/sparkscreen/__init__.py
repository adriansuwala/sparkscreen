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
    Finding,
    Reason,
    Report,
    Severity,
    UNKNOWN_REASONS,
    Verdict,
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

__version__ = "0.1.0"

__all__ = [
    "screen",
    "Finding",
    "Report",
    "Verdict",
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
]