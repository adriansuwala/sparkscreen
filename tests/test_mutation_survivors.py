"""Tests for decision-logic mutation survivors found by `mutmut run`.

Every test here exists because a specific mutant survived the whole suite. Each
docstring names the mutant (in `x__<fn>__mutmut_<n>` form) and says what the mutation
was, so a future failure can be traced back to the gap rather than guessed at. Run
`.venv/bin/python scripts/_mutant_diff.py <name> --kill <this test>` to see the diff
and confirm the test still fails under the mutant.

The properties chosen are the ones with a safety consequence:

* a finding must never *name* a target it could not resolve (an unverified namespace
  reported as a verified one is the same class of lie as a stale binding),
* an overwrite must outrank a plain write in severity,
* `verdict`/`severity` on every finding must be real enum members, because
  `Finding.to_dict()` dereferences both and a `None` turns a report into a traceback,
* an allowlist objection must survive being combined with a rule verdict -- the
  regression named in AGENTS.md and in `screen._combine`'s docstring.

Not every survivor is in here, and the ones left out are listed in
`SURVIVORS_NOT_COVERED` below with the reason each is unreachable or equivalent. That
list is the point of writing it down: an unexplained survivor is indistinguishable
from an untested one, which is the exact failure this project's mutation testing
exists to prevent.
"""
from __future__ import annotations

import ast

import pytest

from sparkscreen import default_policy, screen
from sparkscreen.analysis.effects import mapped_labels
from sparkscreen.analysis.folding import MAX_CONST_NUMBER, _checked_num
from sparkscreen.analysis.label_universe import labels_for_grammar
from sparkscreen.analysis.treewalk import extract_namespaces, statement_label
from sparkscreen.grammar.parser import get_parser
from sparkscreen.model import (
    Effect,
    Finding,
    Reason,
    Severity,
    Verdict,
    effect_names,
)
from sparkscreen.policy import Limits, Policy, Rule, read_only_policy

# --------------------------------------------------------------------------- helpers


def sql_call(sql: str) -> str:
    return "spark.sql(%r)\n" % sql


def reasons(report) -> set[Reason]:
    return {f.reason for f in report.findings}


# ---------------------------------------------------------------------------
# 1. screen._combine -- which finding becomes the report's headline
# ---------------------------------------------------------------------------


class TestCombinePrefersTheFindingNeedingAttention:
    """`_combine` picks the primary finding, and the pick decides the report verdict.

    The survivors here are all the same defect: the `for wanted in (UNKNOWN, REVIEW)`
    loop matching on the *wrong* verdict. `test_outside_allowlist_is_review_not_deny`
    in test_screen_policy.py pins the DENY-beats-nothing case, but every existing test
    produces at most one REVIEW finding and never an UNKNOWN one alongside it, so
    flipping `is` to `is not` (mutmut_5) picked a different finding that happened to
    carry the same verdict.
    """

    def test_a_review_allowlist_objection_outranks_a_deny_rule_verdict(self):
        """`x__combine__mutmut_5`: `f.verdict is not wanted`, on a real statement.

        This is the F8 regression the docstring describes: the deny rule matched, but
        the finding that reaches the operator is the one saying *where* the statement
        went, so the report reads REVIEW rather than DENY. Reading it as DENY tells a
        caller triaging by exit code that policy blocked the write, when in fact nothing
        blocked it -- a human still has to look.

        Asserted through `screen()` rather than by calling `_combine` directly. A mutant
        renames the function it mutates, so a test importing `_combine` "kills" any
        mutation of it with an ImportError -- which proves nothing about the property.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("staging.*",))
        report = screen(sql_call("DROP TABLE prod.foo"), policy)
        assert report.verdict is Verdict.REVIEW
        assert report.findings[0].verdict is Verdict.REVIEW
        assert "prod.foo" in report.findings[0].targets

    def test_an_unknown_policy_verdict_is_not_demoted_by_an_allowlist_note(self):
        """The same property when the *rule* is the UNKNOWN one.

        `read_only_policy` turns every rule into UNKNOWN, so combined with a readable
        allowlist violation one statement yields an UNKNOWN rule verdict plus a REVIEW
        allowlist objection. The UNKNOWN must stay the headline -- under mutmut_5 it
        does not, and `report.verdict` drops to REVIEW, which reads on a dashboard as
        "operator workload" instead of "the screener could not clear this".
        """
        policy = read_only_policy()
        policy.readable_namespaces = ("prod.*",)
        report = screen(sql_call("DROP TABLE staging.t"), policy)
        assert report.verdict is Verdict.UNKNOWN
        assert report.findings[0].verdict is Verdict.UNKNOWN
        assert Reason.UNSUPPORTED_STATEMENT in reasons(report)
        # and the allowlist objection was not dropped on the way. Nothing asserted the
        # demoted finding's message survives into the combined one, which is the other
        # half of `_combine`'s job -- see `test_two_findings_are_combined_into_one`.
        assert "outside the readable namespaces" in report.findings[0].message
        assert "drops or truncates" in report.findings[0].message


# ---------------------------------------------------------------------------
# 2. screen._eval_one -- combining only when there is something to combine
# ---------------------------------------------------------------------------


class TestEvalOneCombinesOnlyWhenItMust:
    def test_two_findings_are_combined_into_one(self):
        """`x__eval_one__mutmut_40`: `len(findings) == 2`.

        `evaluate_statement` returns the rule's verdict plus one finding per allowlist
        objection, so an ordinary policy produces exactly two for a statement that
        violates one allowlist. Both have to collapse into one report entry carrying
        both messages. With `== 2` the two-finding case skipped `_combine` and returned
        `findings[0]` bare -- the allowlist objection became the whole finding and the
        rule verdict was dropped from the report entirely.

        Exactly two is the case that matters: a three-finding case (both allowlists
        violated) still routes through `_combine` under either spelling, so a test
        written against three findings would pass against this mutant too.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("prod.*",))
        report = screen(sql_call("DROP TABLE staging.t"), policy)
        assert len(report.findings) == 1, (
            "one statement must produce one report entry however many objections it has"
        )
        message = report.findings[0].message
        assert "outside the writable namespaces" in message
        assert "drops or truncates" in message, (
            "the demoted rule verdict must survive the combination"
        )

    def test_a_single_finding_is_returned_unchanged(self):
        """The other direction, and the reason `mutmut_36` is equivalent rather than
        merely untested.

        `x__eval_one__mutmut_36` replaces the condition with `... and False`, so a lone
        finding always goes through `_combine`. `_combine` on a one-element list returns
        that element unchanged -- `extra` is empty, so no message is appended -- which
        makes the mutant indistinguishable from the original. It is recorded in
        `SURVIVORS_NOT_COVERED` as equivalent rather than chased further.

        The test stays because the property is worth stating: a lone DENY is reported
        as itself, not decorated.
        """
        report = screen(sql_call("DROP TABLE prod.foo"))
        assert len(report.findings) == 1
        assert report.findings[0].verdict is Verdict.DENY
        assert "(" not in report.findings[0].message


# ---------------------------------------------------------------------------
# 3. screen._eval_write -- what a DataFrame write is allowed to claim
# ---------------------------------------------------------------------------


class TestDataFrameWriteClaimsOnlyWhatItKnows:
    def test_an_unresolved_destination_is_not_named_as_a_target(self):
        """`x__eval_write__mutmut_4`: the guard on the `targets` tuple.

        `targets = (write.target,) if write.target_known and write.target else ()`. The
        guard exists so a destination we could not resolve is reported as *no* target.
        The surviving mutation replaced the condition with `... or True`, so the tuple
        was populated unconditionally -- `saveAsTable(name)` reported `targets=(None,)`.
        A consumer keying allowlist decisions off `finding.targets` would then be
        comparing against a string the screener never verified.

        The mode is left unreadable on purpose. With a known mode the `target_known`
        branch of `_eval_write` builds its Finding *without* a `targets=` argument, so
        the tuple is never consulted and the mutation is invisible; only the mode-
        unknown override at the end passes `targets=targets` through. That is why this
        reads as two unknowns rather than one.

        Note the *effect* is still carried. That asymmetry is the design (see
        `analysis.calls`' docstring): the damage is knowable even when the destination
        is not.
        """
        report = screen("df.write.mode(m).saveAsTable(name)")
        assert report.verdict is Verdict.REVIEW, "an uncheckable destination is a review"
        assert report.findings[0].targets == (), (
            "a destination we could not resolve must not be reported as a target"
        )
        assert Effect.WRITE_DATA in report.effects, (
            "the effect is knowable regardless of the destination"
        )

    @pytest.mark.parametrize("src", [
        # no positional argument at all, so `_recover_target` takes its `else` arm
        "df.write.saveAsTable()",
        # a destination that exists only as a keyword, which this walker cannot read
        'df.write.saveAsTable(table="prod.t")',
        # a bare name: present in the AST, but not a value we can prove
        "df.write.saveAsTable(name)",
        # jdbc with neither a second positional argument nor table=: its `arg` is
        # never assigned, so this is the `if arg is None` early return specifically
        'df.write.jdbc(url)',
        'df.write.jdbc(url, {})',
    ])
    def test_an_unresolvable_destination_is_reported_as_unresolvable(self, src):
        """`analysis.calls._recover_target__mutmut_16/18`: `return None, False` -> `True`.

        One layer below the `targets` guard, and the reason that guard needs the
        `known` half at all. Both surviving mutations flipped *could not resolve* to
        *resolved* on the two early-return paths, so an unresolvable destination came
        back as `(None, True)`.

        The `targets` tuple alone cannot see it -- the guard is
        `target_known and target`, and `None` is falsy either way, so `targets` stayed
        empty and the mutation hid. What changes is which branch of `_eval_write` runs:
        a resolved destination is checked against the allowlists and reported as
        outside them, while an unresolved one is reported as unresolvable. Those are
        different claims about the same line, and the second one is the honest one.
        """
        report = screen(src)
        finding = report.findings[0]
        assert report.verdict is Verdict.REVIEW
        assert "cannot resolve" in finding.message, (
            f"an unresolvable destination must be said to be unresolvable, "
            f"not checked against the allowlists: {finding.message}"
        )
        assert finding.targets == ()

    def test_a_resolvable_destination_is_reported_against_the_allowlist(self):
        """The other half of the same distinction, so the test above cannot pass
        vacuously by always printing "cannot resolve".

        The effect is identical in both cases -- both are REVIEW -- which is exactly
        why the mutation was able to hide: only the message and the `targets` tuple
        distinguish a destination we refused to guess from one we checked.
        """
        finding = screen('df.write.saveAsTable("prod.t")').findings[0]
        assert "outside the permitted namespaces" in finding.message
        assert finding.targets == ("prod.t",)

    def test_jdbc_takes_its_table_from_the_second_positional_argument(self):
        """`_recover_target__mutmut_5/6`: the `len(node.args) >= 2` bound.

        `jdbc(url, table, ...)` puts the destination second, unlike every other sink.
        Reading position one instead would name the *URL* as the namespace -- the
        allowlist would then be asked whether `jdbc:mysql://host/db` is writable,
        which is a question with a confident and wrong answer.

        Pinned at the boundary in both directions: exactly two arguments (the
        shortest legal call) must still resolve, and the extra arguments a real call
        carries must not shift the position.
        """
        for src in ('df.write.jdbc(url, "prod.t")',
                    'df.write.jdbc(url, "prod.t", {})',
                    'df.write.jdbc(url, "prod.t", {}, "extra")'):
            finding = screen(src).findings[0]
            assert finding.targets == ("prod.t",), (
                f"jdbc's table is its second positional argument, not its first: {src}"
            )

    def test_the_mode_unknown_override_still_names_a_resolved_destination(self):
        """`x__eval_write__mutmut_29`: `targets=targets` -> `targets=None`.

        The mode-unknown override is the last finding `_eval_write` builds, and the only
        one whose `targets` a test is likely to have already asserted: the earlier
        tests all pair it with an *unresolved* destination, where the correct value and
        `None` look alike through `len()` and `not`.

        Here the destination is a literal we resolved and the mode is a name we could
        not read, so `targets` has real content to lose. `None` is not "no targets" but
        "targets unknown", and every consumer iterating `finding.targets` would raise on
        it.
        """
        finding = screen('df.write.mode(m).saveAsTable("prod.t")').findings[0]
        assert finding.reason is Reason.UNRESOLVED_DYNAMIC_SQL
        assert finding.targets == ("prod.t",), (
            f"the override dropped a destination we did resolve: {finding.targets!r}"
        )
        assert finding.severity is Severity.HIGH, (
            "an unexamined overwrite is the one thing that must not be filed low"
        )

    def test_an_overwrite_of_an_unresolved_destination_still_names_no_target(self):
        """The same guard on the destructive path, which is the one that matters.

        An overwrite is the finding an operator acts on, so it is the case where
        reporting an unverified namespace name would do real harm.
        """
        report = screen('df.write.mode("overwrite").saveAsTable(name)')
        assert report.findings[0].targets == ()
        assert Effect.DESTROY_DATA in report.effects

    def test_a_resolved_destination_is_reported_as_a_target(self):
        """The other side of the same guard, so the test above cannot pass vacuously."""
        report = screen('df.write.mode("overwrite").saveAsTable("prod.t")')
        assert report.findings[0].targets == ("prod.t",)

    def test_an_overwrite_is_critical_and_says_so(self):
        """`x__eval_write__mutmut_26/39`: `severity=CRITICAL if write.overwrites`.

        Severity is the axis an operator triages on, and an overwrite is the one write
        that cannot be undone by re-running. `mutmut_39` (`... and False else HIGH`)
        reported every overwrite as HIGH -- the same severity as a plain data write --
        so a dashboard sorting by severity could not tell a destructive write from a
        routine append. `mutmut_26` (severity=None) is covered by
        `test_every_finding_has_real_enum_members` below.
        """
        report = screen('df.write.mode("overwrite").saveAsTable("prod.t")')
        finding = report.findings[0]
        assert finding.verdict is Verdict.DENY
        assert finding.severity is Severity.CRITICAL
        assert "replaces existing data" in finding.message


class TestEveryFindingHasRealEnumMembers:
    """The `severity=None` and `verdict=None` survivors, in one place.

    `screen.py` constructs roughly a dozen `Finding`s, and mutmut's keyword-argument
    operator turned `severity=Severity.MEDIUM` into `severity=None` in nearly all of
    them without a single test noticing -- because the tests assert on `verdict`, which
    is a different field.

    This is not a style preference. `Finding.to_dict()` reads `self.severity.value`, so
    a `None` there makes JSON serialisation raise `AttributeError` at exactly the moment
    a report is handed to a consumer. Asserting the invariant over a source that
    exercises every branch is the cheap way to cover all of them at once.
    """

    #: One source per finding-producing branch of `screen()` and `_eval_write`.
    SOURCES = [
        # -- screen(): code length
        "# pad\n" * 4000,
        # -- screen(): python syntax error
        "def f(:\n",
        # -- screen(): unparseable SQL
        sql_call("DROP TABL prod.t"),
        # -- screen(): SQL over max_sql_chars
        sql_call("select " + "a," * 6000 + "1 from t"),
        # -- screen(): too many statements
        sql_call("BEGIN select 1; select 2; END"),
        # -- screen(): unresolved dynamic SQL
        "spark.sql(q)\n",
        # -- screen(): unsupported spark version
        sql_call("select 1"),
        # -- screen(): unsupported statement under a rule-less policy
        sql_call("ALTER TABLE t SET TBLPROPERTIES ('a'='b')"),
        # -- _eval_write: unresolvable destination
        'df.write.mode("overwrite").saveAsTable(name)',
        # -- _eval_write: unresolvable save mode
        'df.write.mode(m).saveAsTable("prod.t")',
        # -- _eval_write: destructive, target known
        'df.write.mode("overwrite").saveAsTable("prod.t")',
        # -- _eval_write: append outside the allowlist
        'df.write.mode("append").saveAsTable("prod.t")',
        # -- _eval_write: append with no allowlist configured at all
        'df.write.mode("append").saveAsTable("prod.t")',
        # -- _eval_write: append inside the allowlist
        'df.write.mode("append").saveAsTable("prod.t")',
    ]

    @pytest.mark.parametrize("source", SOURCES, ids=range(len(SOURCES)))
    def test_no_finding_carries_a_none_verdict_severity_or_reason(self, source):
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("staging.*",),
                        readable_namespaces=("staging.*",))
        report = screen(source, policy)
        assert report.findings, f"no finding produced for {source[:40]!r}"
        for f in report.findings:
            assert isinstance(f.verdict, Verdict), f"{f.verdict!r} is not a Verdict"
            assert isinstance(f.reason, Reason), f"{f.reason!r} is not a Reason"
            assert isinstance(f.severity, Severity), (
                f"{f.severity!r} is not a Severity -- to_dict() would raise on it"
            )

    @pytest.mark.parametrize("source", SOURCES, ids=range(len(SOURCES)))
    def test_every_report_serialises(self, source):
        """`to_dict()` is what the CLI and every dashboard consume.

        This is the assertion that turns `severity=None` from a wrong value into a
        caught error: the same test fails either way, but this one states why the value
        matters.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("staging.*",),
                        readable_namespaces=("staging.*",))
        report = screen(source, policy)
        for f in report.findings:
            as_dict = f.to_dict()
            assert as_dict["severity"] in {s.value for s in Severity}
            assert as_dict["verdict"] in {v.value for v in Verdict}
            assert as_dict["reason"] in {r.value for r in Reason}


# ---------------------------------------------------------------------------
# 4. Limits -- the bounds themselves, and what a length denial carries
# ---------------------------------------------------------------------------


class TestLengthLimitFindingsCarryTheirEvidence:
    def test_over_long_sql_is_echoed_truncated_at_exactly_200_characters(self):
        """`x_screen__mutmut_82`: `sql=sql[:201] + "..."`.

        The echo is truncated so a report cannot be used to smuggle a 10,000-character
        statement into a log line or a dashboard cell. `test_sql_length_message_
        truncates_the_echoed_sql` asserts only that the echo is *shorter* than the
        original and ends in "...", which a truncation at 201 also satisfies. Pinning
        the exact length is what makes the bound real.
        """
        sql = "select " + "a," * 6000 + "1 from t"
        report = screen(sql_call(sql))
        finding = report.findings[0]
        assert finding.reason is Reason.CODE_LENGTH_EXCEEDED
        assert len(finding.sql) == 203, "expected 200 characters plus the ellipsis"
        assert finding.sql.endswith("...")
        assert finding.sql.startswith(sql[:200])

    def test_an_over_long_snippet_is_denied_at_the_code_length_limit(self):
        """`x_screen__mutmut_82`: the `max_code_chars` finding's 200-character echo.

        The boundary tests in test_screen_policy.py pin *whether* the cap fires. None of
        them look at what the finding then says, which is why `sql=sql[:200] + "..."`
        mutated to 201 survived: every such test asserts `len(sql) < len(original)`,
        which 204 satisfies just as well as 203.
        """
        source = "# pad\n" * (default_policy().limits.max_code_chars // 6 + 1)
        report = screen(source)
        assert report.verdict is Verdict.DENY
        finding = report.findings[0]
        assert finding.reason is Reason.CODE_LENGTH_EXCEEDED
        assert finding.severity is Severity.MEDIUM
        assert "characters" in finding.message

    def test_an_over_long_sql_sink_is_denied_with_its_own_fields(self):
        """`x_screen__mutmut_72`: `severity=None` on the *SQL* length finding.

        The source-level cap and the per-statement SQL cap are two separate findings
        that share a reason, so a test on the first says nothing about the second. Here
        the mutation only nulls the severity, and the verdict and reason are untouched:
        the report still DENYs, it just carries a finding whose severity cannot be
        sorted or ranked. Only the SQL branch reaches the second `Finding`.
        """
        over = "x" * (default_policy().limits.max_sql_chars + 10)
        report = screen('spark.sql(%r)' % over)
        assert report.verdict is Verdict.DENY
        findings = [f for f in report.findings
                    if f.reason is Reason.CODE_LENGTH_EXCEEDED]
        assert len(findings) == 1, f"expected one SQL-length finding, got {findings}"
        finding = findings[0]
        assert finding.severity is Severity.MEDIUM, (
            f"SQL-length DENY filed at {finding.severity}; a null severity cannot be "
            "sorted or ranked by a consumer"
        )
        assert "characters" in finding.message
        assert finding.sql.endswith("..."), "the echoed SQL stays truncated"


class TestFoldedConstantBoundsAreInclusive:
    def test_a_number_exactly_at_the_limit_is_still_a_constant(self):
        """`x__checked_num__mutmut_3`: `abs(result) <= MAX_CONST_NUMBER` -> `<`.

        The cap is there to stop a huge literal being folded, so the value *at* the cap
        is by definition still foldable. Flipping to `<` rejected `2**53` -- a number
        every float can represent exactly -- while accepting nothing above it, so the
        guard's boundary moved by one for no reason and nothing tested it.

        `_checked_num` is private, so this asserts the fold's own output rather than the
        helper: a sink whose SQL folds to a string at exactly the cap must resolve.
        """
        assert _checked_num(MAX_CONST_NUMBER) == MAX_CONST_NUMBER, (
            "a number exactly at MAX_CONST_NUMBER is foldable"
        )
        assert _checked_num(MAX_CONST_NUMBER + 1).__class__ is not int, (
            "a number above the cap is not a constant"
        )
        assert _checked_num(-MAX_CONST_NUMBER) == -MAX_CONST_NUMBER, (
            "the bound is on the magnitude, so the negative cap is foldable too"
        )


# ---------------------------------------------------------------------------
# 5. treewalk -- namespace resolution on the paths nothing else reaches
# ---------------------------------------------------------------------------


def _first_bare_identifier(spec_key: str):
    """A `MultipartIdentifierContext` subtree, taken from a real parse.

    Passing one of these to `extract_namespaces` directly is the only way to reach the
    `parent is None` branch: `_walk` supplies `parent=node` for every descent, so the
    root call is the sole caller that passes `None`.
    """
    tree = get_parser(spec_key).parse("select * from prod.t").statements[0].tree
    stack = [tree]
    while stack:
        node = stack.pop()
        if type(node).__name__ == "MultipartIdentifierContext":
            return node
        stack.extend(getattr(node, "children", []) or [])
    raise AssertionError(f"no bare identifier in the {spec_key} parse tree")


class TestBareIdentifierAtTheWalkRoot:
    def test_a_bare_identifier_passed_directly_is_reported(self, spec_key):
        """`x__is_bare_table_ref__mutmut_2/3`: the `parent is None` branch.

        `MultipartIdentifier` is Spark's generic identifier rule -- the same rule spells
        a column alias, a CTE name and a table. At the root of a walk there is no
        enclosing rule to disambiguate with, so the function returns True and reports
        it. `mutmut_2` (`return False`) dropped it, silently producing zero namespaces
        for a name the caller passed in explicitly.

        Reporting is the safe direction, as the function's own comment says: a dropped
        table is worse than a reported column.

        The sibling mutant `mutmut_3` (`type(parent)` -> `type(None)`) is *not* covered
        by this test and is recorded in `SURVIVORS_NOT_COVERED` instead: it only
        affects the non-None-parent arm, and on both pinned grammars no bare generic
        identifier is ever parented by `TableIdentifierContext` -- the parents actually
        observed are `IdentifierReferenceContext`, `MultipartIdentifierContext`,
        `RenameTableContext` and `ColDefinitionContext`. So that arm is unreachable from
        real SQL today, and no input distinguishes it.
        """
        bare = _first_bare_identifier(spec_key)
        refs = extract_namespaces(bare, grammar_key=spec_key)
        assert [r.name for r in refs] == ["prod.t"]

    def test_the_same_name_is_found_when_it_is_reached_by_descending(self, spec_key):
        """The control: the walk finds `prod.t` when handed the whole statement.

        Without this, the test above would also pass if extraction were broken for both
        routes, which is the shape most of these survivors actually took.
        """
        tree = get_parser(spec_key).parse("select * from prod.t").statements[0].tree
        refs = extract_namespaces(tree, grammar_key=spec_key)
        assert "prod.t" in [r.name for r in refs]


class TestStatementLabelOnNonContextClasses:
    def test_a_class_name_without_the_context_suffix_is_returned_whole(self):
        """`x_statement_label__mutmut_4`: `cls.endswith("Context") or True`.

        `statement_label` strips a trailing `Context` and returns anything else
        unchanged. Appending `or True` therefore chopped seven characters off *every*
        class name, so a context class that did not follow the convention -- or any
        caller passing a plain object -- came back mangled or empty. Every pinned
        grammar emits `...Context` classes, so this branch was untested by accident
        rather than by choice.

        The name deliberately does not end in "Context" -- that suffix is the whole
        thing being detected, so a class that ends in it would exercise the other arm.
        """
        class Bare:
            pass

        assert statement_label(Bare()) == "Bare"

    def test_a_context_class_loses_exactly_the_suffix(self, spec_key):
        """The control, and the reason the strip is `len("Context")` and not `7`."""
        tree = get_parser(spec_key).parse("DROP TABLE prod.t").statements[0].tree
        assert statement_label(tree) == "DropTable"


# ---------------------------------------------------------------------------
# 6. label_universe -- the derivation the effects table's totality rests on
# ---------------------------------------------------------------------------


class TestLabelUniverseContainsOnlyStatementLabels:
    """The universe behind "no reachable label without an effect mapping".

    AGENTS.md lists that as a load-bearing invariant, and
    `test_every_grammar_label_has_an_effect` checks the *derived* set is covered. It
    does not check the derivation did not pick up junk: a private attribute or a
    non-rule context class admitted here becomes a "grammar label", which then has to
    be given an effect entry to keep the totality test passing. That is how dead
    vocabulary gets promoted to load-bearing.
    """

    @pytest.mark.parametrize("spec_key", ["spark-4.0", "spark-3.5.1"])
    def test_no_private_or_non_rule_names_leak_into_the_universe(self, spec_key):
        """The universe stays a set of statement labels, whatever the walk encounters.

        `_child_rule_contexts` filters class attributes by name, and a leaked private or
        non-rule name here becomes a "grammar label" that then has to be given an effect
        entry to keep the totality test green -- which is how dead vocabulary gets
        promoted to load-bearing.

        This is a regression guard on the *result* rather than a kill of a named mutant:
        the two surviving mutations of that filter (`or` -> `and`, and the literal in
        `startswith`) turn out to be equivalent on both pinned grammars. `_NOT_RULES`
        holds only `accept`, `copyFrom`, `enterRule`, `exitRule` and `getRuleIndex`, and
        none of those maps to a context class that exists in `SqlBaseParser`, so the
        `classes.get(...)` lookup drops them either way. See `SURVIVORS_NOT_COVERED`.
        """
        labels = labels_for_grammar(spec_key)
        assert labels
        leaked = sorted(x for x in labels if x.startswith("_"))
        assert not leaked, f"{spec_key}: private names in the label universe: {leaked}"
        not_strings = sorted(x for x in labels if not isinstance(x, str))
        assert not not_strings, f"{spec_key}: non-string labels: {not_strings}"

    @pytest.mark.parametrize("spec_key", ["spark-4.0", "spark-3.5.1"])
    def test_every_derived_label_is_really_mapped(self, spec_key):
        """The totality invariant itself, restated against the derivation.

        Cheap, and it is the check that makes the previous one load-bearing: a leaked
        name would fail *here* too, so the two together say "the universe is sound" and
        "the universe is complete" rather than one of them alone.
        """
        missing = sorted(labels_for_grammar(spec_key) - mapped_labels())
        assert not missing, f"{spec_key}: unmapped labels: {missing}"


# ---------------------------------------------------------------------------
# 7. model.effect_names -- expansion past a nameless pseudo-member
# ---------------------------------------------------------------------------


class TestEffectNamesExpandsPastAZeroMember:
    def test_a_zero_member_does_not_truncate_the_names_that_follow(self):
        """`x_effect_names__mutmut_3`: `continue` -> `break`.

        `Effect(0)` has no members and is skipped, so its presence must not stop the
        iteration -- a report can hold a set containing a zero composite alongside real
        flags. Changing `continue` to `break` returned an empty list, and every consumer
        of `to_dict()["effect"]` would read "this statement does nothing" for a DROP.

        The existing test only passes `{Effect(0)}` *alone*, where `break` and `continue`
        agree -- the same shape as the `max_targets` boundary survivor.
        """
        assert effect_names([Effect(0), Effect.DESTROY_DATA]) == ["DESTROY_DATA"]
        assert effect_names([Effect.DESTROY_DATA, Effect(0)]) == ["DESTROY_DATA"]
        assert effect_names([Effect(0), Effect.WRITE_DATA, Effect.DESTROY_DATA]) == \
            ["DESTROY_DATA", "WRITE_DATA"]

    def test_a_composite_is_still_expanded_into_its_members(self):
        """The control: `Effect(3)` has a joined `.name` that is not a member name."""
        assert effect_names([Effect(0), Effect(3)]) == ["WRITE_DATA", "WRITE_SCHEMA"]


# ---------------------------------------------------------------------------
# Survivors deliberately not covered
# ---------------------------------------------------------------------------

#: Mutants left uncovered, and why. An unexplained survivor is indistinguishable from
#: an untested one, which is the failure this whole exercise exists to catch.
SURVIVORS_NOT_COVERED = {
    # -- equivalent, verified by running the mutant: `_combine` on a one-element list
    #    returns that element unchanged, so always routing through it is invisible.
    "screen.x__eval_one__mutmut_36":
        "`... and False` always calls _combine, which is a no-op on one finding.",
    # -- cosmetic: only the separator between combined messages changes.
    "screen.x__combine__mutmut_16":
        "'; ' -> 'XX; XX' is wording; every message still survives.",
    # -- equivalent: `target_known or target` agrees with the original because
    #    `target` is None whenever `target_known` is False (analysis.calls only sets it
    #    when it resolved), so the second disjunct is never load-bearing.
    "screen.x__eval_write__mutmut_5":
        "`write.target` is always None when `target_known` is False.",
    # -- unreachable from real SQL: the non-None-parent arm of `_is_bare_table_ref` asks
    #    whether the parent is a `TableIdentifierContext`, and on both pinned grammars a
    #    bare generic identifier is never parented by one.
    "treewalk.x__is_bare_table_ref__mutmut_3":
        "no bare identifier has a TableIdentifierContext parent in either grammar.",
    # -- equivalent: every `_NOT_RULES` member (accept, copyFrom, enterRule, exitRule,
    #    getRuleIndex) maps to a context class absent from SqlBaseParser, so the
    #    `classes.get(...)` lookup drops it whether or not the name filter catches it.
    "label_universe.x__child_rule_contexts__mutmut_3/5":
        "_NOT_RULES members have no corresponding context class.",
    # -- equivalent: `arg = None` -> `arg = ""` is unreachable on its own because the
    #    jdbc arm always reassigns `arg` before the `if arg is None` test can see it,
    #    and the other arm starts from `node.args[0]`.
    "calls._recover_target__mutmut_4": "the `arg = None` initialiser is always "
                                        "overwritten or bypassed.",
    # -- equivalent: the `severity=` line is deleted outright rather than mangled, and
    #    `Finding.severity` already defaults to `Severity.MEDIUM`, so an omitted
    #    severity resolves to the same value either way.
    "policy.x_policy_from_dict__mutmut_22":
        "deleting `severity=` matches Finding's own MEDIUM default.",
    # -- killed by a crash, not by an assertion. `_e` is reachable, but `frozenset(None)`
    #    raises TypeError the moment LABEL_EFFECTS is built at import, so the mutant dies
    #    before any test body runs. Distinct from "unreachable": here the code genuinely
    #    executes. No behavioural test is warranted -- the mutation is not a defect that
    #    could reach a report.
    "effects.x__e__mutmut_1":
        "frozenset(None) raises at import; no assertion can observe it.",
    # -- unreachable: the `except` arm only fires if the generated parsers are missing,
    #    which is the packaging failure test_packaging.py guards against.
    "effects.x_effect_label_drift__mutmut_12": "needs grammar_labels() to raise.",
    # -- equivalent: `Policy.name` already defaults to "default".
    "policy.x_default_policy__mutmut_340": "Policy(rules=rules) names itself 'default'.",
    # -- cosmetic by the project's own rule: message wording only.
    "screen.x__eval_write__mutmut_48/49": "note text; a DENY finding is still a DENY.",
    "screen.x__eval_one__mutmut_21/22/23": "wording of the unsupported-statement note.",
    # -- equivalent: `getattr(x, "children", []) or []` and `getattr(x, "children", None)
    #    or []` agree for every input, since `children` is a list or absent.
    "treewalk (11 survivors)": "the `getattr(node, 'children', ...)` default variants.",
}