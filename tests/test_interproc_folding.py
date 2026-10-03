"""Bounded interprocedural folding: function parameters and simple loop iterations.

`test_folding.py` pins the intra-procedural folder. This module pins the two
reachability steps layered on top of it, and — more importantly — pins the *edges*,
because the whole risk of interprocedural analysis is in resolving something it
cannot prove. The project's rule (AGENTS.md: "a stale binding is worse than no
binding") means every case below must land in exactly one of:

  * RESOLVED, with SQL that is byte-for-byte what the engine is sent;
  * UNRESOLVED, identical to what the folder reported before this feature existed.

Nothing lands in "resolved but wrong", and nothing is silently dropped.

**What IS resolved** (each shape asserted exactly, in `RESOLVES`):

  * a module-level `def` whose every call site in the file passes a literal, and
    every call site agrees on every parameter's value and type;
  * literal defaults for parameters the call sites omit;
  * a `for` over a literal list/tuple of foldable elements, one entry per distinct
    statement the loop really issues.

**What is NOT resolved** (each asserted to stay UNKNOWN, in `STAYS_UNKNOWN`):

  * recursion and mutual recursion; decorators; `async def`; methods; nested defs;
    a function never called in this file, passed around as a value, or rebound;
  * call sites that spread `*args`/`**kwargs`, or pass a variable rather than a
    literal, or pass a non-literal default (`def f(t=TBL)`);
  * call sites that *disagree* — `drop("a")` and `drop("b")` is genuine ambiguity,
    and picking one would be a specific, wrong finding;
  * second-level calls (`f(g("x"))`), which this pass deliberately does not chase;
  * `for` loops over anything but a literal list/tuple, and `for` bodies containing
    a nested loop or loop control (`break`/`continue`/`return`/`yield`);
  * loops over the caps, which stay UNKNOWN rather than being truncated.

**The oracle.** `test_resolution_equals_what_a_real_interpreter_sends` executes each
snippet against a recording shim and asserts the folder's `resolved` set is exactly
the set of strings Python actually passed to `spark.sql`. That is the one test here
that can catch an unsound resolution rather than merely a missing one, so it is the
reason to believe the rest. The shim exists only in this test file: the library
never executes the code it screens (docs/threads.md T1).

Grammar-independent by construction — these assertions are about the folder and the
verdict, not about SQL shape — except the `screen()` cases, which are parametrised
over `spec_key` so both pinned grammars are exercised.
"""
from __future__ import annotations

import ast
import time

import pytest

from sparkscreen import Reason, Verdict, screen
from sparkscreen.analysis.folding import (
    MAX_CONST_STRING,
    MAX_LOOP_UNROLL,
    MAX_UNROLL_TOTAL,
    FoldFailure,
    SinkKey,
    fold_sinks,
)

# --------------------------------------------------------------------------- helpers


def fold(source: str):
    return fold_sinks(ast.parse(source))


def resolved_of(source: str) -> list[str]:
    return sorted(fold(source).resolved.values())


def unresolved_lines(source: str) -> set[int]:
    folder = fold(source)
    return {key.line for key in folder.unresolved}


def sink_calls(source: str) -> list[ast.Call]:
    """Every call the folder treats as a sink, counted the way it counts them."""
    out = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr in ("sql", "sqlQuery")) or (
                isinstance(func, ast.Name) and func.id in ("sql", "sqlQuery")
            ):
                out.append(node)
    return out


# --------------------------------------------------------------------------- resolved

#: (id, source, the exact SQL `resolved` must contain)
RESOLVES = [
    dict(
        id="parameter-from-literal-call",
        src='def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.users")\n',
        sql=["DROP TABLE prod.users"],
    ),
    dict(
        id="parameter-from-keyword-call",
        src='def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop(t="prod.users")\n',
        sql=["DROP TABLE prod.users"],
    ),
    dict(
        id="two-callers-that-agree",
        src='def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.t")\ndrop("prod.t")\n',
        sql=["DROP TABLE prod.t"],
    ),
    dict(
        id="literal-default-not-overridden",
        src='def drop(t="prod.t"):\n    spark.sql(f"DROP TABLE {t}")\ndrop()\n',
        sql=["DROP TABLE prod.t"],
    ),
    dict(
        id="literal-default-overridden",
        src='def drop(t="prod.t"):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.other")\n',
        sql=["DROP TABLE prod.other"],
    ),
    dict(
        id="several-parameters",
        src='def d(schema, t):\n    spark.sql(f"DROP TABLE {schema}.{t}")\n'
            'd("prod", "users")\n',
        sql=["DROP TABLE prod.users"],
    ),
    dict(
        id="spare-parameter-the-sink-ignores",
        src='def d(spark, t):\n    spark.sql(f"DROP TABLE {t}")\nd(1, "prod.t")\n',
        sql=["DROP TABLE prod.t"],
    ),
    dict(
        id="loop-over-literal-list",
        src='for t in ["prod.users", "prod.orders"]:\n    spark.sql(f"DROP TABLE {t}")\n',
        sql=["DROP TABLE prod.orders", "DROP TABLE prod.users"],
    ),
    dict(
        id="loop-over-literal-tuple",
        src='for t in ("prod.a", "prod.b"):\n    spark.sql(f"DROP TABLE {t}")\n',
        sql=["DROP TABLE prod.a", "DROP TABLE prod.b"],
    ),
    dict(
        id="loop-over-module-constants",
        src='A = "prod.a"\nB = "prod.b"\nfor t in [A, B]:\n    spark.sql(f"DROP {t}")\n',
        sql=["DROP prod.a", "DROP prod.b"],
    ),
    dict(
        id="loop-iterations-that-agree-collapse-to-one",
        src='for t in ["prod.a", "prod.a"]:\n    spark.sql(f"DROP {t}")\n',
        sql=["DROP prod.a"],
    ),
    dict(
        id="loop-inside-a-bound-function",
        src='def drop(t):\n    for x in ["a", "b"]:\n'
            '        spark.sql(f"DROP {t}.{x}")\ndrop("prod.t")\n',
        sql=["DROP prod.t.a", "DROP prod.t.b"],
    ),
    dict(
        id="function-called-from-a-literal-loop",
        src='def drop(t):\n    spark.sql(f"DROP {t}")\nfor _ in ["x", "y"]:\n'
            '    drop("prod.t")\n',
        sql=["DROP prod.t"],
    ),
    dict(
        id="loop-body-with-an-if",
        src='for t in ["prod.a", "prod.b"]:\n'
            '    if t:\n        spark.sql(f"DROP {t}")\n',
        sql=["DROP prod.a", "DROP prod.b"],
    ),
    dict(
        id="def-defined-after-its-call-site",
        src='drop("prod.t")\ndef drop(t):\n    spark.sql(f"DROP {t}")\n',
        sql=["DROP prod.t"],
    ),
]


@pytest.mark.parametrize("case", RESOLVES, ids=[c["id"] for c in RESOLVES])
def test_resolves_exactly(case):
    """The recovered set is exactly the expected SQL -- no more, no less."""
    folder = fold(case["src"])
    assert sorted(folder.resolved.values()) == sorted(case["sql"])
    assert folder.unresolved == {}, (
        "a resolved sink must not also be reported unresolved: "
        f"{folder.unresolved}"
    )


@pytest.mark.parametrize("case", RESOLVES, ids=[c["id"] for c in RESOLVES])
def test_resolution_is_attributed_to_the_sink_line(case):
    """Findings point at the sink call, which is where an operator looks.

    For a sink inside a function or a loop that is the line of the call inside the
    body, not of the call site that made the value provable.
    """
    folder = fold(case["src"])
    real = {call.lineno for call in sink_calls(case["src"])}
    assert {key.line for key in folder.resolved} <= real
    assert {key.line for key in folder.unresolved} <= real


# --------------------------------------------------------------------------- unresolved

#: Every shape this feature must refuse. Each one is a place where the analysis could
#: report a specific statement for code that will not run that way.
STAYS_UNKNOWN = [
    dict(id="never-called", src='def drop(t):\n    spark.sql(f"DROP {t}")\n'),
    dict(id="callers-disagree", src='def drop(t):\n    spark.sql(f"DROP {t}")\n'
                                    'drop("prod.a")\ndrop("prod.b")\n'),
    dict(id="callers-disagree-on-type",
         src='def f(t):\n    spark.sql(f"DROP {t}")\nf(1)\nf(True)\n'),
    dict(id="one-caller-is-dynamic",
         src='def drop(t):\n    spark.sql(f"DROP {t}")\n'
             'drop("prod.t")\ndrop(input())\n'),
    dict(id="argument-is-a-variable",
         src='TBL = "prod.t"\ndef drop(t):\n    spark.sql(f"DROP {t}")\ndrop(TBL)\n'),
    dict(id="default-is-a-name",
         src='TBL = "prod.t"\ndef drop(t=TBL):\n    spark.sql(f"DROP {t}")\ndrop()\n'),
    dict(id="argument-starred", src='def drop(t):\n    spark.sql(f"DROP {t}")\n'
                                    'drop(*["prod.t"])\n'),
    dict(id="argument-kwargs-splat",
         src='def drop(t):\n    spark.sql(f"DROP {t}")\ndrop(**{"t": "prod.t"})\n'),
    dict(id="function-used-as-a-value",
         src='def drop(t):\n    spark.sql(f"DROP {t}")\nhandlers = [drop]\ndrop("prod.t")\n'),
    dict(id="function-rebound-after-def",
         src='def drop(t):\n    spark.sql(f"DROP {t}")\ndrop("prod.t")\ndrop = print\n'),
    dict(id="function-redefined",
         src='def drop(t):\n    spark.sql(f"DROP {t}")\ndrop("prod.t")\n'
             'def drop(t):\n    spark.sql(f"SELECT {t}")\n'),
    dict(id="function-imported-under-its-own-name",
         src='import os as drop\ndef drop(t):\n    spark.sql(f"DROP {t}")\n'
             'drop("prod.t")\n'),
    dict(id="direct-recursion",
         src='def drop(t):\n    spark.sql(f"DROP {t}")\n    drop("prod.t")\n'),
    dict(id="mutual-recursion",
         src='def a(t):\n    spark.sql(f"DROP {t}")\n    b("prod.t")\n'
             'def b(t):\n    a(t)\na("prod.t")\n'),
    dict(id="decorated",
         src='@deco\ndef drop(t):\n    spark.sql(f"DROP {t}")\ndrop("prod.t")\n'),
    dict(id="async-def", src='async def drop(t):\n    spark.sql(f"DROP {t}")\n'
                             'drop("prod.t")\n'),
    dict(id="method", src='class C:\n    def drop(self, t):\n'
                          '        spark.sql(f"DROP {t}")\nC().drop("prod.t")\n'),
    dict(id="def-inside-a-function",
         src='def outer():\n    def drop(t):\n        spark.sql(f"DROP {t}")\n'
             '    drop("prod.t")\n'),
    dict(id="second-level-call",
         src='def g(x):\n    return x\ndef drop(t):\n    spark.sql(f"DROP {t}")\n'
             'drop(g("prod.t"))\n'),
    dict(id="required-parameter-omitted",
         src='def drop(t):\n    spark.sql(f"DROP {t}")\ndrop()\n'),
    dict(id="too-many-arguments",
         src='def drop(t):\n    spark.sql(f"DROP {t}")\ndrop("a", "b")\n'),
    dict(id="loop-over-a-runtime-call",
         src='for t in list_tables():\n    spark.sql(f"DROP TABLE {t}")\n'),
    dict(id="loop-over-a-runtime-name",
         src='tables = config()\nfor t in tables:\n    spark.sql(f"DROP {t}")\n'),
    dict(id="loop-over-enumerate",
         src='for i, t in enumerate(tables):\n    spark.sql(f"DROP {t}")\n'),
    dict(id="loop-over-a-set-literal",
         src='for t in {"prod.a", "prod.b"}:\n    spark.sql(f"DROP {t}")\n'),
    dict(id="loop-with-one-unknown-element",
         src='for t in ["prod.a", input()]:\n    spark.sql(f"DROP {t}")\n'),
    dict(id="loop-with-break",
         src='for t in ["a", "b"]:\n    if t == "a":\n        break\n'
             '    spark.sql(f"DROP {t}")\n'),
    dict(id="loop-with-continue",
         src='for t in ["a", "b"]:\n    if t == "a":\n        continue\n'
             '    spark.sql(f"DROP {t}")\n'),
    dict(id="loop-with-return",
         src='def drop():\n    for t in ["a", "b"]:\n'
             '        spark.sql(f"DROP {t}")\n        return\n'),
    dict(id="loop-with-nested-loop",
         src='for a in ["x", "y"]:\n    for b in ["p", "q"]:\n'
             '        spark.sql(f"DROP {a}.{b}")\n'),
    dict(id="loop-with-while",
         src='for t in ["a", "b"]:\n    while flag:\n        spark.sql(f"DROP {t}")\n'),
    dict(id="loop-with-tuple-target",
         src='for i, t in [("1", "a")]:\n    spark.sql(f"DROP {t}")\n'),
    dict(id="loop-over-an-empty-literal",
         src='for t in []:\n    spark.sql(f"DROP {t}")\n'),
    dict(id="generator-body",
         src='def rows(t):\n    yield spark.sql(f"DROP {t}")\nrows("prod.t")\n'),
]


@pytest.mark.parametrize("case", STAYS_UNKNOWN, ids=[c["id"] for c in STAYS_UNKNOWN])
def test_refused_shapes_report_unresolved_not_resolved(case):
    """Refusing must look exactly like refusing did before the feature existed."""
    folder = fold(case["src"])
    assert folder.resolved == {}, f"unsoundly resolved: {folder.resolved}"
    assert folder.unresolved, "a refused shape must still be reported, not dropped"


@pytest.mark.parametrize("case", STAYS_UNKNOWN, ids=[c["id"] for c in STAYS_UNKNOWN])
def test_refused_shapes_carry_a_reason(case):
    folder = fold(case["src"])
    for failure in folder.unresolved.values():
        assert isinstance(failure, FoldFailure)
        assert failure.reason and failure.expression


def test_refused_shapes_are_byte_identical_to_the_pre_feature_fallback():
    """The refusal path is the pre-existing single-visit path, not a new one.

    Every refused shape must produce exactly one entry per syntactic sink with a
    "not a compile-time constant"-shaped reason. If this ever changes, the UNKNOWN
    rate is changing for a reason nobody wrote down.
    """
    for case in STAYS_UNKNOWN:
        folder = fold(case["src"])
        assert len(folder.resolved) + len(folder.unresolved) == len(
            sink_calls(case["src"])
        ), f"{case['id']}: {folder.resolved} / {folder.unresolved}"


# --------------------------------------------------------------------------- ambiguity

def test_two_callers_that_disagree_stay_ambiguous():
    """The headline fail-closed case: real disagreement must not pick a winner.

    `drop("prod.a")` and `drop("prod.b")` means the body issues two different
    statements. Reporting either one alone is a clean, specific finding for a
    statement the reader is not looking at.
    """
    src = 'def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.a")\ndrop("prod.b")\n'
    assert resolved_of(src) == []
    assert unresolved_lines(src) == {2}


def test_one_dynamic_caller_poisons_the_others():
    src = ('def drop(t):\n    spark.sql(f"DROP TABLE {t}")\n'
           'drop("prod.a")\ndrop(input())\n')
    assert resolved_of(src) == []
    assert unresolved_lines(src) == {2}


def test_one_unknown_element_poisons_the_loop():
    """A partial unroll would be a specific finding about a loop we cannot describe."""
    src = 'for t in ["prod.a", input()]:\n    spark.sql(f"DROP TABLE {t}")\n'
    assert resolved_of(src) == []
    assert unresolved_lines(src) == {2}


def test_a_partly_resolvable_loop_never_resolves():
    """A sink that resolves on one iteration and fails on another is UNKNOWN.

    `for t in [1, -1]: spark.sql("ab" * t)` -- `"ab" * 1` is a constant, `"ab" * -1`
    raises at runtime. We can read one of them; we cannot claim the sink is only ever
    the readable one.
    """
    src = 'for t in [1, -1]:\n    spark.sql("ab" * t)\n'
    folder = fold(src)
    assert folder.resolved == {}
    assert len(folder.unresolved) == 1


def test_parameter_rebound_in_the_body_is_not_reported_as_the_callers_value():
    """A parameter the body overwrites is the body's value, not the caller's."""
    src = 'def drop(t):\n    t = "prod.staging"\n    spark.sql(f"DROP TABLE {t}")\n'
    assert resolved_of(src) == ["DROP TABLE prod.staging"]


def test_parameter_deleted_in_the_body_is_unresolved():
    src = ('def drop(t):\n    del t\n    spark.sql(f"DROP TABLE {t}")\n'
           'drop("prod.t")\n')
    assert resolved_of(src) == []
    assert unresolved_lines(src) == {3}


def test_a_sink_that_ignores_the_parameter_still_resolves():
    """Binding a parameter cannot break a body that does not read it."""
    src = 'def run(spark, tbl):\n    return spark.sql("SELECT 1")\nrun(1, 2)\n'
    assert resolved_of(src) == ["SELECT 1"]


def test_agreeing_callers_do_not_multiply_the_finding():
    """Two callers sending the same SQL is one statement's worth of information."""
    src = 'def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.t")\ndrop("prod.t")\n'
    folder = fold(src)
    assert len(folder.resolved) == 1


# --------------------------------------------------------------------------- bounds

def test_loop_over_the_per_loop_cap_is_not_unrolled():
    """At the cap the loop resolves; one past it, it does not.

    The refusal is all-or-nothing rather than "resolve the first 32". Truncating
    would report 32 statements for a loop that issues 33, which is the specific,
    wrong finding the project treats as worse than silence.
    """
    def loop(n: int) -> str:
        elements = ", ".join(f'"prod.t{i}"' for i in range(n))
        return f"for t in [{elements}]:\n    spark.sql(f\"DROP TABLE {{t}}\")\n"

    assert len(resolved_of(loop(MAX_LOOP_UNROLL))) == MAX_LOOP_UNROLL
    over = loop(MAX_LOOP_UNROLL + 1)
    assert resolved_of(over) == []
    assert unresolved_lines(over) == {2}


def test_loop_over_a_ten_thousand_element_literal_is_bounded():
    """The whole point of the cap: a huge literal must not cost a huge walk."""
    elements = ", ".join(f'"prod.t{i}"' for i in range(10_000))
    src = f'for t in [{elements}]:\n    spark.sql(f"DROP TABLE {{t}}")\n'
    started = time.monotonic()
    folder = fold(src)
    elapsed = time.monotonic() - started
    assert folder.resolved == {}
    assert len(folder.unresolved) == 1
    assert elapsed < 5.0, f"parsing plus folding took {elapsed:.1f}s"


def test_the_module_wide_budget_bounds_nested_and_repeated_loops():
    """`MAX_UNROLL_TOTAL` caps the module, not just each loop.

    Twenty 32-element loops would otherwise be 640 iterations; the budget spends 256
    and the remaining loops fall back to UNKNOWN. Over-reporting is the fail-closed
    direction here, and the count is asserted rather than assumed.
    """
    body = "for t in [%s]:\n    spark.sql(f\"DROP TABLE {t}\")\n" % ", ".join(
        f'"prod.t{i}"' for i in range(MAX_LOOP_UNROLL)
    )
    src = body * ((MAX_UNROLL_TOTAL // MAX_LOOP_UNROLL) + 5)
    folder = fold(src)
    assert len(folder.resolved) == MAX_UNROLL_TOTAL
    assert MAX_UNROLL_TOTAL == 256 and MAX_LOOP_UNROLL == 32


def test_a_single_loop_never_exceeds_the_module_budget():
    assert MAX_LOOP_UNROLL <= MAX_UNROLL_TOTAL


# --------------------------------------------------------------------------- invariants

ALL_SHAPES = (
    [c["src"] for c in RESOLVES + STAYS_UNKNOWN]
    + [
        'for t in [1, -1]:\n    spark.sql("ab" * t)\n',
        't = "safe"\nfor t in ["prod.a"]:\n    spark.sql(f"DROP {t}")\n',
        'for t in ["a"]:\n    pass\nspark.sql(f"DROP {t}")\n',
        'def drop(t):\n    del t\n    spark.sql(f"DROP {t}")\ndrop("prod.t")\n',
        'spark.sql("SELECT 1"); spark.sql("DROP TABLE prod.t")\n',
        'for t in ["a", "b"]:\n    spark.sql(f"DROP {t}")\n    spark.sql("SELECT 1")\n',
    ]
)


def test_no_syntactic_sink_is_dropped():
    """Every sink call is accounted for, as resolved or as unresolved.

    A loop sink contributes one entry per *distinct statement it issues*, which can
    exceed one per call, so the assertion is a lower bound rather than equality.
    """
    for src in ALL_SHAPES:
        folder = fold(src)
        overlap = set(folder.resolved) & set(folder.unresolved)
        assert not overlap, f"{src!r}: {sorted(overlap)} in both dicts"
        assert len(folder.resolved) + len(folder.unresolved) >= len(
            sink_calls(src)
        ), f"{src!r}: a sink was dropped"


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_sink_keys_stay_unique_ordered_and_dense(src):
    """The SinkKey contract still holds now that one sink can yield several entries."""
    folder = fold(src)
    keys = [*folder.resolved, *folder.unresolved]
    assert len(set(keys)) == len(keys), "duplicate sink keys"
    assert all(isinstance(key, SinkKey) for key in keys)
    assert sorted(key.ordinal for key in keys) == list(range(len(keys)))
    for table in (folder.resolved, folder.unresolved):
        assert list(table) == sorted(table), "iteration order must be source order"


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_resolved_values_are_bounded_strings(src):
    for sql in fold(src).resolved.values():
        assert isinstance(sql, str)
        assert len(sql) <= MAX_CONST_STRING


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_lines_are_real_sink_lines(src):
    real = {call.lineno for call in sink_calls(src)}
    folder = fold(src)
    for key in (*folder.resolved, *folder.unresolved):
        assert key.line in real, f"line {key.line} holds no sink in {src!r}"


# --------------------------------------------------------------------------- oracle


class _RecordingSpark:
    """A `spark` that records the SQL it is handed instead of running it."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def sql(self, query, *args, **kwargs):
        self.seen.append(query)


#: Snippets safe to hand to a real interpreter: no input, no IO, no cluster, and no
#: names left unbound. These are checked for *completeness* -- the folder must account
#: for every statement Python sends, resolved or not.
ORACLE_CASES = [
    'spark.sql(f"DROP TABLE prod.t")\n',
    'def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.users")\n',
    'def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.a")\ndrop("prod.b")\n',
    'def drop(t="prod.t"):\n    spark.sql(f"DROP TABLE {t}")\ndrop()\n',
    'for t in ["prod.users", "prod.orders"]:\n    spark.sql(f"DROP TABLE {t}")\n',
    'for t in ("prod.a", "prod.b"):\n    spark.sql(f"DROP {t}")\n',
    'A = "prod.a"\nB = "prod.b"\nfor t in [A, B]:\n    spark.sql(f"DROP {t}")\n',
    'def drop(t):\n    for x in ["a", "b"]:\n'
    '        spark.sql(f"DROP {t}.{x}")\ndrop("prod.t")\n',
    'for t in ["a", "a"]:\n    spark.sql(f"DROP {t}")\n',
    'def f(t):\n    spark.sql(f"DROP {t}")\nf(1)\nf(2)\n',
    'def drop(t):\n    del t\n    spark.sql(f"DROP {t}")\ndrop("prod.t")\n',
    'def drop(t):\n    t = "prod.staging"\n    spark.sql(f"DROP TABLE {t}")\n'
    'drop("prod.t")\n',
    't = "prod.a"\nfor t in ["prod.b"]:\n    spark.sql(f"DROP {t}")\n',
    'spark.sql(f"DROP {a}")\n',
    'for t in ["prod.a"]:\n    if t:\n        spark.sql(f"DROP {t}")\n    spark.sql("SELECT 1")\n',
    'for t in ["a", "b"]:\n    spark.sql(f"DROP {t}")\n    spark.sql("SELECT 1")\n',
    't = "prod.a"\nif t:\n    del t\nspark.sql(f"DROP {t}")\n',
    'def f():\n    for t in ["a", "b"]:\n        spark.sql(f"DROP {t}")\n        return\n',
    'for t in ["a", "b"]:\n    if t == "a":\n        break\n    spark.sql(f"DROP {t}")\n',
    'for t in [1, -1]:\n    spark.sql("ab" * t)\n',
]

#: The subset of the above the folder is expected to resolve completely.
ORACLE_FULLY_RESOLVED = [
    'spark.sql(f"DROP TABLE prod.t")\n',
    'def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.users")\n',
    'def drop(t="prod.t"):\n    spark.sql(f"DROP TABLE {t}")\ndrop()\n',
    'for t in ["prod.users", "prod.orders"]:\n    spark.sql(f"DROP TABLE {t}")\n',
    'for t in ("prod.a", "prod.b"):\n    spark.sql(f"DROP {t}")\n',
    'A = "prod.a"\nB = "prod.b"\nfor t in [A, B]:\n    spark.sql(f"DROP {t}")\n',
    'def drop(t):\n    for x in ["a", "b"]:\n'
    '        spark.sql(f"DROP {t}.{x}")\ndrop("prod.t")\n',
    'for t in ["a", "a"]:\n    spark.sql(f"DROP {t}")\n',
    'def drop(t):\n    t = "prod.staging"\n    spark.sql(f"DROP TABLE {t}")\n'
    'drop("prod.t")\n',
    't = "prod.a"\nfor t in ["prod.b"]:\n    spark.sql(f"DROP {t}")\n',
    'for t in ["prod.a"]:\n    if t:\n        spark.sql(f"DROP {t}")\n    spark.sql("SELECT 1")\n',
    'for t in ["a", "b"]:\n    spark.sql(f"DROP {t}")\n    spark.sql("SELECT 1")\n',
]


def _executed(source: str) -> list[str] | None:
    """The SQL a real interpreter sends, or None if the snippet raises.

    Several shapes here are refusals precisely because they blow up or never run --
    a generator body, `del t` then `t`, a missing name. The oracle still has
    something to say about those: for code that does not execute, the folder must
    claim nothing.
    """
    spark = _RecordingSpark()
    try:
        exec(compile(source, "<snippet>", "exec"), {"spark": spark})  # noqa: S102
    except Exception:
        return None
    return spark.seen


@pytest.mark.parametrize("src", ORACLE_CASES, ids=range(len(ORACLE_CASES)))
def test_the_folder_never_claims_sql_the_interpreter_does_not_send(src):
    """The soundness oracle: `resolved` is a subset of what Python actually sends.

    This is the assertion the whole feature stands on, and the only one here that
    can catch an unsound resolution rather than merely a missing one. It runs the
    snippet against a recording shim and demands that every string the folder claims
    really reached `spark.sql`. A resolution that is merely plausible -- a default
    not applied, a loop iterated the wrong number of times, a disagreement resolved
    to one of its values -- fails here even when every table-driven expectation in
    this file still passes.

    The shim exists only here. `sparkscreen` never executes what it screens
    (docs/threads.md T1); this is a test oracle, not a runtime path.
    """
    claimed = set(fold(src).resolved.values())
    sent = _executed(src)
    if sent is None:
        # The snippet raises, so the engine never receives this SQL. Claiming it
        # would be a finding about a statement that cannot run.
        assert claimed == set(), f"claimed {sorted(claimed)} for code that raises"
        return
    assert claimed <= set(sent), (
        f"claimed {sorted(claimed - set(sent))}, which is never sent"
    )


@pytest.mark.parametrize(
    "src", ORACLE_FULLY_RESOLVED, ids=range(len(ORACLE_FULLY_RESOLVED))
)
def test_a_fully_resolved_snippet_matches_the_interpreter_exactly(src):
    """The completeness half of the oracle, on the shapes we expect to resolve.

    `resolved` must equal what Python sends -- not a subset, which would let the
    folder quietly stop reporting a statement -- and `unresolved` must be empty.
    """
    folder = fold(src)
    sent = _executed(src)
    assert sent is not None, "precondition: the snippet runs"
    # Sets, not lists: a loop that issues the same statement twice is one statement
    # for the operator, and the folder deliberately reports it once.
    assert set(folder.resolved.values()) == set(sent)
    assert folder.unresolved == {}, "nothing was left unaccounted for"


def test_a_loop_that_disagrees_with_itself_reports_every_statement_it_sends():
    """Divergent iterations must all be reported -- none chosen, none dropped."""
    src = 'for t in ["prod.a", "prod.b", "prod.c"]:\n    spark.sql(f"DROP {t}")\n'
    assert set(fold(src).resolved.values()) == set(_executed(src))
    assert len(fold(src).resolved) == 3


# --------------------------------------------------------------------------- soundness


#: Rebindings that happen *inside a nested block*. Every one of these used to leave
#: the enclosing constant in place, because a block body gets a child frame and the
#: invalidation only touched that child. The folder then reported `DROP prod.a` for
#: code that can never reach that statement -- the stale binding AGENTS.md names as
#: the failure mode this module exists to prevent.
#:
#: These belong in `tests/test_folding.py` next to the other rebinding cases; they are
#: here because binding function parameters is what made them reachable from a second
#: direction, and a fix with no test is a fix that comes back.
NESTED_BLOCK_REBINDINGS = [
    dict(id="del", src='t = "prod.a"\nif c:\n    del t\nspark.sql(f"DROP {t}")'),
    dict(
        id="assignment-of-an-unreadable-value",
        src='t = "prod.a"\nif c:\n    t = input()\nspark.sql(f"DROP {t}")',
    ),
    dict(id="import", src='t = "prod.a"\nif c:\n    import t\nspark.sql(f"DROP {t}")'),
    dict(
        id="from-import",
        src='t = "prod.a"\nif c:\n    from m import t\nspark.sql(f"DROP {t}")',
    ),
    dict(
        id="match-capture",
        src='t = "prod.a"\nif c:\n    match x:\n        case t:\n'
            '            pass\nspark.sql(f"DROP {t}")',
    ),
    dict(
        id="except-target",
        src='t = "prod.a"\nif c:\n    try:\n        pass\n    except E as t:\n'
            '        pass\nspark.sql(f"DROP {t}")',
    ),
    dict(
        id="augmented-assign",
        src='t = "prod.a"\nif c:\n    t += "x"\nspark.sql(f"DROP {t}")',
    ),
    dict(
        id="with-target",
        src='t = "prod.a"\nif c:\n    with ctx() as t:\n        pass\n'
            'spark.sql(f"DROP {t}")',
    ),
    dict(
        id="for-target",
        src='t = "prod.a"\nif c:\n    for t in y:\n        pass\n'
            'spark.sql(f"DROP {t}")',
    ),
    dict(
        id="walrus",
        src='t = "prod.a"\nif c:\n    (t := input())\nspark.sql(f"DROP {t}")',
    ),
    dict(id="while-body", src='t = "prod.a"\nwhile c:\n    del t\nspark.sql(f"DROP {t}")'),
    dict(
        id="try-body",
        src='t = "prod.a"\ntry:\n    del t\nexcept NameError:\n    pass\n'
            'spark.sql(f"DROP {t}")',
    ),
    dict(
        id="loop-body",
        src='t = "prod.a"\nfor _ in y:\n    del t\nspark.sql(f"DROP {t}")',
    ),
    dict(
        id="inside-a-bound-parameter",
        src='def drop(t):\n    del t\n    spark.sql(f"DROP {t}")\ndrop("prod.t")',
    ),
]


@pytest.mark.parametrize(
    "case", NESTED_BLOCK_REBINDINGS, ids=[c["id"] for c in NESTED_BLOCK_REBINDINGS]
)
def test_a_rebinding_inside_a_block_invalidates_the_enclosing_binding(case):
    """A `del` or an unreadable rebinding in a nested block must drop the constant.

    The block body gets a child frame, so this is not automatic: the invalidation has
    to reach the frame that actually holds the binding. Reporting the pre-block value
    is a clean, specific DROP finding for a statement that will never execute.
    """
    folder = fold(case["src"])
    assert folder.resolved == {}, f"reported a stale constant: {folder.resolved}"
    assert folder.unresolved, "the sink must be reported, not dropped"


@pytest.mark.parametrize(
    "src",
    [
        't = "prod.a"\nif c:\n    pass\nspark.sql(f"DROP {t}")',
        't = "prod.a"\ndef f():\n    return 1\nf()\nspark.sql(f"DROP {t}")',
        't = "prod.a"\ndef f():\n    t = "local"\nspark.sql(f"DROP {t}")',
        't = "prod.a"\ndef f():\n    del t\nspark.sql(f"DROP {t}")',
    ],
    ids=["untouched", "function-body", "local-write", "local-del"],
)
def test_a_nested_scope_that_does_not_rebind_the_outer_name_keeps_the_constant(src):
    """The fix must not over-reach: only actual rebindings drop the binding.

    A function body has its own scope, so a `del` or an assignment of `t` inside it
    says nothing about the module's `t`. Refusing here would turn analysable code
    into UNKNOWN for no soundness gain.
    """
    assert resolved_of(src) == ["DROP prod.a"]


# --------------------------------------------------------------------------- end to end


def test_parameterised_drop_is_denied(spec_key):
    """The motivating case, end to end, on both pinned grammars."""
    source = (
        "def drop(t):\n"
        '    spark.sql(f"DROP TABLE {t}")\n'
        'drop("prod.users")\n'
    )
    report = screen(source, spec=spec_key)
    assert report.verdict is Verdict.DENY
    assert not report.ok
    assert "prod.users" in " ".join(f.sql or "" for f in report.findings)


def test_literal_loop_of_drops_is_denied(spec_key):
    source = (
        "for t in ['prod.users', 'prod.orders']:\n"
        "    spark.sql(f'DROP TABLE {t}')\n"
    )
    report = screen(source, spec=spec_key)
    assert report.verdict is Verdict.DENY
    sql = " ".join(f.sql or "" for f in report.findings)
    assert "prod.users" in sql and "prod.orders" in sql


def test_every_refused_shape_is_never_allowed(spec_key):
    """The direction that matters: nothing here may come back ALLOW.

    ALLOW means "analysed, nothing objected". A shape the folder cannot read has not
    been analysed, so ALLOW would be a false assurance with a confident voice.
    """
    for case in STAYS_UNKNOWN:
        report = screen(case["src"], spec=spec_key)
        assert not report.ok, f"{case['id']} was allowed: {report.to_dict()}"
        assert report.verdict in (Verdict.DENY, Verdict.UNKNOWN, Verdict.REVIEW)


def test_unresolved_loop_stays_unknown_end_to_end(spec_key):
    report = screen(
        "for t in list_tables():\n    spark.sql(f'DROP TABLE {t}')\n", spec=spec_key
    )
    assert report.verdict is Verdict.UNKNOWN
    assert {f.reason for f in report.findings} == {Reason.UNRESOLVED_DYNAMIC_SQL}