"""The differential CI matrix: three real engines, three jobs, per-version expectations.

Why this file exists
--------------------
`sparkscreen-bp4` asks for the live-Spark differential suite to run against pyspark
3.5.1, 4.1.x and 4.2.x as **three separate jobs**. Before that matrix existed, every
differential helper selected its grammar with a two-way ternary:

    "spark-3.5.1" if pyspark.__version__.startswith("3.5") else "spark-4.2"

Three engines do not fit two branches. The 4.1.3 leg would have compared the 4.1 engine
against the *4.2* grammar and reported agreement about a comparison nobody made -- green,
and wrong. `engine_matrix.grammar_key_for_engine` replaces that with an exact lookup that
raises on an unrecorded engine.

What each leg actually proves
-----------------------------
`ENGINE_EXPECTATIONS` in `corpus.py` records, per engine version, what that engine was
observed to accept or reject. The assertions here are deliberately asymmetric:

  * For a statement the engine **rejects**, we must reject it too. Accepting what the
    engine refuses means analysing SQL that will never run.
  * For a statement the engine **accepts**, rejecting it is a spurious UNKNOWN -- safe,
    noisy, and not a reason to fail a job. So that direction is asserted only for the
    4.2-only constructs, where we know the grammar claims to carry the feature and a
    regression would mean the pin moved under a name that still says 4.2.

Run:
    JAVA_HOME=... <venv with the engine> -m pytest tests/differential/ -v

Requires pyspark + a JVM; skipped otherwise, so the fast suite stays inert and JVM-free.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparkscreen.grammar.parser import SqlSyntaxError, get_parser  # noqa: E402
from sparkscreen.grammar.spec import SPECS  # noqa: E402

from differential.corpus import (  # noqa: E402
    BOTH_FOUR_X_ACCEPTED,
    BOTH_FOUR_X_ACCEPTED_WITHOUT_ENGINE_AGREEMENT,
    ENGINE_EXPECTATIONS,
)
from differential.engine_matrix import (  # noqa: E402
    ENGINE_TO_GRAMMAR,
    UnmappedEngineError,
    grammar_key_for_engine,
)

pyspark = pytest.importorskip("pyspark", reason="differential tests need pyspark")
pytestmark = pytest.mark.slow

VERSION = pyspark.__version__
KEY = grammar_key_for_engine(VERSION)


# ---------------------------------------------------------------------------
# The mapping itself. These run on every leg, and on the fast suite would be skipped
# with the module -- which is why the two no-pyspark assertions live in
# `test_grammar_pin_integrity.py` instead, where they actually run everywhere.
# ---------------------------------------------------------------------------

def test_engine_matrix_covers_every_pinned_grammar():
    """Each pinned grammar must have a real engine to be checked against.

    The matrix and the grammar pins have to stay in step in both directions. A grammar
    with no engine is a grammar whose behaviour no engine has ever contradicted -- an
    unverified claim, which is the F17 shape. An engine with no grammar is a comparison
    nobody can make.
    """
    keys = {s.key for s in SPECS}
    assert set(ENGINE_TO_GRAMMAR.values()) == keys, (
        f"engine matrix covers {sorted(set(ENGINE_TO_GRAMMAR.values()))} but "
        f"sparkscreen pins {sorted(keys)}"
    )


def test_every_mapped_engine_is_a_real_grammar_version():
    """Each engine version must be one the pinned spec actually claims to describe.

    This is the assertion that catches a matrix entry invented to make a job green. If
    someone types a version the grammar does not cover, `spark_versions` disagrees and
    the leg would be comparing an engine against a grammar that makes no claim about it.
    """
    for version, key in ENGINE_TO_GRAMMAR.items():
        spec = next(s for s in SPECS if s.key == key)
        assert version in spec.spark_versions, (
            f"pyspark {version} is mapped to {key}, whose spec claims only "
            f"{spec.spark_versions}"
        )


def test_an_unrecorded_engine_raises_rather_than_borrowing_a_grammar():
    """4.1.0 exists upstream and is deliberately absent here.

    It is absent on purpose: nobody recorded its behaviour. A prefix-matching fallback
    would hand it `spark-4.1` and the leg would report agreement it never measured, which
    is the failure this whole file is about.
    """
    for version in ("4.1.0", "4.0.0", "3.5.9", "5.0.0", ""):
        with pytest.raises(UnmappedEngineError):
            grammar_key_for_engine(version)


def test_the_running_engine_is_one_of_the_matrixed_ones():
    """Guard against an unpinned install running the matrix locally.

    `grammar_key_for_engine` raises on anything unmapped, so reaching this line already
    means the version is known. Asserting it explicitly is what makes the failure legible
    if someone installs `.[diff]` and gets a different Spark than CI runs.
    """
    assert VERSION in ENGINE_TO_GRAMMAR, (
        f"pyspark {VERSION} is not a matrixed engine; "
        f"expected one of {sorted(ENGINE_TO_GRAMMAR)}"
    )


# ---------------------------------------------------------------------------
# Per-version expectations, against the live engine on this leg.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def spark_session():
    if not (os.environ.get("JAVA_HOME") or os.environ.get("JRE_HOME")):
        pytest.skip("differential tests need a JVM (set JAVA_HOME)")
    from pyspark.sql import SparkSession

    session = (
        SparkSession.builder
        .master("local[1]")
        .appName("sparkscreen-version-matrix")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", f"/tmp/sparkscreen-matrix-{VERSION}")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    try:
        yield session
    finally:
        session.stop()


def _expectations_for_this_engine() -> tuple[tuple[str, bool], ...]:
    if VERSION not in ENGINE_EXPECTATIONS:
        pytest.skip(
            f"no recorded per-engine expectations for pyspark {VERSION}; "
            "record them in corpus.py before relying on this leg"
        )
    return ENGINE_EXPECTATIONS[VERSION]


def _ours(sql: str) -> bool:
    try:
        return get_parser(KEY).parse(sql) is not None
    except SqlSyntaxError:
        return False


def _ids(cases):
    return [sql for sql, _ in cases]


@pytest.mark.parametrize("sql,engine_accepts", _expectations_for_this_engine(),
                         ids=_ids(_expectations_for_this_engine()))
def test_our_verdict_matches_this_engine(spark_session, sql, engine_accepts):
    """The whole point of the matrix: each engine checked against its own grammar.

    Both sides are measured in the same run -- the shipped parser for `KEY`, and a live
    Spark session for `VERSION`. The fail-closed direction is asserted strictly; the
    permissive direction is asserted only where the grammar claims the feature.
    """
    from differential.corpus import real_spark_verdict

    actual = real_spark_verdict(spark_session, sql)
    assert actual == engine_accepts, (
        f"recorded expectation is stale for {sql!r}: this file says Spark "
        f"{VERSION} {'accepts' if engine_accepts else 'rejects'} it, but it "
        f"{'accepts' if actual else 'rejects'}. Update tests/differential/corpus.py "
        "from a real session -- not from the grammar."
    )

    ours = _ours(sql)
    if not engine_accepts:
        # Never accept what the engine refuses: that is analysing SQL nobody can run.
        assert not ours, (
            f"{KEY} accepts {sql!r} but real Spark {VERSION} rejects it. A screener "
            "must not analyse SQL the engine will not run."
        )
    else:
        # Accepting is required where the construct belongs to this line: a rejection
        # would mean the pin no longer carries the feature its key names.
        assert ours, (
            f"{KEY} rejects {sql!r} but real Spark {VERSION} accepts it. Rejecting "
            "what the engine runs yields a spurious UNKNOWN."
        )


@pytest.mark.parametrize("sql", BOTH_FOUR_X_ACCEPTED)
def test_both_4x_engines_accept_the_4_1_era_constructs(spark_session, sql):
    """The half of the matrix where 4.1.3 and 4.2.0 are *believed* to agree.

    Kept as its own test on purpose. Keeping 4.1 and 4.2 as separate jobs despite a
    belief that they do not diverge is the issue's explicit instruction: the point is to
    be first to know when the belief breaks. If this test starts failing on one leg only,
    that is the divergence, and it arrives as a failure rather than as a surprise in
    production.

    On the 3.5.1 leg these are skipped rather than asserted-false, because 3.5.1 rejecting
    them is not what this test is about -- the 4.x agreement is.
    """
    if not VERSION.startswith("4."):
        pytest.skip(f"4.x-only expectation; running against {VERSION}")

    from differential.corpus import real_spark_verdict

    assert real_spark_verdict(spark_session, sql), (
        f"Spark {VERSION} rejects {sql!r}, which the 4.1-era feature list says both 4.x "
        "engines accept. If 4.1 and 4.2 have diverged, this is where it shows."
    )
    assert _ours(sql), f"{KEY} rejects {sql!r}, which its own line accepts"


@pytest.mark.parametrize("sql", BOTH_FOUR_X_ACCEPTED_WITHOUT_ENGINE_AGREEMENT)
def test_claims_the_live_engines_do_not_support(spark_session, sql):
    """Pins the two brief claims that measurement refuted.

    `WINDOW` and `TABLESAMPLE` were described as 4.1-era constructs that 3.5.1 rejects.
    All three engines accept them, and the shipped 3.5.1 grammar agrees with the engine.
    Recording that here rather than deleting the claim means the next person to see it
    finds the measurement instead of re-deriving a wrong expectation from the note.
    """
    from differential.corpus import real_spark_verdict

    assert real_spark_verdict(spark_session, sql), (
        f"Spark {VERSION} now rejects {sql!r}, which it accepted when recorded. "
        "BOTH_FOUR_X_ACCEPTED_WITHOUT_ENGINE_AGREEMENT may be able to move to "
        "BOTH_FOUR_X_ACCEPTED -- or to a genuinely version-specific table."
    )


# ---------------------------------------------------------------------------
# Grammar-only assertions: these need no engine, so they are cheap and total.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", BOTH_FOUR_X_ACCEPTED + BOTH_FOUR_X_ACCEPTED_WITHOUT_ENGINE_AGREEMENT)
def test_4x_grammars_agree_on_4x_constructs(sql):
    """Both 4.x grammars must accept what both 4.x engines accept.

    Parametrised over every grammar key rather than this leg's key on purpose: under the
    matrix this assertion would otherwise run three times over two grammars and never
    over the third, which is the coverage shape that let `spark-4.0` sit mislabelled
    through two releases.
    """
    for key in ("spark-4.1", "spark-4.2"):
        assert _ours_for(key, sql), f"{key} rejects {sql!r}, which Spark 4.x accepts"


@pytest.mark.parametrize("sql,engine_accepts", _expectations_for_this_engine(),
                         ids=_ids(_expectations_for_this_engine()))
def test_grammar_rejects_exactly_what_the_older_engines_reject(sql, engine_accepts):
    """A 4.2-only construct must be rejected by both older grammars.

    This is the F18 claim stated as a test rather than a note: five statements the older
    engines reject, plus the `REPLACE ON` half of the rule split. It needs no engine, so
    it runs on all three legs and pins the grammar side of the divergence independently
    of whichever engine happens to be installed.
    """
    if engine_accepts:
        pytest.skip("4.2-only construct; nothing older is expected to reject it")
    for key in ("spark-4.1", "spark-3.5.1"):
        assert not _ours_for(key, sql), (
            f"{key} accepts {sql!r}, which Spark rejects. Accepting SQL the engine will "
            "not run is the direction that costs precision; see corpus.py."
        )


def _ours_for(key: str, sql: str) -> bool:
    try:
        return get_parser(key).parse(sql) is not None
    except SqlSyntaxError:
        return False