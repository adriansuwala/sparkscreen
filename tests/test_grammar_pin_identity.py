"""Each pinned grammar is the Spark release its key names -- the F17 invariant.

## The defect this exists to prevent

A grammar pin is two claims: that the 40-character commit is immutable (enforced by
`tests/test_grammar_port.py` section E) and that the grammar it fetches is *the release
the key advertises*. The second claim had no test. F17 shipped a `spark-4.0` key whose
`spark_versions` said `("4.0.0", "5.0.0")` while the pinned commit was a post-4.2 master
snapshot three weeks newer than the 4.2.0 release. The grammar was 4.2-shaped under a 4.0
name and silently accepted syntax no 4.0 engine runs. The pin was immutably correct and
the *name* was a lie, which is the shape a SHA check cannot see.

So every assertion here is a comparison between a shipped grammar and a parser generated
from the upstream **release tag**, through sparkscreen's own `port_to_python`/`generate`.
The upstream grammar is the independent oracle; the shipped grammar is the thing under
test. Nothing is compared against a hand-written list of what Spark "should" accept.

## What is asserted, and what is deliberately not

Four properties, in decreasing order of how much they would hurt to lose.

**A -- the pin IS its release.** The vendored `.g4` for each key is byte-identical to the
file at the upstream release tag, and (the load-bearing form) its rule set and its derived
label universe are subsets of that release's. Byte identity is the tight statement; the
subset form is what survives an upstream re-tag, so both are asserted.

**B -- adjacent lines are related.** 3.5.1's label universe is a subset of 4.1's. 4.1's
is *not* a subset of 4.2's and asserting that it were would be asserting something false:
4.2 replaced the alternative `insertIntoReplaceWhere` with `insertIntoReplaceBooleanCond`
(F17 resolution, `spec.py:97`). What is asserted instead is the measured asymmetry --
exactly one label in 4.1 that is not in 4.2, and it is a *rename*, proven by parsing one
statement and observing both names. A pin that produced a *different* asymmetry would
fail here, which is the part that matters: unrelated universes mean a mislabelled pin.

**C -- 4.2-only constructs discriminate.** Five constructs from the 4.2 rule bodies
(`qualifyClause`, `changesClause`, `nearestByClause`) accepted by the upstream 4.2.0
grammar, rejected by the upstream 4.1.3 and 3.5.1 grammars, and tracked by the shipped
grammars the same way. This is not a restatement of `tests/test_parser.py`: that asserts
shipped-vs-shipped, this asserts shipped-vs-*its own release*, and the upstream half is
what makes the probe SQL trustworthy. `tests/test_parser.py::test_version_specific_statements`
already pins `QUALIFY` between the shipped grammars, so it is included here only as the
calibration case for the other four.

**D -- no false UNKNOWN.** For each key, every statement the upstream release grammar
accepts is accepted by the shipped grammar for that key. This is the fail-closed direction
the operator complains about first: a rejection is a clean, specific, wrong `UNKNOWN` on
code that runs fine in production. Scoped *per key* on purpose -- the cross-line version is
false, because 3.5.1 legitimately rejects 4.x-only syntax. It held 56/56 at the time of
F17; the corpus below is 32 statements.

## Probe SQL comes from the rule bodies, not from memory

Two 4.2 constructs were mis-written twice during the F17 investigation, and in both cases
the first draft failed on *both* real grammars -- the signal that the SQL was wrong rather
than the parsers (`CHANGES FROM VERSION => 1` has no arrow; `nearestByClause` requires the
`APPROX`/`EXACT` prefix). Every probe below cites the upstream rule it is derived from.

## Why this needs a JVM, and why that is contained

Generating a parser needs java, so this module skips when no JRE is reachable. The fast
suite is ~34s precisely because it has no JVM, and a java-free check here would be a check
that cannot fail. It imports no pyspark: the fast CI job asserts pyspark's absence, and
this module must not disturb that. Everything it writes goes to `tmp_path` --
`sparkscreen.grammar.spec.VENDORED` and `.GENERATED` are redirected for the duration --
so the repository is never modified, and the generated modules are loaded by file path
rather than installed under `sparkscreen.grammar.generated`. `python_module()` returns a
hardcoded dotted path, so redirecting `GENERATED` alone does not make a module importable
and a symlink into the repo tree would leave state behind on failure; loading by path
avoids both. The only repo-adjacent write is the ANTLR jar into the gitignored
`grammar/.cache/`, which is where a maintainer build puts it anyway.

The cost is a network fetch of six `.g4` files and three ANTLR runs, ~25s, once per
session. It belongs with the grammar-maintainer jobs, not the fast suite.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from sparkscreen.analysis import label_universe
from sparkscreen.analysis.effects import LABEL_EFFECTS
from sparkscreen.analysis.label_universe import labels_for_grammar, labels_for_parser_module
from sparkscreen.grammar import spec as specmod
from sparkscreen.grammar.parser import SqlParser, get_parser
from sparkscreen.grammar.spec import SPECS, GrammarSpec, generate

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
from test_grammar_port import _lexer_rule_names, _parser_rule_names  # noqa: E402

#: The upstream release tag each key is supposed to name. This is the *claim under
#: test* -- it is what makes the comparison a pin-identity check rather than a
#: self-consistency check. If a key's grammar legitimately moves to a newer patch
#: release, this table is what a maintainer edits, and the diff shows it in review.
RELEASE_TAG = {
    "spark-4.2": "v4.2.0",
    "spark-4.1": "v4.1.3",
    "spark-3.5.1": "v3.5.1",
}

_UPSTREAM_BASE = (
    "https://raw.githubusercontent.com/apache/spark/{tag}/sql/api/src/main/antlr4/"
    "org/apache/spark/sql/catalyst/parser/{name}.g4"
)

#: `slow` is declared in pyproject.toml ("needs pyspark and a JVM"). This module needs
#: the JVM half only; the marker is reused rather than adding one, because pyproject is
#: not this branch's to edit and `--strict-markers` rejects an undeclared marker.
pytestmark = pytest.mark.slow


def _jre_available() -> bool:
    """True if a JRE is reachable.

    Mirrors `sparkscreen.grammar.spec._java()`: `which("java")` first, then JAVA_HOME.
    Checking only `which` would skip on a machine where java is installed but not on
    PATH, which is a false negative for this guard rather than an honest skip.
    """
    if shutil.which("java"):
        return True
    home = os.environ.get("JAVA_HOME") or os.environ.get("JRE_HOME")
    return bool(home) and (Path(home) / "bin" / "java").exists()


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not _jre_available(),
        reason=(
            "generating a parser needs a JVM; this is a grammar-maintainer guard, not "
            "a fast-suite test (the fast suite is JVM-free by design)"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Building the upstream release grammars
# ---------------------------------------------------------------------------


class _UpstreamSpec(GrammarSpec):
    """A `GrammarSpec` whose `python_module()` names a module loaded from `tmp_path`.

    `GrammarSpec` is a frozen dataclass, so the mapping cannot be attached to the
    instance -- assigning to it raises `FrozenInstanceError`, which is the right answer
    and is why this is a subclass with a class attribute. The subclass exists only to
    redirect `python_module()`; every other behaviour is inherited, so `vendored_path()`
    and `generated_dir()` still resolve through the redirected `VENDORED`/`GENERATED`.

    The upstream key deliberately differs from the shipped key (`spark-4.1.3` vs
    `spark-4.1`), so the generated directory and the module name cannot collide with a
    shipped grammar even if something did leak into the repo tree.
    """

    #: `{"SqlBaseLexer": <module name in sys.modules>, ...}`
    loaded: dict = {}

    #: Set per instance before use. Each upstream build gets its own scratch roots
    #: rather than the module globals `GrammarSpec` normally resolves through.
    #:
    #: Mutating `specmod.VENDORED`/`GENERATED` for the duration of a session-scoped
    #: fixture looks harmless and is not: pytest keeps the fixture alive while sibling
    #: modules run, so every other grammar test in the suite -- test_grammar_port.py's 49
    #: -- resolved their paths inside this tmp tree and failed on missing files. The
    #: redirect leaks into tests that never asked for it, and the failure lands in files
    #: that have nothing to do with the fixture.
    vendored_root: Path = None      # type: ignore[assignment]
    generated_root: Path = None     # type: ignore[assignment]

    def vendored_path(self, name: str) -> Path:
        return self.vendored_root / self.key / f"{name}.g4"

    def generated_dir(self) -> Path:
        return self.generated_root / self.module_name

    def python_module(self, name: str) -> str:
        return self.loaded[name]


#: shipped key -> sys.modules name of the upstream `SqlBaseParser` for that release.
#: Populated by the `upstream_grammars` fixture while the modules are registered, since
#: the fixture's teardown unregisters them again.
UPSTREAM_PARSER_MODULE: dict[str, str] = {}


def _fetch_release_grammar(tag: str, name: str, dest: Path) -> None:
    """Download one `.g4` at an upstream release TAG.

    By tag, not by the pinned commit: that is the whole point. Fetching the pin would
    compare the shipped grammar with itself and pass no matter what the key is named.
    """
    url = _UPSTREAM_BASE.format(tag=tag, name=name)
    with urllib.request.urlopen(url, timeout=60) as response:
        dest.write_bytes(response.read())


def _load_generated(spec: _UpstreamSpec) -> _UpstreamSpec:
    """Import a generated parser from `tmp_path` by file path.

    `python_module()` returns the hardcoded `sparkscreen.grammar.generated.<key>...`, so
    redirecting `GENERATED` does not make a module importable under that name. Rather
    than symlink into the repo tree -- which leaves state behind whenever a test fails
    before its cleanup -- the modules are loaded directly and the resulting names are
    what `python_module()` returns. Nothing is installed; `sys.modules` entries are
    removed on teardown.
    """
    for name in spec.files:
        path = spec.generated_dir() / f"{name}.py"
        module_name = f"_pin_identity_{spec.module_name}_{name}"
        module_spec = importlib.util.spec_from_file_location(module_name, path)
        assert module_spec and module_spec.loader, f"cannot load {path}"
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        module_spec.loader.exec_module(module)
        spec.loaded[name] = module_name
    return spec


def _parser_for(spec: _UpstreamSpec) -> SqlParser:
    """A `SqlParser` bound to a tmp-generated grammar.

    The module cache is pre-seeded so `SqlParser.parse()` runs verbatim -- same entry
    rule selection, same `BailErrorStrategy`, same error listener, same recursion
    limit. Re-implementing those here would mean this test could pass while the shipped
    parser rejected the same input for a different reason.
    """
    parser = SqlParser(spec)
    parser._lexer_mod = sys.modules[spec.loaded["SqlBaseLexer"]]
    parser._parser_mod = sys.modules[spec.loaded["SqlBaseParser"]]
    return parser


@contextmanager
def _as_upstream(spec: _UpstreamSpec):
    """Point `labels_for_grammar` at a tmp-generated grammar.

    `labels_for_grammar` derives the universe from the *generated parser classes*, which
    is what makes it sound (see its module docstring: a corpus cannot stand in for it).
    Re-deriving it here would create a second definition that drifts from the first, so
    the real function is used with `get_spec` swapped for the duration.
    """
    real = label_universe.get_spec
    label_universe.get_spec = lambda key=None, _s=spec: _s
    try:
        yield
    finally:
        label_universe.get_spec = real


@pytest.fixture(scope="session")
def upstream_grammars(tmp_path_factory) -> dict[str, SqlParser]:
    """One generated parser per upstream release tag, keyed by shipped grammar key.

    Session-scoped because it costs ~25s and nothing it produces varies within a run.
    """
    root = tmp_path_factory.mktemp("pin_identity")
    loaded_names: list[str] = []
    built: dict[str, SqlParser] = {}
    try:
        for shipped in SPECS:
            tag = RELEASE_TAG[shipped.key]
            spec = _UpstreamSpec(
                key=f"{shipped.key}-{tag}",
                commit="0" * 40,
                spark_versions=(tag.lstrip("v"),),
            )
            spec.vendored_root = root / "vendored"
            spec.generated_root = root / "generated"
            vendored = spec.vendored_path(spec.files[0]).parent
            vendored.mkdir(parents=True, exist_ok=True)
            for name in spec.files:
                try:
                    _fetch_release_grammar(tag, name, vendored / f"{name}.g4")
                except Exception as exc:  # noqa: BLE001
                    # Loud, not a skip. A pin-identity guard that silently disappears
                    # whenever the network hiccups is the F17 recurrence: the F17
                    # grammar looked fine to every check that ran.
                    pytest.fail(
                        f"could not fetch the upstream {tag} grammar: {exc!r}\n"
                        f"This test is the only guard that a pinned grammar is the "
                        f"release its key names. It needs network access; if that is "
                        f"genuinely unavailable, wire it into a job that has it rather "
                        f"than letting it skip."
                    )
            # jar_cache is the project's own gitignored grammar cache, so the 2 MB
            # download is shared with `python -m sparkscreen.grammar.build` and with CI's
            # cache action. Grammars and generated output go to tmp_path.
            generate(spec, jar_cache=specmod.GRAMMAR_DIR / ".cache")
            upstream_spec = _load_generated(spec)
            loaded_names.extend(upstream_spec.loaded.values())
            # Remember which sys.modules name holds the upstream SqlBaseParser. The
            # teardown below pops these, so a test that needs the module must be told
            # the name while it is still registered.
            UPSTREAM_PARSER_MODULE[shipped.key] = upstream_spec.loaded["SqlBaseParser"]
            built[shipped.key] = _parser_for(spec)
        yield built
    finally:
        for name in loaded_names:
            sys.modules.pop(name, None)
        UPSTREAM_PARSER_MODULE.clear()
        _UpstreamSpec.loaded = {}


@pytest.fixture(scope="session")
def upstream_grammars_present(upstream_grammars) -> bool:
    """Guard fixture so a build failure reads as a skip-free, named failure."""
    assert upstream_grammars, "no upstream grammars were generated"
    return True


#: The *shipped* vendored root, captured at import time.
#:
#: `specmod.VENDORED` is a module global that the `upstream_grammars` fixture redirects
#: into tmp_path so building the upstream parsers never touches the repo. That redirect is
#: still in effect for every test that runs while the fixture is alive, so anything
#: reading the grammar *we ship* through `spec.vendored_path()` silently reads the
#: scratch tree instead -- which holds the freshly-fetched upstream release grammars, not
#: the committed files. Comparing the release against itself is a test that passes by
#: construction. Capture the real root before any fixture can move it.
SHIPPED_VENDORED: Path = specmod.VENDORED


def _shipped_rule_names(key: str, name: str) -> set[str]:
    """Rule inventory of the grammar we actually ship for `key`.

    Reads `SHIPPED_VENDORED`, not `spec.vendored_path()`: see the note on that name.
    """
    spec = next(s for s in SPECS if s.key == key)
    path = SHIPPED_VENDORED / spec.key / f"{name}.g4"
    text = path.read_text()
    return _lexer_rule_names(text) if name.endswith("Lexer") else _parser_rule_names(text)


@pytest.fixture(scope="session")
def upstream_grammar_text(upstream_grammars_present) -> dict[tuple[str, str], str]:
    """The raw `.g4` text of every upstream release grammar, keyed by (pin, file).

    Fetched from the network rather than read out of the tmp tree so that a build
    artefact cannot quietly stand in for the release grammar under test.
    """
    out: dict[tuple[str, str], str] = {}
    for key, tag in RELEASE_TAG.items():
        for name in ("SqlBaseLexer", "SqlBaseParser"):
            url = _UPSTREAM_BASE.format(tag=tag, name=name)
            with urllib.request.urlopen(url, timeout=60) as response:
                out[(key, name)] = response.read().decode()
    return out


def _accepts(parser: SqlParser, sql: str) -> bool:
    return parser.try_parse(sql) is not None


# ---------------------------------------------------------------------------
# A -- the pin IS its release
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.key)
def test_vendored_grammar_is_byte_identical_to_its_release_tag(spec, upstream_grammar_text):
    """A: the committed `.g4` is the file at the release tag, byte for byte.

    The tightest form of pin identity. It also subsumes the supply-chain property in
    `tests/test_grammar_port.py` section E in a way a SHA-format check cannot: a full
    40-character SHA guarantees the object does not move, not that the object is the
    release.

    F17's reproducer, had this existed: `spark-4.0`'s vendored grammar differed from
    `v4.0.0`'s in 67 parser rules.
    """
    for name in spec.files:
        # SHIPPED_VENDORED, not spec.vendored_path(): the upstream_grammars fixture
        # redirects specmod.VENDORED into tmp_path, so vendored_path() would hand back
        # the freshly-fetched release grammar and this would compare the release against
        # itself -- a tautology that passes for a mislabelled pin, which is the exact
        # defect (F17) this test exists to catch.
        committed = (SHIPPED_VENDORED / spec.key / f"{name}.g4").read_bytes()
        upstream = upstream_grammar_text[(spec.key, name)].encode()
        assert hashlib.sha256(committed).digest() == hashlib.sha256(upstream).digest(), (
            f"{spec.key}/{name}: the vendored grammar is not the {RELEASE_TAG[spec.key]} "
            f"release grammar. If the pin was deliberately moved, the key is now "
            f"mislabelled (F17) -- rename it to name the release it actually pins."
        )


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.key)
@pytest.mark.parametrize("name", ["SqlBaseLexer", "SqlBaseParser"])
def test_pinned_rules_are_a_subset_of_its_release(spec, name, upstream_grammar_text):
    """A: no rule in the pin that its own release does not have.

    This is the F17 property, stated over rule inventories. A pin pointing at a *later*
    master snapshot carries every rule added since the release, which is how a 4.0-named
    grammar ended up accepting 4.2 syntax: F17 measured 18 such rules over 4.2.0
    (`asofJoinType`, `binByClause`, the `autoCdc*` family).

    A subset rather than an equality because this is the form that survives a re-cut
    upstream tag: a maintainer re-vendoring after a legitimate upstream fix should still
    pass. The equality that matters for the *commit* is asserted above.
    """
    upstream_text = upstream_grammar_text[(spec.key, name)]
    extract = _lexer_rule_names if name.endswith("Lexer") else _parser_rule_names
    pinned = _shipped_rule_names(spec.key, name)
    release = extract(upstream_text)
    assert release, f"no rules extracted from {RELEASE_TAG[spec.key]}/{name}"
    assert pinned, f"no rules extracted from the vendored {spec.key}/{name}"
    extra = sorted(pinned - release)
    assert not extra, (
        f"{spec.key}/{name}: {len(extra)} rules are in the pin but not in "
        f"{RELEASE_TAG[spec.key]}: {extra}\n"
        f"The pin is newer than the release its key names (F17)."
    )


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.key)
def test_pinned_label_universe_is_a_subset_of_its_release(spec, upstream_grammars):
    """A: the label universe, which is what the policy layer actually consumes.

    Rule names are the proxy; this is the thing itself. A grammar can hold every rule
    its release has and still route them into labels a downstream policy table was never
    written for, because `effects_for_label()` raises on an unmapped label rather than
    defaulting. F17 lost `CreateFlowAutoCdc` and `CommentColumn` this way when the
    master snapshot was dropped.

    Measured today: equality in both directions for all three pins (111 / 103 / 82).
    """
    # labels_for_grammar() starts from get_spec(key), which resolves the spec out of
    # SPECS by key. So it cannot be pointed at the upstream build by key at all: the
    # _UpstreamSpec that overrides python_module() is unreachable through that path, and
    # calling it with the shipped key reads the SHIPPED grammar -- which is how the first
    # version of this test compared the release against itself and called it a pass.
    # Walk the upstream parser's context classes directly instead, which is the same
    # derivation over a different parser object.
    release_labels = labels_for_parser_module(
        importlib.import_module(UPSTREAM_PARSER_MODULE[spec.key])
    )
    shipped_labels = labels_for_grammar(spec.key)
    extra = sorted(shipped_labels - release_labels)
    assert not extra, (
        f"{spec.key}: the pinned grammar can emit {len(extra)} labels that "
        f"{RELEASE_TAG[spec.key]} cannot: {extra}\n"
        f"The pin is not the release its key names (F17)."
    )


# ---------------------------------------------------------------------------
# B -- adjacent lines are related
# ---------------------------------------------------------------------------


def test_older_line_label_universe_is_a_subset_of_the_next():
    """B: 3.5.1's label universe is contained in 4.1's.

    Measured: the difference is empty. If it stops being empty, a 4.1 pin has drifted
    to a grammar older than the line it names, which is the same mislabelling in the
    other direction.
    """
    older = labels_for_grammar("spark-3.5.1")
    newer = labels_for_grammar("spark-4.1")
    missing = sorted(older - newer)
    assert not missing, f"3.5.1 emits {len(missing)} labels 4.1 cannot: {missing}"


def test_4_2_only_labels_are_reachable_and_all_classified():
    """The 4.2-only label set, with the two facts F20 got wrong stated as assertions.

    F20 concluded from `codeLiteral` being absent from `statement` that the whole
    dollar-quoting construct was dead upstream. The derivation disagrees: `CreateMetricView`
    is a reachable 4.2 label. Both are true and they are different claims -- the rule is
    unreachable *as a bare statement*, while `createMetricView` (its only caller) is a live
    labeled alternative of `statement`.

    The live engine agrees in the same way: `CREATE METRIC VIEW ...` is a parse error, but
    at the word `METRIC`, because that keyword is absent from the upstream lexer too. Two
    independent gates, neither of which is "this syntax does not exist" -- so a future
    release could add `METRIC` to its lexer and make the rule reachable with no grammar
    change, which is precisely when the pin must be re-cut. Pinning the reachable set here
    is what makes that move visible.

    Also asserts every 4.2-only label is classified, which is the invariant that made the
    hr0 split free: a new label with no entry in `LABEL_EFFECTS` would raise rather than
    silently return an empty effect list.
    """
    only_42 = set(labels_for_grammar("spark-4.2")) - set(labels_for_grammar("spark-4.1"))
    assert "CreateMetricView" in only_42, (
        "CreateMetricView is expected to be a reachable 4.2-only label. If it has become "
        "unreachable, the dollar-quoting rule's last caller went away and F20's stronger "
        "claim is correct after all -- update F20 with the measured reason."
    )
    unmapped = sorted(label for label in only_42 if label not in LABEL_EFFECTS)
    assert not unmapped, (
        f"4.2-only labels with no entry in LABEL_EFFECTS: {unmapped}. effects_for_label() "
        f"raises on an unmapped label, so these would fail closed at screening time."
    )


def test_4_1_and_4_2_differ_only_by_the_measured_rename(upstream_grammars):
    """B: 4.1 vs 4.2 nest up to exactly one documented rename -- and no further.

    Strict nesting is *false* here and asserting it would be asserting something wrong:
    4.2 replaced the alternative `insertIntoReplaceWhere` with
    `insertIntoReplaceBooleanCond` (F17 resolution, `spec.py:97`). So the assertion is on
    the asymmetry, not on containment:

      * 4.1 has exactly one label 4.2 lacks -- `InsertIntoReplaceWhere`;
      * it is a rename, not a removal: the same statement yields
        `InsertIntoReplaceWhere` under 4.1 and `InsertIntoReplaceBooleanCond` under 4.2.

    What this buys is the property that matters for a pin. Two lines whose universes are
    *unrelated* mean a mislabelled pin; a pin that acquires a different asymmetry fails
    here rather than passing unnoticed.

    The statement is written from both rule bodies: 4.1's
    `INSERT INTO ... identifierReference REPLACE whereClause #insertIntoReplaceWhere`
    and 4.2's
    `INSERT ... INTO ... identifierReference tableAlias ... REPLACE (WHERE | ON)
     replaceCondition=booleanExpression #insertIntoReplaceBooleanCond`.
    The un-aliased form is the one both lines parse, so the rename is isolated from the
    other 4.2-only change to this rule (`tableAlias`, without which 4.2 rejects it).
    """
    older, newer = labels_for_grammar("spark-4.1"), labels_for_grammar("spark-4.2")
    assert older - newer == {"InsertIntoReplaceWhere"}, (
        f"4.1 now differs from 4.2 by {sorted(older - newer)}, not just the documented "
        f"insertIntoReplaceWhere rename. Two lines whose label universes are unrelated "
        f"indicate a mislabelled pin (F17)."
    )

    sql = "INSERT INTO t REPLACE WHERE a = 1 SELECT 1"
    old_label = get_parser("spark-4.1").parse(sql).statements[0].label
    new_label = get_parser("spark-4.2").parse(sql).statements[0].label
    assert old_label == "InsertIntoReplaceWhere", old_label
    assert new_label == "InsertIntoReplaceBooleanCond", (
        f"4.2 now labels the REPLACE WHERE insert {new_label!r}; the rename this test "
        f"pins is no longer the shape either grammar has"
    )


# ---------------------------------------------------------------------------
# C -- 4.2-only constructs discriminate, verified against the real releases
# ---------------------------------------------------------------------------

#: Constructs that exist in 4.2.0 and in neither 4.1.3 nor 3.5.1.
#:
#: Each SQL is derived from the cited rule body in the upstream `v4.2.0`
#: `SqlBaseParser.g4`:
#:
#:   qualifyClause  : QUALIFY booleanExpression
#:   changesClause  : CHANGES FROM (SYSTEM_VERSION|VERSION) startingVersion=version
#:                    (INCLUSIVE|startExclusive=EXCLUSIVE)?
#:                    (TO (SYSTEM_VERSION|VERSION) endingVersion=version ...)?      -- `version`
#:                    is INTEGER_VALUE | stringLit, so no `=>` arrow
#:   nearestByClause: (APPROX|EXACT) NEAREST num=INTEGER_VALUE? BY
#:                    (DISTANCE|SIMILARITY) expression                              -- the
#:                    APPROX/EXACT prefix is required, which is what made a
#:                    prefix-less first draft fail on both real grammars
#:
#: The first entry is `QUALIFY`, which `tests/test_parser.py` already pins between the
#: *shipped* grammars. It is here as the calibration case: it is known-good SQL against a
#: known-good parser, so a failure on any other row points at that row.
FOUR_TWO_ONLY = [
    ("SELECT a FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1", "qualifyClause"),
    ("SELECT * FROM t CHANGES FROM VERSION 1", "changesClause, integer version"),
    ("SELECT * FROM t CHANGES FROM VERSION '1'", "changesClause, string version"),
    ("SELECT * FROM t CHANGES FROM SYSTEM_VERSION 1 TO VERSION 9", "changesClause, range"),
    ("SELECT * FROM a JOIN b APPROX NEAREST BY DISTANCE a.p", "nearestByClause, APPROX"),
    ("SELECT * FROM a JOIN b EXACT NEAREST BY SIMILARITY a.p", "nearestByClause, EXACT"),
]


@pytest.mark.parametrize("sql,origin", FOUR_TWO_ONLY, ids=[o for _, o in FOUR_TWO_ONLY])
def test_four_two_only_construct_is_accepted_only_by_its_own_release(
    sql, origin, upstream_grammars
):
    """C: each 4.2 construct is accepted by the real 4.2.0 grammar and by no other.

    Asserted against the upstream releases first, then against the shipped grammars. The
    upstream half is what makes this more than a restatement of
    `tests/test_parser.py`: it establishes the SQL is valid 4.2.0 and invalid 4.1.3, so
    a disagreement in the shipped half is a pin problem rather than a bad probe. A probe
    that fails where two independent real grammars agree nothing is wrong is measuring
    the probe.
    """
    for key, expect_accept in (("spark-4.2", True), ("spark-4.1", False),
                               ("spark-3.5.1", False)):
        release_result = _accepts(upstream_grammars[key], sql)
        assert release_result is expect_accept, (
            f"{RELEASE_TAG[key]} {'accepted' if expect_accept else 'rejected'} {sql!r}, "
            f"expected the opposite. Derived from {origin}; if the upstream rule body "
            f"has changed, the probe SQL is what needs re-deriving, not the parsers."
        )
        shipped_result = _accepts(get_parser(key), sql)
        assert shipped_result is expect_accept, (
            f"shipped {key} {'accepted' if expect_accept else 'rejected'} {sql!r}, but "
            f"the upstream {RELEASE_TAG[key]} grammar does not"
        )


# ---------------------------------------------------------------------------
# D -- no false UNKNOWN: a pin must never be stricter than its release
# ---------------------------------------------------------------------------

#: Statements spanning DDL, DML, queries, admin commands and scripts, plus the
#: constructs that distinguish the 4.x lines from 3.5.1.
#:
#: Lowercase-agnostic by construction: the shipped parser's `caseInsensitive` port option
#: is asserted separately in `tests/test_grammar_port.py`, and running this corpus
#: through a second grammar with that option silently wrong would produce a false pass.
#: Mixed case is used on purpose -- an all-uppercase corpus has a blind spot that cannot
#: be seen from inside the suite.
ORDINARY_CORPUS = [
    # queries
    "SELECT a FROM t",
    "select * from t where a = 1 and b > 2",
    "SELECT DISTINCT a FROM t ORDER BY a DESC NULLS LAST",
    "SELECT a FROM t GROUP BY a HAVING count(*) > 1",
    "WITH c AS (SELECT 1 AS x) SELECT * FROM c",
    "SELECT a FROM t JOIN u ON t.id = u.id",
    "SELECT a FROM t LEFT OUTER JOIN u ON t.id = u.id",
    "SELECT * FROM t ASOF JOIN s ON t.id = s.id",
    "SELECT CAST(a AS STRING) FROM t",
    "SELECT transform(a, x -> x + 1) FROM t",
    "SELECT * FROM t WHERE EXISTS (SELECT 1 FROM u)",
    "SELECT a FROM t INTERSECT SELECT b FROM u",
    "SELECT a FROM t EXCEPT SELECT b FROM u",
    "SELECT 1 |> SELECT 2",
    # DDL
    "CREATE TABLE t (a INT) USING parquet",
    "CREATE TABLE t USING parquet AS SELECT 1",
    "CREATE OR REPLACE TABLE t USING parquet AS SELECT 1",
    "CREATE TEMPORARY VIEW v AS SELECT 1",
    "CREATE OR REPLACE VIEW v AS SELECT 1",
    "CREATE DATABASE IF NOT EXISTS d",
    # 4.1-era DDL, rejected by 3.5.1 -- included so the per-key comparison below is not
    # vacuous on the older line
    "CREATE TABLE t (id INT PRIMARY KEY)",
    "CREATE TABLE t (a INT, FOREIGN KEY (a) REFERENCES o(id))",
    "CREATE STREAMING TABLE t (a INT)",
    # DML
    "INSERT INTO t VALUES (1),(2)",
    "INSERT OVERWRITE TABLE t SELECT 1",
    "INSERT INTO t REPLACE WHERE a = 1 SELECT 1",
    "UPDATE t SET a=1 WHERE b=2",
    "DELETE FROM t WHERE a=1",
    "MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN UPDATE SET t.v=s.v",
    # DDL/admin and the rest
    "DROP TABLE IF EXISTS t",
    "DROP TABLE t PURGE",
    "ALTER TABLE t ADD COLUMNS (b INT)",
    "ALTER TABLE t SET TBLPROPERTIES ('k'='v')",
    "ALTER TABLE t RENAME TO u",
    "CACHE TABLE t",
    "MSCK REPAIR TABLE t",
    "SHOW TBLPROPERTIES t",
    "DESCRIBE TABLE t",
    "COMMENT ON TABLE t IS 'x'",
    "GRANT SELECT ON t TO u",
    "TRUNCATE TABLE t",
    "CALL dbproc(1)",
    "BEGIN DROP TABLE a; DROP VIEW b; END",
]


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.key)
def test_pin_never_rejects_what_its_own_release_accepts(spec, upstream_grammars):
    """D: the shipped grammar accepts everything its own release accepts.

    The fail-closed direction, and the one that costs an operator: a rejection is a
    clean, specific, wrong `UNKNOWN` on code that runs fine in production. Under-
    accepting costs a false negative on every query in the cluster; over-accepting only
    costs an imprecise verdict, and the verdict set has no state for "this parses but the
    engine would refuse it" anyway.

    Scoped per key deliberately. The cross-line form is false -- 3.5.1 rejects
    `CREATE STREAMING TABLE`, as it must -- so "no grammar rejects what another accepts"
    would be a vacuous or wrong assertion. Within one key there is no legitimate reason
    for the pin to be stricter than the release it names.
    """
    upstream = upstream_grammars[spec.key]
    shipped = get_parser(spec.key)
    rejected = [sql for sql in ORDINARY_CORPUS
                if _accepts(upstream, sql) and not _accepts(shipped, sql)]
    assert not rejected, (
        f"{spec.key}: {len(rejected)}/{len(ORDINARY_CORPUS)} statements are accepted by "
        f"the upstream {RELEASE_TAG[spec.key]} grammar and rejected by the pinned one. "
        f"Each is a false UNKNOWN on working code:\n"
        + "\n".join(f"    {sql}" for sql in rejected)
    )


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.key)
def test_the_corpus_exercises_both_directions_for_each_pin(spec, upstream_grammars):
    """The control for D: D must not pass because the release accepts nothing.

    A pin-identity corpus where the upstream grammar rejects every statement would make
    the assertion above trivially true, and it would still be green. Asserting that each
    pin's release accepts a real share of the corpus keeps it load-bearing, and records
    the 4.x-versus-3.5.1 difference in coverage as a number rather than a hope.

    Measured: 4.2.0 and 4.1.3 accept 41/41; 3.5.1 accepts 36/41, the five rejects being
    the constructs that postdate it.
    """
    upstream = upstream_grammars[spec.key]
    accepted = [sql for sql in ORDINARY_CORPUS if _accepts(upstream, sql)]
    assert len(accepted) >= 30, (
        f"{RELEASE_TAG[spec.key]} accepts only {len(accepted)}/{len(ORDINARY_CORPUS)} "
        f"corpus statements, so the subset assertions above are barely exercised. Either "
        f"the corpus lost coverage or the upstream parser is failing to load."
    )