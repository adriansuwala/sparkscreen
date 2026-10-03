"""DataFrame writes reached through an aliased writer.

`w = df.write` followed by `w.saveAsTable("prod.t")` is the same write spelled
differently, and it is the shape PySpark's own examples use. Detecting it means
resolving a name to an *object*, which the write detector previously never had to do,
so the risk here is not "does the code parse" but "does the screener now believe a
binding it should not".

Every test below is therefore paired: the positive case that must be found, and the
ambiguous case beside it that must still produce nothing. A test suite for this
feature that only asserted the positives would pass just as happily against an
implementation that resolved every `Name` it saw -- and that implementation would
report a function parameter's write as a provable DataFrame write.

The live-Spark differential in `tests/differential/test_dataframe_writes.py` proves
the *semantics*; it cannot cover any of this, because none of these cases can be put
to a live session: the whole question is what the screener does with a name it cannot
prove.
"""
from __future__ import annotations

import ast

import pytest

from sparkscreen import Effect, Verdict, screen
from sparkscreen.analysis.calls import find_dataframe_writes
from sparkscreen.analysis.folding import fold_sinks
from sparkscreen.grammar.spec import SPECS


def writes(source: str):
    tree = ast.parse(source)
    return find_dataframe_writes(tree, fold_sinks(tree))


def one(source: str):
    found = writes(source)
    assert len(found) == 1, f"expected exactly one write in {source!r}, got {len(found)}"
    return found[0]


def names(effects) -> list[str]:
    return sorted(str(e) for e in effects)


#: The five shapes T5b names. Each is a distinct sink method behind one alias, so they
#: are pinned together: a fix that resolved `save` but not `jdbc` would pass any
#: single-case assertion.
ALIAS_TARGETS = [
    'w = df.write\nw.saveAsTable("prod.t")',
    'w = df.write\nw.mode("overwrite").saveAsTable("prod.t")',
    'w = df.write\nw.save("s3://b/x")',
    'w = df.write\nw.insertInto("prod.t", ["a", "b"])',
    'w = df.write\nw.jdbc(url, "tbl", mode="overwrite")',
]


# ---------------------------------------------------------------------------
# The gap. Before aliases were tracked, `save` and `jdbc` needed a visible `.write`
# in the receiver chain and these were found by nothing at all.
# ---------------------------------------------------------------------------

class TestAliasedWritesAreFound:
    @pytest.mark.parametrize("source", ALIAS_TARGETS)
    def test_found(self, source):
        assert writes(source), f"missed an aliased DataFrame write: {source!r}"

    @pytest.mark.parametrize("source", [
        # `save` and `jdbc` are the two that need the receiver to resolve. The other
        # three were already found, because their method names are PySpark-specific
        # enough to stand alone -- but they are pinned here so that resolving a name
        # cannot change their behaviour while fixing the first two.
        'w = df.write\nw.save("s3://b/x")',
        'w = df.write\nw.jdbc(url, "tbl")',
        'w = df.write\nw.format("parquet").save("s3://b/x")',
        'w = df.write\nw.partitionBy("a", "b").save("s3://b/x")',
        'w = df.write\nw.mode("overwrite").save("s3://b/x")',
        'w = df.write\nw.jdbc(url, "tbl", mode="append")',
    ])
    def test_chain_methods_still_reach_the_sink(self, source):
        """Configuration calls on an alias must not hide the write behind them."""
        assert writes(source), f"an aliased chain lost its sink: {source!r}"

    def test_alias_in_a_function_body_is_found(self):
        # The overwhelmingly common real shape: everything inside `def load(...)`.
        source = (
            "def load(df):\n"
            "    w = df.write\n"
            '    return w.mode("overwrite").saveAsTable("prod.t")\n'
        )
        assert writes(source)

    def test_alias_of_an_alias_is_found(self):
        # `w2 = w` is still provably the same writer object, and PySpark code does this
        # when it builds a writer in a helper and hands it on.
        assert writes('w = df.write\nw2 = w\nw2.save("s3://b/x")')

    def test_self_attribute_write_is_bound(self):
        assert writes('w = self.df.write\nw.save("s3://b/x")')

    def test_write_from_a_call_is_bound(self):
        assert writes('w = get_df().write\nw.save("s3://b/x")')


# ---------------------------------------------------------------------------
# Effects and targets are unchanged by the alias. The alias changes how we *reach*
# the call; it must not change what the call is judged to be.
# ---------------------------------------------------------------------------

class TestAliasedWritesClassifyIdentically:
    @pytest.mark.parametrize("aliased,direct,expected", [
        ('w = df.write\nw.saveAsTable("prod.t")',
         'df.write.saveAsTable("prod.t")', {"WRITE_DATA"}),
        ('w = df.write\nw.insertInto("prod.t")',
         'df.write.insertInto("prod.t")', {"WRITE_DATA"}),
        ('w = df.write\nw.save("s3://b/x")',
         'df.write.save("s3://b/x")',
         {"WRITE_DATA", "REACHES_EXTERNAL"}),
        ('w = df.write\nw.jdbc(url, "tbl", mode="overwrite")',
         'df.write.jdbc(url, "tbl", mode="overwrite")',
         {"WRITE_DATA", "DESTROY_DATA", "REACHES_EXTERNAL"}),
        ('w = df.write\nw.mode("overwrite").saveAsTable("prod.t")',
         'df.write.mode("overwrite").saveAsTable("prod.t")',
         {"WRITE_DATA", "DESTROY_DATA"}),
    ])
    def test_same_effects_as_the_direct_form(self, aliased, direct, expected):
        assert names(one(aliased).effects) == sorted(expected)
        # The pin is the *comparison*: an alias must not launder a destructive write
        # into a different classification than the same code written out in full.
        assert names(one(aliased).effects) == names(one(direct).effects)

    def test_target_is_still_resolved_through_an_alias(self):
        # `w.saveAsTable(t)` with a folded `t`: the namespace allowlists still need a
        # name, and the alias is transparent to them.
        source = 't = "prod.t"\nw = df.write\nw.saveAsTable(t)'
        w = one(source)
        assert w.target_known and w.target == "prod.t"

    def test_jdbc_keyword_mode_is_read_through_an_alias(self):
        # The mode is a keyword on jdbc, not a chain call, so nothing about the alias
        # should change it. Missing this reported every jdbc as a benign append.
        w = one('w = df.write\nw.jdbc(url, "tbl", mode="overwrite")')
        assert w.overwrites and w.mode_known
        assert Effect.DESTROY_DATA in w.effects

    def test_jdbc_table_is_still_the_second_argument(self):
        assert one('w = df.write\nw.jdbc(url, "prod.t")').target == "prod.t"


# ---------------------------------------------------------------------------
# The fail-closed boundary. This is the half that matters: an alias resolved on a
# guess is a confidently wrong finding, which is worse than no finding.
# ---------------------------------------------------------------------------

class TestAmbiguousAliasesAreNotResolved:
    @pytest.mark.parametrize("source", [
        # A name bound to something that is not a writer.
        'w = something\nw.save("p")',
        'w = df.read\nw.save("p")',
        'w = "s3://b/x"\nw.save("p")',
        'w = 1\nw.save("p")',
        'w = "s3://b/x"\nw.jdbc(url, "tbl", mode="overwrite")',
        'w = "s3://b/x"\nw.mode("overwrite").save("s3://b/x")',
        # A dict lookup: the key is a runtime value, so the receiver is unknown.
        'writers["a"].save("p")',
        'writers[k].mode("overwrite").save("p")',
        # A method on the writer is not the writer.
        'w = df.write\nf = w.format\nf("parquet")',
        'w = df.write\nf = w.mode\nf("overwrite")',
    ])
    def test_not_a_write(self, source):
        assert not writes(source), f"resolved an alias we cannot prove: {source!r}"

    @pytest.mark.parametrize("source,why", [
        ('w = df.write\ndef g(w):\n    w.save("p")',
         "a parameter shadows the module binding for the whole body"),
        ('w = df.write\ndef g(w=df.write):\n    w.save("p")',
         "a default does not make the parameter provable"),
        ('for w in writers:\n    w.save("p")',
         "the loop variable is whatever the iterable holds"),
        ('writers = [df.write]\nfor w in writers:\n    w.save("p")',
         "and stays unknown after the loop, because the loop may not run"),
        ('with open("f") as w:\n    w.save("p")',
         "`with` binds a file object"),
        ('try:\n    pass\nexcept ValueError as w:\n    w.save("p")',
         "an exception handler binds an exception"),
        ('w = df.write\nx = [w.save("p") for w in writers]',
         "a comprehension variable is its own scope"),
        ('def f():\n    w = df.write\nw.save("q")',
         "a function body does not leak its bindings to module level"),
    ])
    def test_shadowed_alias_is_not_a_write(self, source, why):
        assert not writes(source), (
            f"a stale binding was trusted ({why}): {source!r}"
        )

    def test_an_unresolved_alias_adds_no_finding(self):
        """Unprovable must degrade to exactly what it produced before, and no further.

        Stated as "no *new* finding" rather than "never ALLOW", because the two are
        different claims and only the first is true. `save` and `jdbc` are common
        method names in the wider Python world, so they are only ever reported when the
        receiver provably came from a `.write`; with an unresolved alias there is no
        such proof and the file is silent. That silence is pre-existing and deliberate
        (`test_save_needs_a_write_in_the_chain` pins the same property for a bare name)
        -- flagging every `obj.save(x)` in a codebase would cry wolf on half of it.

        What must not happen is a *new* finding appearing here that did not exist
        before alias tracking, because that would mean an unprovable receiver had been
        treated as a proven writer.
        """
        source = 'w = df.write\ndef g(w):\n    w.save("s3://b/x")'
        assert writes(source) == []
        assert len(screen(source).findings) == len(screen('x = 1').findings) == 0

    def test_an_unresolved_alias_is_never_allowed_for_a_pyspark_specific_sink(self):
        """Where a sink *is* reported without a visible `.write`, it must still review.

        `saveAsTable` and `insertInto` are trusted on their name alone, so they are
        reported even when the receiver resolves to nothing. That makes them the case
        where an unresolved alias could plausibly have become a false ALLOW, and it is
        the reason this is a separate test from the one above.
        """
        for source in ('w = something\nw.saveAsTable("prod.t")',
                       'def f(w):\n    w.insertInto("prod.t")'):
            r = screen(source)
            assert r.verdict is Verdict.REVIEW, source
            assert r.findings and r.findings[0].verdict is not Verdict.ALLOW

    def test_two_scopes_with_the_same_name_are_judged_separately(self):
        """One provable, one not, in the same file.

        This is the test that a name-keyed implementation would fail. Both bodies use
        the identifier `w`; the module-level one is a real writer and the parameter is
        not, and an implementation that recorded "is `w` a writer?" once would resolve
        both or neither.
        """
        source = (
            "w = df.write\n"
            "w.save('s3://b/real')\n"
            "def g(w):\n"
            "    w.save('s3://b/param')\n"
        )
        found = writes(source)
        assert [w.target for w in found] == ["s3://b/real"], (
            "the parameter's write was reported as provable"
        )


class TestStaleBindingsInvalidate:
    """The project's rule: a stale binding is worse than no binding.

    Each case is `w = df.write` followed by something that rebinds `w`. The second
    binding wins, so the alias is unresolvable and the write is not reported -- which
    is the fail-closed direction, since the second value is not one we can vouch for.
    """

    @pytest.mark.parametrize("source,why", [
        ('w = df.write\nw = other\nw.save("p")', "plain reassignment"),
        ('w = df.write\nw = df.read\nw.save("p")', "reassigned to another object"),
        ('w = df.write\nw += 1\nw.save("p")', "augmented assignment"),
        ('w = df.write\ndel w\nw.save("p")', "deleted"),
        ('w = df.write\nimport w\nw.save("p")', "shadowed by an import"),
        ('w = df.write\nfor w in xs:\n    pass\nw.save("p")',
         "a loop variable outlives its loop"),
        ('w = df.write\nw = other', "and there is nothing to report at all"),
    ])
    def test_second_binding_wins(self, source, why):
        assert not writes(source), f"a stale writer binding survived ({why})"

    def test_a_stale_binding_is_not_read_as_a_writer(self):
        """The specific failure this class exists to prevent.

        Distinct from `w = df.write` alone, which is silent because the name is simply
        never bound. Here the name *is* bound, twice: once to a writer and once to
        something else. An implementation that recorded "this name was ever a writer"
        would keep resolving it and report a clean, specific finding for a write whose
        receiver is not a DataFrameWriter -- the "confidently wrong" outcome the
        project's rule exists to rule out.
        """
        source = 'w = df.write\nw = "not a writer"\nw.save("s3://b/x")'
        assert writes(source) == []
        assert screen(source).findings == []

    def test_rebinding_to_another_writer_is_still_reported(self):
        """The mirror of the case above, and the reason it is worth having both.

        `w = other.write` binds a writer -- the receiver's identity is not something
        the screener knows, but the `.write` attribute is the same proof `df.write`
        offers. So the second binding wins *and* resolves, and the write is reported.
        A test suite that only asserted silence after a rebinding would be satisfied by
        an implementation that had simply stopped resolving aliases entirely.
        """
        source = 'w = df.write\nw = other.write\nw.save("s3://b/x")'
        assert [found.target for found in writes(source)] == ["s3://b/x"]

    def test_a_writer_bound_after_the_rebinding_is_found_again(self):
        """Invalidation is not permanent: a later real binding still counts."""
        source = 'w = df.write\nw = other\nw = df.write\nw.save("p")'
        assert writes(source)

    def test_shadowing_after_a_provable_write_does_not_erase_the_first(self):
        """The earlier, provable write was real and stays reported.

        The mirror image of the case above. If invalidation were implemented by
        dropping every mention of the name, this would lose a genuine write.
        """
        source = 'w = df.write\nw.save("s3://b/first")\nw = other\nw.save("p")'
        found = writes(source)
        assert [w.target for w in found] == ["s3://b/first"]


# ---------------------------------------------------------------------------
# The mode. Resolving an alias creates a hazard the direct form does not have, and
# getting it wrong here is a false ALLOW on a destructive write.
# ---------------------------------------------------------------------------

class TestModeThroughAnAlias:
    def test_mode_on_the_chain_is_still_read(self):
        w = one('w = df.write\nw.mode("overwrite").saveAsTable("prod.t")')
        assert w.mode_known and w.overwrites
        assert Effect.DESTROY_DATA in w.effects

    def test_folded_mode_through_an_alias(self):
        source = 'm = "overwrite"\nw = df.write\nw.mode(m).saveAsTable("prod.t")'
        w = one(source)
        assert w.mode_known and w.overwrites

    def test_unreadable_mode_through_an_alias_is_reviewed_never_allowed(self):
        r = screen('w = df.write\nw.mode(m).saveAsTable("prod.t")')
        assert r.verdict is Verdict.REVIEW
        assert Effect.WRITE_DATA in r.effects

    @pytest.mark.parametrize("source", [
        # The writer builder mutates in place and every configuration call returns
        # `self`, so `w` still names it -- and it may now be in overwrite mode. The
        # chain walk cannot see a statement that already ran.
        'w = df.write\nw.mode("overwrite")\nw.save("s3://b/x")',
        'w = df.write\nw.mode(flag)\nw.save("s3://b/x")',
        # Same, via a binding rather than a bare statement.
        'w = df.write.mode("overwrite")\nw.save("s3://b/x")',
        'w = df.write\nw2 = w.mode("overwrite")\nw.save("s3://b/x")',
    ])
    def test_a_mutated_writer_never_reports_a_known_safe_mode(self, source):
        """The hazard that alias resolution introduces, and the reason it is closed.

        `df.write.save(...)` can report a *known* default mode: the writer was
        constructed on that line and nothing has had a chance to reconfigure it. An
        aliased writer cannot, because a previous line may have. Reporting the default
        here would be a false ALLOW on a write that may destroy data -- the one
        failure this tool exists to prevent -- so the mode stays unknown and the
        verdict degrades to REVIEW.
        """
        w = one(source)
        assert not w.mode_known, "an aliased writer's mode was called known"
        assert not w.overwrites
        assert Effect.DESTROY_DATA not in w.effects
        assert screen(source).verdict is Verdict.REVIEW

    def test_a_fresh_write_still_reports_a_known_default_mode(self):
        """The counterpart, so the case above cannot be satisfied by giving up.

        Something has to distinguish the fresh writer from the mutated one, or the
        fix is just "always say unknown" -- which would be a fail-closed move that
        costs every ordinary append a REVIEW.
        """
        w = one('df.write.save("s3://b/x")')
        assert w.mode_known and not w.overwrites
        assert one('df.write.saveAsTable("prod.t")').mode_known

    def test_the_alias_itself_does_not_make_the_mode_unknown(self):
        """`w = df.write` alone changes nothing about the mode.

        Only a *resolved* alias bottoms the chain out at a name. An unresolved one
        keeps the pre-existing behaviour exactly, so this feature cannot have shifted
        the verdict on code it does not recognise.
        """
        w = one('df.write.saveAsTable("prod.t")')
        assert w.mode_known


# ---------------------------------------------------------------------------
# Verdict-level behaviour, through the public entry point.
# ---------------------------------------------------------------------------

class TestAliasedWritesThroughScreen:
    def test_an_aliased_overwrite_is_denied(self):
        r = screen('w = df.write\nw.mode("overwrite").saveAsTable("prod.t")')
        assert r.verdict is Verdict.DENY
        assert Effect.DESTROY_DATA in r.effects

    def test_an_aliased_overwrite_is_denied_even_in_a_writable_namespace(self):
        """DESTROY_DATA is not waivable by namespace, same as for SQL and for the
        direct form. The alias must not be a way around it."""
        from sparkscreen.policy import Policy

        policy = Policy(writable_namespaces=("staging.*",), readable_namespaces=("*",))
        r = screen('w = df.write\nw.mode("overwrite").saveAsTable("staging.t")', policy)
        assert r.verdict is Verdict.DENY

    def test_an_aliased_append_in_an_allowed_namespace_is_allowed(self):
        """Proves the alias does not just REVIEW everything.

        The counterpart to every "must never be ALLOW" test above: an alias that could
        only ever produce REVIEW would satisfy all of them while being useless.
        """
        from sparkscreen.policy import Policy

        policy = Policy(
            writable_namespaces=("staging.*",), readable_namespaces=("staging.*",)
        )
        r = screen('w = df.write\nw.mode("append").saveAsTable("staging.t")', policy)
        assert r.verdict is Verdict.ALLOW

    def test_an_aliased_append_outside_the_allowlist_is_reviewed(self):
        from sparkscreen.policy import Policy

        policy = Policy(
            writable_namespaces=("staging.*",), readable_namespaces=("staging.*",)
        )
        r = screen('w = df.write\nw.mode("append").saveAsTable("prod.t")', policy)
        assert r.verdict is Verdict.REVIEW

    def test_an_aliased_write_with_no_allowlist_is_not_allowed(self):
        """No `writable_namespaces` is not permission to write -- the same fail-open
        the direct form is guarded against."""
        r = screen('w = df.write\nw.mode("append").saveAsTable("prod.t")')
        assert r.verdict is Verdict.REVIEW

    def test_a_file_of_only_aliased_writes_is_not_silently_allowed(self):
        """`screen()` returns early when there are no SQL sinks.

        Regression guard for the early return in a new shape: a file containing nothing
        but `w = df.write; w.save(...)` must not come back with zero findings.
        """
        r = screen('w = df.write\nw.save("s3://b/x")')
        assert r.findings, "an aliased-write-only file produced no findings at all"
        assert {f.statement for f in r.findings} == {"DataFrameWriter.save"}

    def test_aliased_and_direct_writes_are_both_reported(self):
        """Two sinks, two findings -- the per-sink ordinal invariant."""
        source = ('w = df.write\n'
                  'w.mode("overwrite").saveAsTable("prod.aliased")\n'
                  'df.write.mode("overwrite").saveAsTable("prod.direct")\n')
        found = writes(source)
        assert len(found) == 2
        assert {w.target for w in found} == {"prod.aliased", "prod.direct"}
        assert screen(source).verdict is Verdict.DENY

    def test_an_aliased_write_does_not_disturb_sql_parsing(self):
        source = 'w = df.write\nw.saveAsTable("prod.t")\nspark.sql("drop table prod.x")'
        r = screen(source)
        assert r.verdict is Verdict.DENY
        assert any(f.statement == "DropTable" for f in r.findings)


# ---------------------------------------------------------------------------
# Both pinned grammars. The DataFrame path never reaches the SQL parser, so these
# assertions ought to be grammar-independent -- and "ought to be" is not a reason to
# leave it unchecked, since the whole point of the `spec_key` convention is that a
# grammar-affecting assumption that looks obviously true is exactly the one that is
# not.
# ---------------------------------------------------------------------------

class TestAliasDetectionIsGrammarIndependent:
    @pytest.mark.parametrize("source", ALIAS_TARGETS + [
        # ...and the boundary, which must be unaffected either way.
        'w = df.write\nw = other\nw.save("p")',
        'w = df.write\ndef g(w):\n    w.save("p")',
    ])
    def test_same_verdict_under_both_grammars(self, source, spec_key):
        report = screen(source, spec=spec_key)
        assert report.grammar == spec_key
        # Compared against the sibling grammars, so this fails if one grammar disagrees
        # rather than merely if the default one is self-consistent.
        siblings = [
            screen(source, spec=other.key).verdict
            for other in SPECS if other.key != spec_key
        ]
        assert all(v is report.verdict for v in siblings), (
            f"alias detection differs by grammar on {source!r}"
        )


# ---------------------------------------------------------------------------
# Alias tracking must stay out of the SQL constant folder.
# ---------------------------------------------------------------------------

class TestWritersDoNotLeakIntoSqlFolding:
    def test_a_writer_name_does_not_fold_as_sql(self):
        """The two trackers are separate and must stay separate.

        The write finder reuses the constant folder's scope machinery, so the obvious
        failure mode is a `DataFrameWriter` sentinel leaking into SQL text: `spark.sql(w)`
        would fold to a fabricated string and be reported as *resolved*, which is a
        false assurance rather than an UNKNOWN. SQL text it cannot fold must stay
        unresolved.
        """
        from sparkscreen.analysis.folding import fold_sinks

        folder = fold_sinks(ast.parse('w = df.write\nspark.sql(w)'))
        assert not folder.resolved, "a writer leaked into the SQL constant table"

    def test_sql_sinks_are_collected_once_not_twice(self):
        """`find_dataframe_writes` walks the tree too; it must not double-count.

        Cheap to get wrong and cheap to pin: the write finder deliberately does not
        call the folder's `visit_Call`, so a SQL sink is folded by exactly one visitor.
        """
        source = 'spark.sql("drop table prod.x")'
        folder = fold_sinks(ast.parse(source))
        assert len(folder.resolved) + len(folder.unresolved) == 1
        assert writes(source) == []

    def test_string_constants_are_unaffected_by_writer_tracking(self):
        """The ordinary folding path still works alongside alias tracking."""
        from sparkscreen.analysis.folding import fold_sinks

        source = 'w = df.write\nt = "prod.t"\nspark.sql(f"drop table {t}")'
        folder = fold_sinks(ast.parse(source))
        assert list(folder.resolved.values()) == ["drop table prod.t"]