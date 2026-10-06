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


#: The canonical seed pool for the mutation sweeps, shared by the JVM-free property
#: fuzz (tests/test_properties.py section 5) and the live-engine sweep
#: (test_fuzz_against_real_spark.py). Mutating ONE pool is the point: the two layers
#: fuzz the same surface, and the seed-completeness guard in test_properties.py
#: asserts this pool reaches every destructive label in LABEL_EFFECTS, so destructive
#: statement kinds cannot silently leave the fuzzed surface when seeds drift.
FUZZ_SEEDS: tuple[str, ...] = (
    # Reads: the benign half the sweeps must not scare into a verdict.
    "select 1",
    "select * from prod.t",
    "select a from t1 where a > 1 group by a having count(*) > 1 order by a limit 5",
    "with x as (select 1) select * from x",
    # Destructive: namespaced objects. Each seed reaches one destructive label; the
    # seed-completeness guard in test_properties.py enforces the mapping both ways.
    "drop table prod.users",
    "drop view prod.v",
    "drop index i on t",
    "drop schema prod.s",
    "drop function f",
    "truncate table t",
    # Destructive: table shape, partitions, constraints.
    "alter table t add column b int",
    "alter table t drop column a",
    "alter table t drop if exists partition (a = 1)",
    "alter table t drop constraint c",          # parses on 4.1+ only
    "alter table t replace columns (a int)",
    "replace table t (a int) using parquet",
    # Destructive: rows and writes.
    "delete from t where id = 1",
    "update t set a = 1",
    "merge into prod.t using s on t.id = s.id when matched then update set *",
    "insert into t values (1)",
    "insert overwrite table prod.t select 1",
    "insert overwrite directory '/tmp/x' select 1",          # the Hive-dir form
    "insert overwrite directory '/tmp/x' using csv select 1",  # the tableProvider form
    "insert into t replace where a > 1 select 1",   # InsertIntoReplaceWhere / BooleanCond
    "insert into t replace using (a) select 1",     # 4.2 only
    "insert into t replace on a > 1 values (2)",    # 4.2 only
    "from s insert into t1 select 1 insert into t2 select 2",  # F26: crashes screen()
    # Code loading.
    "add jar /tmp/x.jar",
    "create function f as 'x.y'",
    "create function f(a int) returns int return a + 1",       # 4.x SQL UDF form
    # Config: the INVALID_SET_SYNTAX divergence family (F27).
    "set spark.sql.shuffle.partitions=200",
    # Uppercase spellings. Real agent code is mostly lowercase and the corpus is
    # written lowercase, but F6 came from exactly that assumption -- a few uppercase
    # seeds keep the mutation pool case-honest.
    "SELECT 1",
    "SELECT * FROM prod.t",
    "DROP TABLE prod.users",
    "TRUNCATE TABLE t",
    "SET spark.sql.shuffle.partitions=200",
    "ALTER TABLE t ADD COLUMN b INT",
)


# ---------------------------------------------------------------------------
# Per-engine expectations, recorded against three live engines.
# ---------------------------------------------------------------------------
# CI matrixes the differential suite over real Spark 3.5.1, 4.1.3 and 4.2.0
# (see the `differential` job in .github/workflows/ci.yml). A single boolean cannot
# describe a statement whose behaviour changes across those, so `ENGINE_EXPECTATIONS`
# says, per engine version, what that engine was *observed* to do.
#
# Every value here was observed by running the statement through the engine named in the
# key -- live sessions, not the grammar, not memory. That distinction is the whole reason
# this table exists and it is not decorative: three of these expectations contradict what
# the grammar-only analysis predicted, in both directions. See the comments on the entries.
#
# Recorded with pyspark 3.5.1 / 4.1.3 / 4.2.0 on JDK 17, using `real_spark_verdict`
# below, i.e. ParseException means rejected and anything else means accepted.
ENGINE_EXPECTATIONS: dict[str, tuple[tuple[str, bool], ...]] = {
    # --- accepted by 4.2.0 only: the F18 six ------------------------------------
    # Each is the 4.2 form of its construct. `QUALIFY`, both `CHANGES` shapes and both
    # `NEAREST` shapes are absent from the 4.1.3 grammar, and 4.1.3 rejects them at the
    # parser with PARSE_SYNTAX_ERROR -- verified, not inferred.
    "4.2.0": (
        ("SELECT a FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1", True),
        ("SELECT * FROM t CHANGES FROM VERSION 1", True),
        ("SELECT * FROM t CHANGES FROM SYSTEM_VERSION 1 TO VERSION 9", True),
        ("SELECT * FROM a JOIN b APPROX NEAREST BY DISTANCE a.p", True),
        ("SELECT * FROM a JOIN b EXACT NEAREST BY SIMILARITY a.p", True),
        # The 4.2 half of the `insertIntoReplaceWhere` split. 4.2's rule is
        # `REPLACE (WHERE | ON) booleanExpression`; the `ON` spelling is 4.2-only and
        # 4.1.3's error message for it is "missing 'WHERE'", which is the parse failure
        # naming the rule that was replaced. Deriving this from the rule body rather than
        # from memory is what makes it a test of the split.
        ("INSERT INTO t REPLACE ON a > 1 VALUES (2)", True),
    ),
    "4.1.3": (
        ("SELECT a FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1", False),
        ("SELECT * FROM t CHANGES FROM VERSION 1", False),
        ("SELECT * FROM t CHANGES FROM SYSTEM_VERSION 1 TO VERSION 9", False),
        ("SELECT * FROM a JOIN b APPROX NEAREST BY DISTANCE a.p", False),
        ("SELECT * FROM a JOIN b EXACT NEAREST BY SIMILARITY a.p", False),
        ("INSERT INTO t REPLACE ON a > 1 VALUES (2)", False),
    ),
    "3.5.1": (
        ("SELECT a FROM t QUALIFY ROW_NUMBER() OVER (ORDER BY a) = 1", False),
        ("SELECT * FROM t CHANGES FROM VERSION 1", False),
        ("SELECT * FROM t CHANGES FROM SYSTEM_VERSION 1 TO VERSION 9", False),
        ("SELECT * FROM a JOIN b APPROX NEAREST BY DISTANCE a.p", False),
        ("SELECT * FROM a JOIN b EXACT NEAREST BY SIMILARITY a.p", False),
        ("INSERT INTO t REPLACE ON a > 1 VALUES (2)", False),
    ),
}


#: Constructs present in *both* 4.x lines and rejected by 3.5.1 -- the 4.1-era features
#: that survive into 4.2. Kept separate from `ENGINE_EXPECTATIONS` because the point is
#: that 4.1.3 and 4.2.0 agree here: it is the "believed not to diverge" half of the
#: matrix, and the reason 4.1 and 4.2 are separate jobs is to be first to know if that
#: belief stops being true.
BOTH_FOUR_X_ACCEPTED: tuple[str, ...] = (
    "CREATE TABLE t (a INT PRIMARY KEY)",
    "CREATE TABLE t (a INT, FOREIGN KEY (a) REFERENCES u (b))",
    "CREATE STREAMING TABLE x PARTITIONED BY (a) AS SELECT * FROM t",
)


#: And two of the brief's claims that the live engines did NOT confirm, recorded so the
#: matrix cannot quietly "fix" them later by editing the probe.
#:
#: `WINDOW` and `TABLESAMPLE` were listed as 4.1-era constructs that 3.5.1 rejects. Real
#: 3.5.1 accepts both (they reach TABLE_OR_VIEW_NOT_FOUND, so they parsed). The shipped
#: 3.5.1 grammar agrees with the engine and also accepts both. So they are not evidence
#: of anything about the 4.x line, and asserting otherwise would have been a probe bug of
#: exactly the kind F18 warns about: two independent sources agreeing that nothing is
#: wrong while the note claims otherwise.
BOTH_FOUR_X_ACCEPTED_WITHOUT_ENGINE_AGREEMENT: tuple[str, ...] = (
    "SELECT sum(a) OVER w FROM t WINDOW w AS (ORDER BY a)",
    "SELECT * FROM t TABLESAMPLE (10 PERCENT)",
)


#: Constructs where the engine's *grammar* accepts but the engine itself rejects
#: before the statement is built. Discovered by the mutation fuzz (F27), confirmed on
#: live 3.5.1 / 4.1.3 / 4.2.0 via `parsePlan`. The engine's *grammar* accepts each of
#: these -- `SET .*?` matches the first family, and the type rule deliberately matches
#: an identifier for the second -- but the engine's AST builder rejects them with the
#: error class recorded per row. sparkscreen parses with the grammar and therefore
#: accepts them: the recorded divergence class. Screened verdicts stay fail-closed
#: (REVIEW and DENY respectively), so the gap costs a confident verdict on SQL the
#: engine will never run, not a wrong verdict on SQL it will.
#:
#: These rows are the *evidence list*, not the tolerance set. The sweep
#: (test_fuzz_against_real_spark.py) tolerates by the engine's error CLASS -- see
#: TOLERATED_REJECTION_CLASSES there -- because the deep scheduled sweep generates
#: dozens of members per family, which no exact-string set tracks. A new member with a
#: KNOWN class is tolerated and printed; a member with PARSE_SYNTAX_ERROR (grammar
#: level, a port defect) or an unrecognised class fails the sweep. New evidence rows
#: still belong here, with the class that was observed.
KNOWN_AST_LAYER_REJECTIONS: tuple[tuple[str, str], ...] = (
    ("SET (spark.sql.shuffle.partitions=200)", "INVALID_SET_SYNTAX"),
    ("set( spark.sql.shuffle.partitions=200)", "INVALID_SET_SYNTAX"),
    ("ALTER TABLE t ADD COLUMN b unionINT", "UNSUPPORTED_DATATYPE"),
    ("alter table t add column b intunion", "UNSUPPORTED_DATATYPE"),
    ("alter table t add column b partitionint", "UNSUPPORTED_DATATYPE"),
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