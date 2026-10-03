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

#: (mutant name, test class in tests/test_mutation_survivors.py that must kill it).
CLAIMED = [
    # -- screen._combine: which finding becomes the report headline
    ("x__combine__mutmut_5", "TestCombinePrefersTheFindingNeedingAttention"),
    # -- screen._eval_one: combine only when there is something to combine
    ("x__eval_one__mutmut_40", "TestEvalOneCombinesOnlyWhenItMust"),
    # -- screen._eval_write: targets, and severity
    ("x__eval_write__mutmut_4", "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x__eval_write__mutmut_39", "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    # -- analysis.calls: the layer that decides whether a destination is knowable
    ("x\u01c1_WriteFinder\u01c1_recover_target__mutmut_16",
     "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x\u01c1_WriteFinder\u01c1_recover_target__mutmut_18",
     "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x\u01c1_WriteFinder\u01c1_recover_target__mutmut_5",
     "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x\u01c1_WriteFinder\u01c1_recover_target__mutmut_6",
     "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x__eval_write__mutmut_26", "TestEveryFindingHasRealEnumMembers"),
    ("x__eval_write__mutmut_29", "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x_screen__mutmut_72", "TestLengthLimitFindingsCarryTheirEvidence"),
    ("x__eval_write__mutmut_105", "TestSeverityIsRankedAsDocumented"),
    ("x_default_policy__mutmut_55", "TestSeverityIsRankedAsDocumented"),
    # -- limits
    ("x_screen__mutmut_82", "TestLengthLimitFindingsCarryTheirEvidence"),
    ("x__checked_num__mutmut_3", "TestFoldedConstantBoundsAreInclusive"),
    # -- treewalk
    ("x__is_bare_table_ref__mutmut_2", "TestBareIdentifierAtTheWalkRoot"),
    ("x_statement_label__mutmut_4", "TestStatementLabelOnNonContextClasses"),
    ("x_statement_label__mutmut_5", "TestStatementLabelOnNonContextClasses"),
    ("x_statement_label__mutmut_8", "TestStatementLabelOnNonContextClasses"),
    # -- model.effect_names
    ("x_effect_names__mutmut_3", "TestEffectNamesExpandsPastAZeroMember"),
    # -- documented as NOT covered: these must still survive
    ("x__eval_one__mutmut_36", "TestEvalOneCombinesOnlyWhenItMust"),
    ("x__eval_write__mutmut_5", "TestDataFrameWriteClaimsOnlyWhatItKnows"),
    ("x__is_bare_table_ref__mutmut_3", "TestBareIdentifierAtTheWalkRoot"),
    ("x__child_rule_contexts__mutmut_3",
     "TestLabelUniverseContainsOnlyStatementLabels"),
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
    for mutant, cls in CLAIMED:
        node = f"tests/test_mutation_survivors.py::{cls}"
        proc = subprocess.run([PY, TOOL, mutant, "--kill", node], cwd=ROOT,
                              capture_output=True, text=True, timeout=900)
        out = proc.stdout
        if "CONTROL FAILED" in out:
            failures.append(f"{mutant}: CONTROL FAILED -- the result means nothing")
            print(f"!! {mutant:48s} control failed")
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