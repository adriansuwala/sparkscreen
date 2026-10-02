"""Parse-tree walking: statement discovery, labels, and identifier extraction.

`analysis/treewalk.py` is the layer that turns ANTLR's tree into the three facts a
policy reasons over: *what kind of statement is this*, *which objects does it
touch*, and *which paths/literals does it name*. Everything downstream keys on
those, so a wrong answer here is a policy decision made on fiction.

What these tests pin:

  * a multi-statement script yields one labeled context per statement, and
  * `effective_label` resolves the *specific* statement kind rather than the
    wrapper rule it hangs off (`DmlStatement > SingleInsertQuery >
    InsertOverwriteTable` must not read as `DmlStatement`)
  * `extract_namespaces` reports tables, not column names, aliases, CTE names,
    column-definition names or table providers
  * `extract_string_literals` finds the paths that matter and unescapes the
    way the engine does
  * extraction never raises on anything the parser accepted, and the
    `grammar_key` argument tracks the grammar the tree actually came from

The `xfail` markers are real, diagnosed defects -- see the comments on each for
the root cause and the fix. They are `strict=False` where the behaviour is
known-wrong-but-nuanced, so an XPASS is reported rather than hidden.
"""
from __future__ import annotations

import pytest

from sparkscreen.analysis.treewalk import (
    QUERY_LABEL,
    NamespaceRef,
    effective_label,
    executed_immediate_sql,
    extract_namespaces,
    extract_string_literals,
    identifier_token_types,
    statement_label,
    top_level_statement_contexts,
)
from sparkscreen.grammar.parser import SqlSyntaxError, get_parser

#: Both pinned grammars, by key. The `spec_key` fixture in conftest.py already
#: parametrizes over these; tests that need a *specific* one (a 4.0-only syntax)
#: use these constants directly.
BOTH = ("spark-4.0", "spark-3.5.1")
V4 = "spark-4.0"
V351 = "spark-3.5.1"


def parse_one(sql: str, key: str):
    """Parse and return the single statement's `(label, tree)`.

    Refuses multi-statement input loudly -- a helper that silently returns
    statement 1 of 3 would make every test below quietly weaker than it looks.
    """
    stmts = get_parser(key).parse(sql).statements
    assert len(stmts) == 1, f"expected one statement, got {len(stmts)}: {sql!r}"
    return stmts[0].label, stmts[0].tree


def names(sql: str, key: str) -> list[str]:
    _, tree = parse_one(sql, key)
    return [n.name for n in extract_namespaces(tree, grammar_key=key)]


def literals(sql: str, key: str) -> list[str]:
    _, tree = parse_one(sql, key)
    return extract_string_literals(tree)


# ===========================================================================
# 1. top-level statement discovery
# ===========================================================================

def test_begin_end_script_yields_each_statement_with_its_own_label():
    """A BEGIN...END script is several statements, and each gets its own label.

    This is the case that makes `top_level_statement_contexts` worth existing:
    a policy that only ever looked at the root would see one opaque
    `SingleCompoundStatement` and miss the `DROP TABLE` inside it entirely.
    """
    sql = "BEGIN DROP TABLE prod.users; SELECT 1; END"
    stmts = get_parser(V4).parse(sql).statements
    assert [s.label for s in stmts] == ["DropTable", "StatementDefault"]


def test_begin_end_labels_survive_grammars_label_order():
    """Three statements, three distinct labels -- no collapse onto the wrapper."""
    sql = "BEGIN TRUNCATE TABLE prod.t; INSERT INTO prod.u VALUES (1); SELECT 1; END"
    labels = [s.label for s in get_parser(V4).parse(sql).statements]
    assert labels == ["TruncateTable", "InsertIntoTable", "StatementDefault"]


def test_not_atomic_begin_end_is_also_flattened():
    sql = "BEGIN NOT ATOMIC SELECT 1; SELECT 2; END"
    labels = [s.label for s in get_parser(V4).parse(sql).statements]
    assert labels == ["StatementDefault", "StatementDefault"]


def test_each_statement_carries_its_own_subtree():
    """The DROP's context must contain the DROP's table, not the SELECT's facts.

    `ParsedStatement.statements` re-points `tree` at the context the label came
    from (`parser._tighten`). If it handed back the shared wrapper, every
    statement in a script would report the union of all their targets.
    """
    stmts = get_parser(V4).parse("BEGIN DROP TABLE prod.users; SELECT 1; END").statements
    drop, query = stmts
    assert drop.context_class == "DropTableContext"
    assert [n.name for n in extract_namespaces(drop.tree, grammar_key=V4)] == ["prod.users"]
    assert extract_namespaces(query.tree, grammar_key=V4) == []


def test_begin_end_is_a_syntax_error_on_3_5_1():
    """3.5.1 has no compound-statement rule, so the script must be rejected.

    Pinned because the alternative -- accepting it -- would mean the screener
    analysed SQL the pinned engine cannot run.
    """
    with pytest.raises(SqlSyntaxError):
        get_parser(V351).parse("BEGIN DROP TABLE prod.users; SELECT 1; END")


def test_bare_statement_list_is_rejected_on_both():
    """`SELECT 1; SELECT 2` is not a statement in either grammar."""
    for key in BOTH:
        with pytest.raises(SqlSyntaxError):
            get_parser(key).parse("SELECT 1; SELECT 2")


@pytest.mark.parametrize("sql,expected", [
    ("select 1", 1),
    ("drop table prod.users", 1),
])
def test_exactly_one_statement_for_single_statements(sql, expected, spec_key):
    ctxs = list(top_level_statement_contexts(get_parser(spec_key).parse(sql).tree))
    assert len(ctxs) == expected


def test_single_statement_begin_end_is_one_context():
    """4.0 only. A one-statement compound script is still a compound script:
    the wrapper must be unwrapped to find its single statement."""
    ctxs = list(top_level_statement_contexts(
        get_parser(V4).parse("BEGIN select 1; END").tree))
    assert [statement_label(c) for c in ctxs] == ["StatementDefault"]


def test_top_level_contexts_accepts_a_raw_parse_tree(spec_key):
    """`top_level_statement_contexts` works on the tree, not just on ParsedStatement."""
    tree = get_parser(spec_key).parse("drop table prod.users").tree
    ctxs = list(top_level_statement_contexts(tree))
    assert [statement_label(c) for c in ctxs] == ["DropTable"]


# ===========================================================================
# 2. label specificity
# ===========================================================================

#: (sql, expected effective label). Every one of these must resolve the same way
#: on both pinned grammars -- a label that only works on one is a policy that
#: silently stops firing on the other Spark version.
LABEL_CASES = [
    ("drop table prod.users", "DropTable"),
    ("truncate table prod.users", "TruncateTable"),
    ("insert overwrite table prod.t select 1", "InsertOverwriteTable"),
    ("insert into prod.t values (1)", "InsertIntoTable"),
    ("delete from prod.t where id = 1", "DeleteFromTable"),
    ("update prod.t set a = 1", "UpdateTable"),
    (
        "merge into a.b s using c.d t on s.i = t.i "
        "when matched then update set s.x = t.x",
        "MergeIntoTable",
    ),
    ("load data local inpath '/etc/passwd' into table prod.users", "LoadData"),
    ("add jar /tmp/x.jar", "ManageResource"),
    ("create function f as 'com.example.Cls'", "CreateFunction"),
    ("alter table t add columns (a int)", "AddTableColumns"),
    ("alter table t add column b int", "AddTableColumns"),
    ("select 1", "StatementDefault"),
    ("select * from prod.t", "StatementDefault"),
]


@pytest.mark.parametrize("sql,expected", LABEL_CASES, ids=lambda v: v if isinstance(v, str) and " " in v else "")
def test_effective_label_is_specific_on_both_grammars(sql, expected, spec_key):
    label, _ = parse_one(sql, spec_key)
    assert label == expected


def test_insert_overwrite_and_insert_into_are_distinguishable(spec_key):
    """The whole reason `effective_label` descends through wrappers.

    `INSERT OVERWRITE` destroys data and `INSERT INTO` does not. Both parse as
    `DmlStatement > SingleInsertQuery > <specific>`; a policy keying on the
    wrapper cannot tell them apart, and would either deny all inserts or allow
    all overwrites.
    """
    over, _ = parse_one("insert overwrite table prod.t select 1", spec_key)
    into, _ = parse_one("insert into prod.t values (1)", spec_key)
    assert over == "InsertOverwriteTable"
    assert into == "InsertIntoTable"
    assert over != into


def test_plain_query_reports_statementdefault_on_both_grammars(spec_key):
    """A SELECT's top-level labelled alternative is already `StatementDefault`,
    so `effective_label` passes it straight through (it is not a wrapper)."""
    tree = get_parser(spec_key).parse("select a from t where a > 1").tree
    ctx = next(iter(top_level_statement_contexts(tree)))
    assert statement_label(ctx) == QUERY_LABEL == "StatementDefault"
    assert effective_label(ctx) == QUERY_LABEL


def test_effective_label_descends_the_query_wrapper_hierarchy():
    """The query rules themselves *are* wrappers: reaching one must bottom out at
    `StatementDefault` rather than reporting `Query`/`QueryTermDefault`/....

    Constructed by hand from a real context so the descent is exercised directly
    rather than only through the statement-list path.
    """
    from sparkscreen.analysis.treewalk import (
        QUERY_LABEL_PREFIXES, _is_wrapper,
    )
    assert QUERY_LABEL_PREFIXES == ("Query",)
    for wrapper in ("Query", "QueryTermDefault", "QueryPrimaryDefault"):
        assert _is_wrapper(wrapper)
    assert not _is_wrapper("StatementDefault")
    assert not _is_wrapper("DropTable")


def test_with_cte_query_is_still_a_plain_query(spec_key):
    sql = "with q as (select * from prod.t) select * from q"
    label, _ = parse_one(sql, spec_key)
    assert label == "StatementDefault"


def test_nested_alter_table_forms_resolve_to_their_own_label(spec_key):
    """Every ALTER TABLE alternative is a flat labeled alternative of `statement`.

    None of them sits behind a wrapper, so `effective_label` must return the
    alternative's own name -- not `StatementDefault`, not a parent rule.
    """
    cases = [
        ("alter table t drop column a", "DropTableColumns"),
        ("alter table t rename to t2", "RenameTable"),
        ("alter table t rename column a to b", "RenameTableColumn"),
        ("alter table t set tblproperties ('a'='b')", "SetTableProperties"),
        ("alter table t unset tblproperties ('a')", "UnsetTableProperties"),
        ("alter table t alter column a type int", "AlterTableAlterColumn"),
        ("alter table t change column a b int", "HiveChangeColumn"),
        ("alter table t replace columns (a int)", "HiveReplaceColumns"),
        ("alter table t partition (p=1) set location 's3://b'", "SetTableLocation"),
        ("alter table t recover partitions", "RecoverPartitions"),
        ("alter table t add if not exists partition (p=1) location 's3://b'",
         "AddTablePartition"),
        ("alter table t partition (p=1) rename to partition (p=2)",
         "RenameTablePartition"),
        ("alter table t drop if exists partition (p=1) purge", "DropTablePartitions"),
        ("alter table t set serde 'org.apache.hadoop.hive.serde2.OpenCSVSerde'",
         "SetTableSerDe"),
        ("alter view v as select 1", "AlterViewQuery"),
        ("alter namespace n set dbproperties ('a'='b')", "SetNamespaceProperties"),
        ("alter namespace n set location 's3://b'", "SetNamespaceLocation"),
        ("msck repair table t add partitions", "RepairTable"),
    ]
    for sql, expected in cases:
        label, _ = parse_one(sql, spec_key)
        assert label == expected, f"{sql!r} labelled {label!r}, expected {expected!r}"


# ---------------------------------------------------------------------------
# BUG 1 -- `ALTER TABLE t COLLATE <name>` yields statement=None
# ---------------------------------------------------------------------------

def test_alter_table_collate_does_not_parse_at_all(spec_key):
    """`ALTER TABLE t COLLATE latin1` is not a Spark 4.0 statement. It fails to
    parse, so `screen()` reports UNPARSEABLE_SQL with `statement=None`.

    BUG 1, part 1 -- the None is not produced by `effective_label`. Spark's
    grammar has no `COLLATE` alternative under `ALTER TABLE`; the only
    collation-bearing ALTER alternatives are
    `ALTER TABLE identifierReference collationSpec   #alterTableCollation`
    and `collationSpec : DEFAULT COLLATION identifier`.
    `collateClause : COLLATE multipartIdentifier` is reachable only from
    `primaryExpression collateClause #collate` and from the `STRING`/`CHAR`/
    `VARCHAR` dataType rules -- i.e. expression position, never DDL position.
    Confirmed identical in the vendored 4.0 grammar and in apache/spark master
    at SqlBaseParser.g4 (both spell the alternative `collationSpec`).

    So the real finding is a *screen()* reporting detail, not a treewalk defect:
    `screen()` sets `statement=label` on every policy finding but leaves it
    `None` on the `UNPARSEABLE_SQL` path, so an unanalysed statement is
    indistinguishable in the report from a statement with no label at all.
    """
    with pytest.raises(SqlSyntaxError):
        get_parser(spec_key).parse("alter table t collate latin1")


def test_alter_table_collate_appears_in_no_pinned_grammar():
    """The syntax is invalid on *both* pins, so this is not a 4.0-vs-3.5.1 drift."""
    for key in BOTH:
        with pytest.raises(SqlSyntaxError):
            get_parser(key).parse("alter table t collate latin1")


def test_alter_table_default_collation_parses_and_is_labelled_4_0():
    """The form Spark *does* accept: `DEFAULT COLLATION`, label `AlterTableCollation`.

    This is the correct label for the nearest valid statement to the one in the
    bug report, and it is what a policy should be matching on.
    """
    label, tree = parse_one("alter table t default collation latin1", V4)
    assert label == "AlterTableCollation"
    assert [n.name for n in extract_namespaces(tree, grammar_key=V4)] == ["t"]


def test_collate_expression_is_a_query_not_alter():
    """`COLLATE` in expression position is a `collateClause` under a primary
    expression -- a read-only query, on 4.0 only."""
    label, _ = parse_one("select a collate latin1 from t", V4)
    assert label == "StatementDefault"


def test_bug1_4_0_only_alter_forms_are_rejected_by_3_5_1_not_mislabelled():
    """The 4.0 ALTER additions (`cluster by`, constraints, `DEFAULT COLLATION`)
    are absent from 3.5.1 and are rejected there. Rejection, not a wrong label.

    Pinned so that if a future grammar port ever made 3.5.1 accept these, the
    resulting label change would be a deliberate, visible diff.
    """
    for sql in (
        "alter table t cluster by (a, b)",
        "alter table t default collation latin1",
        "alter table t add constraint c primary key (a)",
        "alter table t drop constraint c",
    ):
        with pytest.raises(SqlSyntaxError):
            get_parser(V351).parse(sql)
        label, _ = parse_one(sql, V4)
        assert label in {"AlterClusterBy", "AlterTableCollation",
                         "AddTableConstraint", "DropTableConstraint"}


# ===========================================================================
# 3. extract_namespaces
# ===========================================================================

def test_drop_table_yields_exactly_the_target(spec_key):
    """The regression guard for the whole namespace story.

    `DROP TABLE prod.users` must yield `prod.users` and nothing else. Any fix
    that narrows which contexts are treated as table references has to keep
    this working -- a "cleaner" extractor that returned `()` here would make
    every drop look harmless.
    """
    assert names("drop table prod.users", spec_key) == ["prod.users"]


def test_drop_table_quoted_and_if_exists_purge(spec_key):
    for sql in (
        "drop table if exists prod.users",
        "drop table prod.users purge",
        "drop table `prod`.`users`",
    ):
        assert names(sql, spec_key) == ["prod.users"], sql


def test_identifier_case_is_preserved_not_normalised(spec_key):
    """`prod.Users` stays `prod.Users`.

    SQL is case-insensitive but Spark resolves table names case-sensitively, so
    folding case here would make `NamespaceRef.matches("prod.users")` claim to
    match a table the engine may not resolve. Preserved verbatim is the safe
    direction: the policy compares the name the engine will compare.
    """
    assert names("drop table prod.Users", spec_key) == ["prod.Users"]


def test_query_over_two_tables_yields_both(spec_key):
    sql = "select a.id from prod.t as x join other.s as y on x.id = y.id"
    assert sorted(names(sql, spec_key)) == ["other.s", "prod.t"]


def test_three_way_join_yields_all_three_tables(spec_key):
    sql = "select 1 from a.b join c.d on 1 = 1 join e.f on 1 = 1"
    assert sorted(names(sql, spec_key)) == ["a.b", "c.d", "e.f"]


@pytest.mark.parametrize("sql,expected", [
    ("select 1", []),
    ("select * from prod.t", ["prod.t"]),
    ("truncate table prod.t", ["prod.t"]),
    ("delete from prod.t where id = 1", ["prod.t"]),
    ("update prod.t set a = 1", ["prod.t"]),
    ("insert overwrite table prod.t select 1", ["prod.t"]),
    ("load data local inpath '/etc/passwd' into table prod.users", ["prod.users"]),
    ("alter table prod.t set tblproperties ('a'='b')", ["prod.t"]),
    ("select * from (select * from a.b) x", ["a.b"]),
    ("select * from t where a in (select b from u)", ["t", "u"]),
    # `update ... set a = 1` also reports `a` today: the assignment target is a
    # bare ErrorCapturingIdentifier under AssignmentContext. Asserted here so the
    # over-collection is visible; the correction is xfailed in the BUG 2 block.
])
def test_namespace_extraction_basics(sql, expected, spec_key):
    assert sorted(names(sql, spec_key)) == sorted(expected)


def test_quoted_forms_resolve_to_the_same_canonical_name(spec_key):
    """`` `prod`.`users` `` and `prod.users` must be indistinguishable to a
    policy -- otherwise quoting evades an allowlist."""
    plain = names("select * from prod.users", spec_key)
    quoted = names("select * from `prod`.`users`", spec_key)
    assert plain == quoted == ["prod.users"]


def test_quoted_flag_records_that_quoting_was_used(spec_key):
    """`NamespaceRef.quoted` is the hook for a stricter policy; it must be set."""
    _, tree = parse_one("select * from `prod`.`users`", spec_key)
    (ref,) = extract_namespaces(tree, grammar_key=spec_key)
    assert isinstance(ref, NamespaceRef)
    assert ref.quoted is True
    assert ref.parts == ("prod", "users")
    assert ref.name == "prod.users"
    assert ref.namespace == ("prod",)
    assert ref.table == "users"


def test_unquoted_reference_is_not_marked_quoted(spec_key):
    _, tree = parse_one("drop table prod.users", spec_key)
    (ref,) = extract_namespaces(tree, grammar_key=spec_key)
    assert ref.quoted is False


def test_namespace_ref_matches_wildcards():
    ref = NamespaceRef(parts=("prod", "users"))
    assert ref.matches("prod.users")
    assert ref.matches("prod.*")
    assert ref.matches("*.*")
    assert not ref.matches("other.users")
    assert not ref.matches("prod.users.extra")


def test_backquoted_embedded_backquote_is_unescaped(spec_key):
    """``` `` ``` is Spark's escape for a literal backquote inside a quoted ident."""
    assert names("select * from `pr``od`.`us``ers`", spec_key) == ["pr`od.us`ers"]


# ---------------------------------------------------------------------------
# BUG 2 -- over-collection
# ---------------------------------------------------------------------------

BUG2 = pytest.mark.xfail(
    reason=(
        "BUG 2: extract_namespaces collects every ErrorCapturingIdentifier / "
        "MultipartIdentifier, including column aliases, CTE names, column-definition "
        "names and table providers. The extractor cannot tell a table position from "
        "a column position, so SELECT aliases leak into the target list. "
        "Fix: restrict collection to IdentifierReferenceContext / "
        "TemporalTableIdentifierReferenceContext, and allow a bare "
        "ErrorCapturingIdentifier / MultipartIdentifier only under a table-bearing "
        "parent rule."
    ),
    strict=False,
)


@BUG2
@pytest.mark.parametrize("sql,noise", [
    # select-list column alias: `b` is an ErrorCapturingIdentifier directly under
    # NamedExpressionContext (`namedExpression : expression (AS? name=errorCapturingIdentifier)?`)
    ("select a as b from t", "b"),
    ("select 1 as one, x.y as z from t", "one"),
    ("select 1 as one, x.y as z from t", "z"),
    # CTE name: `q` is an ErrorCapturingIdentifier under NamedQueryContext
    ("with q as (select * from prod.t) select * from q", "q"),
    # column-definition name: `a` is an ErrorCapturingIdentifier under ColDefinitionContext
    ("create table prod.t (a int) using parquet", "a"),
    # table provider: `parquet` is a MultipartIdentifier under TableProviderContext
    ("create table prod.t (a int) using parquet", "parquet"),
    ("create table t using delta", "delta"),
    # MERGE assignment target: the LHS of `SET s.x = t.x` is a MultipartIdentifier
    # under AssignmentContext
    ("merge into a.b s using c.d t on s.i = t.i "
     "when matched then update set s.x = t.x", "s"),
    # rename-column source/target
    ("alter table t rename column a to b", "a"),
    ("alter table t rename column a to b", "b"),
    # UPDATE assignment target: `a` is a bare ErrorCapturingIdentifier under
    # AssignmentContext -- the same rule as the MERGE SET case above
    ("update prod.t set a = 1", "a"),
    ("update prod.t set a = 1, b = 2 where c = 3", "b"),
    # select-list alias with no table at all
    ("select 1 as one", "one"),
    ("select 'x' as label", "label"),
])
def test_namespace_extraction_does_not_over_collect(sql, noise, spec_key):
    got = names(sql, spec_key)
    assert noise not in got, (
        f"{sql!r} reported column/alias/provider {noise!r} as a namespace; got {got}"
    )


@BUG2
def test_literal_only_select_has_no_namespaces(spec_key):
    """`SELECT 'x' AS label` names no object at all -- not even a table.

    The cleanest demonstration of the over-collection: the only namespace
    reported is the output alias.
    """
    assert names("select 'x' as label", spec_key) == []


@BUG2
def test_join_aliases_are_not_reported(spec_key):
    """The example from the bug report: aliases x/y must not appear."""
    got = sorted(names(
        "select a, b from prod.t as x join other.s as y on x.id = y.id", spec_key))
    assert got == ["other.s", "prod.t"]


def test_insert_into_target_is_found_even_though_the_query_is_narrowed(spec_key):
    """`parser._tighten` re-points `tree` at `InsertIntoTableContext`, which holds
    only the target table -- the source query is outside the labelled alternative
    and is not walked. The destructive target is what matters, and it is found."""
    assert names("insert into prod.t select * from other.s", spec_key) == ["prod.t"]


@pytest.mark.xfail(
    reason=(
        "Not the reported bug, found while validating the BUG 2 fix: on 3.5.1 "
        "`createTableLike` uses `tableIdentifier` (a distinct rule) rather than "
        "`identifierReference`, and `TableIdentifierContext` is absent from "
        "_IDENTIFIER_REF_RULES, so no namespace is found at all for "
        "`CREATE TABLE t2 LIKE prod.t`. Fix: add TableIdentifierContext to "
        "_IDENTIFIER_REF_RULES (it is table-position in both grammars)."
    ),
    strict=False,
)
def test_create_table_like_finds_both_tables(spec_key):
    assert sorted(names("create table t2 like prod.t", spec_key)) == ["prod.t", "t2"]


# ===========================================================================
# 4. extract_string_literals
# ===========================================================================

def test_load_data_local_inpath_is_found(spec_key):
    """The `/etc/passwd` case the extractor exists for."""
    assert literals("load data local inpath '/etc/passwd' into table prod.users",
                    spec_key) == ["/etc/passwd"]


def test_load_data_paths_come_with_their_table(spec_key):
    sql = "load data local inpath '/etc/passwd' into table prod.users"
    assert names(sql, spec_key) == ["prod.users"]
    assert literals(sql, spec_key) == ["/etc/passwd"]


@pytest.mark.xfail(
    reason=(
        "Real gap: `manageResource : op=(ADD | LIST) simpleIdentifier .*?` uses "
        "the `. *?` wildcard, so the resource path is not wrapped in any rule "
        "context -- it arrives as loose terminals. `ADD JAR /tmp/x.jar` therefore "
        "yields no literal, quoted or not. Fix: collect STRING_LITERAL (and, for "
        "the unquoted spelling, the terminals skipped by the wildcard) directly "
        "under ManageResourceContext."
    ),
    strict=False,
)
def test_add_jar_path_is_found(spec_key):
    assert "/tmp/x.jar" in literals("add jar /tmp/x.jar", spec_key)


def test_create_function_using_jar_finds_the_path(spec_key):
    """`USING resource` *is* a proper rule context, so this form is covered."""
    lits = literals("create function f as 'c' using jar '/tmp/x.jar'", spec_key)
    assert "/tmp/x.jar" in lits
    assert "c" in lits


def test_location_spec_is_found(spec_key):
    assert "s3://bucket/path" in literals(
        "create table t (a int) using parquet location 's3://bucket/path'", spec_key)


def test_escaped_single_quote_is_unescaped(spec_key):
    """`''` is Spark's escape for a literal quote inside a string."""
    assert "it's" in literals("select 'it''s' as a", spec_key)


def test_backslash_escape_is_left_alone(spec_key):
    """Only `''` is a Spark escape; `\\'` stays two characters.

    4.0's lexer STRING_LITERAL rule is
    `'\'' ( ~('\''|'\\') | ('\\' .) | ('\'' '\'') )* '\''`, so `\'` is a
    backslash followed by end-of-string -- not an escaped quote. Unescaping it
    would report a different string than the engine has.
    """
    assert "a\\'b" in literals("select 'a\\'b' as c", spec_key)


def test_backslash_n_is_not_interpreted(spec_key):
    """`extract_string_literals` unquotes, it does not interpret escapes.

    `\\n` stays a backslash and an `n`; Spark's own unescaping is a later step
    in the analyzer. Rewriting it here would make a literal-prefix policy match
    a string the engine never sees.
    """
    assert "\\n" in literals("select '\\n' as d", spec_key)


def test_literal_with_no_escape_is_returned_verbatim(spec_key):
    assert literals("select 'plain' as e", spec_key) == ["plain"]


def test_tblproperties_key_and_value_are_both_found(spec_key):
    lits = literals("alter table t set tblproperties ('a'='b')", spec_key)
    assert sorted(lits) == ["a", "b"]


def test_execute_immediate_hides_sql_from_the_top_level(spec_key):
    """`EXECUTE IMMEDIATE 'DROP TABLE ...'` never appears as a top-level
    statement, so a policy that only walks statements would miss it entirely."""
    sql = "EXECUTE IMMEDIATE 'DROP TABLE prod.users'"
    _, tree = parse_one(sql, spec_key)
    assert executed_immediate_sql(tree) == ["DROP TABLE prod.users"]


# ===========================================================================
# 5. invariants
# ===========================================================================

#: Odd but valid statements. Each one is here because it is a shape the parser
#: accepts and the walker has no special knowledge of -- the point is that
#: extraction is total over accepted input, not that it is clever.
CORPUS = [
    "select 1",
    "select * from t",
    "select a.b.c from x.y.z",
    "select `weird name` from t",
    "select identifier('lit.tbl') from t",
    "select ''''",
    "select '' from t",
    "select a, b, c from t order by a desc limit 10",
    "select a from t group by a having count(*) > 1",
    "select a from t1 union all select b from t2",
    "select a from t1 except select b from t2",
    "select a from t1 intersect select b from t2",
    "select a from t1 minus select b from t2",
    "select * from t cross join u",
    "select * from t left outer join u on t.a = u.a",
    "select * from t full join u on t.a = u.a",
    "select * from t natural join u",
    "select * from t, u",
    "select * from t as x, u as y",
    "select * from (select 1) as q",
    "with a as (select 1), b as (select 2) select * from a, b",
    "with recursive r as (select 1) select * from r",
    "select struct(1, 2).a from t",
    "select map_from_arrays(array(1), array(2)) from t",
    "select cast(a as decimal(10, 2)) from t",
    "select a from t where a between 1 and 2",
    "select a from t where a in (1, 2, 3)",
    "select a from t where exists (select 1 from u)",
    "select nvl(a, 0) from t",
    "select a from t tablesample (10 percent)",
    "select a from t version as of 1",
    "select * from t pivot (sum(x) for y in ('a', 'b'))",
    "insert into t values (1), (2), (3)",
    "insert into t partition (p=1) values (1)",
    "insert overwrite table t partition (p=1) select 1",
    "insert overwrite directory '/tmp/out' select 1",
    "delete from t where id in (select id from u)",
    "update t set a = 1, b = 2 where c = 3",
    "merge into a s using b t on s.i = t.i when matched then delete",
    "merge into a s using b t on s.i = t.i when not matched then insert *",
    "create table t (a int, b string) using parquet partitioned by (a) "
    "location 's3://b' tblproperties ('k'='v')",
    "create or replace table t (a int) as select 1",
    "create view v as select 1",
    "create or replace temporary view v using parquet options (path 's3://b')",
    "create namespace if not exists n",
    "create function f as 'c' using resource 'file:///x.jar'",
    "drop function if exists f",
    "drop view if exists v",
    "drop namespace if exists n cascade",
    "msck repair table t",
    "refresh table t",
    "cache table t",
    "uncache table t",
    "set spark.sql.shuffle.partitions=200",
    "set -v",
    "reset",
    "use prod",
    "show tables in prod like 'x*'",
    "show create table t",
    "describe table t",
    "describe formatted t",
    "explain select 1",
    "comment on table t is 'x'",
    "add jar /tmp/x.jar",
    "add file /tmp/x.txt",
    "list jars",
    "truncate table t partition (p=1)",
    "alter table t add columns (a int, b string)",
    "alter table t drop columns (a, b)",
    "alter table t change column a b int",
    "repair table t add partitions",
    "load data local inpath '/etc/passwd' into table t",
    "analyze table t compute statistics",
    "call sys.system_info()",
    "select a from t where a > 1 -- trailing comment",
    "SELECT 1",
    "SeLeCt 1",
    "select 1 /* inline */ + 2",
    "select \"double quoted\" from t",
    "select a from t where b like '%x%'",
    "select interval 1 day",
    "select current_date()",
    "select * from t where a > 1 and (b < 2 or c = 3)",
    "select 1 as one",
    "select * from t for system_time as of 1",
    "select * from read_parquet('s3://b')",
    "values (1), (2)",
    "table t",
    "from t select a",
]

#: Statements the corpus contains that a given grammar does not accept (a 4.0
#: construct on 3.5.1, or vice versa). Parametrised tests skip these rather than
#: asserting a parse failure -- the point of the corpus is the walker, not the
#: version matrix.
NOT_IN_351 = {
    "call sys.system_info()",
    "select a from t version as of 1",
    "select * from t for system_time as of 1",
    "select * from t pivot (sum(x) for y in ('a', 'b'))",
    "with recursive r as (select 1) select * from r",
    "select interval 1 day",
}


def _parseable(sql: str, key: str) -> bool:
    try:
        get_parser(key).parse(sql)
    except SqlSyntaxError:
        return False
    return True


@pytest.mark.parametrize("sql", CORPUS, ids=lambda s: s[:48])
def test_fuzz_lite_extraction_never_raises(sql, spec_key):
    """`extract_namespaces` + `extract_string_literals` must be total over any
    tree the parser accepted.

    Extraction is best-effort by design (a missing identifier is reported as
    absent, never guessed), but "best-effort" is not "may raise": an exception
    here would escape `screen()` and turn a policy decision into a crash. The
    tree, not the text, is what is walked -- so the assertion is on the walk.
    """
    if sql in NOT_IN_351 and spec_key == V351:
        pytest.skip(f"not valid on {spec_key}")
    try:
        parsed = get_parser(spec_key).parse(sql)
    except SqlSyntaxError:
        pytest.skip(f"not accepted by {spec_key}")
    for stmt in parsed.statements:
        ns = extract_namespaces(stmt.tree, grammar_key=spec_key)
        lits = extract_string_literals(stmt.tree)
        assert isinstance(ns, list)
        assert all(isinstance(n, NamespaceRef) for n in ns)
        assert isinstance(lits, list)
        assert all(isinstance(s, str) for s in lits)


@pytest.mark.parametrize("sql", CORPUS, ids=lambda s: s[:48])
def test_fuzz_lite_namespaces_have_no_garbage_components(sql, spec_key):
    """No namespace may contain whitespace-only or comma-bearing components.

    A component like `''` or `'a, b'` means the extractor lost track of where an
    identifier ended and started matching text -- the failure mode that would
    let a protected table name slip past a namespace allowlist by appearing as
    `prod`, `users` and `prod, users` at once.
    """
    if sql in NOT_IN_351 and spec_key == V351:
        pytest.skip(f"not valid on {spec_key}")
    try:
        parsed = get_parser(spec_key).parse(sql)
    except SqlSyntaxError:
        pytest.skip(f"not accepted by {spec_key}")
    for stmt in parsed.statements:
        for ref in extract_namespaces(stmt.tree, grammar_key=spec_key):
            assert ref.parts, f"{sql!r} produced a namespace with no parts"
            for part in ref.parts:
                assert part.strip(), f"{sql!r} produced blank component in {ref.parts!r}"
                assert part.strip() == part, (
                    f"{sql!r} produced untrimmed component {part!r} in {ref.parts!r}"
                )
                assert "," not in part, (
                    f"{sql!r} produced comma-bearing component {part!r} in {ref.parts!r}"
                )
                assert not any(c.isspace() for c in part) or " " in part, sql


@pytest.mark.parametrize("sql,expected", [
    ("select 1", []),
    ("select 'literal only'", []),
])
def test_statements_without_identifiers_yield_empty_lists(sql, expected, spec_key):
    assert names(sql, spec_key) == expected


def test_statement_with_only_a_literal_has_no_namespaces(spec_key):
    """`SELECT 'x'` names no object; an empty target list is the honest answer."""
    _, tree = parse_one("select 'x' as label", spec_key)
    assert extract_namespaces(tree, grammar_key=spec_key) == []


def test_empty_label_is_never_returned(spec_key):
    """A context that is a wrapper must always resolve to a concrete label.

    `effective_label` bottoms out at `StatementDefault`; it never returns
    `""`/`None`, because a falsy label would silently match a policy rule with
    no `labels` (which applies to *everything*).
    """
    from sparkscreen.analysis.treewalk import WRAPPER_LABELS, _is_wrapper

    for sql in ("select 1", "drop table t", "insert into t values (1)",
                "with q as (select 1) select * from q"):
        ctxs = list(top_level_statement_contexts(get_parser(spec_key).parse(sql).tree))
        for ctx in ctxs:
            label = effective_label(ctx)
            assert label
            assert isinstance(label, str)
            assert not _is_wrapper(label) or label == QUERY_LABEL


def test_wrapper_labels_are_actually_treated_as_wrappers():
    """Sanity-check the wrapper set the label tests depend on."""
    from sparkscreen.analysis.treewalk import WRAPPER_LABELS

    for label in ("DmlStatement", "SingleInsertQuery", "Ctes"):
        assert label in WRAPPER_LABELS
    # a real statement kind must not be listed as a wrapper
    for label in ("DropTable", "InsertOverwriteTable", "StatementDefault"):
        assert label not in WRAPPER_LABELS


# ---- grammar_key consistency ----------------------------------------------

def test_identifier_token_numbers_differ_between_grammars():
    """The premise of the `grammar_key` parameter: the two grammars number
    IDENTIFIER differently, so resolving against the wrong one yields an empty
    target list rather than an error."""
    four = identifier_token_types(V4)
    three = identifier_token_types(V351)
    assert four and three
    assert four != three, (
        "the two pinned grammars happen to number IDENTIFIER identically; "
        "the per-grammar_key cache is still correct, but this test's premise "
        "no longer holds and should be revisited"
    )


@pytest.mark.parametrize("sql", [
    "drop table prod.users",
    "select * from prod.t",
    "select * from `prod`.`users`",
    "truncate table prod.users",
    "delete from prod.t where id = 1",
])
def test_grammar_key_must_match_the_parser_it_came_from(sql, spec_key):
    """Extraction with the matching key finds the target; with the *other* key
    it finds nothing.

    This is the failure the `grammar_key` parameter exists to prevent, so it is
    pinned as a differential: wrong key is empty, right key is correct. An
    empty result is the dangerous direction (a destructive target goes
    unreported), which is why `screen.py` threads `spec.key` through.
    """
    other = V351 if spec_key == V4 else V4
    right = names(sql, spec_key)
    assert right, f"{sql!r} yielded nothing even with the matching key"
    _, tree = parse_one(sql, spec_key)
    assert [n.name for n in extract_namespaces(tree, grammar_key=other)] == []


def test_grammar_key_defaults_to_the_default_spec():
    """`grammar_key=None` resolves against the default grammar, so it agrees
    with that grammar and disagrees with the other one."""
    _, tree = parse_one("drop table prod.users", V4)
    assert [n.name for n in extract_namespaces(tree, grammar_key=None)] == ["prod.users"]
    _, tree3 = parse_one("drop table prod.users", V351)
    assert [n.name for n in extract_namespaces(tree3, grammar_key=None)] == []


def test_grammar_key_cache_is_per_grammar_not_global():
    """Repeated calls for different keys must not share a cached token set."""
    a = identifier_token_types(V4)
    b = identifier_token_types(V351)
    assert a == identifier_token_types(V4)
    assert b == identifier_token_types(V351)
    assert a != b
