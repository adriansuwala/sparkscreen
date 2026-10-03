"""Assertions for decision-core behaviour that the main suite never pins.

Every test here exists because a specific `mutmut` survivor would let a real defect
through the screening pipeline unnoticed, and no existing test noticed. Each docstring
names the mutant (`x__<fn>__mutmut_<n>` form) and states what the mutation changed, so a
future failure traces back to a gap rather than to a guess. Verify one with:

    .venv/bin/python scripts/_mutant_diff.py <mutant> \
        --kill tests/test_decision_boundaries.py::<Class>::<test>

`--kill` runs the node twice: once unmutated as a control, once with the single
mutation applied. Only a control that passes makes the second result meaningful.

Deliberately disjoint from `test_mutation_survivors.py`, which covers an earlier slice
(`_combine`'s verdict preference, `_eval_write`'s `targets` guard, `_is_bare_table_ref`,
`effect_names`, and the import-time `_e`). Nothing is duplicated here.

The properties are grouped by the consequence of getting them wrong, not by module:

* A finding must be **attributable**: it names the rule that fired, the source line, the
  statement, and the namespace it objected to. A finding that has lost one of those is
  still emitted, still counts toward the verdict, and is unactionable for the human the
  tool exists to hand the decision to.
* Severity is the **ranking** axis. `AGENTS.md` records that DENY > UNKNOWN > REVIEW >
  ALLOW, and a screener that files an unexamined overwrite at MEDIUM buries it.
* The read-only policy is a **transformation** over the default policy, and nothing
  tests the transformation itself -- only its use.
* A pattern's **quoting and empty components** are the two ways a namespace allowlist is
  silently widened or narrowed, and both are per-component decisions.
"""
from __future__ import annotations

import pytest

from sparkscreen import Reason, Severity, Verdict, screen
from sparkscreen.analysis.treewalk import NamespaceRef, extract_namespaces
from sparkscreen.grammar.parser import get_parser
from sparkscreen.model import Effect, Finding
from sparkscreen.policy import (
    Limits,
    Policy,
    Rule,
    default_policy,
    policy_from_dict,
    policy_label_drift,
    read_only_policy,
)

# --------------------------------------------------------------------------- helpers


def sql_call(sql: str) -> str:
    return "spark.sql(%r)\n" % sql


def deny_on(label: str, **kw) -> Policy:
    """A one-rule policy that denies exactly `label`, so one finding's fields are visible."""
    kw.setdefault("reason", Reason.DENY_RULE)
    kw.setdefault("severity", Severity.HIGH)
    return Policy(name="p", rules=[
        Rule(id="r.test", verdict=Verdict.DENY, message="denied by test rule",
             labels=(label,), **kw),
    ])


#: The severity every rule in `default_policy()` is documented to carry. `AGENTS.md`
#: treats severity as what sorts a reviewer's queue, so the shipped severities are part
#: of the tool's contract rather than incidental configuration.
DEFAULT_SEVERITIES = {
    "deny.drop": Severity.CRITICAL,
    "deny.overwrite": Severity.CRITICAL,
    "deny.destructive-ddl": Severity.HIGH,
    "deny.load-local-data": Severity.CRITICAL,
    "deny.add-resource": Severity.CRITICAL,
    "review.config": Severity.MEDIUM,
    "review.row-mutation": Severity.HIGH,
    "review.call": Severity.MEDIUM,
    "review.structural-index": Severity.MEDIUM,
    "allow.query": Severity.INFO,
}


# ---------------------------------------------------------------------------
# 1. A finding must say which rule fired
# ---------------------------------------------------------------------------


class TestFindingsNameTheRuleThatFired:
    """`Policy.evaluate_statement` attaches the matched rule's id to every finding.

    Without it a report says "this was denied" and not "by which rule", so a reviewer
    cannot go and change the policy. The three fields are separate keyword arguments in
    the same call, which is exactly why each needs its own assertion.
    """

    def test_a_rule_verdict_names_its_rule(self, spec_key):
        """`policy.xǁPolicyǁevaluate_statement__mutmut_124`: `matched_label=None`.

        Reached through `screen()` rather than by calling `evaluate_statement` directly:
        a mutant renames the function it mutates, so a test that imports it "kills" every
        mutation of it with an ImportError, which proves nothing about the property.

        Both `rule` and `matched_label` are asserted on the same finding. They are two
        separate keyword arguments carrying the same value, so asserting only one leaves
        the other able to go missing without anything noticing.
        """
        policy = Policy(name="p", rules=default_policy().rules)
        report = screen(sql_call("DROP TABLE prod.users"), policy, spec=spec_key)
        drop = next(f for f in report.findings if f.reason is Reason.DENY_RULE)
        assert drop.rule == "deny.drop"
        assert drop.matched_label == "deny.drop"

    #: The two allowlist checks are separate `Finding(...)` calls with their own `rule=`
    #: argument -- exactly the independent checks AGENTS.md records as once having been
    #: joined by an `elif` (F8). Each is armed alone so the survivor under test is
    #: unambiguously the one being hit.
    ALLOWLIST_CASES = {
        "writable": {"writable_namespaces": ("staging.*",)},
        "readable": {"readable_namespaces": ("staging.*",)},
    }

    def _one_allowlist_note(self, policy, spec_key):
        report = screen(sql_call("DROP TABLE prod.users"), policy, spec=spec_key)
        notes = [f for f in report.findings if f.reason is Reason.OUTSIDE_ALLOWLIST]
        assert len(notes) == 1, (
            f"expected exactly one allowlist objection, got "
            f"{[(f.rule, f.message[:50]) for f in report.findings]}"
        )
        return notes[0]

    def test_the_writable_objection_names_its_rule(self, spec_key):
        """`xǁPolicyǁevaluate_statement__mutmut_76/77/78/84-87/90/91`: `rule=None`.

        The allowlist objection is the finding a human acts on -- it is the one that says
        *where* the statement went -- so losing the rule id leaves it with no way back to
        the policy that raised it.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        **self.ALLOWLIST_CASES["writable"])
        note = self._one_allowlist_note(policy, spec_key)
        assert note.rule == "deny.drop"
        assert note.targets == ("prod.users",)
        assert note.line == 1
        assert "writable" in note.message

    def test_the_readable_objection_names_its_rule(self, spec_key):
        """`xǁPolicyǁevaluate_statement__mutmut_99/102/110/113/114`: `rule=None`.

        The readable check is the one that fired first and ate the writable one when the
        two were joined, so it is the one whose `rule=` argument must stay intact.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        **self.ALLOWLIST_CASES["readable"])
        note = self._one_allowlist_note(policy, spec_key)
        assert note.rule == "deny.drop"
        assert note.targets == ("prod.users",)
        assert note.line == 1
        assert "readable" in note.message


# ---------------------------------------------------------------------------
# 2. A finding must say where it is
# ---------------------------------------------------------------------------


class TestFindingsCarryTheirProvenance:
    """`line`, `statement` and `targets` are how a finding is located.

    All three are keyword arguments in one `Finding(...)` call, so dropping any one of
    them compiles cleanly and changes nothing about the verdict -- the report still
    denies, it just cannot be acted on.
    """

    def test_a_sql_finding_carries_line_statement_and_targets(self, spec_key):
        """The verdict finding's own `line`/`statement`/`targets` (L294-303).

        Source is two statements so the asserted line is not trivially 1.
        """
        source = 'x = 1\nspark.sql(%r)\n' % "DROP TABLE prod.users"
        policy = Policy(name="p", rules=default_policy().rules)
        report = screen(source, policy, spec=spec_key)
        drop = next(f for f in report.findings if f.reason is Reason.DENY_RULE)
        assert drop.line == 2
        assert drop.statement == "DropTable"
        assert drop.targets == ("prod.users",)

    def test_a_resource_limit_finding_carries_where_it_was_triggered(self, spec_key):
        """`xǁPolicyǁevaluate_statement__mutmut_10/11/100/101`: line/sql/statement.

        The sibling mutants `9` (max_targets branch) and `22` (max_literals branch) set
        `message=None` on their `Finding(...)`. `Finding.message` is typed `str`, so the
        mutant installs a type-violating value: the finding still constructs and still
        carries the right verdict, reason, line, sql and statement, and the defect only
        surfaces wherever a consumer reads `.message`.

        They are deliberately NOT claimed here. This test asserts on provenance -- line,
        sql, statement -- and asserting the message text would pin wording, which is
        the kind of test that breaks when someone improves a message without changing a
        behaviour. The honest description is: message-provenance is covered by the
        mutation ledger, and these two are recorded there as unclaimed because closing
        them means pinning the wording.

        (Checked rather than assumed in both directions. An earlier note here called them
        "message wording only" on the theory that mutmut deleted the line; the diff shows
        it rewrites it to `None`, and a probe confirms omitting the field entirely would
        raise TypeError since `message` has no default. So neither the deletion reading
        nor the crash reading was right.)

        A statement over `max_targets` is refused *before* any rule is consulted, so this
        finding is all the report says about it. Without a line it cannot be found in the
        file; without the statement label a dashboard cannot group it with anything.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        limits=Limits(max_targets=1))
        source = 'x = 1\nspark.sql(%r)\n' % "SELECT * FROM prod.a JOIN prod.b ON 1=1"
        report = screen(source, policy, spec=spec_key)
        limited = next(f for f in report.findings
                       if f.reason is Reason.RESOURCE_LIMIT)
        assert limited.verdict is Verdict.UNKNOWN
        assert limited.line == 2
        assert limited.sql == "SELECT * FROM prod.a JOIN prod.b ON 1=1"
        # The limit is checked before the grammar's labeled alternative is resolved, so
        # the label is the generic `StatementDefault` rather than `SelectQuery`. Asserted
        # as it actually is: what matters is that it names the statement at all.
        assert limited.statement == "StatementDefault"

    #: Two DataFrame writes that reach *different* `Finding(...)` branches of
    #: `_eval_write`, since the survivors are spread across branches and a test on one
    #: branch says nothing about another.
    DF_ALLOW = 'df.write.mode("append").saveAsTable("prod.audit")\n'
    DF_OUTSIDE = 'df.write.mode("append").saveAsTable("prod.audit")\n'

    def test_an_allowed_dataframe_write_names_the_table_it_wrote(self):
        """`screen.x__eval_write__mutmut_85`: `targets=None` on the ALLOW branch (L281).

        The SQL path and the DataFrame path are documented to produce reports that "read
        the same way"; this is the half of that promise that makes a finding usable. An
        ALLOW finding with no target cannot be audited after the fact.

        The sibling `mutmut_92` deletes the `targets=` argument outright on this same
        branch, so `targets` falls back to its `()` default and the finding loses its
        target while keeping a non-empty verdict. Same observable defect, opposite
        mechanism: `mutmut_85` passes `None`, `mutmut_92` omits the field. Both are
        killed here for the same reason -- the assertion is on the value, not on how
        the mutant damaged it.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("prod.*",), readable_namespaces=("*",))
        report = screen(self.DF_ALLOW, policy)
        assert len(report.findings) == 1
        finding = report.findings[0]
        assert finding.verdict is Verdict.ALLOW
        assert finding.targets == ("prod.audit",)

    def test_a_dataframe_write_outside_the_allowlist_names_the_table(self):
        """`screen.x__eval_write__mutmut_67`: `targets=None` on the REVIEW branch (L271).

        This is the *complaint* finding -- the one saying the write went somewhere it
        should not have -- so a missing target makes it unactionable. Asserted on the
        outside-allowlist branch specifically because that is where the survivor lives.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("staging.*",), readable_namespaces=("*",))
        report = screen(self.DF_OUTSIDE, policy)
        finding = report.findings[0]
        assert finding.verdict is Verdict.REVIEW
        assert finding.reason is Reason.OUTSIDE_ALLOWLIST
        assert finding.targets == ("prod.audit",)

    def test_a_dataframe_finding_carries_the_line_it_is_on(self):
        """`screen.x__eval_write__mutmut_65`: `line=None` on the REVIEW branch (L269).

        A DataFrame finding with no line points at a whole file. Source has a leading
        statement so the expected line is not trivially 1.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("staging.*",), readable_namespaces=("*",))
        report = screen("import x\n" + self.DF_OUTSIDE, policy)
        assert report.findings[0].line == 2

    def test_a_dataframe_finding_labels_its_statement(self):
        """`screen.x__eval_write__mutmut_66`: `statement=None` on the REVIEW branch (L270).

        `statements(report)` throughout the existing suite is built from this field, so a
        None here makes the write invisible to every grouping-by-statement check. The
        The REVIEW branch (L270) and the ALLOW branch (L282) each have their own
        `statement=` argument, so both are asserted.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("staging.*",), readable_namespaces=("*",))
        outside = screen(self.DF_OUTSIDE, policy).findings[0]
        assert outside.statement == "DataFrameWriter.saveAsTable"

        allowed = Policy(name="p", rules=default_policy().rules,
                         writable_namespaces=("prod.*",), readable_namespaces=("*",))
        report = screen('df.write.mode("append").insertInto("prod.audit")\n', allowed)
        assert report.findings[0].statement == "DataFrameWriter.insertInto"

    def test_an_unresolved_destination_finding_names_the_writer(self):
        """`screen.x__eval_write__mutmut_13/14`: provenance on that branch (L213-214).

        `df.write.saveAsTable(dest())` -- a destination we could not resolve. The
        finding says "we found a write, we could not resolve where it goes, and the effect
        is still known", which is useless without the line and the writer method.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("prod.*",), readable_namespaces=("*",))
        report = screen("import x\ndf.write.saveAsTable(dest())\n", policy)
        finding = report.findings[0]
        assert finding.reason is Reason.UNRESOLVED_DYNAMIC_SQL
        assert finding.line == 2
        assert finding.statement == "DataFrameWriter.saveAsTable"


# ---------------------------------------------------------------------------
# 3. Severity is the ranking axis
# ---------------------------------------------------------------------------


class TestSeverityIsRankedAsDocumented:
    """`AGENTS.md`: severity is what decides what a reviewer looks at first."""

    def test_each_default_rule_keeps_its_documented_severity(self):
        """`policy.x_default_policy__mutmut_55/84/166/186/211/252/287/307/329`.

        Ten survivors, one defect: dropping `severity=` from a `Rule(...)` leaves the
        dataclass default, `Severity.MEDIUM`. Every `deny.*` rule then files at MEDIUM,
        so the queue a reviewer sees no longer puts a DROP above a config change.

        Asserted as one whole-map comparison rather than parametrized per rule. A
        per-rule param only guards the one rule it names, so a mutation of any *other*
        rule's severity slips through it -- which is how all ten survived. One comparison
        makes any single missing severity fail.
        """
        rules = {r.id: r.severity for r in default_policy().rules}
        assert rules == DEFAULT_SEVERITIES

    def test_an_overwrite_dataframe_write_is_critical(self):
        """`screen.x__eval_write__mutmut_39`: `and False`, so overwrite never ranks CRITICAL.

        `_eval_write`'s own comment calls the unreadable-mode case "an unexamined
        overwrite"; an overwrite we *did* confirm outranks it, and must outrank a plain
        write too.

        The sibling `mutmut_40` (`or True`, so every deny ranks CRITICAL) is NOT killed
        here and is not claimed: its `else Severity.HIGH` arm is unreachable from real
        DataFrame writes. `denies_regardless_of_namespace` is only true when the write
        carries `DESTROY_DATA`, and every write mode that produces `DESTROY_DATA` also
        sets `overwrites` -- checked across `saveAsTable`, `insertInto`, `save` with and
        without `format`/`partitionBy`/`partitionOverwriteMode`. So the two arms never
        disagree in practice and the mutant is equivalent against this input class.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("prod.*",), readable_namespaces=("*",))
        report = screen('df.write.mode("overwrite").saveAsTable("prod.users")\n', policy)
        finding = report.findings[0]
        assert finding.verdict is Verdict.DENY
        assert finding.severity is Severity.CRITICAL

    def test_an_unreadable_save_mode_outranks_an_unresolved_destination(self):
        """`screen.x__eval_write__mutmut_105`: `severity=Severity.HIGH` dropped -> MEDIUM.

        Both are REVIEW, and both leave the write unexamined, but they are not equally
        bad: an unreadable *mode* means the write may be an overwrite. `_eval_write`
        checks it last precisely because it "can turn a benign append into an unexamined
        overwrite", and then files it below a merely-unresolvable target.
        """
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("prod.*",), readable_namespaces=("*",))
        source = 'df.write.mode(FLAG).saveAsTable("prod.users")\ndf.write.saveAsTable(dest())\n'
        by_line = {f.line: f for f in screen(source, policy).findings}
        assert by_line[1].reason is Reason.UNRESOLVED_DYNAMIC_SQL
        assert by_line[2].reason is Reason.UNRESOLVED_DYNAMIC_SQL
        assert by_line[1].severity is Severity.HIGH, "an unexamined overwrite"
        assert by_line[2].severity is Severity.MEDIUM, "an unresolvable target"

    def test_sql_hidden_in_execute_immediate_is_high(self):
        """`screen.x__check_execute_immediate__mutmut_8/14`: `severity` dropped -> MEDIUM.

        SQL we could not parse inside `EXECUTE IMMEDIATE` is exactly the case the
        `UNKNOWN` verdict exists for: we found a statement and could not look at it.
        Filing it at MEDIUM puts it below ordinary review items.
        """
        source = 'spark.sql(%r)\n' % "EXECUTE IMMEDIATE 'this is not sql at all'"
        report = screen(source)
        unparseable = next(f for f in report.findings
                           if f.reason is Reason.UNPARSEABLE_SQL)
        assert unparseable.verdict is Verdict.UNKNOWN
        assert unparseable.severity is Severity.HIGH

    def test_an_unanalysed_sink_is_never_below_medium(self, spec_key):
        """Every UNKNOWN finding ranks at HIGH or above.

        The umbrella version of the two survivors above: UNKNOWN outranks REVIEW because
        "a report containing something we could not look at is the more urgent of the
        two". Asserting it over all UNKNOWN findings catches the severity being dropped
        from any of them, including ones added later.
        """
        source = (
            'spark.sql(%r)\n' % "EXECUTE IMMEDIATE 'this is not sql at all'"
            + 'df.write.mode(FLAG).saveAsTable("prod.users")\n'
        )
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("prod.*",), readable_namespaces=("*",))
        report = screen(source, policy, spec=spec_key)
        unknown = [f for f in report.findings if f.verdict is Verdict.UNKNOWN]
        assert unknown, "expected the unparseable hidden SQL to be reported as UNKNOWN"
        for finding in unknown:
            assert finding.severity in (Severity.HIGH, Severity.CRITICAL), (
                f"{finding.reason} filed at {finding.severity}; UNKNOWN outranks REVIEW"
            )


# ---------------------------------------------------------------------------
# 4. EXECUTE IMMEDIATE must keep looking after a payload it could not parse
# ---------------------------------------------------------------------------


class TestExecuteImmediateKeepsGoing:
    """`_check_execute_immediate` walks every hidden statement it finds.

    `continue` -> `break` here is not a missed finding, it is a *silenced* one: the
    remaining hidden statements are never parsed and so never reported. A file whose
    first `EXECUTE IMMEDIATE` is malformed and whose second contains a DROP would screen
    clean.
    """

    #: The two pinned grammars do not agree on `EXECUTE IMMEDIATE`: Spark 4.0 parses it
    #: and hides a real statement inside it, Spark 3.5.1 does not have the rule at all
    #: and reports UNKNOWN for the whole string. Both are fail-closed, but only the
    #: first reaches the loop `_check_execute_immediate` walks, so only the first can
    #: distinguish `continue` from `break`. Asserting both keeps the report honest
    #: about which grammar is being exercised rather than silently skipping one.
    HIDDEN_DROP_SQL = "DROP TABLE prod.users"

    def _extracts_hidden_sql(self, spec_key) -> bool:
        """False when the grammar cannot even parse the construct, as 3.5.1 cannot."""
        from sparkscreen.analysis.treewalk import executed_immediate_sql
        from sparkscreen.grammar.parser import SqlSyntaxError

        try:
            tree = get_parser(spec_key).parse(
                "EXECUTE IMMEDIATE %r" % self.HIDDEN_DROP_SQL
            ).statements[0].tree
        except SqlSyntaxError:
            return False
        return list(executed_immediate_sql(tree)) == [self.HIDDEN_DROP_SQL]

    def test_a_malformed_payload_does_not_hide_the_next_one(self, spec_key):
        """`screen.x__check_execute_immediate__mutmut_17`: `continue` -> `break`.

        Under `break` the second `EXECUTE IMMEDIATE` is never parsed and so never
        reported -- a file whose first payload is malformed and whose second contains a
        DROP would screen as if the DROP were not there.
        """
        if not self._extracts_hidden_sql(spec_key):
            pytest.skip(f"{spec_key} does not parse EXECUTE IMMEDIATE at all")

        # Both payloads must sit inside ONE sink, because `_check_execute_immediate` is
        # called once per sink: two separate `spark.sql(...)` calls give the loop one
        # payload each and never reach the `continue`. A `BEGIN...END` script is the
        # construct that carries several statements -- and so several hidden payloads --
        # through a single `spark.sql`.
        bad = "EXECUTE IMMEDIATE 'this is not sql at all'"
        good = "EXECUTE IMMEDIATE %r" % self.HIDDEN_DROP_SQL
        report = screen("spark.sql(%r)\n" % ("BEGIN %s; %s; END" % (bad, good)),
                        spec=spec_key)
        assert any(f.reason is Reason.UNPARSEABLE_SQL for f in report.findings)
        drop = [f for f in report.findings if f.statement == "DropTable"]
        assert len(drop) == 1, (
            "a payload we could not parse must not stop the next one being reported; "
            f"got {[(f.reason, f.statement) for f in report.findings]}"
        )
        assert report.verdict is Verdict.DENY

    def test_an_unparseable_payload_keeps_the_verdict_and_the_line(self, spec_key):
        """`x__check_execute_immediate__mutmut_5/11/15`: verdict/severity/line dropped.

        The hidden SQL is quoted on one line of the source, so the finding has to point
        at that line to be locatable at all. `sql` is asserted only to be non-empty:
        which text it carries depends on whether the failure was the hidden payload or
        the whole statement, and that differs between the two grammars.
        """
        source = 'import x\nspark.sql(%r)\n' % "EXECUTE IMMEDIATE 'not sql at all'"
        report = screen(source, spec=spec_key)
        unparseable = next(f for f in report.findings
                           if f.reason is Reason.UNPARSEABLE_SQL)
        assert unparseable.verdict is Verdict.UNKNOWN
        assert unparseable.line == 2
        assert unparseable.sql, "a finding with no sql text cannot be acted on"


# ---------------------------------------------------------------------------
# 5. read_only_policy is a transformation, and nothing tested the transformation
# ---------------------------------------------------------------------------


class TestReadOnlyPolicyTransformsEveryRule:
    """`read_only_policy` rewrites `default_policy()` in place and returns it.

    The existing suite screens code *with* the read-only policy but never inspects the
    policy itself, so every survivor of the rewrite loop is invisible: one rule left at
    DENY means "this statement is forbidden" where the operator's actual intent is "a
    person should look at this".
    """

    def test_every_rule_but_the_query_is_demoted(self):
        """`policy.x_read_only_policy__mutmut_5`: `continue` -> `break`.

        `allow.query` is the *last* rule in `default_policy()`, so `break` on the first
        non-query rule leaves every rule after `deny.drop` at its default DENY verdict.
        Five of the ten rules stay DENY.
        """
        for rule in read_only_policy().rules:
            if rule.id == "allow.query":
                assert rule.verdict is Verdict.ALLOW, "queries must still be allowed"
            else:
                assert rule.verdict is Verdict.UNKNOWN, (
                    f"{rule.id} left at {rule.verdict}; read-only means 'needs a human', "
                    "not 'forbidden'"
                )
                assert rule.reason is Reason.UNSUPPORTED_STATEMENT

    def test_demotion_only_touches_the_info_rule(self):
        """`x_read_only_policy__mutmut_8/9`: `is not Severity.INFO` / `severity=None`.

        The intent is to stop the query rule's INFO ("nothing to see here") reading as
        more informative than it now is. Inverting the test demotes every *serious* rule
        instead, so `deny.drop` files at LOW in a read-only review.
        """
        severities = {r.id: r.severity for r in read_only_policy().rules}
        assert severities["allow.query"] is Severity.INFO
        for rule_id, severity in DEFAULT_SEVERITIES.items():
            if rule_id == "allow.query":
                continue
            assert severities[rule_id] is severity, (
                f"{rule_id} downgraded to {severities[rule_id]} by read_only_policy"
            )

    def test_the_policy_is_named(self):
        """`policy.x_read_only_policy__mutmut_10/12`: `p.name = None` / `"READ-ONLY"`.

        The name is the only thing in a report distinguishing this policy from the
        default one, so `None` makes two differently-configured reports indistinguishable.
        """
        assert read_only_policy().name == "read-only"


# ---------------------------------------------------------------------------
# 6. Namespace patterns: quoting and empty components
# ---------------------------------------------------------------------------


class TestNamespacePatternMatching:
    """`NamespaceRef.matches` splits a pattern into components and compares them.

    Two per-component decisions in four lines of code, and each has a documented failure
    mode in its own docstring. `prod.*` must mean "tables in prod" and nothing wider; a
    stray empty component from a hand-written policy must not change the arity.
    """

    def test_an_empty_component_does_not_change_the_pattern_arity(self):
        """`treewalk.xǁNamespaceRefǁmatches__mutmut_6`: `p != ""` -> `p != "XXXX"`.

        The filter that drops empty components exists so `"prod..users"` and
        `"prod.users."` still mean `prod.users`. Without it a doubled dot silently
        becomes a three-component pattern, matches nothing, and the namespace the
        operator *did* allowlist reads as out of bounds -- a false REVIEW on every write
        to a table they explicitly permitted.
        """
        ref = NamespaceRef(parts=("prod", "users"))
        assert ref.matches("prod..users")
        assert ref.matches(".prod.users")
        assert ref.matches("prod.users.")

    def test_a_bare_star_still_matches_anything(self):
        """The "no restriction" pattern, which the docstring calls out as rule 1.

        Guards the case that a fix to rule 2 (arity must agree) would otherwise break.
        """
        assert NamespaceRef(parts=("prod", "users")).matches("*")
        assert NamespaceRef(parts=("a", "b", "c", "d")).matches("*")

    def test_a_star_matches_exactly_one_component(self):
        """Rule 2, and the widening bug the docstring names: `prod.*` matched
        `prod.staging.x` before. Asserted on a name deep enough to catch it."""
        assert NamespaceRef(parts=("prod", "users")).matches("prod.*")
        assert not NamespaceRef(parts=("prod", "staging", "x")).matches("prod.*")
        assert not NamespaceRef(parts=("prod",)).matches("prod.*")

    @pytest.mark.parametrize("sql,name,quoted", [
        ("DROP TABLE `prod`.`users`", "prod.users", True),
        ("DROP TABLE prod.users", "prod.users", False),
    ])
    def test_backquoted_components_resolve_and_are_marked_quoted(
        self, spec_key, sql, name, quoted
    ):
        """`treewalk.x__parts__mutmut_13/14/15`: `BackQuotedIdentifierContext` mangled.

        Backquoting is the documented way an identifier that is not what it looks like
        gets into the tree. If the class name stops matching, `` `prod`.`users` `` stops
        resolving at all and the DROP is reported against no namespace -- so a readable
        or writable allowlist has nothing to check, and the write screens clean.
        """
        parser = get_parser(spec_key)
        tree = parser.parse(sql).statements[0].tree
        refs = extract_namespaces(tree, grammar_key=spec_key)
        assert [(r.name, r.quoted) for r in refs] == [(name, quoted)]


# ---------------------------------------------------------------------------
# 7. The effect axis is additive and must not lose a flag
# ---------------------------------------------------------------------------


class TestEffectFlagsAccumulate:
    """`Finding.effect_flags` folds a finding's effect set into one flag.

    The effect axis is orthogonal to the verdict on purpose -- it is what answers "what
    did this do" when the verdict says "allowed". Dropping a flag makes the two axes
    disagree: a write reported as allowed, then described as not writing.
    """

    def test_two_effects_become_the_union_of_both(self):
        """`model.xǁFindingǁeffect_flags__mutmut_4`: `out |= e` -> `out = e`.

        Assigning instead of accumulating keeps only the last flag, so an
        `INSERT OVERWRITE` reads as a plain write.
        """
        finding = Finding(verdict=Verdict.DENY, reason=Reason.DESTRUCTIVE_STATEMENT,
                         message="m")
        finding.effect = frozenset({Effect.WRITE_DATA, Effect.DESTROY_DATA})
        assert finding.effect_flags() == Effect.WRITE_DATA | Effect.DESTROY_DATA

    def test_two_effects_are_not_narrowed_to_their_intersection(self):
        """`model.xǁFindingǁeffect_flags__mutmut_5`: `out |= e` -> `out &= e`."""
        finding = Finding(verdict=Verdict.DENY, reason=Reason.DESTRUCTIVE_STATEMENT,
                         message="m")
        finding.effect = frozenset({Effect.WRITE_DATA, Effect.DESTROY_DATA})
        assert Effect.DESTROY_DATA in finding.effect_flags()

    def test_a_destructive_dataframe_write_reports_both_effects(self, spec_key):
        """The same property through the pipeline rather than through the constructor."""
        policy = Policy(name="p", rules=default_policy().rules,
                        writable_namespaces=("prod.*",), readable_namespaces=("*",))
        report = screen(
            'df.write.mode("overwrite").saveAsTable("prod.users")\n', policy, spec=spec_key
        )
        finding = report.findings[0]
        assert finding.effect_flags() == Effect.WRITE_DATA | Effect.DESTROY_DATA


# ---------------------------------------------------------------------------
# 8. policy_from_dict defaults decide what a hand-written policy means
# ---------------------------------------------------------------------------


class TestPolicyFromDictDefaults:
    """A policy file may omit fields; what it then means is the fail-closed question.

    `policy_from_dict` is the only way a policy arrives from outside the code, and its
    defaults are the difference between "you forgot to say" and "deny". These are
    survivors because no existing test feeds `policy_from_dict` a *partial* rule -- they
    all pass complete dictionaries, where no default is ever read.
    """

    def test_an_omitted_verdict_denies_rather_than_allows(self):
        """`x_policy_from_dict__mutmut_31/33/36/37`: the `"deny"` default changed.

        A rule with no verdict is a rule whose author did not think about the verdict.
        Every alternative default is worse: `"allow"` silently permits what the policy
        file names, and the uppercase/mangled variants raise `ValueError` on load, so a
        screener that cannot parse its own config returns nothing rather than denying.
        """
        rule = policy_from_dict({"rules": [{"id": "r.forgot"}]}).rules[0]
        assert rule.verdict is Verdict.DENY

    def test_an_explicit_severity_and_reason_are_honoured(self):
        """`x_policy_from_dict__mutmut_56/39`: `r.get("k", d)` -> `r.get(None, d)`.

        The neighbouring tests only ever omit the key, and a None-key lookup returns
        the default for an omitted key too -- so mangling the key name to `None` is
        invisible until a policy file says what it *means*.

        This is the fail-open direction: an operator who wrote `"severity": "critical"`
        in a policy file would get MEDIUM instead, and one who wrote a reason would get
        the generic `deny_rule`. The report would still load, still DENY, and quietly
        misfile. The omitted-key tests cannot see it; only an explicit value can.
        """
        rule = policy_from_dict(
            {"rules": [{"id": "r.explicit", "severity": "critical"}]}
        ).rules[0]
        assert rule.severity is Severity.CRITICAL, (
            f"an explicit severity was ignored; read as {rule.severity}"
        )
        explicit_reason = policy_from_dict(
            {"rules": [{"id": "r.explicit", "reason": "destructive_statement"}]}
        ).rules[0]
        assert explicit_reason.reason is Reason.DESTRUCTIVE_STATEMENT, (
            f"an explicit reason was ignored; read as {explicit_reason.reason}"
        )

    def test_an_omitted_severity_is_medium(self):
        """`x_policy_from_dict__mutmut_56/61`: the `"medium"` default mangled.

        `mutmut_22` is *not* covered here and is recorded in
        test_mutation_survivors.SURVIVORS_NOT_COVERED instead: it deletes the
        `severity=` line outright rather than mangling the literal, and that is
        equivalent, because `Finding.severity` already defaults to `Severity.MEDIUM`.
        Read the two defaults together and the explicit line is redundant.

        This test still earns its place for the mutants that change the *literal* --
        `"high"`, `"MEDIUM"`, `None` -- where a malformed policy file would file a DENY
        at the wrong rank or fail to load at all.
        """
        rule = policy_from_dict({"rules": [{"id": "r.forgot"}]}).rules[0]
        assert rule.severity is Severity.MEDIUM

    def test_an_omitted_reason_is_the_generic_deny_reason(self):
        """`x_policy_from_dict__mutmut_39/40/42/44/45/46`: the `"deny_rule"` default."""
        rule = policy_from_dict({"rules": [{"id": "r.forgot"}]}).rules[0]
        assert rule.reason is Reason.DENY_RULE

    def test_omitted_match_criteria_mean_no_criteria_not_all_criteria(self):
        """`x_policy_from_dict__mutmut_15/16/24/25/66/68/72-84`: list defaults -> None.

        `labels`, `target_patterns` and `literal_prefixes` all default to an empty tuple
        -- "this rule constrains nothing further". A `None` default either raises inside
        `tuple()` at load time or makes the rule match nothing at all, which is the
        silent-no-op version of the same defect.
        """
        rule = policy_from_dict({"rules": [{"id": "r.forgot"}]}).rules[0]
        assert rule.labels == ()
        assert rule.target_patterns == ()
        assert rule.literal_prefixes == ()

    def test_an_omitted_label_list_makes_a_rule_a_catch_all(self):
        """What the empty-`labels` default above actually buys, which is not "no effect".

        `Rule.applies_to` reads `not self.labels or label in self.labels`, so a rule with
        no labels matches *every* statement. Asserted in the direction that makes the
        consequence visible: the rule fires on a label it was never written to mention.

        For a DENY rule this is fail-closed and harmless. For an ALLOW rule it would be
        a hole -- an unlabelled "allow" in a policy file waives every statement in the
        codebase -- which is why the default is asserted here at all.
        """
        policy = policy_from_dict({"rules": [{
            "id": "r.unlabelled", "verdict": "deny", "reason": "deny_rule",
            "message": "matches everything",
        }]})
        report = screen(sql_call("DROP TABLE prod.users"), policy)
        assert report.verdict is Verdict.DENY
        assert [f.rule for f in report.findings] == ["r.unlabelled"]


# ---------------------------------------------------------------------------
# 9. policy_label_drift accumulates across rules
# ---------------------------------------------------------------------------


class TestPolicyLabelDriftAccumulates:
    """`policy_label_drift` answers "which labels does no rule cover?".

    It is the function that tells a policy author what their policy has not been taught,
    so a survivor here is a hole in the tool's own coverage report.
    """

    def test_labels_from_every_deny_rule_are_collected(self):
        """`policy.x_policy_label_drift__mutmut_9`: `deny_labels |= ...` -> `= ...`.

        Assigning instead of accumulating keeps only the *last* rule's labels, so a
        policy whose first deny rule covers two labels reports both of them as
        uncovered -- the opposite of what the report is for.
        """
        policy = Policy(name="c", rules=[
            Rule(id="d1", verdict=Verdict.DENY, reason=Reason.DENY_RULE, message="m1",
                 severity=Severity.HIGH, labels=("ShowTables", "DescribeQuery")),
            Rule(id="d2", verdict=Verdict.DENY, reason=Reason.DENY_RULE, message="m2",
                 severity=Severity.HIGH, labels=("CacheTable",)),
        ])
        result = policy_label_drift(policy)
        # CacheTable is destructive so it is filtered out of deny_rules_only; the two
        # labels from the *first* rule are the ones a non-accumulating version loses.
        assert "ShowTables" in result["deny_rules_only"]
        assert "DescribeQuery" in result["deny_rules_only"]

    def test_the_documented_keys_are_present(self):
        """`x_policy_label_drift__mutmut_22/23`: the `"review_rules_only"` key mangled.

        These keys are a published interface -- a caller reads them by name -- so a
        renamed key is a `KeyError` in whatever consumes the report.
        """
        assert set(policy_label_drift()) == {
            "deny_rules_only", "destructive_only", "review_rules_only",
        }