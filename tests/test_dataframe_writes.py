"""DataFrame write detection, tested without a JVM.

The live-Spark differential in `tests/differential/test_dataframe_writes.py` proves the
*semantics* -- that overwrite really does replace rows, that default mode really does
refuse. This file covers the AST analysis itself: every case here is decided by reading
Python, and none of them needs Spark to be running.

The split matters. A differential test tells you the classification is right; it does not
tell you the classification is *reached*, because a missed call produces no finding and
no assertion. Most of what follows is about reachability, plus the fail-closed cases that
a live session cannot produce (you cannot hand Spark a variable it will decline to
resolve and still observe the outcome).
"""
from __future__ import annotations

import ast

import pytest

from sparkscreen import Effect, Verdict, read_only_policy, screen
from sparkscreen.analysis.calls import find_dataframe_writes
from sparkscreen.analysis.folding import fold_sinks


def writes(source: str):
    tree = ast.parse(source)
    return find_dataframe_writes(tree, fold_sinks(tree))


def one(source: str):
    found = writes(source)
    assert len(found) == 1, f"expected exactly one write in {source!r}, got {len(found)}"
    return found[0]


def names(effects) -> list[str]:
    return sorted(str(e) for e in effects)


# ---------------------------------------------------------------------------
# Reachability. A missed call produces no finding, so these are the tests that matter.
# ---------------------------------------------------------------------------

class TestEveryWriteShapeIsFound:
    @pytest.mark.parametrize("source", [
        'df.write.saveAsTable("t")',
        'df.write.mode("overwrite").saveAsTable("t")',
        'df.write.save("s3://b/x")',
        'df.write.mode("overwrite").save("s3://b/x")',
        'df.write.insertInto("t")',
        'df.write.jdbc(url, "t")',
        'df.write.jdbc(url, "t", mode="overwrite")',
        'df.write.format("parquet").saveAsTable("t")',
        'df.write.partitionBy("a", "b").mode("overwrite").save("s3://b/x")',
        'df.write.option("compression", "gzip").saveAsTable("t")',
        'self.df.write.mode("overwrite").saveAsTable("t")',
        'spark.table("x").write.mode("overwrite").saveAsTable("t")',
        'get_df().write.mode("overwrite").saveAsTable("t")',
        'df.write.mode("overwrite").mode("append").saveAsTable("t")',
    ])
    def test_found(self, source):
        assert writes(source), f"missed a DataFrame write: {source!r}"

    def test_nested_inside_a_function_is_found(self):
        source = (
            "def load(df):\n"
            '    return df.write.mode("overwrite").saveAsTable("prod.t")\n'
        )
        assert writes(source)

    def test_two_writes_on_one_line_are_both_found(self):
        # The same sink-identity bug that once hid `spark.sql("DROP ...")` behind a
        # second call on the line. Two writes, two results.
        source = ('df.write.mode("overwrite").saveAsTable("a")\n'
                  'df.write.mode("append").saveAsTable("b")\n')
        found = writes(source)
        assert len(found) == 2
        assert {w.target for w in found} == {"a", "b"}


class TestNonWritesAreNotMatched:
    """`save` and `jdbc` are common method names in the wider Python world.

    Matching them bare would flag a large fraction of any codebase, and a screener that
    cries wolf is one people turn off. `saveAsTable`/`insertInto` are PySpark-specific
    enough to stand alone; the other two require a visible `.write`.
    """

    @pytest.mark.parametrize("source", [
        'config.save("x")',
        'obj.save("x")',
        'json.save(handle)',
        'pickle.save(obj, f)',
        'np.save("arr.npy", arr)',
        'joblib.dump(model, "m.pkl")',
        'shutil.rmtree("/data")',
        'os.system("ls")',
        'os.remove("f")',
        'spark.catalog.dropTempView("t")',
        'df.write',                       # not a call at all
        'df.writeText("hello")',
    ])
    def test_not_a_write(self, source):
        assert not writes(source), f"false positive on {source!r}"

    def test_save_needs_a_write_in_the_chain(self):
        assert not writes('writer.save("s3://b/x")')
        assert writes('df.write.save("s3://b/x")')

    def test_save_as_table_is_trusted_without_write(self):
        # The name is specific enough to be a sink on its own, so an aliased writer is
        # still caught: `w = df.write; w.saveAsTable(...)`.
        assert writes('w.saveAsTable("prod.t")')


# ---------------------------------------------------------------------------
# Effect classification, checked through the public entry point.
# ---------------------------------------------------------------------------

class TestEffectClassification:
    @pytest.mark.parametrize("source,expected", [
        ('df.write.mode("overwrite").saveAsTable("t")',
         {"WRITE_DATA", "DESTROY_DATA"}),
        ('df.write.mode("append").saveAsTable("t")', {"WRITE_DATA"}),
        ('df.write.saveAsTable("t")', {"WRITE_DATA"}),
        ('df.write.insertInto("t")', {"WRITE_DATA"}),
        ('df.write.mode("overwrite").save("s3://b/x")',
         {"WRITE_DATA", "DESTROY_DATA", "REACHES_EXTERNAL"}),
        ('df.write.save("s3://b/x")', {"WRITE_DATA", "REACHES_EXTERNAL"}),
        ('df.write.jdbc(url, "t", mode="overwrite")',
         {"WRITE_DATA", "DESTROY_DATA", "REACHES_EXTERNAL"}),
        ('df.write.jdbc(url, "t")', {"WRITE_DATA", "REACHES_EXTERNAL"}),
    ])
    def test_effects(self, source, expected):
        assert names(one(source).effects) == sorted(expected), source

    def test_table_writes_are_not_external_path_writes_are(self):
        # The distinction is real and worth keeping: saveAsTable goes to the warehouse
        # where the namespace allowlists apply; save(path) goes to a filesystem they
        # cannot reason about.
        assert "REACHES_EXTERNAL" not in names(one('df.write.saveAsTable("t")').effects)
        assert "REACHES_EXTERNAL" in names(one('df.write.save("s3://b/x")').effects)

    def test_last_mode_in_the_chain_wins(self):
        # The writer builder returns self from each call, so the last one applied is
        # effective. Reading it backwards would report an overwrite as a safe append.
        assert one('df.write.mode("append").mode("overwrite").saveAsTable("t")').overwrites
        assert not one('df.write.mode("overwrite").mode("append").saveAsTable("t")').overwrites


class TestModeResolution:
    def test_absent_mode_is_a_known_default(self):
        # Spark's default is `errorifexists`, which refuses when the table exists. That
        # is a *known* default, and known-not-to-destroy, so it is not DESTROY_DATA.
        w = one('df.write.saveAsTable("t")')
        assert w.mode_known
        assert not w.overwrites
        assert Effect.DESTROY_DATA not in w.effects

    def test_literal_mode_is_known(self):
        assert one('df.write.mode("overwrite").saveAsTable("t")').mode_known

    def test_folded_mode_is_known(self):
        source = 'm = "overwrite"\ndf.write.mode(m).saveAsTable("t")'
        w = one(source)
        assert w.mode_known and w.overwrites

    def test_variable_mode_is_unknown(self):
        w = one('df.write.mode(m).saveAsTable("t")')
        assert not w.mode_known, "a runtime variable is not a known mode"

    def test_mode_shadowed_by_a_loop_variable_is_unknown(self):
        # The folder's scope tracking is what stops this reading as the earlier
        # binding -- the same fail-closed property the SQL path depends on.
        source = 'm = "append"\nfor m in modes:\n    df.write.mode(m).saveAsTable("t")'
        assert not one(source).mode_known

    def test_jdbc_keyword_mode_is_read(self):
        # jdbc takes mode as a keyword, not via the chain. Missing this reported every
        # jdbc as a default append -- the fail-open direction for mode="overwrite".
        w = one('df.write.jdbc(url, "t", mode="overwrite")')
        assert w.overwrites and w.mode_known
        assert not one('df.write.jdbc(url, "t")').overwrites

    def test_mode_with_no_argument_is_unknown(self):
        assert not one('df.write.mode().saveAsTable("t")').mode_known


class TestTargetResolution:
    def test_literal_target_is_known(self):
        w = one('df.write.saveAsTable("prod.t")')
        assert w.target_known and w.target == "prod.t"

    def test_variable_target_is_unknown_but_effect_is_not(self):
        """The asymmetry that justifies doing this detection at all.

        The effect of an overwrite is knowable regardless of what the table is called.
        Compare `spark.sql(q)` with an unresolvable q, where neither the effect nor the
        target can be stated -- that is the weaker guarantee the SQL path has to make.
        """
        w = one('df.write.mode("overwrite").saveAsTable(name)')
        assert not w.target_known
        assert Effect.DESTROY_DATA in w.effects

    def test_jdbc_table_is_the_second_argument(self):
        assert one('df.write.jdbc(url, "prod.t")').target == "prod.t"

    def test_jdbc_table_keyword_wins(self):
        assert one('df.write.jdbc(url, table="prod.t")').target == "prod.t"

    def test_target_folded_from_a_variable(self):
        source = 't = "prod.t"\ndf.write.mode("overwrite").saveAsTable(t)'
        w = one(source)
        assert w.target == "prod.t" and w.overwrites

    def test_target_shadowed_by_a_parameter_is_unknown(self):
        source = 'def f(t):\n    df.write.mode("overwrite").saveAsTable(t)'
        assert not one(source).target_known


# ---------------------------------------------------------------------------
# Verdicts, through screen().
# ---------------------------------------------------------------------------

class TestVerdicts:
    def test_overwrite_is_denied(self):
        r = screen('df.write.mode("overwrite").saveAsTable("prod.t")')
        assert r.verdict is Verdict.DENY
        assert Effect.DESTROY_DATA in r.effects

    def test_unknown_mode_is_unknown_never_allow(self):
        """The one degradation in the design, and it degrades closed.

        A runtime mode could be an overwrite, so the write cannot be certified safe.
        Note that the *effect* is still WRITE_DATA -- we know it writes -- but the
        verdict is UNKNOWN because the mode is what decides whether it destroys.
        """
        r = screen('df.write.mode(m).saveAsTable("prod.t")')
        assert r.verdict is Verdict.UNKNOWN, "an unreadable mode must not be ALLOW"
        assert Effect.WRITE_DATA in r.effects
        assert Effect.DESTROY_DATA not in r.effects

    def test_unknown_target_with_overwrite_is_unknown_but_carries_destroY(self):
        r = screen('df.write.mode("overwrite").saveAsTable(name)')
        assert r.verdict is Verdict.UNKNOWN, "no namespace could be checked"
        assert Effect.DESTROY_DATA in r.effects, "the effect is still knowable"

    def test_overwrite_is_denied_even_in_an_allowed_namespace(self):
        """DESTROY_DATA is not waivable by namespace, same as for SQL.

        This is the property that stops `df.write.mode("overwrite").saveAsTable(
        "staging.t")` being waved through because staging is writable.
        """
        from sparkscreen.policy import Policy

        policy = Policy(writable_namespaces=("staging.*",), readable_namespaces=("*",))
        r = screen('df.write.mode("overwrite").saveAsTable("staging.t")', policy)
        assert r.verdict is Verdict.DENY

    def test_append_in_an_allowed_namespace_is_allowed(self):
        from sparkscreen.policy import Policy

        policy = Policy(writable_namespaces=("staging.*",), readable_namespaces=("staging.*",))
        r = screen('df.write.mode("append").saveAsTable("staging.t")', policy)
        assert r.verdict is Verdict.ALLOW

    def test_append_outside_the_allowlist_is_reviewed(self):
        from sparkscreen.policy import Policy

        policy = Policy(writable_namespaces=("staging.*",), readable_namespaces=("staging.*",))
        r = screen('df.write.mode("append").saveAsTable("prod.t")', policy)
        assert r.verdict is Verdict.UNKNOWN

    def test_read_only_policy_does_not_allow_a_dataframe_write(self):
        """No allowlist configured is not permission to write.

        Caught by this test as a fail-open: with `writable_namespaces` empty the
        namespace check produced no objections and the append came back ALLOW with
        `WITHIN_ALLOWLIST` -- the tool certifying a write it had no evidence was safe.
        The SQL path already returns UNKNOWN for a rule-less policy
        (`UNSUPPORTED_STATEMENT`), so the DataFrame path now matches: an absent
        allowlist is an absent answer, not a passing one.
        """
        r = screen('df.write.mode("append").saveAsTable("prod.t")', read_only_policy())
        assert r.verdict is Verdict.UNKNOWN, (
            "a policy with no writable_namespaces must not clear a DataFrame write"
        )
        assert r.findings and r.findings[0].verdict is not Verdict.ALLOW


class TestInteractionWithSql:
    def test_a_file_with_only_dataframe_writes_is_not_silently_allowed(self):
        """Regression guard for the early return.

        `screen()` bails out early when there are no SQL sinks. DataFrame detection has
        to run before that bail, or a file containing nothing but `saveAsTable` comes
        back with zero findings -- which is the original blind spot in another shape.
        """
        r = screen('df.write.mode("overwrite").saveAsTable("prod.t")')
        assert r.findings, "a DataFrame-only file produced no findings at all"

    def test_sql_and_dataframe_writes_are_both_reported(self):
        source = ('df.write.mode("overwrite").saveAsTable("prod.t")\n'
                  'spark.sql("select 1")\n')
        r = screen(source)
        labels = {f.statement for f in r.findings}
        assert "DataFrameWriter.saveAsTable" in labels
        assert r.verdict is Verdict.DENY

    def test_dataframe_writes_do_not_disturb_sql_parsing(self):
        r = screen('df.write.saveAsTable("prod.t")\nspark.sql("drop table prod.x")')
        assert r.verdict is Verdict.DENY
        assert any(f.statement == "DropTable" for f in r.findings)
