"""Which pinned grammar corresponds to the *running* Spark engine.

The differential suite is matrixed over three real engines (3.5.1, 4.1.3, 4.2.0) in CI
-- see the `differential` job in .github/workflows/ci.yml. Until that matrix existed
every helper here picked a grammar with a two-way ternary:

    "spark-3.5.1" if pyspark.__version__.startswith("3.5") else "spark-4.2"

That is wrong in the middle of the matrix and wrong *silently*, which is the failure mode
this module exists to remove. Under pyspark 4.1.3 it returned `spark-4.2`: the suite
would have compared the 4.1 engine against the 4.2 grammar and reported agreement on a
comparison nobody asked for. Three engines, two branches -- the middle one had to land
somewhere and it landed on the newest.

`grammar_key_for_engine()` resolves by *measured* engine version, and raises on an
unmapped version rather than defaulting. An unmapped engine means a Spark release the
recorded expectations were never checked against, which is exactly the situation where
guessing a grammar is most expensive and least honest.

This module deliberately does not import pyspark at module scope: the fast suite
collects `tests/differential/` and must stay importorskip-gated and inert. It takes the
version string as an argument, so the mapping is testable with no engine present.
"""
from __future__ import annotations

#: Exact engine version -> pinned grammar key. Both sides are exact: the engine version is
#: the pyspark release CI installs, and the key is a pin in `sparkscreen.grammar.spec`.
#:
#: These must agree with `SPECS`; `test_engine_matrix_matches_the_pinned_grammars` below
#: asserts that rather than trusting the table to stay in step.
ENGINE_TO_GRAMMAR: dict[str, str] = {
    "3.5.1": "spark-3.5.1",
    "4.1.3": "spark-4.1",
    "4.2.0": "spark-4.2",
}


class UnmappedEngineError(LookupError):
    """No pinned grammar is recorded for this Spark release.

    Raised rather than falling back to the newest grammar. Falling back is the F17 defect
    in a new place: a caller asking to be checked against 4.0 must not silently be handed
    4.2 syntax, and here a caller on an unrecorded release must not be handed a grammar
    whose behaviour nobody has compared to their engine.
    """


def grammar_key_for_engine(version: str | None) -> str:
    """The pinned grammar key for an exact pyspark version string.

    Exact match only -- no prefix fallback. `4.1.0` is a real release (see
    `SPECS.spark_versions`) but its expectations were never recorded, so it raises; that
    is the point. Widening this to a prefix match would reintroduce the two-way ternary's
    bug in a place that looks deliberate.
    """
    if version is None:
        raise UnmappedEngineError(
            "cannot resolve a grammar key without a pyspark version string"
        )
    try:
        return ENGINE_TO_GRAMMAR[version]
    except KeyError:
        raise UnmappedEngineError(
            f"pyspark {version} has no recorded differential expectations; "
            f"known engines are {sorted(ENGINE_TO_GRAMMAR)}. Add the version to "
            "ENGINE_TO_GRAMMAR and record its behaviour in corpus.py before using it, "
            "rather than letting it borrow another release's grammar."
        ) from None


def engine_version() -> str:
    """The running pyspark's version, resolved through the mapping above.

    Importing pyspark here (rather than at module scope) keeps the fast suite's
    collection path free of it.
    """
    import pyspark

    version = pyspark.__version__
    # Fail here, at the single entry point, rather than at each call site -- a caller that
    # never asks for the key is unaffected by an engine it has no expectations for.
    grammar_key_for_engine(version)
    return version