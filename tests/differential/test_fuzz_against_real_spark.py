"""Mutation fuzz against the live engine: the corpus, but generated, not written.

`test_against_real_spark.py` checks a hand-written corpus; hand-written corpora have
the blind spot of everything hand-written -- they cannot contain an input nobody
thought of. This module mutates the corpus seeds with token-level operators and runs
the mutants through the engine's *parser* (`sessionState().sqlParser().parsePlan`),
which throws only on a syntax error and never executes the statement, so CREATE or
INSERT mutants cannot touch the warehouse.

The same seeded generator exists JVM-free in `tests/test_properties.py` (section 5),
where it guards the verdict-level properties. This module is the only place the
parse-acceptance agreement can be checked, and it found F27 on its first run.

Direction (asymmetric, as everywhere in the differential suite):

- we accept && engine rejects -> a failure, UNLESS the statement is a recorded
  AST-layer divergence (`KNOWN_AST_LAYER_REJECTIONS`, F27), which stays open.
- we reject && engine accepts -> noteworthy only. Recorded, printed, not failed:
  the cost is a spurious UNKNOWN on working code, never a missed statement.

Run like the rest of the differential suite:

    JAVA_HOME=/path/to/jre PATH="$JAVA_HOME/bin:$PATH" .venv-pyspark/bin/python \\
        -m pytest tests/differential/test_fuzz_against_real_spark.py -q
"""
from __future__ import annotations

import os
import random
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkscreen.grammar.parser import SqlSyntaxError, get_parser  # noqa: E402

from differential.corpus import KNOWN_AST_LAYER_REJECTIONS  # noqa: E402

#: The recorded divergences, keyed by SQL text (the table rows carry the engine's
#: error class as well; this set is what the sweep tolerates).
_KNOWN_DIVERGENCE_SQL = frozenset(sql for sql, _ in KNOWN_AST_LAYER_REJECTIONS)

pyspark = pytest.importorskip("pyspark", reason="differential tests need pyspark")
pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def spark_session():
    if not (os.environ.get("JAVA_HOME") or os.environ.get("JRE_HOME")):
        pytest.skip("differential tests need a JVM (set JAVA_HOME)")
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder
        .master("local[1]")
        .appName("sparkscreen-fuzz-differential")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", "/tmp/sparkscreen-warehouse")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    try:
        yield session
    finally:
        session.stop()


# Fuzz both spellings: the corpus is uppercase and agent code is lowercase (F6).
# The SET seed exercises the INVALID_SET_SYNTAX class, the alter-table seed the
# UNSUPPORTED_DATATYPE class -- the two AST-layer divergence families in F27.
_SEEDS: tuple[str, ...] = (
    "select 1",
    "select * from prod.t",
    "drop table prod.users",
    "truncate table t",
    "set spark.sql.shuffle.partitions=200",
    "alter table t add column b int",
    "SELECT 1",
    "SELECT * FROM prod.t",
    "DROP TABLE prod.users",
    "TRUNCATE TABLE t",
    "SET spark.sql.shuffle.partitions=200",
    "ALTER TABLE t ADD COLUMN b INT",
)

_FUZZ_KEYWORDS = (
    "drop", "select", "from", "where", "table", "insert", "union", "join",
    "delete", "values", "as", "on", "by", "not", "over", "partition",
)
_FUZZ_TAILS = (";", " limit 1", " union", " group by a", " 'unterminated")
_MUTATIONS = ("delete", "duplicate", "swap", "insert_keyword", "wrap_parens", "append_tail")


def _tokenize(sql: str) -> list[str]:
    out: list[str] = []
    for word in sql.split(" "):
        if word:
            out.append(word)
        out.append(" ")
    if out and out[-1] == " ":
        out.pop()
    return out


def _mutate(rng: random.Random, sql: str) -> str:
    toks = _tokenize(sql)
    if not toks:
        return sql
    op = rng.choice(_MUTATIONS)
    i = rng.randrange(len(toks))
    if op == "delete":
        del toks[i]
    elif op == "duplicate":
        toks.insert(i, toks[i])
    elif op == "swap" and i < len(toks) - 1:
        toks[i], toks[i + 1] = toks[i + 1], toks[i]
    elif op == "insert_keyword":
        toks.insert(i, rng.choice(_FUZZ_KEYWORDS))
    elif op == "wrap_parens":
        toks.insert(i, "(")
        toks.append(")")
    elif op == "append_tail":
        toks.append(rng.choice(_FUZZ_TAILS))
    return "".join(toks).strip()


def _cases() -> list[str]:
    """Seeded mutant list. `random.Random` with a fixed seed is deterministic across
    Python versions, so a failure is reproducible by re-running with the same seed;
    bump the seed to re-sweep after a fix."""
    rng = random.Random(20261006)
    cases = list(_SEEDS)
    seen = set(cases)
    for _ in range(250):
        sql = _mutate(rng, rng.choice(_SEEDS))
        if sql and sql not in seen:
            seen.add(sql)
            cases.append(sql)
    return cases


def test_fuzzed_mutants_do_not_contradict_the_engine(spark_session):
    """The engine's parser is the oracle for parse acceptance; our parser must agree.

    `parsePlan` throws only on a syntax error and never runs the statement, which is
    exactly the question a screener asks -- `spark.sql(...).collect()` would execute
    CREATE/INSERT mutants and conflate analysis errors with parse errors.
    """
    from pyspark.errors import ParseException

    if pyspark.__version__.startswith("3.5"):
        key = "spark-3.5.1"
    elif pyspark.__version__.startswith("4.1"):
        key = "spark-4.1"
    else:
        key = "spark-4.2"
    jparser = spark_session._jsparkSession.sessionState().sqlParser()
    counts: Counter = Counter()
    unexpected_fails: list[str] = []
    noteworthy: list[str] = []

    for sql in _cases():
        try:
            jparser.parsePlan(sql)
            engine = True
        except ParseException:
            engine = False
        except Exception as e:  # noqa: BLE001 - anything else is a probe bug
            pytest.fail(f"parsePlan raised {type(e).__name__} for {sql!r}: {e}")

        try:
            ours = get_parser(key).parse(sql) is not None
        except SqlSyntaxError:
            ours = False

        if ours and not engine:
            if sql in _KNOWN_DIVERGENCE_SQL:
                counts["accepted-divergence(F27, recorded)"] += 1
            else:
                unexpected_fails.append(sql)
                counts["FAIL we-accept/engine-rejects"] += 1
        elif engine and not ours:
            counts["note we-reject/engine-accepts"] += 1
            noteworthy.append(sql)
        else:
            counts["agree:accept" if engine else "agree:reject"] += 1

    assert not unexpected_fails, (
        f"{key}: we accept SQL the engine {pyspark.__version__} rejects; a screener "
        f"must not analyse SQL the engine will not run. Unrecorded divergences: "
        f"{unexpected_fails!r}. If the engine now accepts one of "
        f"KNOWN_AST_LAYER_REJECTIONS, graduate it into CORPUS (see F27)."
    )
    print(f"\n== {key} mutation fuzz, {len(_cases())} cases ==")
    for name, n in sorted(counts.items()):
        print(f"   {name}: {n}")
    if noteworthy:
        print(f"   noteworthy (we reject what the engine accepts): {noteworthy!r}")