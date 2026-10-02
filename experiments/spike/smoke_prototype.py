import sys, os
sys.path.insert(0, "/opt/data/cache/scratch/gen_test/ported")
from antlr4 import InputStream, CommonTokenStream, ParseTreeWalker, BailErrorStrategy
from antlr4.error.ErrorListener import ErrorListener
from antlr4.error.Errors import ParseCancellationException
import SqlBaseLexer, SqlBaseParser
from SqlBaseParserVisitor import SqlBaseParserVisitor


class Collector(ErrorListener):
    def __init__(self):
        self.errors = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.errors.append(f"line {line}:{column} {msg}")


class TopLevel(SqlBaseParserVisitor):
    """Collect the labeled alternatives of top-level statements."""

    def __init__(self):
        self.kinds = []

    def visitCompoundOrSingleStatement(self, ctx):
        single = ctx.singleStatement()
        if single is not None:
            st, srs = single.statement(), single.setResetStatement()
            return self._label(st or srs)
        compound = ctx.singleCompoundStatement()
        if compound is not None:
            return self._compound(compound)
        return self.visitChildren(ctx)

    def _compound(self, ctx):
        body = ctx.compoundBody()
        if body is None:
            return
        for st in body.compoundStatement():
            self._label(st.statement() or st.setStatementInsideSqlScript())

    def _label(self, ctx):
        if ctx is None:
            return
        name = type(ctx).__name__
        if name.endswith("Context"):
            self.kinds.append(name[:-len("Context")])
        self.visitChildren(ctx)


def parse(sql):
    """Strict parse: no error recovery, so nothing is silently accepted.

    BailErrorStrategy is mandatory here -- with ANTLR's default recovery
    strategy `INSERT INTO t SELECT * FROM` yields a usable tree despite being
    invalid, which would let a screener approve malformed SQL.
    """
    errs = Collector()
    lexer = SqlBaseLexer.SqlBaseLexer(InputStream(sql))
    lexer.removeErrorListeners()
    lexer.addErrorListener(errs)
    tokens = CommonTokenStream(lexer)
    parser = SqlBaseParser.SqlBaseParser(tokens)
    parser.removeErrorListeners()
    parser.addErrorListener(errs)
    parser._errHandler = BailErrorStrategy()
    try:
        tree = parser.compoundOrSingleStatement()
    except ParseCancellationException:
        return ("ERROR", ["parse error"])
    except RecursionError:
        return ("ERROR", ["recursion limit"])
    if errs.errors:
        return ("ERROR", errs.errors)
    v = TopLevel()
    v.visit(tree)
    return ("OK", v.kinds)


CASES = [
    ("SELECT 1", ["StatementDefault"]),
    ("SELECT * FROM t WHERE a > 1 GROUP BY b HAVING count(*) > 2 ORDER BY b LIMIT 10",
     ["StatementDefault"]),
    ("SELECT a FROM t1 UNION ALL SELECT b FROM t2",
     ["StatementDefault"]),
    ("WITH x AS (SELECT 1) SELECT * FROM x", ["StatementDefault"]),
    ("DROP TABLE prod.users", ["DropTable"]),
    ("DROP TABLE IF EXISTS prod.users PURGE", ["DropTable"]),
    ("DROP VIEW v", ["DropView"]),
    ("DROP DATABASE IF EXISTS prod CASCADE", ["DropNamespace"]),
    ("TRUNCATE TABLE t", ["TruncateTable"]),
    ("INSERT OVERWRITE TABLE prod.t SELECT 1", ["DmlStatement"]),
    ("INSERT INTO t VALUES (1)", ["DmlStatement"]),
    ("INSERT OVERWRITE DIRECTORY '/tmp/x' SELECT 1", ["DmlStatement"]),
    ("DELETE FROM t WHERE id = 1", ["DmlStatement"]),
    ("UPDATE t SET a = 1 WHERE b = 2", ["DmlStatement"]),
    ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET *",
     ["DmlStatement"]),
    ("CREATE TABLE t (a INT) USING parquet", ["CreateTable"]),
    ("CREATE OR REPLACE TABLE t AS SELECT 1", ["ReplaceTable"]),
    ("REPLACE TABLE t AS SELECT 1", ["ReplaceTable"]),
    ("ALTER TABLE t ADD COLUMN b INT", ["AddTableColumns"]),
    ("ALTER TABLE t DROP PARTITION (dt='1')", ["DropTablePartitions"]),
    ("ALTER TABLE t SET LOCATION 's3://x'", ["SetTableLocation"]),
    ("ALTER TABLE t RENAME TO t2", ["RenameTable"]),
    ("ALTER VIEW v AS SELECT 1", ["AlterViewQuery"]),
    ("MSCK REPAIR TABLE t", ["RepairTable"]),
    ("REFRESH TABLE t", ["RefreshTable"]),
    ("LOAD DATA LOCAL INPATH '/etc/passwd' OVERWRITE INTO TABLE t", ["LoadData"]),
    ("CREATE FUNCTION f AS 'com.x.Y' USING JAR '/tmp/e.jar'", ["CreateFunction"]),
    ("ADD JAR /tmp/e.jar", ["ManageResource"]),
    ("LIST JARS", ["ManageResource"]),
    ("USE prod", ["Use"]),
    ("CREATE DATABASE IF NOT EXISTS d", ["CreateNamespace"]),
    ("SET spark.sql.shuffle.partitions=200", ["SetConfiguration"]),
    ("SET -v", ["SetConfiguration"]),
    ("RESET spark.sql.shuffle.partitions", ["ResetConfiguration"]),
    ("SET ROLE admin", ["FailSetRole"]),
    ("CREATE ROLE r", ["FailNativeCommand"]),
    ("GRANT ALL ON TABLE t TO u", ["FailNativeCommand"]),
    ("CALL sys.system_info()", ["Call"]),
    ("CACHE TABLE t", ["CacheTable"]),
    ("UNCACHE TABLE t", ["UncacheTable"]),
    ("CLEAR CACHE", ["ClearCache"]),
    ("CREATE INDEX i ON TABLE t (a)", ["CreateIndex"]),
    ("DROP INDEX i ON TABLE t", ["DropIndex"]),
    ("ANALYZE TABLE t COMPUTE STATISTICS", ["Analyze"]),
    ("CREATE VIEW v AS SELECT 1", ["CreateView"]),
    ("CREATE OR REPLACE VIEW v AS SELECT 1", ["CreateView"]),
    ("EXPLAIN SELECT 1", ["Explain"]),
    ("EXPLAIN EXTENDED DROP TABLE t", ["Explain"]),
    ("SHOW TABLES", ["ShowTables"]),
    ("DESC TABLE t", ["DescribeRelation"]),
    ("COMMENT ON TABLE t IS 'x'", ["CommentTable"]),
    ("EXECUTE IMMEDIATE 'DROP TABLE t'", ["VisitExecuteImmediate"]),
    ("BEGIN DROP TABLE t; END", ["DropTable"]),
    ("CREATE MATERIALIZED VIEW mv AS SELECT 1", ["CreatePipelineDataset"]),
    ("ALTER TABLE t SET TBLPROPERTIES ('k'='v')", ["SetTableProperties"]),
    ("ALTER TABLE t UNSET TBLPROPERTIES ('k')", ["UnsetTableProperties"]),
    ("ALTER TABLE t ADD IF NOT EXISTS PARTITION (dt='1') LOCATION 'x'",
     ["AddTablePartition"]),
    ("ALTER NAMESPACE d SET LOCATION 'x'", ["SetNamespaceLocation"]),
    ("SELECT CAST(x AS STRUCT<a: INT>) FROM t", ["StatementDefault"]),
    ("SELECT a -> 'b' FROM t", ["StatementDefault"]),
    ("SELECT 1 |> SELECT 2", ["StatementDefault"]),
    # `INTERSECT` is only a set operator after a left-hand queryTerm
    ("SELECT 1 INTERSECT SELECT 2", ["StatementDefault"]),
    # $$...$$ is a codeLiteral (createMetricView), not a general string literal
    ("SELECT 1 |> SELECT 2", ["StatementDefault"]),
    ("CREATE METRIC VIEW mv SQL 'SELECT 1'", ["CreateMetricView"]),
    ("SELECT /*+ BROADCAST(t) */ * FROM t", ["StatementDefault"]),
    ("SELECT 'unclosed /* comment", "ERROR"),
    ("SELCT 1", "ERROR"),
    ("DROP TABLE t; DROP TABLE u", "ERROR"),
    ("INSERT INTO t SELECT * FROM", "ERROR"),
    ("SELECT * FROM t WHERE", "ERROR"),
    ("SELECT * FROM t JOIN", "ERROR"),
    # unterminated / unbalanced constructs
    ("SELECT (1", "ERROR"),
    ("SELECT 'a", "ERROR"),
    ("/* unclosed", "ERROR"),
    # multi-statement scripts must go through BEGIN...END, like Spark itself
    ("BEGIN DROP TABLE t; DROP VIEW v; END", ["DropTable", "DropView"]),
]

fails = 0
for sql, expect in CASES:
    status, got = parse(sql)
    if status == "ERROR":
        if expect == "ERROR":
            print(f"ok   (reject) {sql!r:70.70} -> {got[0]}")
        else:
            fails += 1
            print(f"FAIL {sql!r:70.70} expected {expect} but rejected: {got[0]}")
        continue
    if expect == "ERROR":
        fails += 1
        print(f"FAIL {sql!r:70.70} expected rejection, got {got}")
        continue
    if got != expect:
        fails += 1
        print(f"FAIL {sql!r:70.70} expected {expect} got {got}")
    else:
        print(f"ok   {sql!r:70.70} -> {got[0]}")

print()
print(f"{len(CASES) - fails}/{len(CASES)} passed")
sys.exit(1 if fails else 0)