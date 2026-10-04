"""Re-verify every mutant claim made by tests/test_mutation_survivors.py.

Sequential by necessity: each check rebuilds one shared scratch worktree, so running
this concurrently with itself corrupts that tree and shows up as a spurious
CONTROL FAILED.

Two directions, and the second is the one that matters most. For each mutant the test
file names, run it against that test with a mutation applied *and* unapplied:

  * a mutant documented as killed must make the test fail, and the same test must pass
    against the unmutated tree (the control). Without the control, "the test failed"
    could just mean a broken scratch copy;
  * a mutant documented as NOT covered must still survive. If one of those starts
    dying, the justification in `SURVIVORS_NOT_COVERED` is stale.

Run from the repo root:  .venv/bin/python scripts/verify_survivor_claims.py

Reads the gitignored mutants/ tree in the primary worktree; writes nothing there.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv/bin/python")
TOOL = str(ROOT / "scripts/_mutant_diff.py")

#: (mutant name, test module, test class that must kill it).
#:
#: The module is carried per-entry rather than assumed. Two entries point at
#: `TestSeverityIsRankedAsDocumented`, which lives in test_decision_boundaries.py and
#: NOT in test_mutation_survivors.py -- assuming one file silently produced two
#: permanent CONTROL FAILEDs, which is indistinguishable from a broken scratch tree.
SURVIVORS = "tests/test_mutation_survivors.py"
BOUNDARIES = "tests/test_decision_boundaries.py"

CLAIMED = [
    # -- screen._combine: which finding becomes the report headline
    ("x__combine__mutmut_5", SURVIVORS, "TestCombinePrefersTheFindingNeedingAttention"),
    # -- screen._eval_one: combine only when there is something to combine
    ("x__eval_one__mutmut_40", SURVIVORS, "TestEvalOneCombinesOnlyWhenItMust"),
    # -- screen._eval_write: targets, and severity
    ("x__eval_write__mutmut_4", SURVIVORS, "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x__eval_write__mutmut_39", SURVIVORS, "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    # -- analysis.calls: the layer that decides whether a destination is knowable
    ("xǁ_WriteFinderǁ_recover_target__mutmut_16",
     SURVIVORS, "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("xǁ_WriteFinderǁ_recover_target__mutmut_18",
     SURVIVORS, "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("xǁ_WriteFinderǁ_recover_target__mutmut_5",
     SURVIVORS, "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("xǁ_WriteFinderǁ_recover_target__mutmut_6",
     SURVIVORS, "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x__eval_write__mutmut_26", SURVIVORS, "TestEveryFindingHasRealEnumMembers"),
    ("x__eval_write__mutmut_29", SURVIVORS, "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x_screen__mutmut_72", SURVIVORS, "TestLengthLimitFindingsCarryTheirEvidence"),
    # -- limits
    ("x_screen__mutmut_82", SURVIVORS, "TestLengthLimitFindingsCarryTheirEvidence"),
    ("x__checked_num__mutmut_3", SURVIVORS, "TestFoldedConstantBoundsAreInclusive"),
    # -- treewalk
    ("x__is_bare_table_ref__mutmut_2", SURVIVORS, "TestBareIdentifierAtTheWalkRoot"),
    ("x_statement_label__mutmut_4", SURVIVORS, "TestStatementLabelOnNonContextClasses"),
    ("x_statement_label__mutmut_5", SURVIVORS, "TestStatementLabelOnNonContextClasses"),
    ("x_statement_label__mutmut_8", SURVIVORS, "TestStatementLabelOnNonContextClasses"),
    # -- model.effect_names
    ("x_effect_names__mutmut_3", SURVIVORS, "TestEffectNamesExpandsPastAZeroMember"),
    # -- these two live in test_decision_boundaries.py, NOT in test_mutation_survivors.py
    ("x__eval_write__mutmut_105", BOUNDARIES, "TestSeverityIsRankedAsDocumented"),
    ("x_default_policy__mutmut_55", BOUNDARIES, "TestSeverityIsRankedAsDocumented"),
    # -- documented as NOT covered: these must still survive
    ("x__eval_one__mutmut_36", SURVIVORS, "TestEvalOneCombinesOnlyWhenItMust"),
    ("x__eval_write__mutmut_5", SURVIVORS, "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x__is_bare_table_ref__mutmut_3", SURVIVORS, "TestBareIdentifierAtTheWalkRoot"),
    ("x__child_rule_contexts__mutmut_3",
     SURVIVORS, "TestLabelUniverseContainsOnlyStatementLabels"),
]

#: Mutants the test file documents as NOT covered -- unreachable from real input or
#: equivalent to the original. They must still survive: if one starts dying, the
#: justification recorded beside it is stale.
EXPECT_SURVIVE = {
    "x__eval_one__mutmut_36",
    "x__eval_write__mutmut_5",
    "x__is_bare_table_ref__mutmut_3",
    "x__child_rule_contexts__mutmut_3",
}


def main() -> int:
    failures = []
    for mutant, module, cls in CLAIMED:
        node = f"{module}::{cls}"
        try:
            proc = subprocess.run([PY, TOOL, mutant, "--kill", node], cwd=ROOT,
                                  capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            # An outer timeout must not abort the whole ledger: every entry after this
            # one would go unchecked and the run would still look like it ran.
            failures.append(f"{mutant}: harness timed out after 1800s -- "
                            f"the result is unknown, not a pass")
            print(f"!! {mutant:48s} timed out")
            continue
        out, err = proc.stdout or "", proc.stderr or ""

        # Order matters. _mutant_diff.py returns 2 for CONTROL FAILED and for an
        # uncollectable node, so the rc check would swallow the much more specific
        # CONTROL FAILED diagnostic. Test the specific conditions first.
        #
        # "no mutant named" is a SystemExit from _mutant_diff.find(), and it lands on
        # stderr. pytest's "ERROR: not found:" for a bad node id does NOT -- _Run takes
        # `(proc.stdout or proc.stderr)` and pytest -q still writes a newline to stdout,
        # so the stderr text is dropped before it ever reaches us. That is why the
        # missing verdict marker below is the real backstop for a bad node id.
        if "no mutant named" in err:
            failures.append(f"{mutant}: no such mutant -- the result below is meaningless")
            print(f"!! {mutant:48s} no such mutant")
            continue
        if "CONTROL FAILED" in out:
            failures.append(f"{mutant}: CONTROL FAILED -- the result means nothing")
            print(f"!! {mutant:48s} control failed")
            continue
        # Every verdict _mutant_diff can reach carries one of these markers. Their
        # absence means it exited early (bad node id, SystemExit, crash), which is
        # NOT the same as "survived". Without this, an EXPECT_SURVIVE entry with a
        # typo'd name reports PASS forever.
        if "KILLED" not in out and "SURVIVED" not in out:
            failures.append(f"{mutant}: harness produced no verdict (rc={proc.returncode})"
                            f" -- treat as unverified, not a survive")
            print(f"!! {mutant:48s} no verdict (rc={proc.returncode})")
            continue
        if proc.returncode not in (0, 1):
            failures.append(f"{mutant}: harness exited {proc.returncode}, "
                            f"expected 0 (killed) or 1 (survived)")
            print(f"!! {mutant:48s} harness error rc={proc.returncode}")
            continue
        killed = "KILLED" in out
        want = mutant not in EXPECT_SURVIVE
        if killed != want:
            failures.append(
                f"{mutant}: expected {'kill' if want else 'survive'}, got "
                f"{'kill' if killed else 'survive'}")
        print(f"{'OK ' if killed == want else '!! '}{mutant:48s} "
              f"{'KILLED' if killed else 'survived':9s} "
              f"(expected {'kill' if want else 'survive'})")

    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  ", f)
        return 1
    print(f"all {len(CLAIMED)} mutant checks behaved as documented")
    return 0


if __name__ == "__main__":
    sys.exit(main())