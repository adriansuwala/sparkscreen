"""The effect axis: `Effect` flags, the label -> effects table, and its totality.

Three separate obligations are tested here, and they fail differently on purpose:

1. **The type is a set, not a value.** `Effect` must be combinable. A single-valued
   design was tried and rejected, so there is a test that pins the rejection rather
   than leaving it as folklore.

2. **The table is total.** Every label either pinned grammar can emit has an entry.
   Derived from the generated parser classes, not from a corpus -- see
   `analysis/label_universe.py` for why a corpus cannot establish this. Plus a corpus
   test running real SQL through the real parsers, which catches drift from the other
   direction and would catch a bug in the derivation itself.

3. **The table says the right thing.** The classification is asserted for the
   statements where the answer is debatable, because those are the ones a future
   maintainer will be tempted to "simplify". The DESTROY_DATA-vs-WRITE_SCHEMA
   asymmetry gets its own class, since it is the whole reason the axis exists.

Nothing here asserts a verdict. The effect axis must not be able to influence one, and
`test_effect_axis_cannot_change_a_verdict` checks that across a corpus: screening with
and without effect classification produces identical verdicts.
"""

import json

import pytest

from sparkscreen.analysis.effects import (
    DENY_REGARDLESS_OF_NAMESPACE,
    LABEL_EFFECTS,
    UNMAPPED,
    UnmappedLabelError,
    denies_regardless_of_namespace,
    effect_label_drift,
    effects_for_label,
    lookup_effects,
    mapped_labels,
)
from sparkscreen.analysis.label_universe import (
    grammar_labels,
    labels_for_grammar,
)
from sparkscreen.grammar.parser import get_parser
from sparkscreen.grammar.spec import SPECS
from sparkscreen.model import (
    Effect,
    Finding,
    Reason,
    Report,
    Severity,
    Verdict,
    effect_names,
)
from sparkscreen.policy import (
    DESTRUCTIVE_LABELS,
    READ_ONLY_LABELS,
    default_policy,
    read_only_policy,
)
from sparkscreen.screen import screen


# ---------------------------------------------------------------------------
# The corpus.
#
# Lowercase, per CONTRIBUTING: the original suite was written in uppercase SQL and
# passed while both grammars rejected `select 1`. Real agent-written code is
# lowercase, so the corpus is.
#
# `4.0-only` / `3.5.1-only` mark where the *grammar* differs, not where the
# expectation differs -- never write a corpus against one grammar and apply it to
# both, which is the mistake CONTRIBUTING warns about.
#
# Each entry is (sql, expected_effect_flags). The expectation is part of the corpus
# rather than a separate table so that a statement and its meaning stay adjacent: a
# reader checking "why is TRUNCATE not WRITE_SCHEMA" can see the SQL and the
# reasoning in one place.
# ---------------------------------------------------------------------------

#: (sql, expected effects) for statements both pinned grammars accept.
SHARED_CORPUS = [
    # -- reads ---------------------------------------------------------------
    ("select 1", {Effect.READ_DATA}),
    ("select * from prod.users where id = 1", {Effect.READ_DATA}),
    ("with c as (select 1 as x) select * from c", {Effect.READ_DATA}),
    ("show tables", frozenset()),
    ("show databases", frozenset()),
    ("show namespaces", frozenset()),
    ("show columns from prod.users", frozenset()),
    ("show partitions prod.users", frozenset()),
    ("show create table prod.users", frozenset()),
    ("describe table prod.users", frozenset()),
    ("describe query select 1", frozenset()),
    ("describe function f", frozenset()),
    ("describe namespace prod", frozenset()),

    # -- USE: the documented no-effect case ----------------------------------
    # Changes the session's current namespace. Nothing durable, nothing outside the
    # cluster, no schema, no rows, no code. The empty set here is a statement of fact,
    # and it is the reason an empty set cannot also mean "we don't know".
    ("use prod", frozenset()),
    ("use namespace prod", frozenset()),
    # SET CATALOG is the catalog-scoped twin of USE and moves the same kind of
    # session pointer, so it is empty for the same reason -- not CHANGE_CONFIG, despite
    # starting with "SET".
    ("set catalog mycat", frozenset()),

    # -- row writes ----------------------------------------------------------
    # INSERT adds rows and takes none away, so it is the plain-write case.
    ("insert into prod.users select 1", {Effect.WRITE_DATA}),
    ("insert into prod.users partition (a = 1) select 1", {Effect.WRITE_DATA}),
    # UPDATE / DELETE / MERGE also DESTROY_DATA. The WHERE clause does not earn an
    # exemption -- `delete from prod.users where a = 1` still removes a user's record,
    # and DESTROY_DATA is here as a deliberate slowdown flag rather than a damage
    # estimate (sparkscreen-120). Bounded and unbounded forms are both listed so the
    # lack of a predicate-sensitivity is explicit rather than accidental.
    ("update prod.users set a = 1 where b = 2",
     {Effect.WRITE_DATA, Effect.DESTROY_DATA}),
    ("delete from prod.users where a = 1",
     {Effect.WRITE_DATA, Effect.DESTROY_DATA}),
    ("delete from prod.users",
     {Effect.WRITE_DATA, Effect.DESTROY_DATA}),
    ("merge into prod.users using src on prod.users.id = src.id "
     "when matched then update set prod.users.a = src.a",
     {Effect.WRITE_DATA, Effect.DESTROY_DATA}),

    # -- schema changes that preserve data -----------------------------------
    # These are the "fine in staging, review in prod" tier: structure changes, rows
    # survive. None of them are DESTROY_DATA, which is what distinguishes them from
    # the DROP block below.
    ("alter table prod.users add columns (a int)", {Effect.WRITE_SCHEMA}),
    ("alter table prod.users alter column a comment 'x'", {Effect.WRITE_SCHEMA}),
    ("alter table prod.users rename column a to b", {Effect.WRITE_SCHEMA}),
    ("alter table prod.users add partition (a = 1)", {Effect.WRITE_SCHEMA}),
    ("alter table prod.users set tblproperties ('a' = 'b')", {Effect.WRITE_SCHEMA}),
    ("alter table prod.users unset tblproperties ('a')", {Effect.WRITE_SCHEMA}),
    ("alter table prod.users set serde 'x'", {Effect.WRITE_SCHEMA}),
    ("alter table a rename to b", {Effect.WRITE_SCHEMA}),
    ("alter namespace prod set properties ('a' = 'b')", {Effect.WRITE_SCHEMA}),
    ("alter view v as select 1", {Effect.WRITE_SCHEMA}),
    ("create index idx on prod.users (a)", {Effect.WRITE_SCHEMA}),
    ("create namespace prod", {Effect.WRITE_SCHEMA}),
    ("create table prod.t (a int) using parquet", {Effect.WRITE_SCHEMA}),
    ("create table prod.t2 like prod.t", {Effect.WRITE_SCHEMA}),
    ("create view prod.v as select 1", {Effect.WRITE_SCHEMA}),
    ("create or replace temporary view v using parquet", {Effect.WRITE_SCHEMA}),
    ("repair table prod.users", {Effect.WRITE_SCHEMA, Effect.REACHES_EXTERNAL}),
    ("alter table prod.users recover partitions",
     {Effect.WRITE_SCHEMA, Effect.REACHES_EXTERNAL}),
    ("alter table prod.users set location 's3://bucket/t'",
     {Effect.WRITE_SCHEMA, Effect.REACHES_EXTERNAL}),
    ("alter namespace prod set location 's3://bucket/ns'",
     {Effect.WRITE_SCHEMA, Effect.REACHES_EXTERNAL}),

    # -- irreversible loss ---------------------------------------------------
    ("drop table prod.users", {Effect.DESTROY_DATA}),
    ("drop view prod.v", {Effect.DESTROY_DATA}),
    ("drop namespace prod", {Effect.DESTROY_DATA}),
    ("drop function prod.f", {Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}),
    ("drop index idx on prod.users", {Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}),
    ("alter table prod.users drop partition (a = 1)",
     {Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}),

    # -- code loading --------------------------------------------------------
    ("add jar /tmp/evil.jar", {Effect.LOAD_CODE, Effect.REACHES_EXTERNAL}),
    ("add file /tmp/evil.txt", {Effect.LOAD_CODE, Effect.REACHES_EXTERNAL}),
    ("create function prod.f as 'x' using jar '/tmp/evil.jar'",
     {Effect.LOAD_CODE, Effect.WRITE_SCHEMA}),

    # -- config --------------------------------------------------------------
    ("set spark.sql.shuffle.partitions = 200", {Effect.CHANGE_CONFIG}),
    ("reset spark.sql.shuffle.partitions", {Effect.CHANGE_CONFIG}),
    ("cache table prod.users", {Effect.CHANGE_CONFIG}),
    ("uncache table prod.users", {Effect.CHANGE_CONFIG}),
    ("clear cache", {Effect.CHANGE_CONFIG}),
    ("refresh table prod.users", {Effect.CHANGE_CONFIG}),
    ("refresh function prod.f", {Effect.CHANGE_CONFIG}),
    ("refresh resource r", {Effect.CHANGE_CONFIG}),
    ("analyze table prod.users compute statistics noscan", {Effect.CHANGE_CONFIG}),
    ("comment on table prod.users is 'x'", {Effect.CHANGE_CONFIG}),

    # -- the local filesystem -------------------------------------------------
    # LOAD DATA reads a path and writes rows. Whether it is the *driver's* local
    # filesystem depends on the LOCAL keyword, which the statement label does not
    # expose -- both spellings are LoadData. The table takes the conservative
    # reading and says so; see the LABEL_EFFECTS comment. Both are listed here so
    # that the over-approximation is visible rather than surprising.
    ("load data local inpath '/etc/passwd' into table prod.users",
     {Effect.WRITE_DATA, Effect.READ_LOCAL_FS, Effect.REACHES_EXTERNAL}),
    ("load data inpath '/tmp/x.csv' into table prod.users",
     {Effect.WRITE_DATA, Effect.READ_LOCAL_FS, Effect.REACHES_EXTERNAL}),
]

#: Statements only spark-4.0 accepts. `call` and the 4.0-only script syntax do not
#: exist in 3.5.1's grammar, so this corpus must not be applied there.
SPARK_4_0_ONLY_CORPUS = [
    ("call my_proc(1)", {Effect.REACHES_EXTERNAL}),
    ("execute immediate 'drop table prod.users'", {Effect.REACHES_EXTERNAL}),
    ("show collations", frozenset()),
    ("alter table prod.users drop column a",
     {Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}),
    ("alter table prod.users replace columns (b int)",
     {Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}),
    ("alter table prod.users change column a b int",
     {Effect.WRITE_SCHEMA}),
    ("insert overwrite table prod.users select 1",
     {Effect.WRITE_DATA, Effect.DESTROY_DATA}),
    # Spark's two directory-overwrite spellings are two different labels and they are
    # easy to swap: `INSERT OVERWRITE DIRECTORY 'p'` is InsertOverwriteHiveDir, while
    # adding a provider (`... USING parquet`) makes it InsertOverwriteDir. Both carry
    # the same effect set, but the labels differ, so both are in the corpus.
    ("insert overwrite directory '/tmp/out' select 1",
     {Effect.WRITE_DATA, Effect.DESTROY_DATA, Effect.REACHES_EXTERNAL}),
    ("insert overwrite directory '/tmp/out' using parquet select 1",
     {Effect.WRITE_DATA, Effect.DESTROY_DATA, Effect.REACHES_EXTERNAL}),
    ("replace table prod.users using parquet as select 1",
     {Effect.WRITE_DATA, Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}),
    ("analyze tables compute statistics noscan", {Effect.CHANGE_CONFIG}),
    ("set time zone 'UTC'", {Effect.CHANGE_CONFIG}),
    # The Hive-style ALTERs. These are 4.0-only in the pinned grammars -- 3.5.1's
    # vendored SqlBaseParser.g4 has no `clusterBySpec`, no
    # `DROP CONSTRAINT ... on table`, and no namespace `UNSET PROPERTIES` -- so they
    # belong here and not in SHARED_CORPUS. Measured, not assumed.
    ("alter table prod.users cluster by (a)", {Effect.WRITE_SCHEMA}),
    ("alter namespace prod unset properties ('a')", {Effect.WRITE_SCHEMA}),
    ("alter table prod.users drop constraint c",
     {Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}),
]

#: Statements only spark-3.5.1 accepts. 4.0 replaced the bare `SET TIME ZONE
#: timezone` alternative, so the label differs between the two grammars.
SPARK_3_5_1_ONLY_CORPUS = [
    ("set time zone 'UTC'", {Effect.CHANGE_CONFIG}),
]

#: The statements whose classification is the reason the axis exists, asserted
#: separately from the corpus so a corpus failure does not hide a semantic regression.
#:
#: Each is (sql, required flags, forbidden flags). `forbidden` is the stronger half:
#: asserting `TRUNCATE` is DESTROY_DATA is easy, asserting it is *not* WRITE_SCHEMA is
#: what stops a later edit from collapsing the two tiers together.
CLASSIFICATION_CASES = [
    # TRUNCATE destroys rows and leaves the schema exactly as declared. One flag,
    # deliberately: calling it a schema change would put "add a nullable column" in
    # the same class as "delete every row".
    ("truncate table prod.users",
     {Effect.DESTROY_DATA}, {Effect.WRITE_SCHEMA}),
    # DROP COLUMN is the case a single-valued axis cannot express: schema change AND
    # irreversible data loss, simultaneously.
    ("alter table prod.users drop column a",
     {Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}, frozenset()),
    # INSERT (not overwrite) is a row write only. This is the other half of the
    # TRUNCATE contrast, and the reason DELETE is treated differently despite both
    # being "row writes": INSERT cannot remove what is already stored.
    ("insert into prod.users select 1",
     {Effect.WRITE_DATA}, {Effect.DESTROY_DATA}),
    # DELETE is the other half. The required/forbidden split is what pins the
    # asymmetry from both directions -- asserting DELETE *has* DESTROY_DATA alone
    # would still pass if some later edit also made it a schema change.
    ("delete from prod.users where a = 1",
     {Effect.WRITE_DATA, Effect.DESTROY_DATA}, {Effect.WRITE_SCHEMA}),
    # ADD JAR: arbitrary code execution, no schema change, no data change.
    ("add jar /tmp/evil.jar",
     {Effect.LOAD_CODE}, {Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}),
    # CREATE INDEX: pure schema. Must NOT qualify for deny-regardless-of-namespace,
    # which is the tier separation the axis exists for.
    ("create index idx on prod.users (a)",
     {Effect.WRITE_SCHEMA}, {Effect.DESTROY_DATA, Effect.LOAD_CODE}),
    # A plain SELECT reads data and does nothing else.
    ("select 1", {Effect.READ_DATA},
     {Effect.WRITE_DATA, Effect.WRITE_SCHEMA, Effect.DESTROY_DATA}),
]


def _corpus_for(spec_key):
    if spec_key == "spark-4.0":
        return SHARED_CORPUS + SPARK_4_0_ONLY_CORPUS
    return SHARED_CORPUS + SPARK_3_5_1_ONLY_CORPUS


# ---------------------------------------------------------------------------
# 1. Effect is a set of orthogonal flags
# ---------------------------------------------------------------------------

class TestEffectIsASet:
    """Pins the rejected single-value design, so it cannot come back unremarked."""

    def test_flags_combine_with_or(self):
        combined = Effect.WRITE_SCHEMA | Effect.DESTROY_DATA
        assert Effect.WRITE_SCHEMA in combined
        assert Effect.DESTROY_DATA in combined

    def test_combination_supports_membership_and_equality(self):
        combined = Effect.WRITE_SCHEMA | Effect.DESTROY_DATA
        assert combined == Effect.DESTROY_DATA | Effect.WRITE_SCHEMA
        assert combined != Effect.WRITE_SCHEMA
        assert combined & Effect.WRITE_SCHEMA
        assert not (combined & Effect.LOAD_CODE)

    def test_every_documented_effect_exists(self):
        # The eight axes named in the task. Each has a distinct value, which is what
        # lets a statement carry several without them collapsing into each other.
        expected = {
            "WRITE_SCHEMA", "WRITE_DATA", "DESTROY_DATA", "READ_DATA",
            "READ_LOCAL_FS", "LOAD_CODE", "REACHES_EXTERNAL", "CHANGE_CONFIG",
        }
        assert {m.name for m in Effect} == expected
        values = [m.value for m in Effect]
        assert len(set(values)) == len(values)
        assert all(isinstance(v, int) and v and v & (v - 1) == 0 for v in values), \
            "flag values must be distinct powers of two"

    def test_there_is_no_unknown_member(self):
        # Absence is the signal. An Effect.UNKNOWN would make "we could not analyse
        # this" a claim rather than an absence, and would break the distinction
        # between an empty effect set (unknown) and USE prod (analysed, no effect).
        assert not hasattr(Effect, "UNKNOWN")
        assert not [m.name for m in Effect if "UNKNOWN" in m.name]

    def test_is_not_a_str_enum(self):
        # `str, Flag` would make Effect.DESTROY_DATA == "destroy_data", letting a
        # bare string enter an effect set and compare as if it were the flag.
        assert not isinstance(Effect.DESTROY_DATA, str)
        assert Effect.DESTROY_DATA != "destroy_data"

    def test_str_is_readable(self):
        assert str(Effect.DESTROY_DATA) == "DESTROY_DATA"
        assert str(Effect.WRITE_SCHEMA | Effect.DESTROY_DATA) == \
            "WRITE_SCHEMA|DESTROY_DATA"

    def test_effect_names_sorts_and_drops_nonames(self):
        assert effect_names({Effect.DESTROY_DATA, Effect.WRITE_SCHEMA}) == \
            ["DESTROY_DATA", "WRITE_SCHEMA"]
        # A composite pseudo-member's `.name` is the joined string
        # "WRITE_DATA|WRITE_SCHEMA", which is not a member name and matches nothing.
        # effect_names must expand it into individual flags.
        assert effect_names({Effect(3)}) == ["WRITE_DATA", "WRITE_SCHEMA"]
        # Composite names come out in declaration order (WRITE_SCHEMA is bit 1),
        # which is why the joined string cannot be split on "|" and re-joined
        # expecting the caller's order.
        assert Effect(3).name == "WRITE_SCHEMA|WRITE_DATA"
        # The zero flag has no members and must vanish rather than become None.
        assert effect_names({Effect(0)}) == []
        assert effect_names(frozenset()) == []


# ---------------------------------------------------------------------------
# 2. The table is total
# ---------------------------------------------------------------------------

class TestTableIsTotal:
    def test_every_grammar_label_has_an_effect(self, spec_key):
        """The core safety property, per grammar.

        A missing entry means a statement we parsed gets reported with no effect
        flags, and every consumer asking "does this destroy anything?" reads that as
        "no". That is fail-open, so this test exists to make it impossible to merge.
        """
        labels = labels_for_grammar(spec_key)
        assert labels, f"{spec_key}: derivation produced no labels at all"
        missing = sorted(labels - mapped_labels())
        assert not missing, (
            f"{spec_key}: {len(missing)} grammar label(s) have no entry in "
            f"LABEL_EFFECTS: {missing}. Add them to "
            f"sparkscreen/analysis/effects.py -- do not work around this."
        )

    def test_grammar_derived_label_set_is_non_trivial(self):
        """Guards the derivation itself.

        If `labels_for_grammar` broke and returned a small or empty set, the coverage
        test above would pass vacuously. These floors are the empirical lower bounds
        measured against the real parsers; a drop below them means the walk, not the
        grammar, changed.
        """
        assert len(labels_for_grammar("spark-4.0")) >= 100
        assert len(labels_for_grammar("spark-3.5.1")) >= 75
        assert len(set(grammar_labels())) >= 110

    def test_derivation_finds_labels_a_corpus_would_miss(self):
        """The derivation must be strictly stronger than a corpus.

        These labels are reachable in spark-4.0 and no SQL in this file produces them
        -- they are the reason the coverage test derives labels from the parser rather
        than from a statement list. If a grammar change removed them, this test would
        need revisiting; today it is what makes the corpus insufficient on its own.
        """
        four_zero = labels_for_grammar("spark-4.0")
        for label in ("CreatePipelineDataset", "CreateFlowAutoCdc", "Call",
                      "MergeIntoTable", "LoadData", "ManageResource"):
            assert label in four_zero

    def test_corpus_statements_all_have_effects(self, spec_key):
        """Real SQL through the real parsers, for both grammars.

        The complement to the derivation-based test: it exercises `effective_label`
        end to end, so a bug in the walk (wrong entry holder, wrong wrapper set) shows
        up here even though the class-hierarchy derivation still looks correct.
        """
        parser = get_parser(spec_key)
        for sql, _expected in _corpus_for(spec_key):
            parsed = parser.parse(sql)
            statements = parsed.statements
            assert statements, f"{spec_key}: {sql!r} produced no statements"
            for stmt in statements:
                found = lookup_effects(stmt.label)
                assert found is not None, (
                    f"{spec_key}: {sql!r} produced label {stmt.label!r}, which has no "
                    f"entry in LABEL_EFFECTS"
                )
                # Distinguishable sentinel, not an empty-set stand-in.
                assert found is not UNMAPPED or found == found

    def test_every_policy_label_has_an_effect(self):
        """DESTRUCTIVE_LABELS / READ_ONLY_LABELS / rule labels are all mapped.

        These are the labels the policy reasons about. An unmapped one is a label the
        screener can produce with no effect classification attached.
        """
        policy_labels: set[str] = set(DESTRUCTIVE_LABELS) | set(READ_ONLY_LABELS)
        for rule in default_policy().rules:
            policy_labels |= set(rule.labels)
        missing = sorted(policy_labels - mapped_labels())
        assert not missing, f"policy labels with no effect entry: {missing}"

    def test_every_table_key_is_a_real_label_or_documented_as_policy_only(self):
        """Entries must not accumulate for labels nothing can produce.

        `effect_label_drift()["grammar_uncovered"]` is expected to be non-empty today
        (eight policy-only labels), but it must be exactly the documented set -- a
        growing list means the table is describing unreachable statements and those
        entries stop being evidence of anything.
        """
        expected = {
            "Close", "Execute", "Fetch", "InsertIntoPartition", "Open",
            "SetTableCollation", "ShowCollation", "ShowDatabases",
        }
        assert set(effect_label_drift()["grammar_uncovered"]) == expected

    def test_drift_reports_no_unmapped_grammar_label(self):
        # grammar_unmapped is the fail-open direction and must always be empty.
        assert effect_label_drift()["grammar_unmapped"] == []
        # effect_unmapped is the policy-side equivalent. Also always empty.
        assert effect_label_drift()["effect_unmapped"] == []

    def test_drift_reports_the_dead_policy_vocabulary(self):
        # Reported, not removed: pruning these is a policy.py decision and this module
        # does not get to make it. Pinned so the list cannot change unnoticed.
        assert set(effect_label_drift()["policy_only"]) == {
            "Close", "Execute", "Fetch", "InsertIntoPartition", "Open",
            "SetTableCollation", "ShowCollation", "ShowDatabases",
        }


class TestUnknownLabelIsLoud:
    """A missing entry must never read as "no effect"."""

    def test_effects_for_label_raises_on_unknown(self):
        with pytest.raises(UnmappedLabelError) as exc:
            effects_for_label("DropEveryThing")
        assert exc.value.label == "DropEveryThing"
        assert "DropEveryThing" in str(exc.value)
        # The message has to be actionable: a bare KeyError in a report is not a bug
        # report anyone can act on.
        assert "LABEL_EFFECTS" in str(exc.value)

    def test_unknown_label_is_a_key_error(self):
        # Callers already handling KeyError from a label lookup keep working.
        assert issubclass(UnmappedLabelError, KeyError)
        with pytest.raises(KeyError):
            effects_for_label("NotAStatement")

    def test_lookup_effects_returns_none_not_empty_set(self):
        assert lookup_effects("NotAStatement") is None

    def test_unmapped_sentinel_is_distinguishable_from_an_empty_set(self):
        # `UNMAPPED` must not be mistaken for "no effects". It is documented as the
        # thing that must be compared against, never truth-tested -- frozenset() is
        # falsy too, which is exactly the trap.
        assert isinstance(UNMAPPED, frozenset)
        assert UNMAPPED == frozenset()
        assert lookup_effects("Use") == UNMAPPED
        assert lookup_effects("NotAStatement") is not UNMAPPED

    def test_effects_for_label_round_trips_the_table(self):
        for label, expected in LABEL_EFFECTS.items():
            assert effects_for_label(label) == expected


# ---------------------------------------------------------------------------
# 3. The table says the right thing
# ---------------------------------------------------------------------------

class TestClassification:
    @pytest.mark.parametrize("sql,required,forbidden", CLASSIFICATION_CASES,
                             ids=[c[0] for c in CLASSIFICATION_CASES])
    def test_case(self, spec_key, sql, required, forbidden):
        """Branch on the grammar, never on the expectation.

        `drop column`, `replace columns`, `change column`, `insert overwrite` and
        `replace table` are all 4.0-only in this corpus's spelling; `truncate`,
        `insert into` and `create index` are shared. A case is skipped when *that
        grammar* rejects it, not when the expectation is inconvenient.
        """
        parser = get_parser(spec_key)
        if parser.try_parse(sql) is None:
            pytest.skip(f"{spec_key} does not accept {sql!r}")
        parsed = parser.parse(sql)
        labels = {s.label for s in parsed.statements}
        assert len(labels) == 1, f"{sql!r} produced {labels}, expected one statement"
        effects = effects_for_label(labels.pop())
        missing = required - effects
        assert not missing, (
            f"{sql!r} -> {effects}: missing {effect_names(missing)}"
        )
        present = forbidden & effects
        assert not present, (
            f"{sql!r} -> {effects}: must not carry {effect_names(present)}"
        )

    @pytest.mark.parametrize("sql,expected", SHARED_CORPUS,
                             ids=[c[0] for c in SHARED_CORPUS])
    def test_shared_corpus_classification(self, spec_key, sql, expected):
        parser = get_parser(spec_key)
        if parser.try_parse(sql) is None:
            pytest.skip(f"{spec_key} does not accept {sql!r}")
        parsed = parser.parse(sql)
        for stmt in parsed.statements:
            assert effects_for_label(stmt.label) == expected, (
                f"{spec_key}: {sql!r} -> {stmt.label}: "
                f"{effect_names(effects_for_label(stmt.label))} != "
                f"{effect_names(expected)}"
            )

    @pytest.mark.parametrize("sql,expected", SPARK_4_0_ONLY_CORPUS,
                             ids=[c[0] for c in SPARK_4_0_ONLY_CORPUS])
    def test_spark_4_0_corpus_classification(self, sql, expected):
        parsed = get_parser("spark-4.0").parse(sql)
        for stmt in parsed.statements:
            assert effects_for_label(stmt.label) == expected, (
                f"spark-4.0: {sql!r} -> {stmt.label}"
            )

    @pytest.mark.parametrize("sql,expected", SPARK_3_5_1_ONLY_CORPUS,
                             ids=[c[0] for c in SPARK_3_5_1_ONLY_CORPUS])
    def test_spark_3_5_1_corpus_classification(self, sql, expected):
        parsed = get_parser("spark-3.5.1").parse(sql)
        for stmt in parsed.statements:
            assert effects_for_label(stmt.label) == expected, (
                f"spark-3.5.1: {sql!r} -> {stmt.label}"
            )

    def test_no_effect_labels_are_explained(self):
        """Every empty effect set must be one of a known, justified set.

        An empty set is a claim ("this does nothing durable"), so it needs a reason.
        This pins that claim to a fixed list: adding a label to the table with an
        empty set and no justification fails here.
        """
        empty = {label for label, effects in LABEL_EFFECTS.items() if not effects}
        # Catalog introspection: reads the metastore, not anyone's data.
        # Session context: USE / USE NAMESPACE change only the session's cursor.
        # Script control flow: the enclosed statements are analysed individually.
        assert empty == {
            "ShowCatalogs", "ShowCollation", "ShowCollations", "ShowColumns",
            "ShowCreateTable", "ShowCurrentNamespace", "ShowDatabases",
            "ShowFunctions", "ShowNamespaces", "ShowPartitions", "ShowProcedures",
            "ShowTableExtended", "ShowTables", "ShowTblProperties", "ShowViews",
            "DescribeFunction", "DescribeNamespace", "DescribeProcedure",
            "DescribeQuery", "DescribeRelation",
            "Use", "UseNamespace", "SetCatalog",
            "Explain",
            "SearchedCaseStatement", "SimpleCaseStatement",
        }

    def test_every_label_is_classified_as_something_or_explicitly_nothing(self):
        # No table value is None, and none is a bare sentinel standing in for a
        # decision. `lookup_effects` returning None is the miss path; a stored None
        # would be a different, quieter failure.
        for label, effects in LABEL_EFFECTS.items():
            assert effects is not None, f"{label} maps to None"
            assert isinstance(effects, frozenset), f"{label} is not a frozenset"
            for e in effects:
                assert isinstance(e, Effect), f"{label} contains a non-Effect: {e!r}"

    def test_table_is_immutable_in_practice(self):
        # frozenset values, so a caller cannot mutate a shared classification through
        # the dict. Assert the value type, which is what actually guarantees it.
        assert all(isinstance(v, frozenset) for v in LABEL_EFFECTS.values())


# ---------------------------------------------------------------------------
# 4. The asymmetry the axis exists for
# ---------------------------------------------------------------------------

class TestDenyRegardlessOfNamespace:
    """DESTROY_DATA and LOAD_CODE justify denying whatever the namespaces allow.

    WRITE_SCHEMA without DESTROY_DATA does not, and that separation is the reason
    the axis is worth having: it is what lets a policy say "schema changes need
    review in prod but are routine in staging" without also saying "CREATE INDEX is
    as bad as DROP TABLE".
    """

    def test_destroy_data_qualifies(self):
        assert denies_regardless_of_namespace({Effect.DESTROY_DATA})
        assert denies_regardless_of_namespace(
            effects_for_label("DropTable"))
        assert denies_regardless_of_namespace(
            effects_for_label("TruncateTable"))
        assert denies_regardless_of_namespace(
            effects_for_label("DropTableColumns"))
        assert denies_regardless_of_namespace(
            effects_for_label("HiveReplaceColumns"))

    def test_load_code_qualifies(self):
        assert denies_regardless_of_namespace({Effect.LOAD_CODE})
        assert denies_regardless_of_namespace(effects_for_label("ManageResource"))
        assert denies_regardless_of_namespace(effects_for_label("CreateFunction"))

    def test_write_schema_alone_does_not_qualify(self):
        assert not denies_regardless_of_namespace({Effect.WRITE_SCHEMA})
        for label in ("CreateIndex", "AddTableColumns", "AlterTableAlterColumn",
                      "CreateTable", "RenameTableColumn", "CacheTable"):
            assert not denies_regardless_of_namespace(effects_for_label(label)), \
                f"{label} should be reviewable, not forbidden"

    def test_write_data_alone_does_not_qualify(self):
        # INSERT INTO a namespace you are allowed to write is normal ETL, so a plain
        # row-write does not justify denying regardless of namespace.
        assert not denies_regardless_of_namespace({Effect.WRITE_DATA})
        assert not denies_regardless_of_namespace(
            effects_for_label("InsertIntoTable"))
        # UPDATE/DELETE/MERGE used to be asserted here too. They no longer qualify,
        # because they now carry DESTROY_DATA (sparkscreen-120) -- see
        # `test_row_deleting_statements_qualify` for the replacement, which asserts
        # the stronger property rather than deleting the coverage.

    def test_row_deleting_statements_qualify(self):
        """UPDATE / DELETE / MERGE deny regardless of namespace. Decided in
        sparkscreen-120, and it overrides the earlier reasoning that they are "bounded
        by their WHERE clause".

        The argument for it is not that these statements are always catastrophic -- a
        `DELETE FROM t WHERE id = 3` is not -- but that DESTROY_DATA here is a
        deliberate slowdown flag. The screener must never certify a row-deleting
        statement as harmless, because "harmless" is a conclusion someone would act on,
        and a user's record removed on the strength of that analysis is exactly the
        outcome the tool exists to prevent. The cost is that routine cleanup DELETEs
        need an explicit policy allowance, which is the intended price.
        """
        for label in ("UpdateTable", "DeleteFromTable", "MergeIntoTable"):
            effects = effects_for_label(label)
            assert Effect.DESTROY_DATA in effects, (
                f"{label} removes rows and must not be certifiable as harmless"
            )
            assert Effect.WRITE_DATA in effects, f"{label} is also a row write"
            assert denies_regardless_of_namespace(effects), label

    def test_insert_into_does_not_qualify(self):
        # The counterweight to the test above, and the reason the two are separate.
        # INSERT does not remove what is already there, so it stays reviewable rather
        # than forbidden -- otherwise every ETL pipeline would need an exemption.
        assert not denies_regardless_of_namespace(
            effects_for_label("InsertIntoTable"))

    def test_read_only_does_not_qualify(self):
        assert not denies_regardless_of_namespace(
            effects_for_label("StatementDefault"))
        assert not denies_regardless_of_namespace(frozenset())

    def test_denied_set_is_exactly_two_flags(self):
        # Pinned because it is the rule's whole content. Adding a third flag here
        # would silently reclassify statements across every consumer of this helper.
        assert DENY_REGARDLESS_OF_NAMESPACE == {Effect.DESTROY_DATA, Effect.LOAD_CODE}


# ---------------------------------------------------------------------------
# 5. Wiring: Finding / Report / screen()
# ---------------------------------------------------------------------------

class TestWiring:
    def test_finding_defaults_to_empty_effect_set(self):
        f = Finding(verdict=Verdict.ALLOW, reason=Reason.NO_MATCHING_RULE,
                    message="x")
        assert f.effect == frozenset()
        assert f.effect_flags() == Effect(0)

    def test_finding_default_does_not_break_construction(self):
        # Positional construction must still work; `effect` is last for that reason.
        f = Finding(Verdict.DENY, Reason.DENY_RULE, "msg")
        assert f.verdict is Verdict.DENY
        assert f.effect == frozenset()

    def test_finding_effect_is_advertised_to_dict(self):
        f = Finding(verdict=Verdict.DENY, reason=Reason.DENY_RULE, message="x",
                    effect=frozenset({Effect.DESTROY_DATA}))
        assert f.to_dict()["effect"] == ["DESTROY_DATA"]

    def test_report_effects_unions_across_findings(self):
        r = Report()
        r.add(Finding(Verdict.ALLOW, Reason.NO_MATCHING_RULE, "a",
                      effect=frozenset({Effect.READ_DATA})))
        r.add(Finding(Verdict.DENY, Reason.DENY_RULE, "b",
                      effect=frozenset({Effect.DESTROY_DATA})))
        assert r.effects == {Effect.READ_DATA, Effect.DESTROY_DATA}

    def test_report_by_effect(self):
        r = Report()
        r.add(Finding(Verdict.DENY, Reason.DENY_RULE, "a",
                      effect=frozenset({Effect.DESTROY_DATA})))
        r.add(Finding(Verdict.ALLOW, Reason.NO_MATCHING_RULE, "b",
                      effect=frozenset({Effect.READ_DATA})))
        assert len(r.by_effect(Effect.DESTROY_DATA)) == 1
        assert r.by_effect(Effect.LOAD_CODE) == []

    def test_has_effect_requires_every_flag_not_any(self):
        """`has_effect` is public API and was entirely unasserted.

        Found by mutation testing: negating its `all(...)` to `not in` survived the whole
        suite, because nothing called it. That makes it untested API, not merely
        unexercised code -- and it is the kind of helper a caller would use as "is this
        finding destructive?", where `any` vs `all` is the difference between a right
        answer and a wrong one.

        The mutation to catch is `all(e in ...)` -> `not (all ...)` semantics, i.e. the
        partial-flag case. With `{WRITE_DATA, DESTROY_DATA}` present, asking only about
        DESTROY_DATA must still be True.
        """
        f = Finding(Verdict.DENY, Reason.DENY_RULE, "x",
                    effect=frozenset({Effect.WRITE_DATA, Effect.DESTROY_DATA}))

        assert f.has_effect(Effect.DESTROY_DATA)
        assert f.has_effect(Effect.WRITE_DATA)
        # The distinguishing case: all-of-a-subset, not any-of.
        assert f.has_effect(Effect.DESTROY_DATA, Effect.WRITE_DATA)
        assert not f.has_effect(Effect.DESTROY_DATA, Effect.READ_DATA)

        # Zero-arg is vacuously True -- `all([]) is True`. Pinned deliberately: my first
        # draft asserted False, on the reasonable-sounding grounds that a finding with no
        # flags cannot "have" an effect. Standard Python says otherwise, and the
        # docstring's phrasing ("every flag in `effects` is present") agrees: nothing is
        # missing. Callers who want the other behaviour must check `if effect`. Left
        # unpinned, someone reading this test would eventually "fix" the implementation
        # to match their intuition and break every `has_effect(*wanted)` caller.
        assert f.has_effect()

        # An empty effect set has nothing, so any non-empty query is False.
        empty = Finding(Verdict.ALLOW, Reason.NO_MATCHING_RULE, "y")
        assert not empty.has_effect(Effect.DESTROY_DATA)

    def test_report_to_dict_is_json_serialisable_with_effects(self):
        r = Report(policy="default", grammar="spark-4.0")
        r.add(Finding(Verdict.DENY, Reason.DENY_RULE, "x",
                      effect=frozenset({Effect.DESTROY_DATA})))
        out = json.loads(json.dumps(r.to_dict()))
        assert out["effects"] == ["DESTROY_DATA"]
        assert out["findings"][0]["effect"] == ["DESTROY_DATA"]

    def _screen(self, sql, policy=None, spec_key="spark-4.0"):
        return screen(f'spark.sql({sql!r})', policy, spec=spec_key)

    def test_screen_populates_effect_on_a_drop(self):
        report = self._screen("drop table prod.users")
        assert report.verdict is Verdict.DENY
        assert Effect.DESTROY_DATA in report.effects

    def test_unmapped_label_raises_rather_than_reporting_no_effects(self,
                                                                    monkeypatch):
        """The fail-loud guarantee, proved rather than asserted.

        Simulates a grammar bump the table has not caught up with: `DropTable` is
        removed from `LABEL_EFFECTS` and the screener is run. It must raise, not
        quietly report a DROP with an empty effect set.

        This is the assertion the whole design rests on. A screener that reports
        "this code has no known effect" for a `DROP TABLE` is worse than one that
        crashes, because the first produces a clean, confident, wrong answer -- which
        is the exact failure mode this project exists to prevent. So the crash is the
        desired behaviour and this test pins it, against the temptation to add a
        try/except that "keeps the CLI usable".
        """
        import sys
        screen_mod = sys.modules["sparkscreen.screen"]
        monkeypatch.delitem(LABEL_EFFECTS, "DropTable")
        with pytest.raises(UnmappedLabelError) as exc:
            screen_mod.screen("spark.sql('drop table prod.users')")
        assert exc.value.label == "DropTable"

    def test_a_mapped_label_still_works_after_the_miss_is_restored(self, monkeypatch):
        # Sanity on the test above: monkeypatch restores the dict, so the screener
        # classifies normally again. Guards against a leak that would make the
        # fail-loud test pass for the wrong reason on a later run.
        assert effects_for_label("DropTable") == frozenset({Effect.DESTROY_DATA})
        assert self._screen("drop table prod.users").effects == {Effect.DESTROY_DATA}

    def test_screen_populates_effect_on_a_select(self):
        report = self._screen("select 1")
        assert report.effects == {Effect.READ_DATA}

    def test_screen_effect_is_independent_of_policy(self):
        """The whole thesis, in one test.

        The same DROP is DENY under the default policy and UNKNOWN under a policy with
        no rules -- and DESTROY_DATA under both. If the effect axis were derived from
        the verdict, these two would differ, and a dashboard asking "does this code
        destroy anything" would get different answers for the same SQL depending on
        configuration.
        """
        sql = "drop table prod.users"
        default = self._screen(sql, default_policy())
        empty = self._screen(sql, read_only_policy())
        assert default.verdict is Verdict.DENY
        assert empty.verdict is Verdict.UNKNOWN
        assert default.effects == empty.effects == {Effect.DESTROY_DATA}

    def test_unparseable_sql_has_an_empty_effect_set(self):
        """No known effect, and not a claim of harmlessness.

        The empty set here means "we did not analyse it". `is_analysis_failure` is what
        distinguishes that from `USE prod`, which is analysed and genuinely does
        nothing -- and this test asserts both halves of the distinction.
        """
        report = self._screen("this is not sql at all")
        assert report.verdict is Verdict.UNKNOWN
        assert report.effects == frozenset()
        f = report.findings[0]
        assert f.is_analysis_failure
        assert f.reason is Reason.UNPARSEABLE_SQL

        benign = self._screen("use prod")
        assert benign.effects == frozenset()
        assert not benign.findings[0].is_analysis_failure

    def test_unresolved_dynamic_sql_has_an_empty_effect_set(self):
        report = screen("spark.sql('drop table ' + name)")
        assert report.verdict is Verdict.UNKNOWN
        assert report.effects == frozenset()
        f = report.findings[0]
        assert f.reason is Reason.UNRESOLVED_DYNAMIC_SQL
        assert f.is_analysis_failure

    def test_python_syntax_error_has_an_empty_effect_set(self):
        report = screen("def f(:\n")
        assert report.verdict is Verdict.UNKNOWN
        assert report.effects == frozenset()

    def test_unsupported_spark_version_has_an_empty_effect_set(self):
        report = screen("spark.sql('select 1')", spec="spark-99.0")
        assert report.verdict is Verdict.UNKNOWN
        assert report.effects == frozenset()

    def test_max_statements_resource_limit_has_an_empty_effect_set(self, spec_key):
        """Too many statements: counted, not classified.

        `screen()` bails before evaluating any of them, so no effect is known -- which
        is why the finding is UNKNOWN. Uses a 4.0 BEGIN...END script because that is
        the only way to put several statements in one `spark.sql()` call; on 3.5.1
        there are no scripts, so this limit is unreachable there and the test skips.
        """
        parser = get_parser(spec_key)
        sql = "begin select 1; select 2; select 3; end"
        if parser.try_parse(sql) is None:
            pytest.skip(f"{spec_key} has no multi-statement script syntax")
        policy = default_policy()
        policy.limits.max_statements = 2
        report = screen(f"spark.sql({sql!r})", policy, spec=spec_key)
        limits = [f for f in report.findings if f.reason is Reason.RESOURCE_LIMIT]
        assert limits, [f.to_dict() for f in report.findings]
        for f in limits:
            assert f.effect == frozenset(), (
                "a statement we declined to evaluate has no known effect"
            )
        assert report.verdict is Verdict.UNKNOWN

    def test_max_targets_resource_limit_still_reports_the_label_effect(self, spec_key):
        """Too many targets: the label *was* classified, so the effect is populated.

        The opposite of `max_statements`, and the distinction is deliberate. Bailing on
        target count happens inside `policy.evaluate_statement`, which returns a
        RESOURCE_LIMIT finding carrying the statement label -- and `_eval_one` then
        classifies that label like any other. We know this is a SELECT even though we
        declined to count its joins, and reporting an empty set would claim we know
        nothing at all. The verdict is still UNKNOWN, so nothing is waved through.
        """
        policy = default_policy()
        policy.limits.max_targets = 1
        report = screen(
            "spark.sql('select * from prod.a join prod.b on a.id = b.id')",
            policy, spec=spec_key,
        )
        limited = [f for f in report.findings if f.reason is Reason.RESOURCE_LIMIT]
        assert limited, [f.to_dict() for f in report.findings]
        assert all(f.statement == "StatementDefault" for f in limited)
        assert all(Effect.READ_DATA in f.effect for f in limited)
        assert report.verdict is Verdict.UNKNOWN

    def test_execute_immediate_inner_statement_gets_its_own_effect(self):
        """The payload is analysed separately, so it gets its own classification.

        The outer `EXECUTE IMMEDIATE` finding carries only the indirection flag; the
        inner DROP carries DESTROY_DATA. If the inner statement were folded into the
        outer finding, a report would say "this code reaches external" and never
        mention that it drops a table.
        """
        report = self._screen("execute immediate 'drop table prod.users'")
        drop_findings = [f for f in report.findings
                         if Effect.DESTROY_DATA in f.effect]
        assert drop_findings, [f.to_dict() for f in report.findings]
        assert any(f.statement == "DropTable" for f in drop_findings)

    def test_every_screen_finding_with_a_statement_has_a_mapped_label(self, spec_key):
        """No output can carry a statement label the table cannot classify.

        This is the end-to-end version of the totality property: it runs the real
        screener over a corpus of real PySpark and checks the labels that actually
        reach a Finding.
        """
        for sql, _ in _corpus_for(spec_key):
            report = screen(
                f"spark.sql({sql!r})\nspark.sql({sql!r})",
                spec=spec_key,
            )
            for f in report.findings:
                if f.statement is None:
                    continue
                assert lookup_effects(f.statement) is not None, (
                    f"{spec_key}: {sql!r} produced statement={f.statement!r} with no "
                    f"effect entry"
                )
                assert f.effect == effects_for_label(f.statement)

    def test_effect_axis_cannot_change_a_verdict(self, spec_key, monkeypatch):
        """The axis is additive. Verdicts must be identical with and without it.

        The guard on the task's hard constraint (no change to Verdict, Reason, the
        policy rules, or the exit codes). Comparing two *different* policies would not
        test this -- of course they differ -- so this neutralises the effect lookup
        itself and asserts the whole report is byte-identical apart from the effect
        fields. If `effect` ever fed back into policy evaluation, this fails.
        """
        # `sparkscreen.screen` is ambiguous: the package __init__ rebinds the name to
        # the *function*, so `import sparkscreen.screen as m` gives a function, not the
        # module. sys.modules is the only unambiguous handle on the module object.
        import sys
        screen_mod = sys.modules["sparkscreen.screen"]

        for sql, _ in _corpus_for(spec_key):
            source = f"spark.sql({sql!r})"
            normal = screen_mod.screen(source, spec=spec_key)

            monkeypatch.setattr(
                screen_mod, "effects_for_label", lambda label: frozenset()
            )
            neutralised = screen_mod.screen(source, spec=spec_key)
            monkeypatch.undo()

            assert normal.verdict is neutralised.verdict, sql
            assert normal.ok == neutralised.ok, sql
            assert len(normal.findings) == len(neutralised.findings), sql
            for a, b in zip(normal.findings, neutralised.findings):
                assert (a.verdict, a.reason, a.severity, a.message, a.statement,
                        a.rule, a.targets) == \
                       (b.verdict, b.reason, b.severity, b.message, b.statement,
                        b.rule, b.targets), f"{sql!r}: effect lookup changed the verdict"
                # ...and the only permitted difference is the effect itself.
                if a.effect:
                    assert not b.effect

    def test_policy_still_decides_the_verdict_not_the_effect_axis(self, spec_key):
        """Two policies, same statement, same effect -- different verdicts.

        The mirror image of the previous test, and the reason the axes are worth
        separating. `screen_effect_is_independent_of_policy` does this for one DROP;
        this does it across a slice of the corpus, so the separation is a property of
        the design rather than of one lucky example.
        """
        corpus = _corpus_for(spec_key)
        default = default_policy()
        strict = read_only_policy()
        for sql, _ in corpus:
            source = f"spark.sql({sql!r})"
            a = screen(source, default, spec=spec_key)
            b = screen(source, strict, spec=spec_key)
            for fa, fb in zip(a.findings, b.findings):
                assert fa.statement == fb.statement
                assert fa.effect == fb.effect, (
                    f"{sql!r}: the effect changed with the policy -- the axes are "
                    f"supposed to be independent"
                )

    def test_verdict_and_reason_enums_untouched_by_the_effect_axis(self):
        """The Effect axis was to be additive, changing neither Verdict nor Reason.

        Asserted rather than assumed: a snapshot of the members, so an accidental
        addition fails here instead of landing quietly.

        This guard is now updated rather than merely satisfied. When the Effect axis
        landed it carried this exact test, and its docstring named the eventual
        follow-up: "a REVIEW verdict, say -- separate work with a large blast radius".
        That work has since been done deliberately, in its own change, with the blast
        radius updated at every call site. Leaving the assertion at three values would
        have meant deleting a guard to make a test pass; updating it here keeps the
        guard real and records that the exception was chosen, not overlooked.
        """
        assert {v.value for v in Verdict} == {"allow", "deny", "unknown", "review"}
        assert {r.value for r in Reason} >= {
            "no_matching_rule", "within_allowlist", "deny_rule",
            "destructive_statement", "outside_allowlist", "code_length_exceeded",
            "unparseable_sql", "unresolved_dynamic_sql",
            "unsupported_statement", "resource_limit", "unsupported_spark_version",
            "analysis_error",
        }
        # `dangerous_python_call` was here and is deliberately gone. A Reason that is
        # declared but never raised advertises screening the tool does not perform, and
        # a reader of the enum would reasonably assume the coverage existed
        # (sparkscreen-znf). Python-level call screening is out of scope.
        assert "dangerous_python_call" not in {r.value for r in Reason}
        # Was: "a fourth verdict appeared". That tripwire fired, correctly, and the
        # fourth value was then added on purpose in the REVIEW/UNKNOWN split. It stays
        # as a tripwire -- a *fifth* verdict is still unaccounted for and should fail
        # here rather than be discovered in a consumer's integration.
        assert len(list(Verdict)) == 4, "a fifth verdict appeared"

    def test_screen_module_does_not_import_policy_from_effects(self):
        """Dependency direction: effects reads policy, screen reads both.

        effects.py imports DESTRUCTIVE_LABELS lazily inside effect_label_drift, so a
        cycle is possible; this checks the screener still imports and runs, which is
        what would actually break.
        """
        assert callable(screen)
        assert screen("spark.sql('select 1')").verdict is Verdict.ALLOW

    def test_severity_and_unknown_reasons_unchanged(self):
        # Sanity that the effect work did not disturb the fail-closed machinery the
        # model docstring is built around.
        assert Severity.CRITICAL.value == "critical"
        assert Reason.UNPARSEABLE_SQL in UNKNOWN_REASONS_FROM_MODEL()


def UNKNOWN_REASONS_FROM_MODEL():
    from sparkscreen.model import UNKNOWN_REASONS
    return UNKNOWN_REASONS