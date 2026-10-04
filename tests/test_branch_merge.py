"""A binding made in an `if` body must survive the merge when both arms agree.

F16. The last known fail-open in the screener:

    if flag:
        w = df.write
    else:
        w = df.write
    w.save("/tmp/x")

reported ALLOW with zero findings. `w.save` writes to the local filesystem and
`w.jdbc(url, "t")` reaches an external system -- exactly what `READ_LOCAL_FS` and
`WRITE_DATA` exist to catch -- and neither was reported. The cause was not in the
write detector at all: `visit_If` routed both arms through `_block(leak=True)`, which
invalidates every name a block writes on the grounds that the block may not run. That
is right for a loop or a `with`, and wrong for an `if`, where exactly one of two visible
arms runs and they can be intersected. So the binding was discarded at the merge and
`w` looked like a name nobody ever bound.

Two properties are pinned here and they are not the same property:

  * **arms that agree keep their value.** The write is found. This is the fix.
  * **arms that disagree resolve to nothing.** Never an assumed value, and never a
    verdict better than the one an unresolvable name already gets.

The second matters because the tempting shortcut is to report UNKNOWN for every writer
whose receiver we cannot resolve. That is the opposite of correct here: `save` and
`jdbc` are common method names in the wider Python world, so they are only ever treated
as sinks when the receiver provably came from a `.write`, and flagging every
`obj.save(x)` in a codebase would cry wolf on half of it. T5b made that silence
deliberate (`test_dataframe_aliases.py::test_an_unresolved_alias_adds_no_finding`
asserts it), so the tests below assert the silence is *unchanged* rather than that the
gap is closed everywhere. The fix belongs in the merge, not in the reporting.

Verdicts are asserted through the public `screen()` entry point, not through the
finder's internals, because the invariant is about what an operator sees. Where a
unit-level check of the merge itself is genuinely the thing under test, it goes
through `fold_sinks` -- still the public folding API.
"""
from __future__ import annotations

import ast

import pytest

from sparkscreen import Effect, Verdict, screen
from sparkscreen.analysis.folding import fold_sinks
from sparkscreen.grammar.spec import SPECS


def statements_of(source: str) -> set[str]:
    return {f.statement for f in screen(source).findings if f.statement}


#: The shapes that must resolve. Every one of these is a real `save`/`jdbc` reaching
#: the local filesystem or an external system, and every one of them was ALLOW before.
AGREEING_BRANCH_WRITERS = [
    # The reported case, verbatim.
    'if flag:\n    w = df.write\nelse:\n    w = df.write\nw.save("/tmp/x")\n',
    # The sibling sink, pinned alongside it: a fix that merged for `save` but not for
    # `jdbc` would satisfy any single-case assertion.
    'if flag:\n    w = df.write\nelse:\n    w = df.write\nw.jdbc(url, "t")\n',
    # `.write` is the proof whatever the receiver is, so `other.write` and `df.write`
    # agree just as two literals would.
    'if flag:\n    w = df.write\nelse:\n    w = other.write\nw.save("s3://b/x")\n',
    # No `else` at all: the untaken path leaves the module-level binding in place, so
    # the value after the merge is still provable.
    'w = df.write\nif flag:\n    w = df.write\nw.save("s3://b/x")\n',
    # The mode and destination have to be read through the merged binding too, or the
    # merge would only work for a bare `save`.
    'if flag:\n    w = df.write\nelse:\n    w = df.write\n'
    'w.mode("overwrite").saveAsTable("prod.t")\n',
    'if flag:\n    w = df.write\nelse:\n    w = df.write\n'
    'w.saveAsTable("prod.t")\n',
    'if flag:\n    w = df.write\nelse:\n    w = df.write\nw.insertInto("prod.t")\n',
    # An alias of an alias, merged.
    'if flag:\n    w = df.write\nelse:\n    w = df.write\n'
    'w2 = w\nw2.save("s3://b/x")\n',
    # The overwhelmingly common real shape: inside a function body, so the merge has to
    # work with a parameter shadowing the enclosing scope at the same time.
    'def load(df, flag):\n    if flag:\n        w = df.write\n    else:\n'
    '        w = df.write\n    w.save("s3://b/x")\n',
    # Nested branches: an inner merge whose result an outer merge then joins again.
    'if a:\n    if flag:\n        w = df.write\n    else:\n        w = df.write\n'
    'else:\n    w = df.write\nw.save("s3://b/x")\n',
    # An `if` whose arms both agree with what was already bound -- three-way agreement.
    'w = df.write\nif a:\n    w = other.write\nelse:\n    w = third.write\n'
    'w.save("s3://b/x")\n',
]


# ---------------------------------------------------------------------------
# Direction 1: the arms agree, so the write is found. This is the bug.
# ---------------------------------------------------------------------------

class TestAgreeingArmsSurviveTheMerge:

    @pytest.mark.parametrize("source", AGREEING_BRANCH_WRITERS)
    def test_write_is_found(self, source):
        """The whole point: these were ALLOW with zero findings before the merge.

        Asserted as "not ALLOW with no findings" rather than as a specific verdict,
        because the honest verdict here varies with the save mode and the namespace
        allowlists (a mode we cannot read is REVIEW, an overwrite is DENY). What must
        not vary is that the write is never waved through.
        """
        report = screen(source)
        assert report.verdict is not Verdict.ALLOW, (
            f"a merged writer alias was waved through: {source!r}"
        )
        assert report.findings, f"the merged write produced no findings: {source!r}"

    @pytest.mark.parametrize("source", AGREEING_BRANCH_WRITERS)
    def test_the_write_effect_survives_the_merge(self, source):
        """A finding without its effect would be a review note, not a classification.

        `Effect.WRITE_DATA` is the flag the whole DataFrame axis exists to raise, so
        it is asserted on every shape rather than only on the reported one -- a merge
        that resolved the alias but dropped the classification would pass the test
        above.
        """
        assert Effect.WRITE_DATA in screen(source).effects

    def test_save_and_jdbc_both_carry_reaches_external(self):
        """`save` writes the local filesystem; `jdbc` reaches an external system.

        Pinned separately from `WRITE_DATA` because the two are different claims and
        the one added for the `save`/`jdbc` pair is the one that got lost: both reach
        outside the cluster, which is the distinction from `saveAsTable`.
        """
        for source, operation in (
            ('if flag:\n    w = df.write\nelse:\n    w = df.write\n'
             'w.save("/tmp/x")\n', "save"),
            ('if flag:\n    w = df.write\nelse:\n    w = df.write\n'
             'w.jdbc(url, "t")\n', "jdbc"),
        ):
            report = screen(source)
            assert Effect.REACHES_EXTERNAL in report.effects, operation
            assert statements_of(source) == {f"DataFrameWriter.{operation}"}, operation

    def test_an_overwrite_merged_across_a_branch_is_denied(self):
        """The destructive case must not soften just because the binding was merged."""
        report = screen(
            'if flag:\n    w = df.write\nelse:\n    w = df.write\n'
            'w.mode("overwrite").saveAsTable("prod.t")\n'
        )
        assert report.verdict is Verdict.DENY
        assert Effect.DESTROY_DATA in report.effects

    def test_a_merge_does_not_make_a_mutated_writer_mode_known(self):
        """The T5b hazard still holds after the merge.

        A DataFrameWriter is a mutable builder, so an alias cannot report a *known*
        default mode -- a previous line may have put it into overwrite. This is the
        counterpart to `test_a_fresh_write_still_reports_a_known_default_mode`, and it
        is asserted here because a merge that resolved more bindings is exactly the
        change that could have quietly relaxed it into a false ALLOW.
        """
        report = screen(
            'if flag:\n    w = df.write\nelse:\n    w = df.write\n'
            'w.mode("overwrite")\nw.save("s3://b/x")\n'
        )
        assert report.verdict is Verdict.REVIEW
        assert Effect.DESTROY_DATA not in report.effects

    def test_the_merge_is_invisible_to_a_same_named_parameter(self):
        """The mirror of `test_two_scopes_with_the_same_name_are_judged_separately`.

        A merge that recorded "this name is a writer" rather than joining frame tables
        would resolve the parameter's write too, reporting a clean, specific finding
        for a receiver that is not a DataFrameWriter at all.
        """
        source = (
            'if flag:\n    w = df.write\nelse:\n    w = df.write\n'
            "w.save('s3://b/real')\n"
            "def g(w):\n    w.save('s3://b/param')\n"
        )
        found = [f for f in screen(source).findings if f.statement]
        assert len(found) == 1, [f.message for f in found]
        assert "s3://b/real" in (found[0].message + str(found[0].targets))

    def test_a_merge_before_a_rebinding_does_not_resurrect_the_old_value(self):
        """Agreement at the merge, then a real rebinding: the rebinding wins.

        Ordering matters. If the merge wrote into a structure that the later
        rebinding could not see, this would report a writer for a name that holds
        something else -- a stale binding, which the project rule ranks worse than no
        binding at all.
        """
        source = (
            'if flag:\n    w = df.write\nelse:\n    w = df.write\n'
            'w = something\nw.save("s3://b/x")\n'
        )
        assert screen(source).findings == []

    def test_the_merge_reaches_the_findings_of_a_write_only_file(self):
        """`screen()` returns early when there is no SQL sink at all.

        Regression guard for the early return in `screen()`: a file whose only content
        is the merged `save` must not come back with zero findings. This is the
        pre-existing `test_a_file_of_only_aliased_writes_is_not_silently_allowed` for
        the branch shape.
        """
        source = ('if flag:\n    w = df.write\nelse:\n    w = df.write\n'
                  'w.save("/tmp/x")\n')
        assert statements_of(source) == {"DataFrameWriter.save"}


# ---------------------------------------------------------------------------
# Direction 2: the arms disagree, so nothing survives. Never an assumed value.
# ---------------------------------------------------------------------------

class TestDisagreeingArmsResolveToNothing:

    @pytest.mark.parametrize("source,why", [
        ('if flag:\n    w = df.write\nelse:\n    w = "s3://b/x"\nw.save("s3://b/x")\n',
         "one arm holds a writer and the other a string"),
        ('if flag:\n    w = df.write\nelse:\n    w = df.read\nw.save("s3://b/x")\n',
         "one arm holds a writer and the other a reader"),
        ('if flag:\n    w = df.write\nelse:\n    w = something\nw.save("s3://b/x")\n',
         "one arm is unreadable, so there is no single answer"),
        ('if flag:\n    w = df.write\nelse:\n    w = 1\nw.save("s3://b/x")\n',
         "and `1` vs a writer is a disagreement, not a close call"),
        ('if flag:\n    w = df.write\nelse:\n    del w\nw.save("s3://b/x")\n',
         "one arm deletes the name it was given"),
        ('if flag:\n    w = df.write\nelse:\n    w = df.write\n    del w\n'
         'w.save("s3://b/x")\n',
         "an arm binds and then invalidates, so its own value is not trustworthy"),
        ('if flag:\n    w = df.write\nelse:\n    w = df.write\n    w += 1\n'
         'w.save("s3://b/x")\n',
         "an augmented assign in one arm is a rebinding we do not model"),
        ('if flag:\n    w = df.write\nelse:\n    for w in writers:\n        pass\n'
         'w.save("s3://b/x")\n',
         "a loop target in one arm outlives the loop and may never run"),
        ('if flag:\n    w = df.write\nelse:\n    with open("f") as w:\n        pass\n'
         'w.save("s3://b/x")\n',
         "`with` binds a file object in one arm"),
        # Only one arm binds it and the other never had it, so on the untaken path the
        # name is unbound and the call would raise at runtime. Whether that "should"
        # resolve is arguable; what is not arguable is that it must not be assumed.
        ('if flag:\n    w = df.write\nw.save("s3://b/x")\n',
         "one arm binds, the other leaves the name unbound"),
        ('if flag:\n    w = df.write\n',
         "and with no use afterwards there is nothing to report either way"),
    ])
    def test_unresolvable_alias_stays_silent(self, source, why):
        """No finding, and no worse than ALLOW-for-the-wrong-reason.

        Stated as "no finding" because that is the true and pre-existing contract for a
        `save`/`jdbc` whose receiver cannot be proven: these method names are common
        outside PySpark, so an unresolved receiver means the call is not a sink at all.
        The merge must not change that into a new verdict, which would be flagging
        every `obj.save(x)` in a codebase.
        """
        report = screen(source)
        assert not report.findings, (
            f"a name the arms did not agree on became a finding ({why}): {source!r}"
        )

    @pytest.mark.parametrize("source", [
        'if flag:\n    w = df.write\nelse:\n    w = something\nw.save("s3://b/x")\n',
        'if flag:\n    w = df.write\nw.save("s3://b/x")\n',
        'w = df.write\nif flag:\n    del w\nelse:\n    pass\nw.save("s3://b/x")\n',
    ])
    def test_the_silence_is_the_pre_existing_kind(self, source):
        """Not merely "no findings" -- no findings *beyond* an empty file.

        A merge that reported UNKNOWN for every unresolved writer would satisfy the
        test above and drown ordinary agent code in findings. Pinned against a file
        with no writes at all so the two are distinguishable.
        """
        assert len(screen(source).findings) == len(screen("x = 1").findings) == 0

    @pytest.mark.parametrize("source,absent", [
        # The SQL axis is the mirror image and must fail closed in its own way: an
        # unfolded sink is reported as UNKNOWN, which is a finding and not silence.
        # That is the difference between the two axes, and it is pre-existing.
        ('if flag:\n    t = "prod.a"\nelse:\n    t = "prod.b"\n'
         'spark.sql(f"DROP TABLE {t}")\n', "prod.a"),
        ('if flag:\n    t = "prod.a"\nelse:\n    t = "prod.b"\n'
         'spark.sql(f"DROP TABLE {t}")\n', "prod.b"),
    ])
    def test_disagreeing_string_constants_are_unknown_not_a_guess(self, source, absent):
        """Two different table names must not collapse into one specific finding.

        Reported as UNKNOWN, and specifically *not* resolved to either arm's value: a
        clean, specific DROP for whichever arm we happened to keep is a confidently
        wrong finding, which the project ranks worse than no finding at all. `absent`
        names the arm that must not appear anywhere in the report.
        """
        report = screen(source)
        assert report.verdict is Verdict.UNKNOWN
        rendered = " ".join(
            [f.message for f in report.findings]
            + [f.sql or "" for f in report.findings]
            + [str(f.targets) for f in report.findings]
        )
        assert absent not in rendered, (
            f"the merge resolved the disagreement to {absent!r} and reported it "
            f"cleanly instead of UNKNOWN: {source!r}"
        )

    def test_a_disagreeing_branch_never_resolves_to_one_arm(self):
        """The concrete failure the merge must not introduce.

        If the implementation kept the *last* arm's binding instead of intersecting,
        this source would resolve `t` to `prod.b` and report a clean DROP for it. Both
        arms are dropped, and the verdict says so.
        """
        report = screen(
            'if flag:\n    t = "prod.a"\nelse:\n    t = "prod.b"\n'
            'spark.sql(f"DROP TABLE {t}")\n'
        )
        assert report.verdict is Verdict.UNKNOWN
        assert {f.verdict for f in report.findings} == {Verdict.UNKNOWN}

    def test_a_disagreement_in_type_alone_is_a_disagreement(self):
        """`1` and `True` compare equal but render differently, so they disagree.

        A merge that tested `==` without also testing the type would join this pair and
        then fold `f"SELECT {t}"` to whichever arm it kept -- reporting `SELECT 1` for a
        statement that may well send `SELECT True`. Value *and* type is the rule, and it
        is the same rule `IfExp`, `_agreed_parameters` and the `_value` fold apply; this
        pins it for the merge so the three cannot drift apart.
        """
        source = 'if flag:\n    t = 1\nelse:\n    t = True\nspark.sql(f"SELECT {t}")\n'
        folder = fold_sinks(ast.parse(source))
        assert not folder.resolved, (
            f"a type disagreement was joined: {list(folder.resolved.values())}"
        )
        assert len(folder.unresolved) == 1
        assert screen(source).verdict is Verdict.UNKNOWN

    def test_a_disagreement_in_type_alone_stays_unknown_through_screen(self):
        """The same boundary at the level an operator sees."""
        source = 'if flag:\n    t = 1\nelse:\n    t = "1"\nspark.sql(f"SELECT {t}")\n'
        assert screen(source).verdict is Verdict.UNKNOWN

    def test_agreeing_string_constants_still_resolve(self):
        """The counterpart, so the case above cannot be satisfied by giving up.

        Two arms agreeing on the same table name is provable, and refusing it would cost
        a real resolution without buying any safety.
        """
        source = ('if flag:\n    t = "prod.t"\nelse:\n    t = "prod.t"\n'
                  'spark.sql(f"DROP TABLE {t}")\n')
        assert screen(source).verdict is Verdict.DENY

    def test_a_global_in_a_branch_is_refused(self):
        """`global w` inside the function makes the merge unsafe to write.

        A branch-local `global w` assignment is refused by `_bind` for the same reason:
        the write may never happen, and it may happen at an unknown time. The merge
        inherits that refusal rather than reaching around it.
        """
        source = (
            'w = df.write\ndef g(flag):\n    global w\n    if flag:\n'
            '        w = df.write\n    else:\n        w = df.write\n'
            '    w.save("s3://b/x")\n'
        )
        assert screen(source).findings == []


# ---------------------------------------------------------------------------
# The merge itself, at the level it is implemented.
#
# `screen()` is the contract, but the join has one structural property that no
# end-to-end verdict can pin: that it intersects the arms rather than consulting the
# enclosing scope's table as a tiebreaker. That is checked through `fold_sinks`,
# which is the public folding API rather than a private helper.
# ---------------------------------------------------------------------------

class TestTheMergeIntersectsTheArms:

    def test_a_merged_binding_is_readable_after_the_statement(self):
        source = 'if flag:\n    t = "prod.t"\nelse:\n    t = "prod.t"\nspark.sql(t)\n'
        folder = fold_sinks(ast.parse(source))
        assert list(folder.resolved.values()) == ["prod.t"]
        assert not folder.unresolved

    def test_a_disagreement_leaves_the_name_unbound_rather_than_half_bound(self):
        source = 'if flag:\n    t = "prod.a"\nelse:\n    t = "prod.b"\nspark.sql(t)\n'
        folder = fold_sinks(ast.parse(source))
        assert not folder.resolved, "one arm's value was picked"
        assert len(folder.unresolved) == 1, "the sink must still be accounted for"

    def test_a_single_sink_is_never_neither_resolved_nor_unresolved(self):
        """The project's first invariant, applied to the new shape.

        Every recognised sink lands in exactly one of the two dicts. A merge that
        dropped a sink would be a false ALLOW -- the failure mode this whole change
        exists to remove -- so it is checked for the agree and disagree cases alike.
        """
        for source in (
            'if flag:\n    t = "prod.t"\nelse:\n    t = "prod.t"\nspark.sql(t)\n',
            'if flag:\n    t = "prod.a"\nelse:\n    t = "prod.b"\nspark.sql(t)\n',
            'if flag:\n    t = "prod.t"\nspark.sql(t)\n',
        ):
            folder = fold_sinks(ast.parse(source))
            assert len(folder.resolved) + len(folder.unresolved) == 1, source

    def test_one_sink_per_execution_is_still_one_sink_per_finding(self):
        """The ordinal invariant: the merge visits both arms, and must not double-count.

        Only the taken arm runs at runtime, but we analyse both. If the merge recorded a
        sink twice, a single `spark.sql(t)` would produce two findings for one
        statement -- the same shape of error as keying sinks by line number.
        """
        source = 'if flag:\n    t = "prod.t"\nelse:\n    t = "prod.t"\nspark.sql(t)\n'
        folder = fold_sinks(ast.parse(source))
        keys = list(folder.resolved)
        assert len(keys) == 1
        assert keys[0].ordinal == 0

    def test_a_sink_inside_each_arm_is_still_found_separately(self):
        """A merge of bindings must not suppress a sink inside either arm.

        Two arms each with their own `spark.sql` are two statements, and the merge
        runs both bodies, so both must be accounted for.
        """
        source = ('if flag:\n    spark.sql("DROP TABLE prod.a")\nelse:\n'
                  '    spark.sql("DROP TABLE prod.b")\n')
        folder = fold_sinks(ast.parse(source))
        assert sorted(folder.resolved.values()) == [
            "DROP TABLE prod.a", "DROP TABLE prod.b",
        ]


# ---------------------------------------------------------------------------
# Grammar-independence. The branch merge happens before any grammar is consulted, so
# the verdict must not depend on which grammar the caller screens against -- and
# "ought to be" is not a reason to leave that unchecked when the whole point of the
# `spec_key` convention is that a grammar-affecting assumption which looks obviously
# true is exactly the one that is not.
# ---------------------------------------------------------------------------

class TestBranchMergeIsGrammarIndependent:

    @pytest.mark.parametrize("source", AGREEING_BRANCH_WRITERS + [
        'if flag:\n    w = df.write\nelse:\n    w = "s3://b/x"\nw.save("s3://b/x")\n',
        'if flag:\n    t = "prod.a"\nelse:\n    t = "prod.b"\n'
        'spark.sql(f"DROP TABLE {t}")\n',
    ])
    def test_same_verdict_under_both_grammars(self, source, spec_key):
        report = screen(source, spec=spec_key)
        assert report.grammar == spec_key
        siblings = [
            screen(source, spec=other.key).verdict for other in SPECS
            if other.key != spec_key
        ]
        assert all(v is report.verdict for v in siblings), (
            f"the branch merge behaves differently under a grammar on {source!r}"
        )
