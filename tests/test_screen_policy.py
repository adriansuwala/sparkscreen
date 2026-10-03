"""End-to-end tests for the screener, the policy layer, and the CLI.

The tests are organised around the project's core promise rather than around
function names: a report is only `ALLOW` when every SQL sink in the snippet was
resolved, parsed with a real grammar, and matched no deny rule. Anything that
could not be analyzed must surface as `UNKNOWN` and exit code 2, never as a
quiet "ok".

`spec_key` parametrizes the grammar-affecting tests over both pinned Spark
grammars (see conftest.py), so a verdict that only holds for one grammar fails
loudly here.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from sparkscreen import (
    Reason,
    Severity,
    Verdict,
    default_policy,
    load_policy,
    read_only_policy,
    screen,
)
from sparkscreen.analysis.folding import fold_sinks
from sparkscreen.analysis.treewalk import NamespaceRef
from sparkscreen.cli import (
    EXIT_ALLOW,
    EXIT_DENY,
    EXIT_UNKNOWN,
    main,
)
from sparkscreen.model import ANALYSIS_FAILURE_REASONS
from sparkscreen.policy import (
    DESTRUCTIVE_LABELS,
    READ_ONLY_LABELS,
    Limits,
    Policy,
    Rule,
    default_policy,
    policy_label_drift,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def sql_call(sql: str) -> str:
    """Source that embeds `sql` in a literal spark.sql() sink.

    Uses repr() so single quotes and newlines survive verbatim, which matters for
    the LOAD DATA case where the path is a single-quoted SQL literal.
    """
    return "spark.sql(%r)\n" % sql


def reasons(report) -> set[Reason]:
    return {f.reason for f in report.findings}


def statements(report) -> set[str]:
    return {f.statement for f in report.findings if f.statement}


# ---------------------------------------------------------------------------
# A. Verdict correctness
# ---------------------------------------------------------------------------

#: SQL that must be denied, with the ANTLR statement label we expect to see.
DENY_CASES = [
    ("DROP TABLE prod.users", "DropTable", Reason.DENY_RULE),
    ("TRUNCATE TABLE t", "TruncateTable", Reason.DENY_RULE),
    ("insert overwrite table t select 1", "InsertOverwriteTable",
     Reason.DESTRUCTIVE_STATEMENT),
    ("add jar /tmp/x.jar", "ManageResource", Reason.DENY_RULE),
    ("load data local inpath '/etc/passwd' into table t", "LoadData",
     Reason.DENY_RULE),
    ("replace table t as select 1", "ReplaceTable",
     Reason.DESTRUCTIVE_STATEMENT),
    ("alter table prod.a rename to prod.b", "RenameTable",
     Reason.DESTRUCTIVE_STATEMENT),
    ("drop view prod.v", "DropView", Reason.DENY_RULE),
    ("drop namespace prod", "DropNamespace", Reason.DENY_RULE),
    ("alter table t drop partition (dt='1')", "DropTablePartitions",
     Reason.DENY_RULE),
    ("alter table t add column b int", "AddTableColumns",
     Reason.DESTRUCTIVE_STATEMENT),
    ("CREATE FUNCTION f AS 'class' USING JAR '/tmp/x.jar'",
     "CreateFunction", Reason.DENY_RULE),
    ("ALTER TABLE t DROP COLUMN a", "DropTableColumns", Reason.DENY_RULE),
]


class TestDenyVerdicts:
    @pytest.mark.parametrize(
        "sql,label,reason", DENY_CASES, ids=[c[1] for c in DENY_CASES]
    )
    def test_destructive_sql_is_denied(self, spec_key, sql, label, reason):
        report = screen(sql_call(sql), spec=spec_key)
        assert report.verdict is Verdict.DENY
        assert not report.ok
        assert label in statements(report), (
            f"expected statement label {label}, got {statements(report)}"
        )
        assert reason in reasons(report)

    def test_severity_of_a_drop_is_critical(self, spec_key):
        report = screen(sql_call("DROP TABLE prod.users"), spec=spec_key)
        assert Severity.CRITICAL in {f.severity for f in report.findings}

    def test_finding_carries_the_python_line_number(self, spec_key):
        source = "import pyspark\n\nspark.sql('DROP TABLE prod.users')\n"
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.DENY
        assert [f.line for f in report.findings] == [3]

    def test_summary_counts(self, spec_key):
        report = screen(sql_call("DROP TABLE prod.users"), spec=spec_key)
        assert report.summary() == "DENY: 1 deny, 0 unknown, 0 review, 0 allow"

    def test_deny_outranks_a_concurrent_unknown(self, spec_key):
        """One denied sink and one unresolvable sink: DENY is the report verdict."""
        source = (
            "spark.sql('DROP TABLE prod.users')\n"
            "tbl = input()\n"
            "spark.sql(f'select * from {tbl}')\n"
        )
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.DENY
        assert report.summary() == "DENY: 1 deny, 1 unknown, 0 review, 0 allow"

    def test_multiple_sinks_each_produce_a_finding(self, spec_key):
        source = (
            "spark.sql('DROP TABLE prod.a')\n"
            "spark.sql('select 1')\n"
            "spark.sql('TRUNCATE TABLE t')\n"
        )
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.DENY
        assert len(report.by_verdict(Verdict.DENY)) == 2
        assert len(report.by_verdict(Verdict.ALLOW)) == 1


class TestAllowVerdicts:
    @pytest.mark.parametrize(
        "sql",
        [
            "select * from prod.t",
            "select 1",
            "select a from t1 where a > 1 group by a having count(*) > 2 "
            "order by a limit 5",
            "with x as (select 1) select * from x",
            "show tables",
            "describe table prod.t",
        ],
    )
    def test_read_only_queries_are_allowed(self, spec_key, sql):
        report = screen(sql_call(sql), spec=spec_key)
        assert report.verdict is Verdict.ALLOW, report.summary()
        assert report.ok
        assert reasons(report) == {Reason.NO_MATCHING_RULE}

    def test_no_sql_sink_at_all_is_allowed_with_no_findings(self, spec_key):
        source = (
            "df = spark.table('prod.t')\n"
            "df = df.filter('a > 1')\n"
            "df.count()\n"
        )
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.ALLOW
        assert report.ok
        assert report.findings == []
        assert report.summary() == "ALLOW: 0 deny, 0 unknown, 0 review, 0 allow"

    def test_dataframe_api_does_not_trigger_the_sql_sink_detector(self, spec_key):
        """`spark.table` / `df.filter` take strings but are not SQL sinks.

        This is a false-positive guard: if these were treated as sinks, every
        routine DataFrame script would come back UNKNOWN.
        """
        report = screen(sql_call("select 1").replace("spark.sql", "spark.table"),
                        spec=spec_key)
        assert report.verdict is Verdict.ALLOW
        assert report.findings == []

    def test_folded_constants_are_analyzed_not_skipped(self, spec_key):
        """A constant assembled from parts must still be parsed and denied.

        This is the "no text to evade" claim in policy.py's docstring: the
        statement is identified by its parse tree, so spelling it as
        `"DR" + "OP TABLE t"` vs `"DROP TABLE t"` makes no difference.
        """
        source = (
            "verb = 'DROP'\n"
            "obj = 'TABLE prod.users'\n"
            'spark.sql(verb + " " + obj)\n'
        )
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.DENY, report.summary()
        assert "DropTable" in statements(report)

    def test_obfuscated_spelling_is_denied_identically(self, spec_key):
        """Same statement, split and concatenated differently, same DENY."""
        source = (
            'spark.sql("DR" + "OP TABL" + "E prod.users")\n'
        )
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.DENY
        assert "DropTable" in statements(report)

    def test_comment_obfuscation_is_denied_identically(self, spec_key):
        source = 'spark.sql("DROP /* comment */ TABLE prod.users")\n'
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.DENY
        assert "DropTable" in statements(report)

    def test_alias_sink_name_is_treated_as_a_sink(self, spec_key):
        source = 'sql("DROP TABLE prod.users")\n'
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.DENY


class TestUnknownVerdicts:
    """The important half: anything unanalyzable must not be silently allowed."""

    def test_dynamic_sql_that_cannot_be_folded_is_unknown(self, spec_key):
        source = "tbl = input()\nspark.sql(f'drop table {tbl}')\n"
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert not report.ok
        assert reasons(report) == {Reason.UNRESOLVED_DYNAMIC_SQL}

    def test_unparseable_sql_is_unknown(self, spec_key):
        report = screen(sql_call("SELCT 1"), spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert not report.ok
        assert reasons(report) == {Reason.UNPARSEABLE_SQL}

    def test_loop_over_a_runtime_list_of_tables_is_unknown(self, spec_key):
        source = (
            "for t in list_tables():\n"
            "    spark.sql('drop table ' + t)\n"
        )
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert not report.ok
        assert reasons(report) == {Reason.UNRESOLVED_DYNAMIC_SQL}

    def test_sink_inside_a_function_is_unresolved_even_with_literal_callers(
        self, spec_key
    ):
        """Known interprocedural gap; recorded here so the fail-closed path is pinned.

        folding.py documents that it does not thread constants into function
        bodies. The screener must therefore report UNKNOWN, not ALLOW.
        """
        source = (
            "def run(tbl):\n"
            "    spark.sql(f'drop table {tbl}')\n"
            "run('prod.users')\n"
        )
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert not report.ok

    def test_sql_built_from_a_dict_lookup_is_unknown(self, spec_key):
        source = "spark.sql(TEMPLATES['drop'])\n"
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert not report.ok

    def test_comprehension_built_sql_is_unknown(self, spec_key):
        source = "spark.sql(''.join(f'drop table {t}' for t in tables))\n"
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN

    def test_delete_update_merge_need_review_not_deny(self, spec_key):
        """REVIEW, not DENY, and not UNKNOWN either.

        These are parsed perfectly well -- the label, the table and the effect are all
        known. They are REVIEW because the policy deliberately routes row mutation to a
        human rather than blocking it, which is a different statement from "we could not
        work out what this does". That distinction is the entire reason the scale has
        four values, so it is asserted exactly.
        """
        for sql in (
            "delete from prod.t where id = 1",
            "update prod.t set a = 1 where id = 2",
            "merge into prod.t using s on t.id = s.id when matched then "
            "update set *",
        ):
            report = screen(sql_call(sql), spec=spec_key)
            assert report.verdict is Verdict.REVIEW, (sql, report.verdict)
            assert not report.ok
            assert not report.analysis_failures

    def test_dml_is_flagged_as_row_mutation_by_the_dml_rule(self, spec_key):
        """DELETE/UPDATE/MERGE reach the row-mutation rule, not a generic miss.

        The parser emits DeleteFromTable/UpdateTable/MergeIntoTable, so the rule
        must key on those labels.
        """
        report = screen(sql_call("delete from t where id = 1"), spec=spec_key)
        assert report.findings[0].rule == "review.row-mutation"
        assert report.findings[0].reason is Reason.DESTRUCTIVE_STATEMENT

    @pytest.mark.parametrize(
        "sql,label",
        [
            ("delete from t where id=1", "DeleteFromTable"),
            ("update t set a=1 where id=2", "UpdateTable"),
            ("insert into t values (1)", "InsertIntoTable"),
        ],
    )
    def test_every_row_mutating_label_is_covered(self, spec_key, sql, label):
        report = screen(sql_call(sql), spec=spec_key)
        assert label in statements(report), f"{sql} -> {statements(report)}"
        assert report.findings[0].rule == "review.row-mutation"

    def test_dml_is_not_classified_read_only(self):
        """DmlStatement must not appear in READ_ONLY_LABELS.

        A label in both READ_ONLY_LABELS and a rule is a contradiction: a policy
        that dropped the rule would silently ALLOW row mutation.
        """
        assert "DmlStatement" not in READ_ONLY_LABELS

    def test_destructive_ddl_labels_are_all_destructive(self):
        assert "AlterTableCollation" in DESTRUCTIVE_LABELS

    def test_call_procedure_needs_review(self, spec_key):
        """`CALL p()` is recognised as Call on spark-4.0.

        On spark-3.5.1 the pinned grammar rejects it, so it still fails closed
        via UNPARSEABLE_SQL. Both paths are UNKNOWN; only the reason differs.
        """
        report = screen(sql_call("CALL p()"), spec=spec_key)
        assert not report.ok
        assert reasons(report) <= {Reason.UNSUPPORTED_STATEMENT,
                                   Reason.UNPARSEABLE_SQL}
        # The two grammars land on opposite ends of the scale, and that is correct:
        # the newest grammar parses it and has no rule (REVIEW), while the pinned 3.5.1
        # grammar rejects it outright, so there is nothing to review (UNKNOWN).
        if Reason.UNPARSEABLE_SQL in reasons(report):
            assert report.verdict is Verdict.UNKNOWN
        else:
            assert report.verdict is Verdict.REVIEW

    def test_unknown_outranks_a_concurrent_allow(self, spec_key):
        source = (
            "spark.sql('select 1')\n"
            "tbl = input()\n"
            "spark.sql(f'select * from {tbl}')\n"
        )
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert not report.ok

    def test_unparseable_sql_records_no_statement_label(self, spec_key):
        """We could not parse, so we must not claim to know the label."""
        report = screen(sql_call("SELCT 1"), spec=spec_key)
        assert all(f.statement is None for f in report.findings)

    def test_rename_table_syntax_is_not_valid_spark_sql(self, spec_key):
        """`RENAME TABLE a TO b` is Hive syntax; Spark only accepts ALTER TABLE.

        It therefore lands in UNKNOWN (fail-closed), not in the deny branch. This
        test pins that spelling so nobody later "fixes" it into a silent allow.
        """
        report = screen(sql_call("rename table a to b"), spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert not report.ok


# ---------------------------------------------------------------------------
# B. Limits
# ---------------------------------------------------------------------------


class TestCodeLengthLimit:
    def test_default_limit_is_20k(self):
        assert default_policy().limits.max_code_chars == 20_000

    def test_oversize_code_is_denied(self, spec_key):
        limit = default_policy().limits.max_code_chars
        source = "# pad\n" * (limit // 6 + 1)
        assert len(source) > limit
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.DENY
        assert reasons(report) == {Reason.CODE_LENGTH_EXCEEDED}

    def test_just_under_the_limit_is_not_rejected_for_length(self, spec_key):
        limit = default_policy().limits.max_code_chars
        source = "#" + "a" * (limit - 1)
        assert len(source) == limit
        report = screen(source, spec=spec_key)
        assert Reason.CODE_LENGTH_EXCEEDED not in reasons(report)
        assert report.verdict is Verdict.ALLOW

    def test_exactly_at_the_limit_is_allowed(self, spec_key):
        """The check is `n > limit`, so n == limit passes."""
        limit = default_policy().limits.max_code_chars
        report = screen("#" + "a" * (limit - 1), spec=spec_key)
        assert report.verdict is Verdict.ALLOW

    def test_oversize_code_is_denied_before_any_analysis(self, spec_key):
        """A too-long snippet is DENY even when it is also unanalyzable."""
        limit = default_policy().limits.max_code_chars
        source = "def f(:\n" + "# pad\n" * (limit // 6)
        assert len(source) > limit
        report = screen(source, spec=spec_key)
        assert report.verdict is Verdict.DENY
        assert reasons(report) == {Reason.CODE_LENGTH_EXCEEDED}

    def test_length_denial_is_a_deny_not_an_unknown(self, spec_key):
        """Documented intent: the length cap is the reviewer's explicit decision."""
        source = "# pad\n" * (default_policy().limits.max_code_chars // 6 + 1)
        report = screen(source, spec=spec_key)
        assert not any(f.is_unknown for f in report.findings)


class TestSqlLengthLimit:
    def test_enormous_single_sql_triggers_the_sql_branch(self, spec_key):
        sql = "select " + "a," * 6000 + "1 from t"
        policy = default_policy()
        assert len(sql) > policy.limits.max_sql_chars
        # the enclosing source stays under max_code_chars, so this is the SQL branch
        assert len(sql_call(sql)) <= policy.limits.max_code_chars
        report = screen(sql_call(sql), spec=spec_key)
        assert report.verdict is Verdict.DENY
        assert reasons(report) == {Reason.CODE_LENGTH_EXCEEDED}
        assert report.findings[0].line == 1

    def test_sql_length_message_truncates_the_echoed_sql(self, spec_key):
        sql = "select " + "a," * 6000 + "1 from t"
        report = screen(sql_call(sql), spec=spec_key)
        assert len(report.findings[0].sql) < len(sql)
        assert report.findings[0].sql.endswith("...")

    def test_oversize_sql_is_not_also_reported_as_unparseable(self, spec_key):
        """The length branch `continue`s; it must not fall through to the parser."""
        sql = "select " + "a," * 6000 + "1 from t"
        report = screen(sql_call(sql), spec=spec_key)
        assert Reason.UNPARSEABLE_SQL not in reasons(report)


class TestOtherLimits:
    def test_sql_length_limit_boundary_is_exclusive(self, spec_key):
        """Same boundary as `max_targets`, on the SQL-length cap in screen.py.

        Second mutation survivor of the identical class: `len(sql) > max_sql_chars` could
        be `>=` without any test noticing, because every test that hits the limit uses a
        value far above it. Fixed alongside `max_targets` and `max_literals` because
        finding the class once is not the same as fixing it once.
        """
        from sparkscreen.policy import Policy as _P

        sql = "select 1"
        exact = _P(name="e", rules=default_policy().rules,
                   limits=Limits(max_sql_chars=len(sql)))
        report = screen(sql_call(sql), exact, spec=spec_key)
        assert Reason.CODE_LENGTH_EXCEEDED not in reasons(report), (
            "SQL exactly at max_sql_chars must be allowed"
        )
        under = _P(name="u", rules=default_policy().rules,
                   limits=Limits(max_sql_chars=len(sql) - 1))
        assert Reason.CODE_LENGTH_EXCEEDED in reasons(screen(
            sql_call(sql), under, spec=spec_key))

    def test_statement_limit_boundary_is_exclusive(self, spec_key):
        """`len(statements) > max_statements` -> `>=` was also a survivor."""
        from sparkscreen.policy import Policy as _P

        one = "BEGIN select 1; END"
        exact = _P(name="s", rules=default_policy().rules,
                   limits=Limits(max_statements=1))
        report = screen(sql_call(one), exact, spec=spec_key)
        if Reason.UNPARSEABLE_SQL in reasons(report):
            pytest.skip("this grammar does not accept a multi-statement body")
        assert Reason.RESOURCE_LIMIT not in reasons(report)
        under = _P(name="s2", rules=default_policy().rules,
                   limits=Limits(max_statements=0))
        assert Reason.RESOURCE_LIMIT in reasons(screen(
            sql_call(one), under, spec=spec_key))

    def test_limit_boundary_is_exclusive(self, spec_key):
        """A statement exactly AT a limit is under it; one over is not.

        Found by mutation testing: flipping `len(targets) > max_targets` to `>=`
        survived the entire suite. Both forms pass every other test, because the existing
        tests use `max_targets=0` with one target -- where `>` and `>=` agree. Only the
        exact boundary distinguishes them.

        For a screener this is a real, if small, hole: an off-by-one here admits a
        statement sitting precisely on a limit an operator set, which is exactly the
        value they would expect the limit to exclude.
        """
        from sparkscreen.policy import Policy as _P

        # one target, limit of exactly 1 -> under the limit
        at_limit = _P(name="t", rules=default_policy().rules,
                      limits=Limits(max_targets=1))
        report = screen(sql_call("select * from t"), at_limit, spec=spec_key)
        assert Reason.RESOURCE_LIMIT not in reasons(report), (
            "exactly at the limit must be allowed through"
        )
        # one target, limit of 0 -> over
        over = _P(name="t", rules=default_policy().rules, limits=Limits(max_targets=0))
        assert Reason.RESOURCE_LIMIT in reasons(screen(
            sql_call("select * from t"), over, spec=spec_key))

    def test_literal_limit_boundary_is_exclusive(self, spec_key):
        from sparkscreen.policy import Policy as _P

        at_limit = _P(name="l", rules=default_policy().rules,
                      limits=Limits(max_literals=1))
        report = screen(sql_call("select * from t where a = 'x'"), at_limit, spec=spec_key)
        assert Reason.RESOURCE_LIMIT not in reasons(report)
        over = _P(name="l", rules=default_policy().rules, limits=Limits(max_literals=0))
        assert Reason.RESOURCE_LIMIT in reasons(screen(
            sql_call("select * from t where a = 'x'"), over, spec=spec_key))

    def test_read_only_policy_raises_severity_for_unsupported_statements(self, spec_key):
        """`read_only_policy()` rewrites config-ish rules to UNKNOWN/UNSUPPORTED_STATEMENT.

        Found by mutation testing: negating `r.severity is Severity.INFO` survived, and so
        did flipping the `is` to `is not`. The verdict half is asserted below; the severity
        half is cosmetic but is the difference between a finding that sorts to the top of a
        reviewer's queue and one that does not, so it is pinned too.

        This override is the whole reason `read_only_policy()` is a different policy rather
        than `default_policy()` with fewer rules: for an analysis-only agent, `CACHE TABLE`
        is not "deny this", it is "this agent cannot help you with this".
        """
        from sparkscreen import read_only_policy

        # `CACHE TABLE` matches `review.config` under the default policy (REVIEW), and is
        # rewritten here. (`CREATE PIPELINE` looked like a better choice but is
        # unparseable on these grammars, which exercises a different path entirely.)
        report = screen(sql_call("CACHE TABLE t"), read_only_policy(), spec=spec_key)
        finding = next(f for f in report.findings
                       if f.reason is Reason.UNSUPPORTED_STATEMENT)
        assert finding.verdict is Verdict.UNKNOWN
        assert finding.rule == "review.config"
        assert finding.severity.value != "info", (
            "the rewrite should bump severity off INFO, got "
            f"{finding.severity.value}"
        )

        # and the contrast that makes the override meaningful
        default_report = screen(sql_call("CACHE TABLE t"), spec=spec_key)
        assert default_report.verdict is Verdict.REVIEW

    def test_too_many_targets_is_resource_limit(self, spec_key):
        policy = Policy(name="t", rules=default_policy().rules,
                        limits=Limits(max_targets=0))
        report = screen(sql_call("select * from t"), policy, spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert reasons(report) == {Reason.RESOURCE_LIMIT}

    def test_too_many_literals_is_resource_limit(self, spec_key):
        policy = Policy(name="l", rules=default_policy().rules,
                        limits=Limits(max_literals=0))
        report = screen(sql_call("select * from t where a = 'x'"), policy,
                        spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert reasons(report) == {Reason.RESOURCE_LIMIT}

    def test_too_many_statements_is_resource_limit(self, spec_key):
        """Reachable only on grammars that accept a BEGIN...END script body."""
        source = sql_call("BEGIN select 1; select 2; END")
        policy = Policy(name="s", rules=default_policy().rules,
                        limits=Limits(max_statements=1))
        report = screen(source, policy, spec=spec_key)
        if report.verdict is Verdict.UNKNOWN and \
                reasons(report) == {Reason.UNPARSEABLE_SQL}:
            pytest.skip("grammar does not accept a multi-statement script body")
        assert reasons(report) == {Reason.RESOURCE_LIMIT}
        assert report.verdict is Verdict.UNKNOWN


# ---------------------------------------------------------------------------
# C. Namespace allowlists
# ---------------------------------------------------------------------------


def policy_with_writable(*namespaces: str) -> Policy:
    base = default_policy()
    return Policy(name="writable", rules=base.rules,
                  writable_namespaces=tuple(namespaces))


class TestWritableNamespaceAllowlist:
    def test_drop_inside_the_allowlist_raises_no_objection(self, spec_key):
        report = screen(sql_call("DROP TABLE staging.foo"), policy_with_writable("staging.*"),
                        spec=spec_key)
        assert not any("outside the writable namespaces" in f.message
                       for f in report.findings)

    def test_drop_outside_the_allowlist_is_flagged(self, spec_key):
        report = screen(sql_call("DROP TABLE prod.foo"), policy_with_writable("staging.*"),
                        spec=spec_key)
        assert "prod.foo is outside the writable namespaces" in \
            " ".join(f.message for f in report.findings)

    def test_wildcard_matches_per_component(self, spec_key):
        policy = policy_with_writable("staging.*")
        assert "outside" not in " ".join(
            f.message for f in screen(sql_call("DROP TABLE staging.foo"), policy,
                                      spec=spec_key).findings)
        assert "outside" in " ".join(
            f.message for f in screen(sql_call("DROP TABLE prod.staging.foo"), policy,
                                      spec=spec_key).findings)

    def test_a_wildcard_does_not_absorb_extra_components(self, spec_key):
        """`prod.*` means "tables in prod", not "anything under prod".

        Regression test on `NamespaceRef.matches`, which used to let a trailing `*`
        absorb any number of components, so `prod.*` matched `prod.staging.x` and
        `prod.a.b.c.d.e`. For an *allowlist* that is a fail-open: an operator who wrote
        `readable_namespaces=("prod.*",)` got every descendant namespace, including ones
        they would never have named.

        Asserted on the DataFrame path, which passes the destination through whole and so
        exercises `matches` directly. The SQL path has a separate, pre-existing extraction
        problem for 3+ component names -- recorded as F14, and deliberately not conflated
        with this one, because fixing one while asserting the other would hide both.
        """
        from sparkscreen.policy import Policy as _P

        policy = _P(name="w", rules=default_policy().rules,
                    writable_namespaces=("prod.*",), readable_namespaces=("*",))

        assert screen('df.write.mode("append").saveAsTable("prod.users")', policy,
                      spec=spec_key).verdict is Verdict.ALLOW
        for name in ("prod.staging.x", "prod.a.b.c"):
            report = screen(f'df.write.mode("append").saveAsTable("{name}")', policy,
                            spec=spec_key)
            assert report.verdict is Verdict.REVIEW, (
                f"{name} must not be writable under prod.*, got {report.verdict}"
            )

    def test_a_wildcard_does_not_match_a_longer_component(self, spec_key):
        """`prod.*` must not match `production.users` -- `*` is not a prefix match."""
        from sparkscreen.policy import Policy as _P

        policy = _P(name="r", rules=default_policy().rules,
                    readable_namespaces=("prod.*",))
        assert screen(sql_call("select * from prod.users"), policy,
                      spec=spec_key).verdict is Verdict.ALLOW
        # `*` is a whole-component wildcard, not a string prefix.
        assert screen(sql_call("select * from production.users"), policy,
                      spec=spec_key).verdict is Verdict.REVIEW

    def test_a_bare_star_still_means_no_restriction(self, spec_key):
        """The fix above must not break `("*",)`, which means "everything".

        This is the case a naive arity rule would catch: a bare `*` is one component and
        most names are two or more.
        """
        from sparkscreen.policy import Policy as _P

        # Both lists, because a DataFrame write is also subject to the *writable*
        # check, and an empty writable_namespaces means REVIEW by design (an absent
        # allowlist is an absent answer). Getting that wrong is what made this test fail
        # the first time -- for the right reason, about the wrong thing.
        policy = _P(name="o", rules=default_policy().rules,
                    readable_namespaces=("*",), writable_namespaces=("*",))
        for name in ("prod.users", "staging.foo"):
            assert screen(sql_call(f"select * from {name}"), policy,
                          spec=spec_key).verdict is Verdict.ALLOW, name
        assert screen('df.write.mode("append").saveAsTable("a.b.c.d")', policy,
                      spec=spec_key).verdict is Verdict.ALLOW

    def test_read_only_statements_are_unaffected_by_writable_list(self, spec_key):
        policy = policy_with_writable("staging.*")
        report = screen(sql_call("select * from prod.t"), policy, spec=spec_key)
        assert report.verdict is Verdict.ALLOW

    def test_policy_constructor_signature_accepts_writable_namespaces(self):
        """Guard against a rename that would silently disable the allowlist."""
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("staging.*",))
        assert policy.writable_namespaces == ("staging.*",)

    def test_outside_allowlist_appears_as_a_finding_reason(self, spec_key):
        report = screen(sql_call("DROP TABLE prod.foo"),
                        policy_with_writable("staging.*"), spec=spec_key)
        assert Reason.OUTSIDE_ALLOWLIST in reasons(report)

    def test_outside_allowlist_is_review_not_deny(self, spec_key):
        """Out-of-allowlist is REVIEW -- the weaker verdict, deliberately.

        The deny rule also matches, but the allowlist objection is what a human needs
        to see, so it becomes the primary finding and the report verdict is REVIEW.
        A caller triaging by exit code must not read "outside the allowlist" as
        "blocked by policy".

        This also pins the `_combine` ordering, which had to change when REVIEW was
        split out from UNKNOWN: it used to look for the UNKNOWN finding and fell
        through to a DENY once the allowlist started emitting REVIEW, dropping
        `OUTSIDE_ALLOWLIST` from the report entirely.
        """
        report = screen(sql_call("DROP TABLE prod.foo"),
                        policy_with_writable("staging.*"), spec=spec_key)
        assert report.verdict is Verdict.REVIEW
        assert not report.ok
        assert "prod.foo" in report.by_verdict(Verdict.REVIEW)[0].targets
        # and the rule verdict is not lost, only demoted
        assert Reason.OUTSIDE_ALLOWLIST in reasons(report)


class TestReadableNamespaceAllowlist:
    def test_read_outside_allowlist_must_not_be_ok(self, spec_key):
        """A read outside readable_namespaces is needs-review, never ok.

        This is the fail-closed contract: policy.py documents out-of-scope
        reads as "needs a human", so report.ok must be False.
        """
        base = default_policy()
        policy = Policy(name="readable", rules=base.rules,
                        readable_namespaces=("prod.*",))
        report = screen(sql_call("select * from secret.salaries"), policy,
                        spec=spec_key)
        assert not report.ok
        assert report.verdict is Verdict.REVIEW
        assert Reason.OUTSIDE_ALLOWLIST in reasons(report)

    def test_read_inside_allowlist_is_ok(self, spec_key):
        base = default_policy()
        policy = Policy(name="readable", rules=base.rules,
                        readable_namespaces=("prod.*",))
        report = screen(sql_call("select * from prod.t"), policy, spec=spec_key)
        assert report.ok

    def test_both_lists_should_apply_to_destructive_statements(self, spec_key):
        """A table in the writable set but outside the readable set must object.

        Regression test. policy.py joined the two allowlist checks with `elif`, so
        whichever fired first ate the other: with `writable_namespaces` set, the
        readable check was never consulted for a destructive statement. `staging.x`
        passes the writable check and is plainly outside `prod.*`, yet nothing objected.

        This is the case a policy setting both lists is most likely to rely on --
        "you may write staging, you may read prod" -- so silently checking only half of
        it is a policy hole rather than a cosmetic issue.
        """
        base = default_policy()
        policy = Policy(name="both", rules=base.rules,
                        writable_namespaces=("staging.*",),
                        readable_namespaces=("prod.*",))
        report = screen(sql_call("DROP TABLE staging.x"), policy, spec=spec_key)
        assert "outside the readable namespaces" in \
            " ".join(f.message for f in report.findings)


# ---------------------------------------------------------------------------
# D. read_only_policy
# ---------------------------------------------------------------------------


class TestReadOnlyPolicy:
    @pytest.mark.parametrize(
        "sql",
        [
            "select * from prod.t",
            "select 1",
            "with x as (select 1) select * from x",
        ],
    )
    def test_queries_are_allowed(self, spec_key, sql):
        report = screen(sql_call(sql), read_only_policy(), spec=spec_key)
        assert report.verdict is Verdict.ALLOW
        assert report.ok

    @pytest.mark.parametrize(
        "sql,labels",
        [
            ("DROP TABLE prod.users", {"DropTable"}),
            ("TRUNCATE TABLE t", {"TruncateTable"}),
            ("insert overwrite table t select 1", {"InsertOverwriteTable"}),
            ("replace table t as select 1", {"ReplaceTable"}),
            ("add jar /tmp/x.jar", {"ManageResource"}),
            ("load data local inpath '/etc/passwd' into table t", {"LoadData"}),
            ("alter table a rename to b", {"RenameTable"}),
            ("cache table t", {"CacheTable"}),
            ("set spark.sql.shuffle.partitions=8", {"SetConfiguration"}),
            ("CALL p()", {"Call", None}),
            ("delete from t where id=1", {"DeleteFromTable", "DmlStatement"}),
            ("CREATE FUNCTION f AS 'c' USING JAR '/tmp/x.jar'",
             {"CreateFunction"}),
        ],
    )
    def test_everything_else_needs_review(self, spec_key, sql, labels):
        report = screen(sql_call(sql), read_only_policy(), spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN, f"{sql} -> {report.summary()}"
        assert not report.ok
        assert statements(report) <= labels, f"{sql} -> {statements(report)}"
        assert reasons(report) <= {Reason.UNSUPPORTED_STATEMENT,
                                   Reason.UNPARSEABLE_SQL}

    def test_read_only_never_denies(self, spec_key):
        base = read_only_policy()
        assert all(r.verdict is not Verdict.DENY for r in base.rules)

    def test_read_only_is_distinct_from_default(self, spec_key):
        assert screen(sql_call("DROP TABLE t"), read_only_policy(),
                      spec=spec_key).verdict is Verdict.UNKNOWN
        assert screen(sql_call("DROP TABLE t"), default_policy(),
                      spec=spec_key).verdict is Verdict.DENY

    def test_mutating_read_only_policy_does_not_corrupt_the_default(self, spec_key):
        read_only_policy()
        assert screen(sql_call("DROP TABLE t"), default_policy(),
                      spec=spec_key).verdict is Verdict.DENY

    def test_no_sql_sink_is_still_allowed(self, spec_key):
        report = screen("df = spark.table('t')\n", read_only_policy(), spec=spec_key)
        assert report.verdict is Verdict.ALLOW

    def test_dynamic_sql_stays_unknown_under_read_only(self, spec_key):
        source = "tbl = input()\nspark.sql(f'drop table {tbl}')\n"
        report = screen(source, read_only_policy(), spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert reasons(report) == {Reason.UNRESOLVED_DYNAMIC_SQL}


# ---------------------------------------------------------------------------
# E. CLI
# ---------------------------------------------------------------------------


def run_cli(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def write(tmp_path, name: str, content: str):
    path = tmp_path / name
    path.write_text(content)
    return str(path)


class TestCliExitCodes:
    def test_clean_query_exits_zero(self, tmp_path):
        path = write(tmp_path, "ok.py", sql_call("select * from prod.t"))
        code, out, _ = run_cli(path, "--no-color")
        assert code == EXIT_ALLOW
        assert "ALLOW:" in out

    def test_no_sql_sink_exits_zero(self, tmp_path):
        path = write(tmp_path, "plain.py", "df = spark.table('t')\ndf.count()\n")
        assert run_cli(path, "--no-color")[0] == EXIT_ALLOW

    def test_drop_exits_one(self, tmp_path):
        path = write(tmp_path, "drop.py", sql_call("DROP TABLE prod.users"))
        code, out, _ = run_cli(path, "--no-color")
        assert code == EXIT_DENY
        assert "DENY:" in out

    def test_dynamic_unresolvable_exits_two(self, tmp_path):
        path = write(tmp_path, "dyn.py",
                     "tbl = input()\nspark.sql(f'drop table {tbl}')\n")
        code, out, _ = run_cli(path, "--no-color")
        assert code == EXIT_UNKNOWN
        assert "UNKNOWN:" in out

    def test_unparseable_sql_exits_two(self, tmp_path):
        path = write(tmp_path, "bad.sql", sql_call("SELCT 1"))
        assert run_cli(path, "--no-color")[0] == EXIT_UNKNOWN

    def test_python_syntax_error_exits_two(self, tmp_path):
        path = write(tmp_path, "syntax.py", "def f(:\n  pass\n")
        assert run_cli(path, "--no-color")[0] == EXIT_UNKNOWN

    def test_empty_file_exits_zero(self, tmp_path):
        path = write(tmp_path, "empty.py", "")
        code, out, _ = run_cli(path, "--no-color")
        assert code == EXIT_ALLOW
        assert "no SQL sinks found" in out

    def test_oversize_exits_one(self, tmp_path):
        path = write(tmp_path, "big.py",
                     "# pad\n" * (default_policy().limits.max_code_chars // 6 + 1))
        code, out, _ = run_cli(path, "--no-color")
        assert code == EXIT_DENY
        assert "code_length_exceeded" in out

    def test_missing_file_exits_two_without_raising(self, tmp_path):
        code, _, err = run_cli(str(tmp_path / "nope.py"))
        assert code == EXIT_UNKNOWN
        assert "cannot read" in err

    def test_read_only_flag(self, tmp_path):
        path = write(tmp_path, "drop.py", sql_call("DROP TABLE prod.users"))
        assert run_cli(path, "--read-only", "--no-color")[0] == EXIT_UNKNOWN

    def write_allowlist_policy(self, tmp_path, **kwargs) -> str:
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps({
            "name": "corp",
            "rules": [{
                "id": "corp.allow", "verdict": "allow",
                "reason": "no_matching_rule", "message": "ok",
                "labels": ["StatementDefault"],
            }],
            **kwargs,
        }))
        return str(policy)

    def test_readable_allowlist_violation_must_exit_two(self, tmp_path):
        """An out-of-scope read must exit 2, not 0.

        This is the exit-code half of the fail-closed contract: the CLI used to
        gate on `f.reason in ANALYSIS_FAILURE_REASONS` while Reason.OUTSIDE_ALLOWLIST was
        absent from that set, so it printed UNKNOWN and still exited 0 --
        silently bypassing any allowlist policy in a CI gate.

        The printed verdict is REVIEW, not UNKNOWN: the allowlist check ran and
        returned a negative. REVIEW and UNKNOWN share exit 2 on purpose -- the exit
        status is the tool's binary gate, and the verdict is the richer answer.
        """
        policy = self.write_allowlist_policy(
            tmp_path, readable_namespaces=["prod.*"])
        path = write(tmp_path, "read.py",
                     sql_call("select * from secret.salaries"))
        code, out, _ = run_cli(path, "--policy", policy, "--no-color")
        assert "REVIEW:" in out
        assert code == EXIT_UNKNOWN

    def test_writable_allowlist_violation_must_exit_two(self, tmp_path):
        """A write outside writable_namespaces must exit 2, not 0."""
        policy = self.write_allowlist_policy(
            tmp_path, writable_namespaces=["staging.*"])
        path = write(tmp_path, "drop.py", sql_call("DROP TABLE prod.foo"))
        code, out, _ = run_cli(path, "--policy", policy, "--no-color")
        assert "REVIEW:" in out
        assert code == EXIT_UNKNOWN

    @pytest.mark.parametrize(
        "source,policy_kwargs,expected",
        [
            ("spark.sql('select * from prod.t')\n", {}, EXIT_ALLOW),
            ("spark.sql('SELCT 1')\n", {}, EXIT_UNKNOWN),
            ("spark.sql('select * from secret.s')\n",
             {"readable_namespaces": ["prod.*"]}, EXIT_UNKNOWN),
            ("spark.sql('DROP TABLE prod.foo')\n",
             {"writable_namespaces": ["staging.*"]}, EXIT_UNKNOWN),
            # A policy with only an allow.query rule has nothing for DropTable,
            # so it must fall through to the fail-closed UNKNOWN, never ALLOW.
            ("spark.sql('DROP TABLE prod.foo')\n", {}, EXIT_UNKNOWN),
        ],
        ids=["allow", "unparseable", "read-outside", "write-outside", "no-rule"],
    )
    def test_exit_code_matches_the_reported_verdict(self, tmp_path, source,
                                                    policy_kwargs, expected):
        """The whole point of the CLI: exit code must equal the printed verdict.

        A verdict printed as UNKNOWN with exit 0 is the exact CI failure this
        project exists to prevent, so this sweep is deliberately broad. The DENY
        case is covered separately against the default policy, since a custom
        policy that omits a deny rule is itself an UNKNOWN, not a DENY.
        """
        policy = self.write_allowlist_policy(tmp_path, **policy_kwargs)
        path = write(tmp_path, "case.py", source)
        code, out, _ = run_cli(path, "--policy", policy, "--no-color")
        printed = out.split()[0].rstrip(":")
        assert code == expected, f"printed {printed}, exit {code}"
        # REVIEW and UNKNOWN share exit 2 on purpose: the tool's contract is
        # binary -- safe to run, or a human looks at it. Which of the two it was
        # is the verdict field, not the exit status.
        assert {"ALLOW": EXIT_ALLOW, "DENY": EXIT_DENY,
                "UNKNOWN": EXIT_UNKNOWN,
                "REVIEW": EXIT_UNKNOWN}[printed] == code

    def test_policy_file_flag(self, tmp_path):
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps({
            "name": "corp",
            "rules": [{
                "id": "corp.allow", "verdict": "allow",
                "reason": "no_matching_rule", "message": "ok",
                "labels": ["StatementDefault"],
            }],
        }))
        path = write(tmp_path, "drop.py", sql_call("DROP TABLE prod.users"))
        code, out, _ = run_cli(path, "--policy", str(policy), "--no-color")
        # no rule matches DropTable -> fail-closed UNKNOWN, not ALLOW
        assert code == EXIT_UNKNOWN
        assert "policy=corp" in out
        good = write(tmp_path, "ok.py", sql_call("select 1"))
        assert run_cli(good, "--policy", str(policy), "--no-color")[0] == EXIT_ALLOW

    def test_malformed_policy_file_exits_two(self, tmp_path):
        policy = tmp_path / "bad.json"
        policy.write_text("{not json")
        path = write(tmp_path, "ok.py", sql_call("select 1"))
        code, _, err = run_cli(path, "--policy", str(policy))
        assert code == EXIT_UNKNOWN
        assert "bad policy" in err

    def test_list_grammars(self, tmp_path):
        code, out, _ = run_cli("--list-grammars", "ignored")
        assert code == 0
        assert "spark-4.0" in out and "spark-3.5.1" in out

    def test_json_output_is_valid_json_with_verdict_key(self, tmp_path):
        path = write(tmp_path, "drop.py", sql_call("DROP TABLE prod.users"))
        code, out, _ = run_cli(path, "--json")
        payload = json.loads(out)
        assert code == EXIT_DENY
        assert payload["verdict"] == "deny"
        assert payload["policy"] == "default"
        assert payload["grammar"]
        assert isinstance(payload["findings"], list) and payload["findings"]

    def test_json_output_for_every_verdict(self, tmp_path):
        cases = {
            "allow": (sql_call("select 1"), EXIT_ALLOW),
            "deny": (sql_call("DROP TABLE t"), EXIT_DENY),
            "unknown": (sql_call("SELCT 1"), EXIT_UNKNOWN),
        }
        for expected, (source, expected_code) in cases.items():
            path = write(tmp_path, f"{expected}.py", source)
            code, out, _ = run_cli(path, "--json")
            assert json.loads(out)["verdict"] == expected
            assert code == expected_code

    def test_json_finding_shape_round_trips(self, tmp_path):
        path = write(tmp_path, "drop.py", sql_call("DROP TABLE prod.users"))
        _, out, _ = run_cli(path, "--json")
        finding = json.loads(out)["findings"][0]
        assert finding["verdict"] == "deny"
        assert finding["reason"] == "deny_rule"
        assert finding["statement"] == "DropTable"
        assert finding["rule"] == "deny.drop"
        assert "prod.users" in finding["targets"]
        assert finding["severity"] == "critical"

    def test_json_of_a_clean_run_has_empty_findings(self, tmp_path):
        path = write(tmp_path, "plain.py", "x = 1\n")
        _, out, _ = run_cli(path, "--json")
        payload = json.loads(out)
        assert payload["verdict"] == "allow"
        assert payload["findings"] == []

    def test_text_output_shows_statement_and_rule(self, tmp_path):
        path = write(tmp_path, "drop.py", sql_call("DROP TABLE prod.users"))
        _, out, _ = run_cli(path, "--no-color")
        assert "statement=DropTable" in out
        assert "rule=deny.drop" in out
        assert "line 1" in out

    def test_no_colour_leaves_no_escape_codes(self, tmp_path):
        path = write(tmp_path, "drop.py", sql_call("DROP TABLE prod.users"))
        _, out, _ = run_cli(path, "--no-color")
        assert "\033[" not in out

    def test_spark_version_flag_accepts_a_bare_version(self, tmp_path):
        """--spark takes a bare Spark version as well as a grammar key.

        The flag's help text advertises both, and `spec_for_spark_version` exists
        to resolve the bare form. It used to pass the string straight to screen(),
        which raised KeyError and degraded to UNKNOWN / exit 2.
        """
        path = write(tmp_path, "ok.py", sql_call("select * from prod.t"))
        code, out, _ = run_cli(path, "--spark", "3.5.1", "--no-color")
        assert code == EXIT_ALLOW
        assert "grammar=spark-3.5.1" in out

    def test_unknown_spark_version_exits_two(self, tmp_path):
        """An unusable --spark value is a hard error, reported on stderr.

        The CLI now resolves the flag itself (grammar key or bare version) rather
        than letting screen() raise KeyError and degrade to a report, so the
        failure surfaces before any analysis with the known keys listed.
        """
        path = write(tmp_path, "ok.py", sql_call("select * from prod.t"))
        code, out, err = run_cli(path, "--spark", "9.9.9", "--no-color")
        assert code == EXIT_UNKNOWN
        assert "unknown Spark version" in err
        assert "spark-4.0" in err
        assert out == ""

    def test_grammar_key_is_accepted(self, tmp_path):
        path = write(tmp_path, "ok.py", sql_call("select * from prod.t"))
        code, out, _ = run_cli(path, "--spark", "spark-3.5.1", "--no-color")
        assert code == EXIT_ALLOW
        assert "grammar=spark-3.5.1" in out

    def test_stdin_dash(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(sql_call("DROP TABLE t")))
        code, out, _ = run_cli("-", "--no-color")
        assert code == EXIT_DENY


# ---------------------------------------------------------------------------
# F. Fail-closed invariants
# ---------------------------------------------------------------------------

#: A broad corpus, used by the invariant sweep. Includes every unanalyzable shape
#: the screener claims to handle, so a regression anywhere shows up here.
CORPUS = [
    "",
    "   \n\n  ",
    "# only a comment\n",
    "x = 1\n",
    "def f():\n    return 1\n",
    sql_call("select 1"),
    sql_call("select * from prod.t"),
    sql_call("DROP TABLE prod.users"),
    sql_call("SELCT 1"),
    sql_call("rename table a to b"),
    "tbl = input()\nspark.sql(f'drop table {tbl}')\n",
    "for t in tables:\n    spark.sql('drop table ' + t)\n",
    "spark.sql(TEMPLATES['drop'])\n",
    "def run(tbl):\n    spark.sql(f'drop table {tbl}')\n",
    "spark.sql('DROP TABLE prod.a')\nspark.sql(f'select * from {input()}')\n",
    "spark.sql('select 1')\nspark.sql('SELCT 1')\n",
    "class A:\n    def m(self):\n        spark.sql('DROP TABLE t')\n",
    "lambda: spark.sql('DROP TABLE t')",
    "for _ in range(3):\n    spark.sql('TRUNCATE TABLE t')\n",
    "while True:\n    spark.sql('DROP TABLE t')\n",
    "if flag:\n    spark.sql('DROP TABLE t')\n",
    "spark.sql('')\n",
    "spark.sql('   ')\n",
    "spark.sql('; drop table t')\n",
]


class TestFailClosedInvariants:
    @pytest.mark.parametrize("source", CORPUS, ids=range(len(CORPUS)))
    def test_screen_never_raises(self, spec_key, source):
        """Every failure mode must come back as a report, not an exception."""
        report = screen(source, spec=spec_key)
        assert report.verdict in (Verdict.ALLOW, Verdict.DENY, Verdict.UNKNOWN,
                                  Verdict.REVIEW)

    @pytest.mark.parametrize("source", CORPUS, ids=range(len(CORPUS)))
    def test_analysis_failure_reason_is_never_paired_with_deny(self, spec_key, source):
        """Invariant: no finding may be a DENY whose reason means 'could not tell'.

        A DENY carrying an analysis-failure reason would claim certainty the screener
        does not have, and would make the triage meaningless. Note this is keyed on
        the *reason set*, deliberately: it holds regardless of how the verdict scale
        is arranged, so splitting UNKNOWN into UNKNOWN and REVIEW cannot weaken it.
        """
        report = screen(source, spec=spec_key)
        offenders = [f for f in report.findings
                     if f.verdict is Verdict.DENY
                     and f.reason in ANALYSIS_FAILURE_REASONS]
        assert not offenders, [f.to_dict() for f in offenders]

    @pytest.mark.parametrize("source", CORPUS, ids=range(len(CORPUS)))
    def test_verdict_matches_the_findings_it_summarises(self, spec_key, source):
        """report.verdict must be exactly the worst verdict among its findings."""
        report = screen(source, spec=spec_key)
        if report.by_verdict(Verdict.DENY):
            assert report.verdict is Verdict.DENY
        elif any(f.is_unknown for f in report.findings):
            assert report.verdict is Verdict.UNKNOWN
        elif any(f.needs_review for f in report.findings):
            assert report.verdict is Verdict.REVIEW
        else:
            assert report.verdict is Verdict.ALLOW

    @pytest.mark.parametrize("source", CORPUS, ids=range(len(CORPUS)))
    def test_ok_is_exactly_verdict_is_allow(self, spec_key, source):
        report = screen(source, spec=spec_key)
        assert report.ok is (report.verdict is Verdict.ALLOW)

    def test_unresolvable_dynamic_sink_never_yields_ok(self, spec_key):
        for source in CORPUS:
            if Reason.UNRESOLVED_DYNAMIC_SQL in reasons(screen(source, spec=spec_key)):
                assert not screen(source, spec=spec_key).ok, source

    def test_unparseable_sql_never_yields_ok(self, spec_key):
        for source in CORPUS:
            if Reason.UNPARSEABLE_SQL in reasons(screen(source, spec=spec_key)):
                assert not screen(source, spec=spec_key).ok, source

    def test_empty_string_yields_no_findings(self, spec_key):
        report = screen("", spec=spec_key)
        assert report.findings == []
        assert report.verdict is Verdict.ALLOW
        assert report.ok

    def test_whitespace_only_yields_no_findings(self, spec_key):
        assert screen("   \n\n  ", spec=spec_key).findings == []

    def test_python_syntax_error_is_unknown_not_an_exception(self, spec_key):
        report = screen("def f(:\n  pass\n", spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert reasons(report) == {Reason.ANALYSIS_ERROR}
        assert not report.ok

    def test_syntax_error_reports_a_line_number(self, spec_key):
        report = screen("def f(:\n  pass\n", spec=spec_key)
        assert report.findings[0].line == 1

    def test_unterminated_string_is_unknown(self, spec_key):
        report = screen('x = "abc\n', spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert reasons(report) == {Reason.ANALYSIS_ERROR}

    def test_syntax_error_under_read_only_policy_is_still_unknown(self, spec_key):
        report = screen("def f(:\n", read_only_policy(), spec=spec_key)
        assert report.verdict is Verdict.UNKNOWN
        assert reasons(report) == {Reason.ANALYSIS_ERROR}

    def test_unknown_spark_version_is_unknown_not_an_exception(self, spec_key):
        report = screen(sql_call("select 1"), spec="spark-2.4")
        assert report.verdict is Verdict.UNKNOWN
        assert reasons(report) == {Reason.UNSUPPORTED_SPARK_VERSION}
        assert not report.ok

    def test_unknown_spark_version_still_names_the_policy(self, spec_key):
        report = screen(sql_call("select 1"), spec="nope")
        assert report.policy == default_policy().name

    def test_report_metadata_is_populated(self, spec_key):
        report = screen(sql_call("select 1"), spec=spec_key)
        assert report.policy == "default"
        assert report.grammar == spec_key
        assert report.lines == 1

    def test_grammar_spec_object_is_accepted(self, spec_key):
        from sparkscreen import get_spec
        report = screen(sql_call("DROP TABLE t"), spec=get_spec(spec_key))
        assert report.verdict is Verdict.DENY
        assert report.grammar == spec_key


class TestFindingModel:
    def test_is_unknown_tracks_the_verdict(self):
        """`is_unknown` asks the verdict, and must agree with it on every reason.

        These two are independent axes: DELETE/MERGE are parsed fine and are
        UNKNOWN with reason=DESTRUCTIVE_STATEMENT. Conflating them was a
        fail-open bug; this pins the corrected contract.
        """
        from sparkscreen.model import Finding
        for reason in Reason:
            for verdict in Verdict:
                finding = Finding(verdict=verdict, reason=reason, message="")
                assert finding.is_unknown is (verdict is Verdict.UNKNOWN), (
                    reason, verdict
                )

    def test_real_verdicts_are_not_is_unknown(self):
        from sparkscreen.model import Finding
        for reason in (Reason.NO_MATCHING_RULE, Reason.DENY_RULE,
                       Reason.OUTSIDE_ALLOWLIST):
            f = Finding(verdict=Verdict.DENY, reason=reason, message="")
            assert not f.is_unknown, reason

    def test_to_dict_is_json_serialisable(self):
        report = screen("spark.sql('DROP TABLE prod.users')\n")
        assert json.loads(json.dumps(report.to_dict()))["verdict"] == "deny"

    def test_by_verdict_partitions_findings(self, spec_key):
        report = screen(
            "spark.sql('select 1')\nspark.sql('DROP TABLE t')\n"
            "spark.sql('SELCT 1')\n", spec=spec_key)
        total = sum(len(report.by_verdict(v))
                    for v in (Verdict.ALLOW, Verdict.DENY, Verdict.UNKNOWN,
                              Verdict.REVIEW))
        assert total == len(report.findings)


# ---------------------------------------------------------------------------
# Policy-layer unit behaviour (no parser in the loop)
# ---------------------------------------------------------------------------


class TestPolicyUnit:
    def test_deny_rule_labels_are_classified_as_destructive(self):
        """Every destructive label a rule keys on must be in DESTRUCTIVE_LABELS.

        DESTRUCTIVE_LABELS is what drives the writable-namespaces check, so a
        label present in a deny rule but missing here is one the allowlist
        silently never inspects. It already drifted: AlterTableCollation was in
        deny.destructive-ddl but not in DESTRUCTIVE_LABELS.
        """
        destructive_rule_labels = {
            label
            for r in default_policy().rules
            if r.verdict is Verdict.DENY
            for label in r.labels
        }
        assert destructive_rule_labels <= set(DESTRUCTIVE_LABELS), (
            sorted(destructive_rule_labels - set(DESTRUCTIVE_LABELS))
        )

    def test_no_label_drift_between_destructive_set_and_rules(self):
        """Every label a DENY rule acts on must be classified destructive.

        `DESTRUCTIVE_LABELS` (which drives the writable-namespaces check) and
        the rules' `labels` are maintained by hand, so they drift. It already
        had: `AlterTableCollation` was in a deny rule but not in
        DESTRUCTIVE_LABELS, so the namespace allowlist never inspected it.

        Scope note: only DENY rules are checked. The review.* rules
        deliberately target labels that are not destructive
        (`review.row-mutation` -> DeleteFromTable, `allow.query` ->
        StatementDefault), so requiring those in DESTRUCTIVE_LABELS would be
        wrong, not strict.
        """
        deny_labels = {
            label
            for r in default_policy().rules
            if r.verdict is Verdict.DENY
            for label in r.labels
        }
        assert deny_labels <= set(DESTRUCTIVE_LABELS), (
            sorted(deny_labels - set(DESTRUCTIVE_LABELS))
        )

    def test_no_destructive_label_lacks_a_rule(self):
        """The reverse direction: destructive with nothing acting on it."""
        rule_labels = {l for r in default_policy().rules for l in r.labels}
        assert sorted(set(DESTRUCTIVE_LABELS) - rule_labels) == []

    def test_drift_detector_actually_detects_drift(self):
        """The invariants above are only worth anything if they can fail."""
        from sparkscreen.policy import Rule, policy_label_drift
        mutated = default_policy()
        mutated.rules.append(Rule(
            id="bogus", verdict=Verdict.DENY, reason=Reason.DENY_RULE,
            message="x", labels=("NotARealStatement",),
        ))
        drift = policy_label_drift(mutated)
        assert "NotARealStatement" in drift["deny_rules_only"]
        assert policy_label_drift()["destructive_only"] == []

    def test_rule_for_label_picks_the_first_match(self):
        policy = default_policy()
        assert policy.rule_for_label("DropTable").id == "deny.drop"
        assert policy.rule_for_label("NoSuchStatement") is None

    def test_rule_without_labels_matches_everything(self):
        from sparkscreen.policy import Rule
        catch_all = Rule(id="all", verdict=Verdict.DENY, reason=Reason.DENY_RULE,
                         message="no")
        assert catch_all.applies_to("Anything") is True

    def test_unrecognised_label_is_review_not_analysis_failure(self):
        """A label with no rule is a REVIEW, not an UNKNOWN.

        This one moved with the split and is the sharpest illustration of why. We
        parsed the statement, we know its label and its targets; all we lack is an
        opinion. `UNSUPPORTED_STATEMENT` reads like a failure and used to be
        classified as one, so `Report.analysis_failures` counted it as "we could not
        analyse this" -- which is false, and would have made the two dashboard numbers
        disagree with each other.
        """
        policy = default_policy()
        findings = policy.evaluate_statement("TotallyMadeUp", [], [])
        assert len(findings) == 1
        assert findings[0].verdict is Verdict.REVIEW
        assert findings[0].reason is Reason.UNSUPPORTED_STATEMENT
        assert not findings[0].is_analysis_failure
        assert findings[0].needs_review

    def test_literal_prefix_rule_escalates_to_deny(self):
        from sparkscreen.policy import Rule
        policy = Policy(name="lit", rules=[Rule(
            id="no.jar", verdict=Verdict.ALLOW, reason=Reason.NO_MATCHING_RULE,
            message="fine", labels=("ManageResource",),
            literal_prefixes=("/tmp/",),
        )])
        bad = policy.evaluate_statement("ManageResource", [], ["/tmp/evil.jar"])
        assert bad[-1].verdict is Verdict.DENY
        good = policy.evaluate_statement("ManageResource", [], ["/opt/ok.jar"])
        assert good[-1].verdict is Verdict.ALLOW

    def test_policy_with_no_rules_falls_back_to_the_read_only_allowlist(self, spec_key):
        """An empty Policy is not "deny everything": it is "trust READ_ONLY_LABELS".

        With no rules, a recognised read-only label is allowed and anything else
        needs a human. Worth pinning because `Policy()` is the natural way to write
        an allow-nothing policy, and it does not do that.

        The two refusals are different verdicts and that is the point: `DROP TABLE t`
        is parsed and classified and simply has no rule (REVIEW), while
        `Totally not SQL` never becomes a statement at all (UNKNOWN).
        """
        policy = Policy(name="empty")
        assert screen(sql_call("select 1"), policy, spec=spec_key).verdict is \
            Verdict.ALLOW
        assert screen(sql_call("DROP TABLE t"), policy, spec=spec_key).verdict is \
            Verdict.REVIEW
        assert screen(sql_call("Totally not SQL"), policy, spec=spec_key).verdict \
            is Verdict.UNKNOWN

    def test_policy_round_trips_through_json(self, tmp_path):
        from sparkscreen.policy import policy_from_dict
        original = Policy(
            name="corp", rules=default_policy().rules,
            limits=Limits(max_code_chars=123, max_sql_chars=456),
            writable_namespaces=("staging.*",),
        )
        path = tmp_path / "p.json"
        path.write_text(json.dumps(original.to_dict() if hasattr(original, "to_dict")
                                   else {
            "name": original.name,
            "rules": [{"id": r.id, "verdict": r.verdict.value,
                       "reason": r.reason.value, "message": r.message,
                       "severity": r.severity.value, "labels": list(r.labels)}
                      for r in original.rules],
            "limits": {"max_code_chars": 123, "max_sql_chars": 456},
            "writable_namespaces": ["staging.*"],
        }))
        loaded = load_policy(str(path))
        assert loaded.name == "corp"
        assert loaded.limits.max_code_chars == 123
        assert loaded.writable_namespaces == ("staging.*",)
        assert len(loaded.rules) == len(original.rules)
        assert policy_from_dict({}).name == "custom"


class TestFoldingIntegration:
    """The screener's SQL recovery, as observed through screen()."""

    def test_module_level_constant_is_folded(self):
        source = (
            "verb = 'DR'\n"
            "spark.sql(verb + 'OP TABLE prod.users')\n"
        )
        report = screen(source)
        assert report.verdict is Verdict.DENY

    def test_fstring_over_a_constant_is_folded(self):
        source = (
            "table = 'prod.users'\n"
            "spark.sql(f'DROP TABLE {table}')\n"
        )
        assert screen(source).verdict is Verdict.DENY

    def test_reassigned_non_constant_binding_stays_unresolved(self):
        source = (
            "t = 'prod.users'\n"
            "t = input()\n"
            "spark.sql(f'DROP TABLE {t}')\n"
        )
        assert screen(source).verdict is Verdict.UNKNOWN

    def test_folding_result_is_keyed_per_sink_not_per_line(self):
        """Each sink gets its own entry, keyed by a SinkKey carrying its line.

        Regression test for the two-sinks-on-one-line drop. The old contract keyed
        both dicts by line number, so `spark.sql("DROP TABLE prod.users");
        spark.sql("select 1")` collapsed to a single entry and the DROP vanished.
        """
        import ast
        from sparkscreen.analysis.folding import fold_sinks
        tree = ast.parse("spark.sql('select 1')\nspark.sql(join_sep)\n")
        folder = fold_sinks(tree)
        assert {k.line: sql for k, sql in folder.resolved.items()} == {1: "select 1"}
        assert [k.line for k in folder.unresolved] == [2]

    def test_two_sinks_on_one_line_are_both_screened(self):
        """A line is not a sink identity: both sinks must reach the report."""
        source = 'spark.sql("DROP TABLE prod.users"); spark.sql("select 1")'
        report = screen(source)
        assert report.verdict is Verdict.DENY
        dropped = [f for f in report.findings if "DROP TABLE prod.users" in (f.sql or "")]
        assert dropped, f"the DROP was never reported: {report.to_dict()['findings']}"

    def test_unresolved_finding_names_the_offending_expression(self, spec_key):
        source = "spark.sql('select * from ' + tbl)\n"
        report = screen(source, spec=spec_key)
        assert "'select * from ' + tbl" in report.findings[0].message