"""Constant folding of `spark.sql(...)` arguments.

The screener's whole value rests on one property: if `fold_sinks` says it recovered
the SQL text, that text must be *the text the engine will see*. These tests pin
both directions of that contract:

  * RECOVERS -- the styles agent-written PySpark actually uses (f-strings, `+`,
    `%`, `.format`, `" ".join`, format specs, reassignment) must fold to the exact
    expected string, or the screener reports UNKNOWN on safe code and is useless.
  * DOES NOT RECOVER -- anything whose value depends on runtime state (user input,
    loop variables, parameters, dict lookups, comprehensions, env vars) must land
    in `unresolved` and never in `resolved`. A wrong "safe" here is a security bug,
    so the bar for this direction is: no false confidence, ever.
  * INVARIANTS -- `resolved` and `unresolved` are disjoint, unknown input cannot
    raise, and the pass is total over valid Python.

Deliberately independent of the SQL parser: this module tests folding only.

The folding bugs that used to be recorded at the bottom as non-strict xfails are
fixed and asserted directly; see the `test_bug_*` block. Each one was a fail-open, so
they are kept as ordinary tests rather than deleted -- a regression there is silent.
"""
import ast

import pytest

from sparkscreen.analysis.folding import (
    MAX_CONST_STRING,
    MAX_FOLD_DEPTH,
    FoldFailure,
    SinkKey,
    fold_sinks,
)

# --------------------------------------------------------------------------- helpers


def fold(source: str):
    """Run the folder over `source` and return the StringFolder."""
    return fold_sinks(ast.parse(source))


def by_line(folder_dict: dict[SinkKey, object]) -> dict[int, object]:
    """`{line: value}` view of a SinkKey-keyed dict.

    `resolved`/`unresolved` are keyed by SinkKey, not by line, precisely because a
    line is not a sink identity. This helper exists so the table-driven cases below
    can keep stating their expectations as line -> value; it asserts that no two
    sinks share a line, so it can never hide the collision that caused the silent
    drop. Where two sinks DO share a line, the test uses the raw dicts.
    """
    out: dict[int, object] = {}
    for key, value in folder_dict.items():
        assert isinstance(key, SinkKey)
        assert key.line not in out, (
            f"two sinks share line {key.line}; use the raw dict for this case"
        )
        out[key.line] = value
    return out


def resolved_of(source: str) -> dict[int, str]:
    return by_line(fold(source).resolved)  # type: ignore[return-value]


def unresolved_of(source: str) -> dict[int, FoldFailure]:
    return by_line(fold(source).unresolved)  # type: ignore[return-value]


def failure_on(source: str, line: int) -> FoldFailure:
    """The FoldFailure reported for the sink on `line`."""
    found = by_line(fold(source).unresolved)
    assert line in found, f"no FoldFailure on line {line}; got {found}"
    return found[line]  # type: ignore[return-value]


def sink_count(source: str) -> int:
    """How many calls in `source` the folder is expected to treat as SQL sinks."""
    return len(sink_lines(source))


def sink_lines(source: str) -> list[int]:
    """Line numbers of every call the folder considers a SQL sink.

    Independent reimplementation of the sink test, so the "nothing was dropped"
    invariant is checked against something other than the code under test. Note
    this counts a bare `spark.sql()` as a sink: the folder treats every recognised
    `.sql(...)` / `sql(...)` call as one, because a call with no visible argument
    still executes SQL we cannot see.
    """
    out: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            f = node.func
            attr = getattr(f, "attr", None)
            name = getattr(f, "id", None)
            if attr in ("sql", "sqlQuery") or name in ("sql", "sqlQuery"):
                out.append(node.lineno)
    return sorted(out)


# --------------------------------------------------------------------------- recovers
#
# Each case: id, source, expected SQL, and the line the sink sits on (1-indexed).

RECOVERS = [
    # -- the plain cases ---------------------------------------------------
    dict(
        id="literal",
        src='spark.sql("SELECT 1")',
        sql="SELECT 1",
        line=1,
    ),
    dict(
        id="literal-multiline-call",
        src='spark.sql(\n    "SELECT * FROM prod.t",\n)',
        sql="SELECT * FROM prod.t",
        line=1,
    ),
    dict(
        id="literal-triple-quoted",
        src='spark.sql("""SELECT 1""")',
        sql="SELECT 1",
        line=1,
    ),
    # -- f-strings over module-level constants -----------------------------
    dict(
        id="fstring-const",
        src='tbl = "prod.t"\nspark.sql(f"DROP TABLE {tbl}")',
        sql="DROP TABLE prod.t",
        line=2,
    ),
    dict(
        id="fstring-quoted-literal-interpolation",
        src='day = "2026-01-01"\nspark.sql(f"SELECT * FROM t WHERE d=\'{day}\'")',
        sql="SELECT * FROM t WHERE d='2026-01-01'",
        line=2,
    ),
    dict(
        id="fstring-two-consts",
        src='tbl = "prod.t"\nday = "2026-01-01"\n'
            'spark.sql(f"SELECT * FROM {tbl} WHERE d = \'{day}\'")',
        sql="SELECT * FROM prod.t WHERE d = '2026-01-01'",
        line=3,
    ),
    dict(
        id="fstring-annotated-binding",
        src='tbl: str = "prod.t"\nspark.sql(f"SELECT * FROM {tbl}")',
        sql="SELECT * FROM prod.t",
        line=2,
    ),
    dict(
        id="fstring-int-const-no-spec",
        src='n = 3\nspark.sql(f"SELECT * FROM t LIMIT {n}")',
        sql="SELECT * FROM t LIMIT 3",
        line=2,
    ),
    dict(
        id="fstring-negative-int-const",
        src='n = -3\nspark.sql(f"SELECT {n}")',
        sql="SELECT -3",
        line=2,
    ),
    dict(
        id="fstring-chained-attribute-suffix",
        src='part = "prod"\nspark.sql(f"SELECT * FROM {part}.users")',
        sql="SELECT * FROM prod.users",
        line=2,
    ),
    dict(
        id="fstring-inline-literal",
        src='spark.sql(f"SELECT * FROM prod.t")',
        sql="SELECT * FROM prod.t",
        line=1,
    ),
    # -- conversions and format specs --------------------------------------
    # !r renders the quotes, and it must: that is what the engine receives.
    dict(
        id="fstring-repr-conversion",
        src='tbl = "prod.t"\nspark.sql(f"SELECT * FROM {tbl!r}")',
        sql="SELECT * FROM 'prod.t'",
        line=2,
    ),
    dict(
        id="fstring-ascii-conversion",
        src='tbl = "caf\\u00e9"\nspark.sql(f"SELECT * FROM {tbl!a}")',
        sql="SELECT * FROM 'caf\\xe9'",
        line=2,
    ),
    dict(
        id="fstring-str-conversion",
        src='tbl = "prod.t"\nspark.sql(f"SELECT * FROM {tbl!s}")',
        sql="SELECT * FROM prod.t",
        line=2,
    ),
    dict(
        id="fstring-zero-padded-format-spec",
        src='n = 3\nspark.sql(f"SELECT * FROM t LIMIT {n:04d}")',
        sql="SELECT * FROM t LIMIT 0003",
        line=2,
    ),
    dict(
        id="fstring-width-format-spec",
        src='n = 42\nspark.sql(f"SELECT {n:5d}")',
        sql="SELECT    42",
        line=2,
    ),
    dict(
        id="fstring-nested-format-spec",
        src='n = 3\nw = 5\nspark.sql(f"{n:0{w}d}")',
        sql="00003",
        line=3,
    ),
    dict(
        id="fstring-repr-then-width-spec",
        src='tbl = "t"\nspark.sql(f"{tbl!r:>8}")',
        sql="       t",
        line=2,
    ),
    # -- + concatenation ---------------------------------------------------
    dict(
        id="concat-two",
        src='tbl = "prod.t"\nspark.sql("SELECT * FROM " + tbl)',
        sql="SELECT * FROM prod.t",
        line=2,
    ),
    dict(
        id="concat-three-with-tail",
        src='tbl = "prod.t"\nspark.sql("SELECT * FROM " + tbl + " WHERE x=1")',
        sql="SELECT * FROM prod.t WHERE x=1",
        line=2,
    ),
    dict(
        id="concat-nested",
        src='a = "A"\nb = "B"\nspark.sql(a + "," + b)',
        sql="A,B",
        line=3,
    ),
    dict(
        id="concat-parenthesised-expression",
        src='a = "SELECT 1"\nspark.sql((a) + "; SELECT 2")',
        sql="SELECT 1; SELECT 2",
        line=2,
    ),
    # -- % formatting ------------------------------------------------------
    dict(
        id="percent-scalar",
        src='tbl = "prod.t"\nspark.sql("SELECT * FROM %s" % tbl)',
        sql="SELECT * FROM prod.t",
        line=2,
    ),
    dict(
        id="percent-tuple-mixed-str-int",
        src='tbl = "prod.t"\nspark.sql("SELECT * FROM %s LIMIT %d" % (tbl, 3))',
        sql="SELECT * FROM prod.t LIMIT 3",
        line=2,
    ),
    dict(
        id="percent-tuple-of-names",
        src='tbl = "prod.t"\nn = 3\nspark.sql("SELECT * FROM %s LIMIT %d" % (tbl, n))',
        sql="SELECT * FROM prod.t LIMIT 3",
        line=3,
    ),
    dict(
        id="percent-list-args",
        src='tbl = "prod.t"\nspark.sql("SELECT * FROM %s" % [tbl])',
        sql="SELECT * FROM prod.t",
        line=2,
    ),
    dict(
        id="percent-nested-in-concat",
        src='tbl = "prod.t"\nspark.sql("SELECT " + ("* FROM %s" % tbl))',
        sql="SELECT * FROM prod.t",
        line=2,
    ),
    # -- str.format --------------------------------------------------------
    dict(
        id="format-positional",
        src='tbl = "prod.t"\nspark.sql("SELECT * FROM {}".format(tbl))',
        sql="SELECT * FROM prod.t",
        line=2,
    ),
    dict(
        id="format-two-positional",
        src='tbl = "prod.t"\nn = 3\nspark.sql("SELECT * FROM {} LIMIT {}".format(tbl, n))',
        sql="SELECT * FROM prod.t LIMIT 3",
        line=3,
    ),
    dict(
        id="format-numeric-index",
        src='tbl = "prod.t"\nspark.sql("SELECT * FROM {0}".format(tbl))',
        sql="SELECT * FROM prod.t",
        line=2,
    ),
    dict(
        id="format-escaped-braces",
        src='spark.sql("SELECT {{a}} FROM {}".format("prod.t"))',
        sql="SELECT {a} FROM prod.t",
        line=1,
    ),
    # -- join --------------------------------------------------------------
    dict(
        id="join-space",
        src='tbl = "prod.t"\nspark.sql(" ".join(["SELECT *", "FROM", tbl]))',
        sql="SELECT * FROM prod.t",
        line=2,
    ),
    dict(
        id="join-comma",
        src='cols = "a, b"\nspark.sql("SELECT " + ",".join(["a", "b"]) + " FROM t")',
        sql="SELECT a,b FROM t",
        line=2,
    ),
    dict(
        id="join-empty-list",
        src='spark.sql(" ".join([]))',
        sql="",
        line=1,
    ),
    dict(
        id="join-of-folded-fstring-elements",
        src='tbl = "prod.t"\nspark.sql(" ".join([f"SELECT * FROM {tbl}", "WHERE x=1"]))',
        sql="SELECT * FROM prod.t WHERE x=1",
        line=2,
    ),
    dict(
        id="join-tuple-argument",
        src='a = "x"\nspark.sql(",".join((a, "y")))',
        sql="x,y",
        line=2,
    ),
    # -- str methods on a folded receiver ----------------------------------
    dict(
        id="upper-on-folded-name",
        src='base = "select * from prod.t"\nspark.sql(base.upper())',
        sql="SELECT * FROM PROD.T",
        line=2,
    ),
    dict(
        id="lower-and-strip",
        src='spark.sql("  DROP TABLE PROD.T  ".strip().lower())',
        sql="drop table prod.t",
        line=1,
    ),
    dict(
        id="replace-on-literal",
        src='spark.sql("SELECT * FROM prod.t".replace("prod.", "stg_"))',
        sql="SELECT * FROM stg_t",
        line=1,
    ),
    # -- string repetition and literal containers --------------------------
    # `spark.sql("ab" * 3)` really does execute "ababab"; folding it is faithful.
    dict(
        id="string-multiply",
        src='spark.sql("ab" * 3)',
        sql="ababab",
        line=1,
    ),
    dict(
        id="string-multiply-zero",
        src='spark.sql("ab" * 0)',
        sql="",
        line=1,
    ),
    # -- reassignment ------------------------------------------------------
    dict(
        id="reassignment-latest-value-wins",
        src='tbl = "prod.t"\nspark.sql(f"DROP TABLE {tbl}")\ntbl = "dev.t"\n'
            'spark.sql(f"DROP TABLE {tbl}")',
        sql={2: "DROP TABLE prod.t", 4: "DROP TABLE dev.t"},
        line=None,
    ),
    dict(
        id="reassignment-after-use",
        src='tbl = "prod.t"\nspark.sql(f"SELECT * FROM {tbl}")\ntbl = "dev.t"\n'
            'spark.sql(f"SELECT * FROM {tbl}")',
        sql={2: "SELECT * FROM prod.t", 4: "SELECT * FROM dev.t"},
        line=None,
    ),
    # -- several sinks, each resolved on its own line ----------------------
    dict(
        id="multiple-sinks-multiple-lines",
        src='tbl = "prod.t"\n'
            'spark.sql("SELECT 1")\n'
            'spark.sql(f"DROP TABLE {tbl}")\n'
            'spark.sql("SELECT * FROM %s" % tbl)',
        sql={2: "SELECT 1", 3: "DROP TABLE prod.t", 4: "SELECT * FROM prod.t"},
        line=None,
    ),
    dict(
        id="sink-inside-function",
        src='def run(spark):\n    spark.sql("SELECT 1")\n',
        sql={2: "SELECT 1"},
        line=None,
    ),
    dict(
        id="sink-inside-try-except",
        src='try:\n    spark.sql("SELECT 1")\nexcept Exception:\n    spark.sql("SELECT 2")',
        sql={2: "SELECT 1", 4: "SELECT 2"},
        line=None,
    ),
    dict(
        id="sink-inside-if-branch",
        src='if cond:\n    spark.sql("SELECT 1")',
        sql={2: "SELECT 1"},
        line=None,
    ),
    dict(
        id="sink-in-lambda-body",
        src='q = lambda spark: spark.sql("SELECT 1")',
        sql={1: "SELECT 1"},
        line=None,
    ),
    dict(
        id="async-def-sink",
        src='async def q(spark):\n    spark.sql("SELECT 1")',
        sql={2: "SELECT 1"},
        line=None,
    ),
    dict(
        id="sink-in-comprehension",
        src='tbl = "prod.t"\nres = [spark.sql(f"SELECT * FROM {tbl}") for _ in range(2)]',
        sql={2: "SELECT * FROM prod.t"},
        line=None,
    ),
    # -- sink name variants ------------------------------------------------
    dict(
        id="bare-sql-function",
        src='tbl = "prod.t"\nsql(f"DROP TABLE {tbl}")',
        sql={2: "DROP TABLE prod.t"},
        line=None,
    ),
    dict(
        id="sqlQuery-method",
        src='tbl = "prod.t"\nspark.sqlQuery(f"DROP TABLE {tbl}")',
        sql={2: "DROP TABLE prod.t"},
        line=None,
    ),
    dict(
        id="sink-in-nested-call-args",
        src='helper(spark.sql("SELECT 1"))',
        sql={1: "SELECT 1"},
        line=None,
    ),
    # -- a bound string used as the argument -------------------------------
    # A bare name that WAS bound to a constant is recoverable; an unbound one
    # is not (see DOES_NOT_RECOVER).
    dict(
        id="bare-name-bound-to-constant",
        src='query = "SELECT * FROM prod.t"\nspark.sql(query)',
        sql={2: "SELECT * FROM prod.t"},
        line=None,
    ),
    dict(
        id="ifexp-branches-agree",
        src='tbl = "prod.t"\nspark.sql(f"SELECT * FROM {tbl}" if flag else f"SELECT * FROM {tbl}")',
        sql={2: "SELECT * FROM prod.t"},
        line=None,
    ),
    # -- nested f-string as a replacement field ---------------------------
    # Valid since PEP 701. A nested f-string over a constant folds exactly, so
    # folding it is faithful rather than optimistic.
    dict(
        id="nested-fstring-field",
        src='tbl = "prod.t"\nspark.sql(f"SELECT {f\'{tbl}\'}")',
        sql={2: "SELECT prod.t"},
        line=None,
    ),
    # -- folded (non-scalar) expression as a replacement field -------------
    # The field has no format spec, so the folded string is what the engine sees.
    dict(
        id="fstring-field-folded-binop",
        src='a = "SELECT "\nb = "1"\nspark.sql(f"{a + b}")',
        sql={3: "SELECT 1"},
        line=None,
    ),
]


@pytest.mark.parametrize("case", RECOVERS, ids=[c["id"] for c in RECOVERS])
def test_recovers_exact_sql(case):
    """The folded string is exactly what the engine will execute, on the right line."""
    folder = fold(case["src"])
    expected = case["sql"] if case["line"] is None else {case["line"]: case["sql"]}
    assert by_line(folder.resolved) == expected
    assert folder.unresolved == {}, f"recovered SQL must not also be reported unresolved: {folder.unresolved}"


@pytest.mark.parametrize("case", RECOVERS, ids=[c["id"] for c in RECOVERS])
def test_resolved_lines_are_real_sink_lines(case):
    """Every reported line number corresponds to a source line with a SQL sink."""
    folder = fold(case["src"])
    assert {k.line for k in folder.resolved} <= set(sink_lines(case["src"]))


# --------------------------------------------------------------------------- no recovery
#
# Each case: id, source, and the line the sink sits on.

UNRESOLVED_CASES = [
    dict(id="bare-name-never-bound", src="spark.sql(query)", line=1),
    dict(
        id="input-then-interpolated",
        src='tbl = input()\nspark.sql(f"SELECT * FROM {tbl}")',
        line=2,
    ),
    dict(
        id="input-concatenated",
        src='tbl = input()\nspark.sql("SELECT * FROM " + tbl)',
        line=2,
    ),
    dict(
        id="loop-variable-over-runtime-list",
        src='for t in tables:\n    spark.sql(f"TRUNCATE TABLE {t}")',
        line=2,
    ),
    dict(
        id="loop-variable-enumerate",
        src='for i, t in enumerate(tables):\n    spark.sql(f"TRUNCATE TABLE {t}")',
        line=2,
    ),
    dict(
        id="loop-over-list-of-filenames",
        src='for path in spark.conf.get("paths").split(","):\n    spark.sql(f"LOAD DATA \'{path}\'.json")',
        line=2,
    ),
    dict(
        id="function-parameter",
        src='def q(spark, tbl):\n    return spark.sql(f"SELECT * FROM {tbl}")',
        line=2,
    ),
    dict(
        id="dict-lookup",
        src='tbl = cfg["t"]\nspark.sql(f"SELECT * FROM {tbl}")',
        line=2,
    ),
    dict(
        id="env-var",
        src='import os\ntbl = os.environ.get("TBL")\nspark.sql(f"SELECT * FROM {tbl}")',
        line=3,
    ),
    dict(
        id="list-comprehension-join",
        src='sep = ","\nparts = [x for x in columns]\nspark.sql(sep.join(parts))',
        line=3,
    ),
    dict(
        id="generator-expression-join",
        src='sep = ","\nspark.sql(sep.join(x for x in columns))',
        line=2,
    ),
    dict(
        id="join-over-unbound-iterable",
        src='sep = ","\nspark.sql(sep.join(tables))',
        line=2,
    ),
    dict(
        id="non-scalar-call",
        src='spark.sql(str(1))',
        line=1,
    ),
    dict(
        id="class-attribute",
        src='class C:\n    query = "DROP TABLE prod.t"\nspark.sql(C.query)',
        line=3,
    ),
    dict(
        id="subscript-of-constant",
        src='s = "abcdef"\nspark.sql(f"{s[1:3]}")',
        line=2,
    ),
    dict(
        id="unwhitelisted-str-method",
        src='spark.sql("SELECT 1".split())',
        line=1,
    ),
    dict(
        id="format-with-keyword-arg",
        src='tbl = "prod.t"\nspark.sql("SELECT * FROM {t}".format(t=tbl))',
        line=2,
    ),
    dict(
        id="percent-dict-mapping",
        src='spark.sql("LIMIT %(n)d" % {"n": 3})',
        line=1,
    ),
    dict(
        id="walrus-in-fstring",
        src='spark.sql(f"SELECT * FROM {(t := table_name)}")',
        line=1,
    ),
    dict(
        id="star-args",
        src="spark.sql(*queries)",
        line=1,
    ),
    dict(
        id="ifexp-branches-disagree",
        src='tbl = "prod.t"\nspark.sql(f"SELECT * FROM {tbl}" if flag else "SELECT 1")',
        line=2,
    ),
    dict(
        id="comparison-operator",
        src='spark.sql("SELECT 1" if 1 < 2 else "SELECT 2")',
        line=1,
    ),
    dict(
        id="augassigned-constant",
        src='tbl = "prod.t"\nsql = f"SELECT * FROM {tbl}"\nsql += " WHERE x=1"\nspark.sql(sql)',
        line=4,
    ),
    dict(
        id="read-open-file",
        src='with open(path) as fh:\n    spark.sql(fh.read())',
        line=2,
    ),
    dict(
        id="attribute-of-runtime-object",
        src='spark.sql(cfg.query)',
        line=1,
    ),
    dict(
        id="dynamic-binding-poisons-later-use",
        src='tbl = input()\nsql = tbl + "foo"\nspark.sql(sql)',
        line=3,
    ),
]


@pytest.mark.parametrize(
    "case", UNRESOLVED_CASES, ids=[c["id"] for c in UNRESOLVED_CASES]
)
def test_does_not_recover(case):
    """Runtime-dependent SQL must be reported unresolved -- never as recovered."""
    folder = fold(case["src"])
    assert folder.resolved == {}, "unsoundly recovered a runtime-dependent sink"
    assert case["line"] in {k.line for k in folder.unresolved}, (
        f"expected a FoldFailure on line {case['line']}, got {folder.unresolved}"
    )


@pytest.mark.parametrize(
    "case", UNRESOLVED_CASES, ids=[c["id"] for c in UNRESOLVED_CASES]
)
def test_fold_failure_carries_a_reason(case):
    """An unresolved sink explains itself: a reason plus the source expression."""
    failure = failure_on(case["src"], case["line"])
    assert isinstance(failure, FoldFailure)
    assert failure.reason
    assert failure.expression
    assert str(failure) == f"{failure.reason}: {failure.expression}"


def test_unresolved_expression_is_the_source_text():
    failure = failure_on('tbl = input()\nspark.sql(f"SELECT * FROM {tbl}")', 2)
    assert "tbl" in failure.expression
    assert "SELECT" in failure.expression


def test_long_expression_is_truncated_in_the_failure_message():
    """A pathological expression must not blow up the report."""
    src = "spark.sql(" + "+".join(["x"] * 60) + ")"
    failure = failure_on(src, 1)
    assert len(failure.expression) <= 120
    assert failure.expression.endswith("...")


# Constructs that rebind (or rebind inside) a name the constant table knows about.
# Each one used to leave a confidently wrong SQL string in `resolved`.
REBINDINGS_THAT_INVALIDATE = [
    dict(id="global-statement", src='q = "DROP TABLE prod.t"\ndef f():\n    global q\n    q = "select 1"\nspark.sql(q)', line=5),
    dict(id="nonlocal-rebind", src='def f():\n    q = "DROP TABLE prod.t"\n    def g():\n        nonlocal q\n        q = "select 1"\n    g()\n    spark.sql(q)', line=7),
    dict(id="import-rebinds", src='tbl = "prod.t"\nimport tbl\nspark.sql(f"drop {tbl}")', line=3),
    dict(id="import-from-rebinds", src='tbl = "prod.t"\nfrom x import tbl\nspark.sql(f"drop {tbl}")', line=3),
    dict(id="tuple-unpack-rebinds", src='tbl = "prod.t"\ntbl, other = pair()\nspark.sql(f"drop {tbl}")', line=3),
    dict(id="starred-unpack-rebinds", src='*rest, tbl = pair()\nspark.sql(f"drop {tbl}")', line=2),
    dict(id="match-capture-rebinds", src='tbl = "prod.t"\nmatch x:\n    case tbl:\n        spark.sql(f"drop {tbl}")', line=4),
    dict(id="finally-block-rebinds", src='q = "DROP TABLE prod.t"\ntry:\n    pass\nfinally:\n    q = "select 1"\nspark.sql(q)', line=6),
    dict(id="lambda-body-does-not-leak", src='q = lambda: "DROP TABLE prod.t"\nspark.sql(q)', line=2),
    dict(id="nested-def-does-not-leak", src='def f():\n    def g():\n        q = "DROP TABLE prod.t"\n    g()\nspark.sql(q)', line=5),
    dict(id="comprehension-body-does-not-leak", src='xs = ["DROP TABLE prod.t" for _ in y]\nspark.sql(xs[0])', line=2),
]

#: Keyword-argument sinks PySpark really accepts, and the exact SQL we recover.
KEYWORD_SINK_CASES = [
    dict(id="query-literal", src='spark.sql(query="DROP TABLE prod.t")', sql="DROP TABLE prod.t", line=1),
    dict(id="sql-literal", src='spark.sql(sql="DROP TABLE prod.t")', sql="DROP TABLE prod.t", line=1),
    dict(id="query-fstring", src='tbl = "prod.t"\nspark.sql(query=f"DROP {tbl}")', sql="DROP prod.t", line=2),
    dict(id="query-constant-name", src='q = "DROP TABLE prod.t"\nspark.sql(query=q)', sql="DROP TABLE prod.t", line=2),
    dict(id="query-with-args-kwarg", src='spark.sql(query="DROP TABLE prod.t", args={})', sql="DROP TABLE prod.t", line=1),
    dict(id="sqlQuery-keyword", src='spark.sql(sqlQuery="DROP TABLE prod.t")', sql="DROP TABLE prod.t", line=1),
]

#: Sink shapes where the SQL text is genuinely not visible. All must fail loudly.
UNRECOVERABLE_KEYWORD_SINKS = [
    dict(id="query-dynamic", src="spark.sql(query=cfg.q)", line=1),
    dict(id="query-unbound-name", src="spark.sql(query=q)", line=1),
    dict(id="kwargs-splat", src='spark.sql(**{"query": "DROP TABLE prod.t"})', line=1),
    dict(id="args-only", src="spark.sql(args={})", line=1),
    dict(id="no-arguments", src="spark.sql()", line=1),
    dict(id="positional-starred", src="spark.sql(*queries)", line=1),
    dict(id="unknown-keyword-name", src='spark.sql(cmd="DROP TABLE prod.t")', line=1),
]


# --------------------------------------------------------------------------- invariants

#: A grab-bag of sink shapes; the two dicts must never claim the same sink.
ALL_SHAPES = (
    [c["src"] for c in RECOVERS + UNRESOLVED_CASES]
    + [c["src"] for c in REBINDINGS_THAT_INVALIDATE]
    + [c["src"] for c in KEYWORD_SINK_CASES + UNRECOVERABLE_KEYWORD_SINKS]
)


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_every_sink_lands_in_exactly_one_dict(src):
    """No sink may be silently dropped -- the invariant the module exists for.

    Every call the folder recognises as a SQL sink must appear in exactly one of
    `resolved` / `unresolved`. A sink in neither is a statement we never screened,
    which is indistinguishable from "no SQL here" and reports ALLOW. This is what
    let `spark.sql("DROP TABLE prod.users"); spark.sql("select 1")` through: the
    line-keyed dicts collapsed both sinks onto one key.
    """
    folder = fold(src)
    overlap = set(folder.resolved) & set(folder.unresolved)
    assert not overlap, f"the same sink appears in both dicts: {sorted(overlap)}"
    assert len(folder.resolved) + len(folder.unresolved) == sink_count(src), (
        f"expected {sink_count(src)} sink entries, got "
        f"{len(folder.resolved) + len(folder.unresolved)}: "
        f"resolved={folder.resolved} unresolved={folder.unresolved}"
    )


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_sink_keys_carry_the_line_the_call_sits_on(src):
    """Every reported key names a line that really holds a SQL sink."""
    folder = fold(src)
    real = set(sink_lines(src))
    for key in (*folder.resolved, *folder.unresolved):
        assert isinstance(key, SinkKey)
        assert key.line in real, f"line {key.line} has no sink: {src!r}"


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_sink_keys_are_unique_and_ordered(src):
    """Two sinks may share a line, but never a key."""
    folder = fold(src)
    keys = [*folder.resolved, *folder.unresolved]
    assert len(set(keys)) == len(keys)
    for table in (folder.resolved, folder.unresolved):
        assert list(table) == sorted(table), "iteration order must be source order"


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_ordinals_are_dense_from_zero(src):
    """Ordinals are a dense sequence, so a caller can size an array from them."""
    folder = fold(src)
    ordinals = sorted(k.ordinal for k in (*folder.resolved, *folder.unresolved))
    assert ordinals == list(range(len(ordinals)))


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_resolved_and_unresolved_are_disjoint(src):
    folder = fold(src)
    overlap = set(folder.resolved) & set(folder.unresolved)
    assert not overlap, f"line(s) {sorted(overlap)} appear in both dicts"


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_resolved_values_are_strings(src):
    for key, sql in fold(src).resolved.items():
        assert isinstance(sql, str), f"line {key.line} produced {type(sql).__name__}"
        assert len(sql) <= MAX_CONST_STRING


@pytest.mark.parametrize(
    "src",
    [
        "x = 1\ny = x + 1\n",
        "def helper(a, b):\n    return a + b\n",
        "import os\n\n\nclass Config:\n    pass\n",
        "# just a comment\n",
        "",
    ],
    ids=["arithmetic", "function", "imports-and-class", "comment-only", "empty"],
)
def test_module_with_no_sql_sinks_yields_nothing(src):
    folder = fold(src)
    assert folder.resolved == {}
    assert folder.unresolved == {}


@pytest.mark.parametrize(
    "src",
    [
        'x = input()\nsql = x + "foo"\nspark.sql(sql)',
        'x = input()\nsql = f"{x}"\nspark.sql(sql)',
        'x = input()\nsql = "SELECT " + x\nspark.sql(sql)',
        'x = cfg["k"]\nsql = "{0}".format(x)\nspark.sql(sql)',
        'x = input()\nsql = x\nspark.sql(sql)',
    ],
    ids=["concat", "fstring", "concat-reversed", "format", "alias"],
)
def test_dynamic_rhs_makes_the_binding_non_constant(src):
    """A name bound to a dynamic value must not stay "constant" and be folded later."""
    folder = fold(src)
    assert folder.resolved == {}
    assert len(folder.unresolved) == 1


def test_dynamic_rebinding_clears_the_previous_constant():
    folder = fold(
        'tbl = "prod.t"\ntbl = input()\nspark.sql(f"SELECT * FROM {tbl}")'
    )
    assert folder.resolved == {}
    assert 3 in {k.line for k in folder.unresolved}


@pytest.mark.parametrize(
    "src",
    [
        'tbl = "prod.t"\nspark.sql("SELECT %s %s" % tbl)',          # too few args
        'spark.sql("SELECT %s %s" % ("a",))',                       # too few args
        'spark.sql("LIMIT %d" % "not-a-number")',                   # type mismatch
        'spark.sql("%(n)d" % {"n": 3})',                            # dict mapping
        'spark.sql("SELECT 1" % ("a", "b", "c"))',                  # extra args
        'spark.sql("%s %s" % tbl)',                                 # scalar, two slots
    ],
    ids=[
        "scalar-one-slot-two",
        "tuple-one-of-two",
        "type-mismatch",
        "dict-mapping",
        "too-many-args",
        "scalar-two-slots",
    ],
)
def test_percent_formatting_that_would_raise_is_unresolved_not_a_crash(src):
    folder = fold(src)
    assert folder.resolved == {}
    assert len(folder.unresolved) == 1


@pytest.mark.parametrize(
    "src",
    [
        'spark.sql("{1}".format("a"))',              # index out of range
        'spark.sql("{}".format())',                  # missing argument
        'spark.sql("{}{".format("a"))',              # malformed template
        'spark.sql(" ".join(x))',                    # non-iterable-looking arg
        'spark.sql("ab" * 2.5)',                     # float multiplier
        'spark.sql("ab" * -1)',                      # negative multiplier
        'f = 3\nspark.sql(f"{f:>not-a-spec}")',      # bad format spec
        'f = 3\nspark.sql(f"{f:{unknown}}")',        # unknown nested spec field
    ],
    ids=[
        "format-index-out-of-range",
        "format-missing-arg",
        "format-broken-template",
        "join-non-container",
        "multiply-float",
        "multiply-negative",
        "bad-format-spec",
        "unknown-spec-field",
    ],
)
def test_operations_that_would_raise_are_unresolved_not_a_crash(src):
    folder = fold(src)
    assert folder.resolved == {}
    assert len(folder.unresolved) == 1


@pytest.mark.parametrize(
    "expr",
    [
        "lambda x: x",
        "(lambda x: x)(1)",
        "[i for i in range(3)]",
        "{i: j for i, j in y}",
        "{i for i in y}",
        "(i for i in y)",
        "y[0].z(1).z",
        "*args",
        "a if b else c",
        "(lambda: 0)()",
        "f'{x!r:>{w}}'",
        "b'bytes'",
        "...",
        "None",
        "True",
        "3.14",
        "-2 ** 3",
        "a[1:2:3]",
        "{'k': 'v'}",
        "x.y.z.w",
        "f(*args, **kwargs)",
        "(yield)",
    ],
    ids=[
        "lambda",
        "immediately-invoked-lambda",
        "listcomp",
        "dictcomp",
        "setcomp",
        "generator",
        "attribute-chain",
        "starred",
        "ifexp",
        "immediately-invoked-again",
        "nested-fstring-with-spec",
        "bytes-literal",
        "ellipsis",
        "none-literal",
        "true-literal",
        "float-literal",
        "power-operator",
        "slice",
        "dict-literal",
        "deep-attribute-chain",
        "call-with-star-kwargs",
        "yield-expression",
    ],
)
def test_parser_never_raises_on_weird_but_valid_expressions(expr):
    """Total over valid Python: a strange argument is unresolved, not an exception."""
    folder = fold(f"spark.sql({expr})")
    assert folder.resolved == {} or all(
        isinstance(v, str) for v in folder.resolved.values()
    )
    assert not (set(folder.resolved) & set(folder.unresolved))


@pytest.mark.parametrize(
    "src",
    [
        "spark.sql()",
        "spark.sql(**kw)",
        "spark.sql(query=q)",
        "spark.sql(*queries)",
        "spark.sql(q, query=q)",
    ],
    ids=["no-args", "kwargs-only", "keyword-sink", "starred", "both"],
)
def test_sink_shapes_that_carry_no_string_argument(src):
    """No positional string means nothing to fold; the pass must not crash."""
    fold(src)  # smoke: no exception


def test_star_args_sink_is_reported_unresolved():
    folder = fold("spark.sql(*queries)")
    assert folder.resolved == {}
    assert 1 in {k.line for k in folder.unresolved}


def test_string_multiplication_over_the_cap_is_unresolved():
    """`"a" * 10**9` must not be materialised."""
    folder = fold('spark.sql("a" * 10**9)')
    assert folder.resolved == {}
    assert 1 in {k.line for k in folder.unresolved}


def test_string_multiplication_just_under_the_cap_is_resolved():
    n = MAX_CONST_STRING // 2  # 512 KiB of "ab"
    folder = fold(f'spark.sql("ab" * {n})')
    assert list(folder.resolved.values()) == ["ab" * n]


def test_fold_depth_cap_stops_runaway_recursion():
    """Deeply nested expressions bail out at MAX_FOLD_DEPTH instead of recursing."""
    deep = " + ".join(['"a"'] * 40)
    folder = fold(f'spark.sql({deep})')
    assert folder.resolved == {}
    assert 1 in {k.line for k in folder.unresolved}
    assert MAX_FOLD_DEPTH > 0


def test_sink_line_numbers_point_at_the_call_not_the_argument():
    folder = fold(
        'tbl = "prod.t"\n'
        'spark.sql(\n'
        '    # a comment inside the call\n'
        '    f"SELECT * FROM {tbl}",\n'
        ')\n'
    )
    assert by_line(folder.resolved) == {2: "SELECT * FROM prod.t"}


# --------------------------------------------------------------------------- known bugs
#
# Each of these was a case where folding returned a WRONG answer (or silently dropped
# a sink) rather than merely declining. They were recorded as non-strict xfails; all
# are now fixed and asserted directly. Each was a fail-open: either a destructive
# statement screened as ALLOW, or a clean, specific finding described a statement
# that will never run that way.


def test_bug_two_sinks_on_one_line_are_both_reported():
    """Two sinks, one line: both must be screened, not just one of them.

    This was the worst of the set: `spark.sql("DROP TABLE prod.users"); spark.sql(
    "select 1")` reported only the SELECT, so the whole snippet screened as ALLOW.
    """
    src = 'spark.sql("DROP TABLE prod.t"); spark.sql("SELECT 1")'
    folder = fold(src)
    assert sink_count(src) == 2, "precondition: the source really has two sinks"
    assert len(folder.resolved) + len(folder.unresolved) == 2
    # both are recoverable here, so both must be recovered -- and the destructive
    # one must not be the one that got dropped.
    assert sorted(folder.resolved.values()) == ["DROP TABLE prod.t", "SELECT 1"]


def test_bug_loop_target_shadows_a_module_constant():
    """A for-loop target rebinds the name, so the outer constant is stale.

    The bug reported the loop body as `DROP prod.t` -- a specific, confident DROP of
    a production table that the loop will never execute. UNKNOWN is the right answer.
    """
    folder = fold('tbl = "prod.t"\nfor tbl in tables:\n    spark.sql(f"DROP {tbl}")')
    assert folder.resolved == {}
    assert 3 in {k.line for k in folder.unresolved}


def test_bug_function_parameter_shadows_a_module_constant():
    """A parameter shadows a same-named module constant inside the body.

    Function bodies do not inherit the module constant table, so `tbl` here is the
    caller-supplied argument and the SQL is not knowable.
    """
    folder = fold(
        'tbl = "prod.t"\ndef q(spark, tbl):\n    return spark.sql(f"DROP {tbl}")'
    )
    assert folder.resolved == {}
    assert 3 in {k.line for k in folder.unresolved}


@pytest.mark.parametrize(
    "src",
    [
        'tbl = "prod.t"\nwith ctx() as tbl:\n    spark.sql(f"DROP {tbl}")',
        'tbl = "prod.t"\ntry:\n    pass\nexcept E as tbl:\n    spark.sql(f"DROP {tbl}")',
    ],
    ids=["with", "except"],
)
def test_bug_context_manager_target_shadows_a_module_constant(src):
    """`with ... as x` and `except E as x` bind a fresh value for x.

    Both leak into module scope after the block, and both shadow a module constant
    inside it.
    """
    folder = fold(src)
    assert folder.resolved == {}


def test_bug_augmented_assignment_is_ignored():
    """AugAssign rebinds; the pre-update value is not the SQL.

    Reporting the pre-update text gives a clean, specific finding for a statement
    that will never run that way -- worse than reporting nothing at all.
    """
    folder = fold('q = "DROP TABLE prod.t"\nq += " WHERE x=1"\nspark.sql(q)')
    assert folder.resolved == {}
    assert 3 in {k.line for k in folder.unresolved}


def test_bug_del_does_not_invalidate_a_binding():
    """`del q` unbinds q, so a later use raises NameError at runtime.
    """
    folder = fold('q = "DROP TABLE prod.t"\ndel q\nspark.sql(q)')
    assert folder.resolved == {}


@pytest.mark.parametrize(
    "src",
    [
        'def f():\n    q = "DROP TABLE prod.t"\nspark.sql(q)',
        'class C:\n    q = "DROP TABLE prod.t"\nspark.sql(q)',
    ],
    ids=["function-body", "class-body"],
)
def test_bug_inner_scope_bindings_leak_to_module_scope(src):
    """Function and class bodies do not contribute to the enclosing scope.

    `q` is only ever bound while that body executes; at module scope it is unbound,
    so the sink cannot resolve. A body that DOES resolve its own locals is covered
    by `sink-inside-function` above.
    """
    folder = fold(src)
    assert folder.resolved == {}


@pytest.mark.parametrize(
    "src",
    [
        'a = "A"\nb = "B"\nspark.sql(f"SELECT {a + b}")',
        'a = "A"\nspark.sql(f"SELECT {a.upper()}")',
        'tbl = "prod.t"\nspark.sql(f"SELECT {f\'{tbl}\'}")',
    ],
    ids=["binop-field", "call-field", "nested-fstring"],
)
def test_bug_folded_expression_as_fstring_field_is_rejected(src):
    """A replacement field with no format spec may be any folded expression.

    With no spec, the field is rendered with plain str(), so the folded string is
    exactly what the engine sees. This was fail-closed but needlessly so: it turned
    analysable code into UNKNOWN.
    """
    folder = fold(src)
    assert folder.resolved != {}


@pytest.mark.parametrize(
    "case", REBINDINGS_THAT_INVALIDATE,
    ids=[c["id"] for c in REBINDINGS_THAT_INVALIDATE],
)
def test_bug_any_rebinding_invalidates_the_constant(case):
    """Every construct that can rebind a name must drop the stale constant.

    Each of these used to leave a confidently wrong SQL string in `resolved`: a
    clean, specific finding for a statement that will never run that way. Reporting
    nothing beats reporting the wrong thing.
    """
    folder = fold(case["src"])
    assert folder.resolved == {}, "reported a stale constant as recovered SQL"
    assert case["line"] in {k.line for k in folder.unresolved}


KEYWORD_SINK_CASES = [
    dict(id="query-literal", src='spark.sql(query="DROP TABLE prod.t")', sql="DROP TABLE prod.t", line=1),
    dict(id="sql-literal", src='spark.sql(sql="DROP TABLE prod.t")', sql="DROP TABLE prod.t", line=1),
    dict(id="query-fstring", src='tbl = "prod.t"\nspark.sql(query=f"DROP {tbl}")', sql="DROP prod.t", line=2),
    dict(id="query-constant-name", src='q = "DROP TABLE prod.t"\nspark.sql(query=q)', sql="DROP TABLE prod.t", line=2),
    dict(id="query-with-args-kwarg", src='spark.sql(query="DROP TABLE prod.t", args={})', sql="DROP TABLE prod.t", line=1),
    dict(id="sqlQuery-keyword", src='spark.sql(sqlQuery="DROP TABLE prod.t")', sql="DROP TABLE prod.t", line=1),
]


@pytest.mark.parametrize(
    "case", KEYWORD_SINK_CASES, ids=[c["id"] for c in KEYWORD_SINK_CASES],
)
def test_bug_keyword_argument_sinks_are_recovered(case):
    """`spark.sql(query=...)` and friends are real sinks and must be recovered.

    They were in neither dict: not resolved (so not screened) and not unresolved
    (so not flagged). The whole call disappeared from the report.
    """
    folder = fold(case["src"])
    assert by_line(folder.resolved) == {case["line"]: case["sql"]}
    assert folder.unresolved == {}




@pytest.mark.parametrize(
    "case", UNRECOVERABLE_KEYWORD_SINKS,
    ids=[c["id"] for c in UNRECOVERABLE_KEYWORD_SINKS],
)
def test_bug_unrecoverable_sink_shapes_fail_loudly(case):
    """A sink whose text we cannot see must be UNKNOWN, never absent.

    This is the invariant the whole module exists to protect: there is no path from
    "we could not read the SQL" to "no findings".
    """
    folder = fold(case["src"])
    assert folder.resolved == {}
    assert case["line"] in {k.line for k in folder.unresolved}


def test_bug_keyword_argument_sink_is_silently_ignored():
    """A keyword-argument sink must be accounted for, and with the right SQL.

    The original assertion here was `resolved == {} and 2 in unresolved`: i.e. that
    `spark.sql(query=q)` must be *unresolved* even though `q` is a compile-time
    constant. That is the wrong requirement. The bug was that the sink vanished --
    it appeared in neither dict, so the whole call screened as ALLOW. Either
    recovering the text or reporting a FoldFailure is a correct fix, and recovering
    it is strictly better: the value is exact, so there is no reason to give up a
    resolution the operator would otherwise have to triage by hand.
    """
    src = 'q = "DROP TABLE prod.t"\nspark.sql(query=q)'
    folder = fold(src)
    assert len(folder.resolved) + len(folder.unresolved) == 1, (
        f"the sink must appear in exactly one dict, got resolved={folder.resolved} "
        f"unresolved={folder.unresolved}"
    )
    if folder.resolved:
        assert by_line(folder.resolved) == {2: "DROP TABLE prod.t"}
    else:
        assert 2 in {k.line for k in folder.unresolved}