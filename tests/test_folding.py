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

Known folding bugs are recorded at the bottom as non-strict xfails rather than
asserted as correct behaviour, so a fix turns them green without anyone rewriting
the expectation.
"""
import ast

import pytest

from sparkscreen.analysis.folding import (
    MAX_CONST_STRING,
    MAX_FOLD_DEPTH,
    FoldFailure,
    fold_sinks,
)

# --------------------------------------------------------------------------- helpers


def fold(source: str):
    """Run the folder over `source` and return the StringFolder."""
    return fold_sinks(ast.parse(source))


def resolved_of(source: str) -> dict[int, str]:
    return fold(source).resolved


def unresolved_of(source: str) -> dict[int, FoldFailure]:
    return fold(source).unresolved


def sink_lines(source: str) -> list[int]:
    """Line numbers of every call the folder considers a SQL sink."""
    out: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            f = node.func
            attr = getattr(f, "attr", None)
            name = getattr(f, "id", None)
            if attr in ("sql", "sqlQuery") or name in ("sql", "sqlQuery"):
                if node.args or node.keywords:
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
]


@pytest.mark.parametrize("case", RECOVERS, ids=[c["id"] for c in RECOVERS])
def test_recovers_exact_sql(case):
    """The folded string is exactly what the engine will execute, on the right line."""
    folder = fold(case["src"])
    expected = case["sql"] if case["line"] is None else {case["line"]: case["sql"]}
    assert folder.resolved == expected
    assert folder.unresolved == {}, f"recovered SQL must not also be reported unresolved: {folder.unresolved}"


@pytest.mark.parametrize("case", RECOVERS, ids=[c["id"] for c in RECOVERS])
def test_resolved_lines_are_real_sink_lines(case):
    """Every reported line number corresponds to a source line with a SQL sink."""
    folder = fold(case["src"])
    assert set(folder.resolved) <= set(sink_lines(case["src"]))


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
        id="nested-fstring",
        src='tbl = "prod.t"\nspark.sql(f"SELECT {f\'{tbl}\'}")',
        line=2,
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
    assert case["line"] in folder.unresolved, (
        f"expected a FoldFailure on line {case['line']}, got {folder.unresolved}"
    )


@pytest.mark.parametrize(
    "case", UNRESOLVED_CASES, ids=[c["id"] for c in UNRESOLVED_CASES]
)
def test_fold_failure_carries_a_reason(case):
    """An unresolved sink explains itself: a reason plus the source expression."""
    failure = fold(case["src"]).unresolved[case["line"]]
    assert isinstance(failure, FoldFailure)
    assert failure.reason
    assert failure.expression
    assert str(failure) == f"{failure.reason}: {failure.expression}"


def test_unresolved_expression_is_the_source_text():
    failure = unresolved_of('tbl = input()\nspark.sql(f"SELECT * FROM {tbl}")')[2]
    assert "tbl" in failure.expression
    assert "SELECT" in failure.expression


def test_long_expression_is_truncated_in_the_failure_message():
    """A pathological expression must not blow up the report."""
    src = "spark.sql(" + "+".join(["x"] * 60) + ")"
    failure = unresolved_of(src)[1]
    assert len(failure.expression) <= 120
    assert failure.expression.endswith("...")


# --------------------------------------------------------------------------- invariants

#: A grab-bag of sink shapes; the two dicts must never claim the same line.
ALL_SHAPES = [c["src"] for c in RECOVERS + UNRESOLVED_CASES]


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_resolved_and_unresolved_are_disjoint(src):
    folder = fold(src)
    overlap = set(folder.resolved) & set(folder.unresolved)
    assert not overlap, f"line(s) {sorted(overlap)} appear in both dicts"


@pytest.mark.parametrize("src", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_resolved_values_are_strings(src):
    for line, sql in fold(src).resolved.items():
        assert isinstance(sql, str), f"line {line} produced {type(sql).__name__}"
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
    assert 3 in folder.unresolved


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
    assert 1 in folder.unresolved


def test_string_multiplication_over_the_cap_is_unresolved():
    """`"a" * 10**9` must not be materialised."""
    folder = fold('spark.sql("a" * 10**9)')
    assert folder.resolved == {}
    assert 1 in folder.unresolved


def test_string_multiplication_just_under_the_cap_is_resolved():
    n = MAX_CONST_STRING // 2  # 512 KiB of "ab"
    folder = fold(f'spark.sql("ab" * {n})')
    assert list(folder.resolved.values()) == ["ab" * n]


def test_fold_depth_cap_stops_runaway_recursion():
    """Deeply nested expressions bail out at MAX_FOLD_DEPTH instead of recursing."""
    deep = " + ".join(['"a"'] * 40)
    folder = fold(f'spark.sql({deep})')
    assert folder.resolved == {}
    assert 1 in folder.unresolved
    assert MAX_FOLD_DEPTH > 0


def test_sink_line_numbers_point_at_the_call_not_the_argument():
    folder = fold(
        'tbl = "prod.t"\n'
        'spark.sql(\n'
        '    # a comment inside the call\n'
        '    f"SELECT * FROM {tbl}",\n'
        ')\n'
    )
    assert folder.resolved == {2: "SELECT * FROM prod.t"}


# --------------------------------------------------------------------------- known bugs
#
# Each of these is a case where folding returns a WRONG answer (or silently drops a
# sink) rather than merely declining. Kept as non-strict xfails: the assertions
# describe the correct behaviour, so fixing folding.py turns them green.


@pytest.mark.xfail(reason="resolved is keyed by line number, so two sinks on one "
                          "line collapse and the first is dropped entirely",
                  strict=False)
def test_bug_two_sinks_on_one_line_are_both_reported():
    """Two sinks, one line: both must be screened, not just the last one."""
    src = 'spark.sql("DROP TABLE prod.t"); spark.sql("SELECT 1")'
    folder = fold(src)
    assert len(sink_lines(src)) == 2, "precondition: the source really has two sinks"
    assert len(folder.resolved) + len(folder.unresolved) == 2


@pytest.mark.xfail(reason="loop targets are not invalidated, so a module-level "
                          "constant is reused for the loop variable",
                  strict=False)
def test_bug_loop_target_shadows_a_module_constant():
    folder = fold('tbl = "prod.t"\nfor tbl in tables:\n    spark.sql(f"DROP {tbl}")')
    assert folder.resolved == {}
    assert 3 in folder.unresolved


@pytest.mark.xfail(reason="function parameters are not tracked, so a module-level "
                          "constant of the same name leaks into the function",
                  strict=False)
def test_bug_function_parameter_shadows_a_module_constant():
    folder = fold(
        'tbl = "prod.t"\ndef q(spark, tbl):\n    return spark.sql(f"DROP {tbl}")'
    )
    assert folder.resolved == {}
    assert 3 in folder.unresolved


@pytest.mark.xfail(reason="`with`/`except` targets are not invalidated either",
                  strict=False)
@pytest.mark.parametrize(
    "src",
    [
        'tbl = "prod.t"\nwith ctx() as tbl:\n    spark.sql(f"DROP {tbl}")',
        'tbl = "prod.t"\ntry:\n    pass\nexcept E as tbl:\n    spark.sql(f"DROP {tbl}")',
    ],
    ids=["with", "except"],
)
def test_bug_context_manager_target_shadows_a_module_constant(src):
    folder = fold(src)
    assert folder.resolved == {}


@pytest.mark.xfail(reason="AugAssign is not handled, so the pre-update value is "
                          "reported as the recovered SQL",
                  strict=False)
def test_bug_augmented_assignment_is_ignored():
    folder = fold('q = "DROP TABLE prod.t"\nq += " WHERE x=1"\nspark.sql(q)')
    assert folder.resolved == {}
    assert 3 in folder.unresolved


@pytest.mark.xfail(reason="`del` does not invalidate the binding",
                  strict=False)
def test_bug_del_does_not_invalidate_a_binding():
    folder = fold('q = "DROP TABLE prod.t"\ndel q\nspark.sql(q)')
    assert folder.resolved == {}


@pytest.mark.xfail(reason="bindings made inside a class or function body leak "
                          "out to module scope",
                  strict=False)
@pytest.mark.parametrize(
    "src",
    [
        'def f():\n    q = "DROP TABLE prod.t"\nspark.sql(q)',
        'class C:\n    q = "DROP TABLE prod.t"\nspark.sql(q)',
    ],
    ids=["function-body", "class-body"],
)
def test_bug_inner_scope_bindings_leak_to_module_scope(src):
    folder = fold(src)
    assert folder.resolved == {}


@pytest.mark.xfail(reason="_fold_fstring requires the field to be a bare scalar "
                          "even when no format spec is given, so folded "
                          "expressions inside {} are rejected",
                  strict=False)
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
    folder = fold(src)
    assert folder.resolved != {}


@pytest.mark.xfail(reason="keyword sinks are ignored entirely: no resolved entry "
                          "and no FoldFailure either",
                  strict=False)
def test_bug_keyword_argument_sink_is_silently_ignored():
    folder = fold('q = "DROP TABLE prod.t"\nspark.sql(query=q)')
    assert folder.resolved == {}
    assert 2 in folder.unresolved