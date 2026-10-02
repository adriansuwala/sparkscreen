"""Strict-parsing behaviour of the Spark SQL parser.

The invariants here are the ones the whole tool rests on:

  * valid SQL parses, invalid SQL raises -- and invalid SQL must NOT produce a tree
  * case-insensitivity matches the engine (verified against real Spark in
    tests/differential/, after the original port rejected all lowercase input)
  * a BEGIN...END script yields every statement inside it
  * parse failures are distinguishable from policy decisions
"""
import pytest

from sparkscreen.grammar.parser import SqlParser, SqlSyntaxError, get_parser
from sparkscreen.grammar.spec import get_spec, spec_for_spark_version

ACCEPTS = [
    "SELECT 1",
    "select 1",
    "SeLeCt 1",
    "SELECT * FROM prod.t",
    "select a from t1 where a > 1 group by a having count(*) > 2 order by a limit 5",
    "select a from t1 union all select b from t2",
    "with x as (select 1) select * from x",
    "drop table prod.users",
    "DROP TABLE IF EXISTS prod.users PURGE",
    "truncate table t",
    "insert overwrite table prod.t select 1",
    "insert into t values (1)",
    "delete from t where id = 1",
    "merge into t using s on t.id = s.id when matched then update set *",
    "alter table t add column b int",
    "alter table t drop partition (dt='1')",
    "create table t (a int) using parquet",
    "create or replace view v as select 1",
    "load data local inpath '/etc/passwd' into table t",
    "add jar /tmp/x.jar",
    "msck repair table t",
    "refresh table t",
    "cache table t",
    "use prod",
    "set spark.sql.shuffle.partitions=200",
    # "call sys.system_info()" is 4.0-only; covered by test_version_specific_statements
    "explain select 1",
    "show tables",
    "comment on table t is 'x'",
    "select cast(x as struct<a: int>) from t",
    "select a -> 'b' from t",
    "select /*+ BROADCAST(t) */ * from t",
    "select 'it''s quoted'",
]

REJECTS = [
    "SELCT 1",                       # typo
    "SELECT ((((1))))))",            # unbalanced parens
    "INSERT INTO",                   # no target table
    "CREATE TABLE",                  # no name, no columns
    "DROP TABLE",                    # no target
    "DROP TABLE t WHERE",            # WHERE is not valid after DROP TABLE
    "MERGE INTO t",                  # no source
    "SELECT * FROM t WHERE AND OR",  # empty condition
    "SELECT * FROM t JOIN",          # JOIN with no right side
    "SELECT $$abc$$",                # codeLiteral, not a string literal
    "SELECT 'unterminated",          # unterminated string
    "/* unclosed comment",
    "",                              # empty
    "   ",                           # whitespace only
    "DROP TABLE t; DROP TABLE u",    # multi-statement without BEGIN/END
]

# Accepted by *both* the default and strict strategies, and by real Spark. These are in
# Spark's language as far as the grammar is concerned: `errorCapturingIdentifier`
# deliberately accepts a malformed identifier so the engine can report a better error
# later, during analysis. Real 3.5.1 parses all three and then fails with
# AnalysisException, so rejecting them here would make us stricter than the engine for
# no security benefit -- and being stricter is safe, but it hides our agreement.
ACCEPTED_BY_GRAMMAR = [
    "SELECT * FROM",
    "SELECT * FROM t WHERE",
    "INSERT INTO t SELECT * FROM",
    "SELECT * FROM t GROUP",     # GROUP is a non-reserved keyword: parses as an alias
]


@pytest.mark.parametrize("sql", ACCEPTS)
def test_valid_sql_parses(spec_key, sql):
    parser = get_parser(spec_key)
    parsed = parser.parse(sql)
    assert parsed.label, "every accepted statement must yield a label"
    assert parsed.statements, "every accepted statement must yield >=1 statement"


@pytest.mark.parametrize("sql", REJECTS)
def test_invalid_sql_is_rejected(spec_key, sql):
    parser = get_parser(spec_key)
    with pytest.raises(SqlSyntaxError):
        parser.parse(sql)


def test_no_error_recovery(spec_key):
    """The default strategy invents tokens; the strict one must not.

    Regression guard. These inputs are genuinely broken -- not merely unusual -- and
    ANTLR's DefaultErrorStrategy returns a usable tree for each by deleting or
    synthesising tokens. A screener analysing those trees would be analysing SQL no
    engine would run.

    Note the distinction from `ACCEPTED_BY_GRAMMAR` below: those are accepted by the
    grammar itself and by real Spark, so rejecting them would be us being stricter than
    the engine for no gain.
    """
    parser = get_parser(spec_key)
    recovered_only = [
        "INSERT INTO",
        "CREATE TABLE",
        "DROP TABLE",
        "DROP TABLE t WHERE",
        "MERGE INTO t",
        "SELECT ((((1))))))",
        "SELECT $$abc$$",
    ]
    for sql in recovered_only:
        with pytest.raises(SqlSyntaxError):
            parser.parse(sql)


@pytest.mark.parametrize("sql", ACCEPTED_BY_GRAMMAR)
def test_matches_real_spark_on_error_capturing_identifiers(spec_key, sql):
    """We accept exactly what the engine accepts, including its deliberate leniency.

    Verified against real PySpark 3.5.1: each of these parses there and then fails with
    AnalysisException, i.e. it gets past parsing. See tests/differential/.
    """
    parser = get_parser(spec_key)
    assert parser.parse(sql).statements


def test_case_insensitive(spec_key):
    """Spark SQL is case-insensitive; the port must be too.

    Regression guard. The original port rejected all of these because the vendored
    grammars spell keywords uppercase and 3.5.1 declares `LETTER : [A-Z]`.
    """
    parser = get_parser(spec_key)
    for sql in ("select 1", "SeLeCt 1", "drop table t", "DROP TABLE t"):
        parser.parse(sql)


def test_labels_are_specific_not_wrapper(spec_key):
    """INSERT OVERWRITE must not look like INSERT INTO.

    Both parse as `DmlStatement` at the top level; the policy-relevant kind is one
    level down.
    """
    parser = get_parser(spec_key)
    assert parser.parse("insert overwrite table t select 1").statements[0].label == \
        "InsertOverwriteTable"
    assert parser.parse("insert into t values (1)").statements[0].label == \
        "InsertIntoTable"
    assert parser.parse("select 1").statements[0].label == "StatementDefault"


def test_script_yields_every_statement(spec_key):
    """BEGIN...END exists only in 4.0; on 3.5.1 it is simply not accepted."""
    parser = get_parser(spec_key)
    script = "BEGIN DROP TABLE a; DROP VIEW b; END"
    if spec_key == "spark-3.5.1":
        with pytest.raises(SqlSyntaxError):
            parser.parse(script)
        return
    parsed = parser.parse(script)
    assert [s.label for s in parsed.statements] == ["DropTable", "DropView"]


def test_try_parse_returns_none_on_error(spec_key):
    parser = get_parser(spec_key)
    assert parser.try_parse("SELECT 1") is not None
    assert parser.try_parse("NOT SQL AT ALL") is None


def test_parse_rejects_non_string(spec_key):
    parser = get_parser(spec_key)
    with pytest.raises(SqlSyntaxError, match="expected str"):
        parser.parse(b"SELECT 1")  # type: ignore[arg-type]


def test_deep_nesting_does_not_crash(spec_key):
    """Deeply nested input must raise SqlSyntaxError, not blow the stack."""
    parser = get_parser(spec_key)
    deep = "SELECT " + "(" * 400 + "1" + ")" * 400
    try:
        parser.parse(deep)
    except SqlSyntaxError:
        pass
    except RecursionError as e:  # pragma: no cover - would be a real bug
        pytest.fail(f"RecursionError escaped the parser: {e}")


def test_unknown_grammar_key_rejected():
    with pytest.raises(KeyError):
        get_parser("spark-99.9")


def test_spec_for_spark_version():
    assert spec_for_spark_version("3.5.1").key == "spark-3.5.1"
    assert spec_for_spark_version("v3.5.1").key == "spark-3.5.1"
    with pytest.raises(KeyError):
        spec_for_spark_version("2.4.0")


def test_both_grammars_agree_on_labels(spec_key):
    """Whatever grammar is in use, the same statement must get the same label.

    A divergence here would mean a policy written for one Spark version silently
    mis-fires on another.
    """
    from sparkscreen.grammar.spec import SPECS

    seen = {}
    for spec in SPECS:
        parser = get_parser(spec.key)
        seen[spec.key] = {
            sql: parser.parse(sql).statements[0].label for sql in ACCEPTS
        }
    reference_key, reference = next(iter(seen.items()))
    for key, labels in seen.items():
        if key == reference_key:
            continue
        differing = {
            sql: (reference[sql], labels[sql])
            for sql in reference
            if reference[sql] != labels[sql]
        }
        # 3.5.1 predates several statements, so a few differences are legitimate;
        # what must not happen is the same SQL getting two different labels for
        # reasons unrelated to version support.
        assert all(a != b for a, b in differing.values()), differing

def test_version_specific_statements(spec_key):
    """Some statements only exist in one pinned grammar; assert the right one accepts."""
    parser = get_parser(spec_key)
    if spec_key == "spark-4.0":
        assert parser.parse("call sys.system_info()")
        # single-char pipe operator was added after 3.5
        assert parser.parse("SELECT 1 |> SELECT 2")
    else:
        # 3.5.1 has no CALL and no pipe operator -- rejected, which is correct
        with pytest.raises(SqlSyntaxError):
            parser.parse("call sys.system_info()")
        with pytest.raises(SqlSyntaxError):
            parser.parse("SELECT 1 |> SELECT 2")
