"""Differential test for F14: is a non-reserved-keyword component part of the table name?

The static screener used to drop any dotted-name component spelled as a non-reserved
*keyword* -- `prod.x` came out as `prod`, `prod.x.y` as `prod.y`. `x` is the memorable
case because the lexer has `BINARY_HEX: 'X'`, so it is lexed as a hex-literal marker
rather than an identifier, but the behaviour covers 425 of the 428 `nonReserved`
alternatives.

This is the oracle that settles *meaning*, which the grammar alone cannot (AGENTS.md:
the grammar is a good oracle for shape and a poor one for meaning). The question is not
"does this parse" -- it obviously parses -- but "when Spark reads `prod.AFTER`, is
`AFTER` part of the name or something else?". So each case is answered by:

  1. seeding a table whose name component is the keyword under test, via the DataFrame
     API (`saveAsTable` -- the in-memory catalog has no REPLACE TABLE, see AGENTS.md);
  2. asking Spark to read it back and selecting the seed's unique marker column.

If Spark resolves the keyword-spelled name to the seeded table, the keyword IS a
component of the table name, and an extractor that drops it is wrong.

The control case matters as much as the positive ones: `prod.x AS y` is a genuine
*alias* and Spark accepts it. That is what makes `prod.x.y` a table name rather than
`prod.x` aliased `y` -- an implicit alias is legal SQL, so "there is an extra component"
alone would not have settled it. Only observing that Spark refuses `prod.x.y` while
accepting `prod.x y` proves the dotted component is part of the name.

Run:
    export JAVA_HOME=$(ls -d /opt/data/home/.jre/* | head -1)
    PATH="$JAVA_HOME/bin:$PATH" .venv-pyspark/bin/python -m pytest \
        tests/differential/test_keyword_table_components.py -q

Requires pyspark + a JVM; skipped otherwise, so the fast suite stays JVM-free.
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

pyspark = pytest.importorskip("pyspark", reason="differential suite needs pyspark")

if os.environ.get("JAVA_HOME") is None and os.environ.get("JRE_HOME") is None:
    pytest.skip("set JAVA_HOME to run the keyword-component differential", allow_module_level=True)

from pyspark.sql import SparkSession  # noqa: E402

pytestmark = pytest.mark.slow

#: Components that are non-reserved *keywords* in Spark's grammar. `X` is first because
#: it is the case F14 was reported as, and because it fails hardest: it lexes as
#: BINARY_HEX, so it was invisible to an IDENTIFIER-token filter twice over.
KEYWORD_COMPONENTS = ["X", "AFTER", "SCHEMA", "BIGINT", "USER", "OPTION", "YEAR", "ZONE"]

#: Controls: names with no keyword in them. These worked before the fix and must keep
#: working -- a characterisation test that only pins the new behaviour cannot tell a fix
#: from a regression that happens to agree with it.
PLAIN_COMPONENTS = ["users", "ax"]


@pytest.fixture(scope="module")
def spark():
    # A warehouse that survives between runs makes this file fail on its SECOND run and
    # not its first, which is the worst possible shape for a test: green once, red forever,
    # and the failure reads like a Spark bug rather than a stale directory.
    warehouse = tempfile.mkdtemp(prefix="sparkscreen-f14-warehouse-")
    session = (
        SparkSession.builder.master("local[1]")
        .appName("f14-keyword-components")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", warehouse)
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    session.sql("create database if not exists prod")
    try:
        yield session
    finally:
        session.stop()
        shutil.rmtree(warehouse, ignore_errors=True)


def _seed_and_read_back(spark, component: str) -> str:
    """Seed `prod.<component>`, then ask Spark to read it back.

    Returns the columns Spark returned, or a failure marker. Succeeding means Spark
    resolved the keyword-spelled name to the table we just created under exactly that
    name -- i.e. the keyword is a real component of the table name.
    """
    marker = f"marker_{component.lower()}"
    try:
        spark.sql(f"drop table if exists prod.{component}")
    except Exception:
        pass  # table absent; nothing to drop
    spark.createDataFrame([(1,)], [marker]).write.mode("overwrite").saveAsTable(
        f"prod.{component}"
    )
    columns = spark.sql(f"select {marker} from prod.{component}").columns
    assert columns == [marker], (
        f"seeded prod.{component} with column {marker!r} but reading it back gave "
        f"{columns!r}; the seed or the read is wrong, not the claim under test"
    )
    return marker


@pytest.mark.parametrize("component", KEYWORD_COMPONENTS)
def test_spark_reads_back_a_table_named_with_a_keyword(spark, component):
    """Spark resolves `prod.AFTER` to the table *named* `prod.AFTER`.

    This is the ground truth the whole fix rests on. If it fails, the engine is not
    treating these as table-name components and the extractor must not report them.
    """
    _seed_and_read_back(spark, component)


@pytest.mark.parametrize("component", PLAIN_COMPONENTS)
def test_spark_reads_back_a_table_named_with_a_plain_identifier(spark, component):
    """Control: plain multi-part names still resolve as themselves."""
    _seed_and_read_back(spark, component)


def test_dotted_component_is_a_name_part_not_an_implicit_alias(spark):
    """`prod.x y` is an alias and Spark accepts it; `prod.x.y` is a name and it refuses.

    This is the step that rules out the "trailing component might be an alias" reading
    of `prod.x.y`, which is why F14 was deferred rather than fixed. An implicit alias is
    legal SQL, so parsing `prod.x.y` does not by itself tell you whether Spark read a
    two-part name with an alias or a three-part name. Watching Spark accept one and
    refuse the other does.
    """
    _seed_and_read_back(spark, "x")

    # Control: table `prod.x`, implicit alias `y`. Must succeed.
    assert spark.sql("select * from prod.x y").columns == ["marker_x"]

    # The case under test: same components, joined by a DOT. Spark must refuse it as a
    # single name, because the in-memory catalog is a single-part-namespace catalog and
    # a three-part name does not exist in it.
    with pytest.raises(Exception, match="REQUIRES_SINGLE_PART_NAMESPACE"):
        spark.sql("select * from prod.x.y").collect()

    # Explicit `AS` alias, for completeness: also legal, also two-part.
    assert spark.sql("select * from prod.x as y").columns == ["marker_x"]


def test_we_extract_exactly_what_spark_resolves(spark):
    """Our extraction must agree with Spark for every component shape tested here.

    Asserting both sides in one test is deliberate: the earlier probes established each
    half separately, and nothing pinned them to each other. Spark is the authority on
    meaning; we are the thing under test.
    """
    from sparkscreen.analysis.treewalk import extract_namespaces
    from sparkscreen.grammar.parser import get_parser

    key = "spark-3.5.1" if pyspark.__version__.startswith("3.5") else "spark-4.0"

    def ours(sql: str) -> list[str]:
        tree = get_parser(key).parse(sql).statements[0].tree
        return [n.name for n in extract_namespaces(tree, grammar_key=key)]

    # Seed first: only a name that EXISTS can be shown to resolve to itself.
    for component in KEYWORD_COMPONENTS + PLAIN_COMPONENTS + ["x"]:
        _seed_and_read_back(spark, component)

    for component in KEYWORD_COMPONENTS + PLAIN_COMPONENTS + ["x"]:
        sql = f"select * from prod.{component}"
        # Spark says this name is real: it resolves to a table we seeded under it.
        spark.sql(sql).collect()
        # We must report the same name, component for component.
        assert ours(sql) == [f"prod.{component}"], (
            f"{sql!r}: Spark resolves it to prod.{component}, we reported {ours(sql)}"
        )

    # The keyword must not become a *column* namespace just because it is a keyword.
    assert ours("select AFTER from prod.SCHEMA") == ["prod.SCHEMA"]


def test_a_broader_allowlist_name_can_no_longer_be_produced(spark):
    """The fail-open direction, asserted end-to-end rather than described.

    Before the fix `select * from prod.staging.x` extracted as `prod.staging`, which a
    `prod.*` allowlist matches -- permitting a table the operator never authorised.
    After the fix it extracts as `prod.staging.x`, which `prod.*` does not match. This is
    the property that actually matters, so it is pinned as a matching assertion.
    """
    from sparkscreen.analysis.treewalk import extract_namespaces
    from sparkscreen.grammar.parser import get_parser

    key = "spark-3.5.1" if pyspark.__version__.startswith("3.5") else "spark-4.0"
    tree = get_parser(key).parse("select * from prod.staging.x").statements[0].tree
    (ref,) = extract_namespaces(tree, grammar_key=key)

    assert ref.parts == ("prod", "staging", "x")
    # A `prod.*` allowlist entry must NOT match a three-part name: `*` absorbs exactly
    # one component. If this ever regresses, the truncation bug is back in a new guise.
    # `matches()` takes a dotted string pattern, not a NamespaceRef.
    assert not ref.matches("prod.*")
    assert ref.matches("prod.staging.*")
    assert not ref.matches("prod")
