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

from differential.corpus import FUZZ_SEEDS, KNOWN_AST_LAYER_REJECTIONS  # noqa: E402

#: The recorded divergences, keyed by SQL text (the table rows carry the engine's
#: error class as well; the SWEEP tolerates by class, not by string -- see below).
_KNOWN_DIVERGENCE_SQL = frozenset(sql for sql, _ in KNOWN_AST_LAYER_REJECTIONS)

#: Engine error classes whose rejection happens AFTER the grammar accepted -- the F27
#: class, tolerance by family rather than by exact string. The deep sweep generates
#: dozens of members per family (mangled `DROP INDEX` qualifiers, glued `ADD JAR`
#: resource types, datatype identifiers), which an exact-string set cannot keep up
#: with; the engine's own error taxonomy can, because it is stable and bracketed.
#:
#: `PARSE_SYNTAX_ERROR` is deliberately absent: it is the GRAMMAR-level rejection
#: marker (verified on 3.5.1 and 4.2.0 -- `SELCT 1` arrives bracketed with it), so a
#: we-accept/engine-reject carrying it is a real port defect and must fail.
#: `UNBRACKETED` covers the legacy message shape (`Operation not allowed: ...`) that
#: Spark raises, unbracketed, for several command-layer validations (DROP INDEX,
#: REPLACE COLUMNS, ADD resource types) -- the dominant shape on 3.5.1. Tolerating a
#: message with no machine-readable class is a deliberate trade: failing on it would
#: turn every scheduled 3.5.1 run red on known-family members. The trade is
#: loud-by-default on the bracketed side, printed-counts on the unbracketed one.
TOLERATED_REJECTION_CLASSES = frozenset({
    "INVALID_SET_SYNTAX",
    "UNSUPPORTED_DATATYPE",
    "INVALID_STATEMENT_OR_CLAUSE",
    "UNBRACKETED",
})

pyspark = pytest.importorskip("pyspark", reason="differential tests need pyspark")
pytestmark = pytest.mark.slow


def _rejection_class(exc: Exception) -> str:
    """The engine's own error-class marker from a ParseException message.

    Spark brackets machine-readable error classes (`[INVALID_SET_SYNTAX]`); legacy
    paths raise the same exception with no bracket (`Operation not allowed: ...`).
    The class, not the SQL text, is what the sweep tolerates by -- see
    TOLERATED_REJECTION_CLASSES.
    """
    msg = str(exc)
    if "[" in msg:
        return msg.split("[", 1)[-1].split("]", 1)[0]
    return "UNBRACKETED"

#: Sweep depth. Per-push runs keep the default; `ci_checks.py fuzz-deep` (the scheduled
#: job) sets a larger mutant budget and a fresh seed so each scheduled run explores
#: mutants no previous run -- here or on any push -- ever generated.
_SWEEP_SEED = int(os.environ.get("SPARKSCREEN_FUZZ_SEED", "20261006"))
_SWEEP_MUTANTS = int(os.environ.get("SPARKSCREEN_FUZZ_MUTANTS", "250"))


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


# The canonical seed pool lives in differential/corpus.py so the JVM-free property
# fuzz and this sweep mutate the SAME seeds; see the seed-completeness guard in
# test_properties.py, which asserts the pool reaches every destructive label.
_SEEDS: tuple[str, ...] = FUZZ_SEEDS

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
    """Seeded mutant list. Deterministic by default: `random.Random` with a fixed seed
    is stable across Python versions, so a failure is reproducible by re-running with
    the same seed. The scheduled deep job overrides both values from the environment --
    a fresh seed every run explores mutants no previous run generated -- and prints
    them, so a red run names exactly how to reproduce it. Bump the default seed to
    re-sweep after a fix."""
    rng = random.Random(_SWEEP_SEED)
    cases = list(_SEEDS)
    seen = set(cases)
    for _ in range(_SWEEP_MUTANTS):
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
    family_members: dict[str, list[str]] = {}

    for sql in _cases():
        try:
            jparser.parsePlan(sql)
            engine = True
        except ParseException as e:
            engine = False
            engine_class = _rejection_class(e)
        except Exception as e:  # noqa: BLE001 - anything else is a probe bug
            pytest.fail(f"parsePlan raised {type(e).__name__} for {sql!r}: {e}")

        try:
            ours = get_parser(key).parse(sql) is not None
        except SqlSyntaxError:
            ours = False

        if ours and not engine:
            if sql in _KNOWN_DIVERGENCE_SQL:
                counts[f"accepted-divergence(F27, recorded row)"] += 1
            elif engine_class in TOLERATED_REJECTION_CLASSES:
                counts[f"accepted-divergence(F27, family={engine_class})"] += 1
                family_members.setdefault(engine_class, []).append(sql)
            elif engine_class == "PARSE_SYNTAX_ERROR":
                unexpected_fails.append(f"{sql!r} [grammar-level, {engine_class}]")
                counts["FAIL we-accept/engine-rejects(grammar)"] += 1
            else:
                unexpected_fails.append(f"{sql!r} [new family, {engine_class}]")
                counts["FAIL we-accept/engine-rejects(new-family)"] += 1
        elif engine and not ours:
            counts["note we-reject/engine-accepts"] += 1
            noteworthy.append(sql)
        else:
            counts["agree:accept" if engine else "agree:reject"] += 1

    assert not unexpected_fails, (
        f"{key}: we accept SQL the engine {pyspark.__version__} rejects; a screener "
        f"must not analyse SQL the engine will not run. Unrecorded divergences: "
        f"{unexpected_fails!r}. A [grammar-level] divergence (PARSE_SYNTAX_ERROR) is a "
        f"port defect -- fix the port. A [new family] divergence needs its error class "
        f"added to TOLERATED_REJECTION_CLASSES deliberately (see F27). If the engine "
        f"now accepts one of KNOWN_AST_LAYER_REJECTIONS, graduate it into CORPUS. "
        f"Reproduce with SPARKSCREEN_FUZZ_SEED={_SWEEP_SEED} "
        f"SPARKSCREEN_FUZZ_MUTANTS={_SWEEP_MUTANTS}."
    )
    print(f"\n== {key} mutation fuzz, {len(_cases())} cases "
          f"(seed={_SWEEP_SEED}, mutants={_SWEEP_MUTANTS}) ==")
    for name, n in sorted(counts.items()):
        print(f"   {name}: {n}")
    for cls, members in sorted(family_members.items()):
        print(f"   family {cls}: {len(members)} members, e.g. {members[:3]!r}")
    if noteworthy:
        print(f"   noteworthy (we reject what the engine accepts): {noteworthy!r}")