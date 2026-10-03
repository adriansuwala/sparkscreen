"""F14: a dotted-name component spelled as a non-reserved keyword must survive extraction.

The bug: extraction collected only terminals whose token was IDENTIFIER or
BACKQUOTED_IDENTIFIER. A component spelled with a keyword reaches the tree carrying *that
keyword's* token, so it was silently deleted -- `prod.x` extracted as `prod`, `prod.x.y` as
`prod.y`.

`x` is the memorable case because Spark's lexer has `BINARY_HEX: 'X'`, so the component of
`prod.x` is lexed as a hex-literal marker. It is not special: 425 of the 428 `nonReserved`
alternatives lex the same way, so `prod.AFTER`, `prod.SCHEMA`, `prod.USER` and the rest
were truncated identically. Fixing only `x` would have left the actual defect in place.

Why this is a fail-open, not a cosmetic bug: dropping a component produces a BROADER name
than the operator wrote. `select * from prod.staging.x` extracted as `prod.staging`, which
a `prod.*` allowlist matches -- permitting a table nobody authorised.

These are fast tests with no JVM. The live-Spark oracle that settles what Spark *means*
lives in tests/differential/test_keyword_table_components.py; this file pins the static
behaviour so a regression is caught by the suite that always runs.
"""
from __future__ import annotations

import pytest

from sparkscreen.analysis.treewalk import extract_namespaces
from sparkscreen.grammar.parser import get_parser

BOTH = ("spark-4.0", "spark-3.5.1")


def _names(sql: str, key: str) -> list[str]:
    stmts = get_parser(key).parse(sql).statements
    assert len(stmts) == 1, f"expected one statement, got {len(stmts)}: {sql!r}"
    refs = extract_namespaces(stmts[0].tree, grammar_key=key)
    return [r.name for r in refs]


#: Keyword-spelled components that must survive whole. `x` is the reported case; the rest
#: are the same defect reached a different way, which is the point -- a fix that special-
#: cases `x` would pass the first row and fail the rest.
KEYWORD_COMPONENTS = [
    "x",
    "AFTER",
    "SCHEMA",
    "BIGINT",
    "USER",
    "ORDER",
    "KEY",
    "VALUES",
    "FIRST",
    "LAST",
]


@pytest.mark.parametrize("component", KEYWORD_COMPONENTS)
@pytest.mark.parametrize("key", BOTH)
def test_keyword_component_survives_extraction(component: str, key: str) -> None:
    assert _names(f"drop table prod.{component}", key) == [f"prod.{component}"]


@pytest.mark.parametrize("key", BOTH)
def test_keyword_component_in_every_position(key: str) -> None:
    """The component must survive wherever it appears, not just last."""
    for sql, want in [
        ("drop table prod.x", ["prod.x"]),
        ("drop table prod.x.y", ["prod.x.y"]),
        ("drop table prod.y.x", ["prod.y.x"]),
        ("drop table prod.staging.x", ["prod.staging.x"]),
        ("drop table x.y", ["x.y"]),
        ("insert into prod.x.y values (1)", ["prod.x.y"]),
    ]:
        assert _names(sql, key) == want, sql


@pytest.mark.parametrize("key", BOTH)
def test_alias_is_still_not_part_of_the_name(key: str) -> None:
    """The opposite error: collecting an alias would change the reported name.

    That matters in the other direction -- a deny rule written against the real name
    would stop matching.
    """
    for sql, want in [
        ("select * from prod.x as y", ["prod.x"]),
        ("select * from prod.x y", ["prod.x"]),
        ("select * from prod.AFTER a", ["prod.AFTER"]),
        ("select * from prod.users u", ["prod.users"]),
    ]:
        assert _names(sql, key) == want, sql


@pytest.mark.parametrize("key", BOTH)
def test_plain_identifiers_are_unaffected(key: str) -> None:
    """Control: the ordinary case must behave exactly as before."""
    for sql, want in [
        ("drop table prod.users", ["prod.users"]),
        ("drop table prod.staging.tmp", ["prod.staging.tmp"]),
        ("select * from a.b.c.d", ["a.b.c.d"]),
    ]:
        assert _names(sql, key) == want, sql


@pytest.mark.parametrize("key", BOTH)
def test_wrong_grammar_key_yields_nothing_for_plain_names(key: str) -> None:
    """The canary the fix must not destroy.

    Extraction is keyed on the grammar that produced the tree. Handing a tree to the wrong
    key has to return nothing rather than a plausible name -- that is what
    `test_grammar_key_must_match_the_parser_it_came_from` exists to catch, and the F14 fix
    initially broke it by accepting terminals under `UnquotedIdentifierContext`.

    The tree has to be built by one parser and read by the OTHER. As first written this
    compared `key` against itself -- it parsed and extracted with the same key and then
    asserted the result was empty, which is false for any SQL that extracts at all. It
    failed for that reason, not because the fix was wrong: cross-key really does yield
    nothing, and this now tests that. Verified both directions before the change, so the
    distinction is not taken on trust.
    """
    other = "spark-4.0" if key == "spark-3.5.1" else "spark-3.5.1"
    for sql in ("drop table prod.users", "drop table prod.staging.tmp"):
        tree = get_parser(key).parse(sql).statements[0].tree
        refs = extract_namespaces(tree, grammar_key=other)
        assert refs == [], f"parsed with {key}, read as {other}: {sql}"


@pytest.mark.parametrize("key", BOTH)
def test_broader_name_can_no_longer_be_produced(key: str) -> None:
    """The fail-open itself, pinned as the property that actually matters.

    `prod.staging.x` used to extract as `prod.staging`, which a `prod.*` allowlist matches.
    It must extract whole, and `prod.*` must not match it.
    """
    refs = extract_namespaces(
        get_parser(key).parse("select * from prod.staging.x").statements[0].tree,
        grammar_key=key,
    )
    (ref,) = refs
    assert ref.parts == ("prod", "staging", "x")
    # `matches()` takes a dotted string pattern, not a NamespaceRef.
    assert not ref.matches("prod.*")
    assert ref.matches("prod.staging.*")
    assert not ref.matches("prod")