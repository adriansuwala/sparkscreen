"""The Java -> Python grammar port, and the supply-chain properties around it.

`port_grammar` is the only thing standing between Spark's Java-target ANTLR grammars
and a Python parser. ANTLR's Python3 target copies `@header`/`@members` and inline
actions *verbatim* into the generated module, so any Java that survives the port is
Java that ends up in shipped Python. The tests here are organised around the ways
that can go wrong:

  * A -- the port is TOTAL and DETERMINISTIC on the real vendored grammars, and emits
    `options { caseInsensitive = true; }` for the lexer. That last one is
    correctness-critical with history: the original port omitted it, both grammars
    rejected `select 1`, and real Spark accepts it (see port.py's inline note and
    tests/test_parser.py). A pass/fail on that line is the difference between a
    screener that works and one that reports UNKNOWN on all lowercase SQL.
  * B -- NO JAVA LEAKS into the ported text. Scoped precisely: comments and ANTLR
    string literals are stripped first, because `->` legitimately appears as the
    lexer `-> channel(HIDDEN)` command and as the ARROW token literal, and `getText()`
    legitimately appears in an explanatory comment in our own generated members.
  * C -- TOKEN AND RULE PRESERVATION: the ported grammar declares exactly the rules
    the vendored one did. This is the invariant that makes the generated parser the
    real Spark grammar rather than a mangled one.
  * D -- the port FAILS CLOSED. Unknown Java must raise `PortError`, never be emitted.
  * E -- spec.py integrity. The pinned commit is a supply-chain claim; a short SHA or
    a movable tag breaks the guarantee the module docstring makes in writing.
  * F -- the committed parsers import and parse with no JVM present.

Known source bugs are recorded as non-strict xfails at the bottom of this file with a
reproducer, following the convention in tests/test_folding.py: the assertion stays in
the suite at full strength, and a fix turns it green without anyone rewriting the
expectation. Nothing under src/ is modified by these tests.
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from sparkscreen.grammar.port import (
    LEXER_MEMBERS,
    PARSER_MEMBERS,
    PIPE_START_TOKENS,
    PortError,
    PortResult,
    port_grammar,
    verify_tokens_renamed,
)
from sparkscreen.grammar.spec import GENERATED, SPECS, GrammarSpec, port_to_python

REPO = Path(__file__).resolve().parents[1]

# --- text utilities ----------------------------------------------------------
# The port works on raw grammar text, so the same three things have to come off
# before any assertion about "is this Java?" is meaningful: line comments, block
# comments (the Apache license header is one), and ANTLR string literals such as
# `ARROW: '->';`. Python `#` comments are stripped too because the members blocks
# the port *injects* are Python and carry explanatory prose.

_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/|#[^\n]*", re.S)
_LITERALS = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", re.S)


def _code_only(text: str) -> str:
    """Strip comments and string literals so only grammar code remains."""
    return _LITERALS.sub("''", _COMMENTS.sub("", text))


def _lexer_rule_names(text: str) -> set[str]:
    """Lexer rule / fragment names: `NAME : ...` or `fragment NAME : ...`."""
    return set(re.findall(r"^(?:fragment\s+)?([A-Z_][A-Z_0-9]*)\s*:", text, re.M))


def _parser_rule_names(text: str) -> set[str]:
    """Parser rule names: a lowerCamelCase name alone on a line before its `:`."""
    return set(re.findall(r"^([a-z][A-Za-z_0-9]*)\s*\n\s*:", text, re.M))


def _port(spec: GrammarSpec) -> dict[str, PortResult]:
    """Port both vendored grammars of `spec` -> {name: PortResult}."""
    out: dict[str, PortResult] = {}
    texts = {n: spec.vendored_path(n).read_text() for n in spec.files}
    tokens = verify_tokens_renamed(texts["SqlBaseLexer"], texts["SqlBaseParser"])
    for name in spec.files:
        out[name] = port_grammar(
            texts[name],
            is_lexer=name.endswith("Lexer"),
            pipe_start_tokens=tuple(sorted(tokens)),
        )
    return out


# ===========================================================================
# A -- totality, determinism, case-insensitivity
# ===========================================================================


@pytest.mark.parametrize("name", ["SqlBaseLexer", "SqlBaseParser"])
def test_port_does_not_raise_on_vendored_grammars(spec_key, name):
    """A: the port must handle every vendored grammar of every pinned Spark line.

    A raise here means the committed parsers were built by a version of the port
    that no longer matches the vendored input, i.e. the committed build is stale.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    results = _port(spec)
    assert name in results
    assert results[name].text


def test_ported_grammar_still_declares_its_grammar(spec_key):
    """A: the port emits a real `lexer grammar X;` / `parser grammar X;` declaration.

    The preamble is reconstructed from scratch, so a lost declaration would produce
    text that reads fine but tells ANTLR nothing about what it is looking at.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    for name, result in _port(spec).items():
        keyword = "lexer" if name.endswith("Lexer") else "parser"
        assert f"{keyword} grammar {name};" in result.text, name


@pytest.mark.parametrize("name", ["SqlBaseLexer", "SqlBaseParser"])
def test_port_is_idempotent_across_runs(spec_key, name):
    """A: porting the same input twice must produce byte-identical output.

    The generated parsers are committed and CI checks the committed output matches a
    fresh build, so the port has to be a pure function. Any dependence on dict
    ordering, a set iteration, or a clock would make CI fail at random.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    src = spec.vendored_path(name).read_text()
    is_lexer = name.endswith("Lexer")
    first = port_grammar(src, is_lexer=is_lexer, pipe_start_tokens=PIPE_START_TOKENS[:4])
    second = port_grammar(src, is_lexer=is_lexer, pipe_start_tokens=PIPE_START_TOKENS[:4])
    assert first.text == second.text
    # and the rewrite bookkeeping must be stable too, not just the text
    assert first.applied == second.applied
    assert first.unused_rules == second.unused_rules


def test_committed_generated_grammar_matches_a_fresh_port(spec_key):
    """A: the committed ported .g4 in generated/ equals what port_grammar produces now.

    This is the reproducibility guarantee stated in spec.py's docstring, checked
    against the artefact that actually ships.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    import tempfile

    for name, result in _port(spec).items():
        committed = spec.generated_dir() / f"{name}.g4"
        assert committed.exists(), f"missing {committed}"
        assert committed.read_text() == result.text, (
            f"{spec.key}/{name}: committed ported grammar is stale relative to port.py"
        )


@pytest.mark.parametrize("key", [s.key for s in SPECS])
def test_ported_lexer_enables_case_insensitivity(key):
    """A: the ported LEXER must declare `options { caseInsensitive = true; }`.

    This is correctness-critical with history. The original port omitted it; both
    grammars spell keywords in uppercase and 3.5.1 declares `fragment LETTER :
    [A-Z]`, so every lower-case statement -- `select 1`, `drop table t`, `SeLeCt 1`
    -- was rejected while real Spark accepted all of them. Lowercase SQL is the
    common case in agent-written PySpark, so the omission made the screener useless
    in exactly the situation it exists for, and it did so while the test suite (which
    was written in uppercase) stayed green.

    Asserted on both grammars: a fix that adds the option to only one of them is
    still a bug, just a version-specific one.
    """
    spec = next(s for s in SPECS if s.key == key)
    result = _port(spec)["SqlBaseLexer"]
    assert "options { caseInsensitive = true; }" in result.text, (
        f"{key}: ported lexer lost caseInsensitive; lowercase SQL will be rejected"
    )
    # and the committed artefact must have it too, since that is what ships
    committed = (spec.generated_dir() / "SqlBaseLexer.g4").read_text()
    assert "options { caseInsensitive = true; }" in committed


# ===========================================================================
# B -- no Java leaks into the Python target
# ===========================================================================

#: Java-isms that must not survive into the ported grammar. Each is a construct
#: ANTLR would paste verbatim into generated Python.
JAVA_MARKERS = [
    "String ",       # local declarations
    "List<",         # java.util.List
    "Map<",          # java.util.Map
    "Deque<",
    "boolean ",      # locals/fields
    ".add(",         # collection mutation
    "new ",          # allocation
    "getText()",     # Java-only Lexer/RuleContext API, as a bare call
    "import java",
    "@Override",
    "String>",
]

#: `->` is excluded from the flat marker list on purpose. It is legal, required ANTLR
#: syntax in a lexer rule (`-> channel(HIDDEN)`) and it is the ARROW token literal
#: `ARROW: '->';`. It is only Java when it appears *inside an action*, which is what
#: test_no_lambda_arrow_inside_an_action asserts.
_ACTION_ARROW_SNIPPETS = []


def test_no_java_constructs_survive_the_port(spec_key):
    """B: the ported grammar must contain no Java semantics.

    Comments and string literals are stripped first, so this fails only if Java
    *semantics* remain -- not because the word "new" or "->" appears in an
    explanatory comment or in the ARROW token definition.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    for name, result in _port(spec).items():
        code = _code_only(result.text)
        found = [m for m in JAVA_MARKERS if m in code]
        assert not found, (
            f"{spec.key}/{name}: Java survived the port: {found}\n"
            f"---\n{code}\n---"
        )


@pytest.mark.parametrize("key", [s.key for s in SPECS])
def test_no_lambda_arrow_inside_an_action(key):
    """B: `->` inside `{...}` is a Java lambda and must not appear.

    Outside an action, `->` is ANTLR's own command syntax (`-> channel(HIDDEN)`,
    `-> skip`) and is required, so the assertion is scoped by brace depth rather
    than banning the character.
    """
    spec = next(s for s in SPECS if s.key == key)
    for name, result in _port(spec).items():
        text = result.text
        offenders = []
        for m in re.finditer(r"->", text):
            before = text[: m.start()]
            # brace depth of the enclosing context; a command arrow sits at depth 0
            # relative to its rule, a lambda arrow sits inside the action braces
            if before.count("{") - before.count("}") > 0:
                offenders.append(text[max(0, m.start() - 60): m.start() + 20])
        assert not offenders, f"{spec.key}/{name}: lambda arrow in an action: {offenders}"


def test_members_blocks_are_our_python_not_the_java_originals(spec_key):
    """B: the ported @members must be the injected Python, not Spark's Java.

    A precise complement to the marker scan: whatever the original @members
    contained is gone, and what replaced it is the members block this project
    controls. Catches a regression where the block is stripped but the replacement
    is accidentally omitted.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    for name, result in _port(spec).items():
        if name.endswith("Lexer"):
            expect = LEXER_MEMBERS
            expected_text = expect
        else:
            expect = PARSER_MEMBERS
            # the pipe-token tuple is substituted in, so build the exact block
            resolved = tuple(sorted(verify_tokens_renamed(
                spec.vendored_path("SqlBaseLexer").read_text(),
                spec.vendored_path("SqlBaseParser").read_text(),
            )))
            expected_text = expect.replace("__PIPE_START_TOKEN_NAMES__", repr(resolved))
        assert expected_text.strip() in result.text, (
            f"{spec.key}/{name}: members block missing; the port must emit the "
            f"Python members it controls, not Spark's Java @members"
        )
        # Spot-check a signature that only exists in our Python version.
        if name.endswith("Lexer"):
            assert "def _port_text(self):" in result.text
            assert "def isValidDecimal(self):" in result.text
        else:
            assert "def isOperatorPipeStart(self):" in result.text
            assert "_PIPE_START_TOKEN_NAMES" in result.text


# ===========================================================================
# C -- token and rule preservation
# ===========================================================================


@pytest.mark.parametrize("name", ["SqlBaseLexer", "SqlBaseParser"])
def test_ported_grammar_declares_exactly_the_original_rules(spec_key, name):
    """C: the rule-name set of the ported grammar equals the vendored one.

    This is the invariant that guarantees the generated parser is the real Spark
    grammar rather than a mangled one. A silently dropped rule produces a parser
    that rejects valid SQL -- and, worse, accepts invalid SQL, because the rule that
    was supposed to catch it no longer exists.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    original = spec.vendored_path(name).read_text()
    ported = _port(spec)[name].text
    extract = _lexer_rule_names if name.endswith("Lexer") else _parser_rule_names
    orig_rules, ported_rules = extract(original), extract(ported)
    assert orig_rules, f"no rules extracted from the vendored {name} -- extractor is wrong"
    assert ported_rules, f"no rules extracted from the ported {name} -- extractor is wrong"
    assert orig_rules - ported_rules == set(), "rules lost in the port"
    assert ported_rules - orig_rules == set(), "rules invented by the port"


def test_parser_rule_count_is_plausible(spec_key):
    """C: a floor on parser rules, so a broken extractor cannot make C vacuous.

    Spark 3.5.1 has ~184 parser rules and 4.0 ~301. Anything under 50 means the
    comparison above is passing for the wrong reason.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    ported = _port(spec)["SqlBaseParser"].text
    assert len(_parser_rule_names(ported)) > 50, f"{spec.key}: implausibly few parser rules"


def test_ported_lexer_still_defines_the_tokens_the_parser_members_use(spec_key):
    """C: every token `isOperatorPipeStart` may consult is defined by this grammar.

    `PIPE_START_TOKENS` deliberately spans versions -- EXTEND/ASOF/AGGREGATE/BIN do
    not exist in 3.5.1 -- so the members resolve by name at runtime and skip
    whatever is missing. That is only safe if the *required* ones are always there.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    texts = {n: spec.vendored_path(n).read_text() for n in spec.files}
    resolved = verify_tokens_renamed(texts["SqlBaseLexer"], texts["SqlBaseParser"])
    assert resolved, f"{spec.key}: no pipe-start tokens resolved at all"
    assert resolved <= set(PIPE_START_TOKENS)
    ported_lexer = _port(spec)["SqlBaseLexer"].text
    defined = _lexer_rule_names(_code_only(ported_lexer))
    missing = sorted(resolved - defined)
    assert not missing, f"{spec.key}: parser members reference tokens the lexer lost: {missing}"


# ===========================================================================
# D -- fail closed on unrecognised Java
# ===========================================================================

#: A minimal but well-formed grammar. port_grammar requires a /* license */ block
#: (it is copied verbatim into the preamble) and an @members block containing a Java
#: method, because that is the shape it is built to recognise.
_GOOD_MEMBERS = "@members {\n  public boolean probe() { return true; }\n}\n"


def _synthetic_lexer(action: str) -> str:
    return f"/* synthetic */\nlexer grammar SqlBaseLexer;\n{_GOOD_MEMBERS}A : 'a' {action} ;\n"


def test_synthetic_grammar_without_unported_java_is_accepted():
    """D: control case -- the harness itself is well-formed, so a raise below is
    caused by the Java in the action and not by the surrounding scaffolding."""
    result = port_grammar(_synthetic_lexer("{self.isValidDecimal();}"), is_lexer=True)
    assert "self.isValidDecimal();" in result.text


@pytest.mark.parametrize("action,marker", [
    ("{import java.util.List;}", "import java"),
    ("{Deque<String> d = new ArrayDeque<>();}", "new ArrayDeque"),
    ("{List<String> xs = new ArrayList<String>();}", "String>"),
    ("{Map<String, String> m = new HashMap<String, String>();}", "new HashMap"),
    ("{UNKNOWN_SEMANTIC_FLAG}?", "UNKNOWN_SEMANTIC_FLAG"),
])
def test_unrecognised_java_raises_port_error(action, marker):
    """D: Java the port has no rewrite for must raise, not be emitted.

    The port's whole safety argument is that it handles every Java-ism *by name*, so
    a Spark upgrade that introduces new Java cannot silently yield a mis-parsing
    grammar. Each case here is Java the rewrites do not cover; `PortError` is how
    the build reports that a human must extend port.py.
    """
    with pytest.raises(PortError) as exc:
        port_grammar(_synthetic_lexer(action), is_lexer=True)
    assert marker in str(exc.value) or "unknown semantic predicate" in str(exc.value)


def test_unknown_semantic_predicate_names_itself():
    """D: the error must name the offending flag, or it is unactionable.

    'port by hand' with no flag name is the failure mode this guards: a maintainer
    upgrading Spark needs to know which predicate to add.
    """
    with pytest.raises(PortError) as exc:
        port_grammar(_synthetic_lexer("{brandNewConfigFlag}?"), is_lexer=True)
    assert "brandNewConfigFlag" in str(exc.value)


def test_port_error_on_missing_members_block():
    """D: a grammar with no @members is a shape change, and must raise.

    Guards against a Spark upgrade that moves the members block: without this the
    port would emit a grammar with an empty class and every inline action would
    fail at parse time instead of at build time.
    """
    src = "/* synthetic */\nlexer grammar SqlBaseLexer;\nA : 'a' ;\n"
    with pytest.raises(PortError):
        port_grammar(src, is_lexer=True)


def test_port_error_on_unexpected_members_shape():
    """D: @members present but not recognisably Java methods must raise."""
    src = "/* synthetic */\nlexer grammar SqlBaseLexer;\n@members {\n  x = 1;\n}\nA : 'a' ;\n"
    with pytest.raises(PortError):
        port_grammar(src, is_lexer=True)


def test_port_error_on_unterminated_members_block():
    """D: a truncated @members block raises rather than emitting half a preamble."""
    src = "/* synthetic */\nlexer grammar SqlBaseLexer;\n@members {\n  public boolean a() { return true; }\n"
    with pytest.raises(PortError):
        port_grammar(src, is_lexer=True)


# ===========================================================================
# E -- spec.py integrity (supply chain)
# ===========================================================================

_SHA40 = re.compile(r"\A[0-9a-f]{40}\Z")


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.key)
def test_pinned_commit_is_a_full_40_char_sha(spec):
    """E: every pin is a full 40-character commit SHA.

    A short SHA is ambiguous over time -- GitHub will resolve an 8-char prefix to a
    different commit as the object database grows -- and a tag is movable by anyone
    with push access. spec.py's docstring promises neither can happen. This test is
    that promise, and it currently fails.

    Reproducer:
        python -m sparkscreen.grammar.build --list
        # spark-4.0    commit=3c28a9c0   <- 8 chars, not 40
        # spark-3.5.1  commit=v3.5.1     <- a tag, and therefore movable
    """
    assert _SHA40.match(spec.commit), (
        f"{spec.key}: commit {spec.commit!r} is not a full 40-char SHA "
        f"(len={len(spec.commit)}). Pin the full hash."
    )


def test_pinned_commits_differ_between_spark_lines():
    """E: two Spark lines pinned to the same commit means one of them is mislabelled."""
    commits = [s.commit for s in SPECS]
    assert len(set(commits)) == len(commits), f"duplicate pinned commit across SPECS: {commits}"


def test_commit_is_not_a_ref():
    """E: no pin may be a tag or branch name.

    A ref is a promise about a moving target. `v3.5.1` and `master` are the two
    shapes this catches, and the first of them is present in spec.py today.
    """
    for spec in SPECS:
        commit = spec.commit
        assert not commit.startswith("v"), f"{spec.key}: pin {commit!r} looks like a tag"
        for prefix in ("refs/", "heads/", "origin/", "release-"):
            assert not commit.startswith(prefix), f"{spec.key}: pin {commit!r} is a ref"


@pytest.mark.parametrize("key", ["spark-4.0", "spark-3.5.1", "spark-4", "3.5.1"])
def test_module_name_has_no_illegal_characters(key):
    """E: the illegal characters are removed from the module name.

    The public key contains a dot and a dash (`spark-4.0`), neither of which is legal
    in a Python identifier -- this bit us once already. `module_name` is the fix.
    """
    spec = GrammarSpec(key=key, commit="0" * 40, spark_versions=("1.0",))
    assert "-" not in spec.module_name
    assert "." not in spec.module_name


@pytest.mark.parametrize("key", ["spark-4.0", "spark-3.5.1", "spark-4"])
def test_module_name_is_a_valid_python_identifier(key):
    """E: `module_name` must be importable, because it is a package directory name.

    Scoped to keys that start with a letter, which is the shape every real key has
    (see test_module_name_does_not_handle_a_leading_digit for the gap).
    """
    spec = GrammarSpec(key=key, commit="0" * 40, spark_versions=("1.0",))
    assert spec.module_name.isidentifier(), f"{key}: {spec.module_name!r} is not an identifier"


@pytest.mark.xfail(strict=False, reason=(
    "GAP (spec.py:56): module_name strips only '-' and '.', so a key with a leading "
    "digit yields e.g. '3_5_1', which is not an identifier. No key in SPECS has that "
    "shape today, so it is latent rather than live -- but the docstring promises an "
    "'importable package name' unconditionally."
))
def test_module_name_does_not_handle_a_leading_digit():
    """E: a key beginning with a digit cannot be a module name, and is not fixed up.

    `module_name` only strips `-` and `.`, so a hypothetical `3.5.1` key yields
    `3_5_1`, which is a syntax error as an identifier. No key in SPECS has this
    shape, so it is not a live bug -- but `module_name`'s docstring claims to return
    an "importable package name", and it does not always.
    """
    spec = GrammarSpec(key="3.5.1", commit="0" * 40, spark_versions=("1.0",))
    assert spec.module_name.isidentifier(), (
        f"{spec.module_name!r} is not a valid identifier; a leading digit needs a prefix"
    )


def test_every_spec_module_name_is_importable():
    """E: the generated package for each spec exists on disk under module_name."""
    for spec in SPECS:
        assert spec.module_name.isidentifier()
        assert spec.generated_dir().is_dir(), f"{spec.key}: no generated dir at {spec.generated_dir()}"
        assert (spec.generated_dir() / "__init__.py").exists()


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s.key)
def test_python_module_path_is_importable(spec):
    """E: `GrammarSpec.python_module()` must name a module that actually imports.

    Reproducer:
        from sparkscreen.grammar.spec import get_spec
        import importlib
        importlib.import_module(get_spec("spark-4.0").python_module("SqlBaseParser"))
        # -> ModuleNotFoundError: No module named 'sparkscreen.grammar.generated.spark-4'
    """
    mod = spec.python_module("SqlBaseParser")
    assert mod == f"sparkscreen.grammar.generated.{spec.module_name}.SqlBaseParser", (
        f"{spec.key}: python_module() returns {mod!r}, which is not importable"
    )
    import importlib

    importlib.import_module(mod)


@pytest.mark.parametrize("name", ["SqlBaseLexer", "SqlBaseParser"])
def test_vendored_grammar_is_present_and_licensed(spec_key, name):
    """E: the vendored .g4 exists, is non-empty, and carries the Apache header.

    The license block is not decoration: port.py copies `src[src.index("/*"):...]`
    into the preamble, so a file that lost its header would either crash the build or
    ship generated code with the attribution stripped.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    path = spec.vendored_path(name)
    assert path.exists(), f"missing vendored grammar {path}"
    text = path.read_text()
    assert text.strip(), f"{path} is empty"
    assert text.lstrip().startswith("/*"), f"{path} does not start with a block comment"
    assert "Licensed under the Apache License" in text[:1200], f"{path} has no license header"


def test_antlr_version_is_the_supported_one():
    """E: only ANTLR 4.13.1 generates these grammars; see port.py's rationale."""
    from sparkscreen.grammar.port import SUPPORTED_ANTLR_VERSION

    for spec in SPECS:
        assert spec.antlr_version == SUPPORTED_ANTLR_VERSION, spec.key


# ===========================================================================
# F -- no JVM required
# ===========================================================================

JVM_HINTS = ("subprocess", "os.system", "popen", "java", "jvm", "antlr.Tool")


def test_parser_module_never_shells_out_to_java():
    """F: parser.py must not invoke the ANTLR tool at parse time.

    Generated parsers are committed and shipped, so the JVM is a *build-time* tool.
    If the runtime reached for `java`, every install would need a JRE and the
    "no JVM" property in pyproject.toml would be false.
    """
    src = (Path(REPO) / "src/sparkscreen/grammar/parser.py").read_text()
    for hint in JVM_HINTS:
        assert hint not in src, f"parser.py references {hint!r}; runtime must not need a JVM"


def test_generated_modules_import_nothing_jvm_related():
    """F: no generated module imports a JVM/py4j bridge or shells out.

    Checks the imported *module root* via the AST rather than grepping the line,
    because the ported lexer header legitimately contains
    `from collections import deque as _java_Deque` -- a stdlib deque aliased to
    mirror Java's `Deque`, and a text search for "java" would flag it. What matters
    is that nothing resolves to an actual JVM library.
    """
    jvm_roots = {"java", "javax", "jnius", "jpype", "py4j", "pyjnius", "jniusapi"}
    for path in GENERATED.rglob("*.py"):
        text = path.read_text()
        for bad in ("py4j", "jpype", "jnius"):
            assert bad not in text.lower(), f"{path} references {bad}"
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0].lower()
                    assert root not in jvm_roots, f"{path}: JVM import {alias.name!r}"
                    assert root != "subprocess", f"{path}: shells out ({alias.name!r})"
            elif isinstance(node, ast.ImportFrom):
                if node.level:      # relative import within the generated package
                    continue
                root = (node.module or "").split(".")[0].lower()
                assert root not in jvm_roots, f"{path}: JVM import {node.module!r}"
                assert root != "subprocess", f"{path}: shells out ({node.module!r})"


def test_antlr_jar_is_not_required_because_it_is_gitignored():
    """F: the tool jar is a build artefact, gitignored, so it is absent from a clone.

    Deliberately does not delete or move the local jar -- it only asserts the
    repository does not depend on it being there.
    """
    gitignore = (REPO / ".gitignore").read_text()
    assert "src/sparkscreen/grammar/.cache/" in gitignore
    tracked = subprocess.run(
        ["git", "ls-files", "src/sparkscreen/grammar/.cache/"],
        cwd=REPO, capture_output=True, text=True, check=True,
    )
    assert tracked.stdout.strip() == "", f"ANTLR jar is committed: {tracked.stdout}"


def test_parser_imports_and_parses_with_no_java_on_path():
    """F: the real end-to-end check -- import and parse in a JVM-free environment.

    PATH is emptied of the JRE and the jar cache is shadowed by pointing HOME at a
    temp dir, so anything that tried to locate or download the ANTLR tool would fail.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        env = {"PATH": "/nonexistent", "HOME": home, "PYTHONPATH": "src",
               "PYTHONDONTWRITEBYTECODE": "1"}
        res = subprocess.run(
            [".venv/bin/python", "-c",
             "from sparkscreen.grammar.parser import SqlParser;"
             "p = SqlParser('spark-4.0');"
             "assert p.parse('select 1').label;"
             "print('ok')"],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=180,
        )
    assert res.returncode == 0, f"JVM-free import/parse failed:\n{res.stdout}\n{res.stderr}"
    assert "ok" in res.stdout


# ===========================================================================
# Known source bugs
#
# Each is a real defect in src/ with a reproducer, recorded as a non-strict xfail so
# the assertion keeps its full strength and a fix turns it green by itself.
# ===========================================================================


#: Java action bodies that `_JAVA_MARKERS` is expected to catch. These are the shapes
#: that have actually appeared in Spark grammars, plus the obvious near-misses.
CAUGHT_JAVA = [
    '{x.add("a");}',                # collection mutation
    "{new java.util.ArrayList()}",  # fully-qualified `new`
    "{String s = getText();}",      # Java String declaration
    "{public boolean z() { return true; }}",
    "{Deque<Integer> q = new ArrayDeque<>();}",
]

#: Java action bodies that `_JAVA_MARKERS` does NOT catch, and never will -- no finite
#: denylist is complete. Listed explicitly so the gap is visible and reviewable rather
#: than hidden behind an xfail that reads as "eventually fixed".
#:
#: The actual guarantee is elsewhere and is enforced in CI, not here:
#:   1. the generated parsers are committed, and CI regenerates them and fails on any
#:      diff, so a new Java construct surfaces as a build failure;
#:   2. tests/differential/ runs the parser against a real Spark, so a *silently
#:      mistranslated* lexer is caught even if it imported cleanly.
UNCAUGHT_JAVA = [
    "{this.isHint()}",
    "{if (true) { helper(); }}",
    "{Runnable r = () -> {};}",
]


@pytest.mark.parametrize("action", CAUGHT_JAVA)
def test_known_java_shapes_are_rejected(action):
    """Java in an inline action must raise `PortError`, not reach the generated Python.

    ANTLR copies action bodies into the generated module verbatim. If Java survives, the
    generated parser either fails to import -- caught loudly -- or, worse, is valid
    Python that misbehaves silently.
    """
    with pytest.raises(PortError):
        port_grammar(_synthetic_lexer(action), is_lexer=True)


@pytest.mark.parametrize("action", UNCAUGHT_JAVA)
def test_denylist_gaps_are_known_not_surprising(action):
    """Pin the *known* gap so it stays a recorded decision rather than a surprise.

    `port_grammar`'s docstring previously claimed "an unrecognised one raises". That is
    true only for the constructs in `_JAVA_MARKERS`. This test states the truth: these
    specific shapes slip through, and it will start failing the day someone widens the
    denylist to cover them -- which is the signal to move them into CAUGHT_JAVA.
    """
    port_grammar(_synthetic_lexer(action), is_lexer=True)  # must not raise


def test_missing_license_block_raises_port_error():
    """D: a grammar with no `/* ... */` block must raise `PortError`.

    `port_grammar` does `license_block = src[src.index("/*"): src.index("*/") + 2]`
    after every other guard has passed, so a vendored file that lost its header
    produces `ValueError: substring not found` -- which no caller in the port's
    contract is documented to expect.

    Reproducer:
        port_grammar("lexer grammar L;\\n@members {\\n  public boolean z() { return true; }\\n}\\n"
                     "A:'a';\\n", is_lexer=True)
        # -> ValueError: substring not found
    """
    src = f"lexer grammar SqlBaseLexer;\n{_GOOD_MEMBERS}A : 'a' ;\n"
    with pytest.raises(PortError):
        port_grammar(src, is_lexer=True)


@pytest.mark.xfail(strict=False, reason=(
    "BUG (port.py:116 / spec.py:port_to_python): the docstring claims port_grammar "
    "'asserts that no rewrite is left unused', but unused_rules is only recorded, "
    "never checked, and port_to_python discards it."
))
def test_unused_rewrites_are_reported(spec_key):
    """A: a rewrite that no longer matches the grammar should be surfaced.

    port.py: "port_grammar asserts that no rewrite is left unused against the actual
    grammar, so a grammar that no longer contains one of these is reported rather
    than ignored." It does not assert; it collects. A Spark upgrade that removes a
    construct would therefore leave dead rewrites silently in place.

    Reproducer: spark-3.5.1's lexer has 6 unused rewrites
    (incComplexTypeLevelCounter, decComplexTypeLevelCounter, isShiftRightOperator, ...)
    and spark-3.5.1's parser has 6 more, including the whole pipe-operator family.
    """
    spec = next(s for s in SPECS if s.key == spec_key)
    for name, result in _port(spec).items():
        assert not result.unused_rules, (
            f"{spec.key}/{name}: {len(result.unused_rules)} rewrites no longer match "
            f"the grammar: {result.unused_rules}"
        )
