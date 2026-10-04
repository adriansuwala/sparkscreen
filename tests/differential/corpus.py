"""Corpus shared by the differential harnesses, with recorded expectations.

`CORPUS` entries are (sql, spark_accepts) where `spark_accepts` was **observed** by
running the statement through a real Spark session, not inferred. A `ParseException`
means the engine rejected it; anything else (including `AnalysisException`, meaning the
table does not exist) means it got past parsing.

Recording the observation matters. An earlier version of this file asserted that
`INSERT INTO t SELECT * FROM` should be rejected; real Spark accepts it, because
`errorCapturingIdentifier` deliberately admits the malformed identifier so the engine
can report a better message later. The hand-written corpus was wrong and the parser was
right.
"""
from __future__ import annotations

# (sql, real_spark_parses)
CORPUS: tuple[tuple[str, bool], ...] = (
    # --- accepted by the engine, and by us ---
    ("SELECT 1", True),
    ("select 1", True),
    ("SeLeCt 1", True),
    ("SELECT * FROM prod.t", True),
    ("select * from prod.t", True),
    ("SELECT a FROM t1 WHERE a > 1", True),
    ("DROP TABLE prod.users", True),
    ("drop table prod.users", True),
    ("TRUNCATE TABLE t", True),
    ("INSERT OVERWRITE TABLE t SELECT 1", True),
    ("MERGE INTO prod.t USING s ON t.id=s.id WHEN MATCHED THEN UPDATE SET *", True),
    ("ADD JAR /tmp/x.jar", True),
    ("LOAD DATA LOCAL INPATH '/etc/passwd' INTO TABLE t", True),
    ("MSCK REPAIR TABLE t", True),
    ("SET spark.sql.shuffle.partitions=200", True),
    ("COMMENT ON TABLE t IS 'x'", True),
    ("SELECT CAST(x AS STRUCT<a: INT>) FROM t", True),

    # --- accepted by the engine via errorCapturingIdentifier ---
    # The engine parses these, then fails during analysis. So must we, otherwise we
    # are stricter than the engine for no security gain.
    ("SELECT * FROM", True),
    ("SELECT * FROM t WHERE", True),
    ("SELECT * FROM t GROUP", True),
    ("INSERT INTO t SELECT * FROM", True),

    # --- rejected by the engine ---
    ("SELCT 1", False),
    ("INSERT INTO", False),
    ("CREATE TABLE", False),
    ("DROP TABLE", False),
    ("DROP TABLE t WHERE", False),
    ("MERGE INTO t", False),
    ("SELECT ((((1))))))", False),
    ("SELECT * FROM t JOIN", False),
    ("SELECT * FROM t WHERE AND OR", False),
    ("SELECT $$abc$$", False),
    ("SELECT 'unterminated", False),
    ("/* unclosed comment", False),
    ("DROP TABLE t; DROP TABLE u", False),

    # --- version-specific: see VERSION_SPECIFIC below ---
)


#: Statements whose acceptance differs between the pinned Spark versions. Kept separate
#: from CORPUS because a single boolean cannot describe them.
VERSION_SPECIFIC: tuple[tuple[str, str, bool], ...] = (
    # (sql, grammar_key_that_accepts_it, observed)
    ("SELECT 1 |> SELECT 2", "spark-4.2", True),
    ("BEGIN DROP TABLE a; DROP VIEW b; END", "spark-4.2", True),
)

#: And the ones the older grammar must reject.
VERSION_SPECIFIC_REJECTED: tuple[tuple[str, str], ...] = (
    ("SELECT 1 |> SELECT 2", "spark-3.5.1"),
    ("BEGIN DROP TABLE a; DROP VIEW b; END", "spark-3.5.1"),
    ("CALL sys.system_info()", "spark-3.5.1"),
)


def real_spark_verdict(spark_session, sql: str) -> bool:
    """True when `spark.sql(sql)` gets past *parsing*.

    Distinguishing parse failures from analysis failures is the whole point: an
    AnalysisException means the statement is valid SQL that happens to reference
    something absent, which for our purposes counts as accepted.
    """
    from pyspark.errors import ParseException

    try:
        spark_session.sql(sql).collect()
        return True
    except ParseException:
        return False
    except Exception:
        # AnalysisException, AnalysisTimeOut, etc. -- parsing succeeded.
        return True