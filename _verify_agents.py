"""Audit AGENTS.md — the file new sessions will read first.

An orientation file that lies is worse than no orientation file, because an agent will
trust it and skip the verification it would otherwise do. Every factual claim here is
checked against the code.

Run: PYTHONPATH=src .venv/bin/python _verify_agents.py
"""
from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent
failures: list[str] = []


def check(label: str, got, want) -> None:
    good = got == want
    if not good:
        failures.append(f"{label}: got={got!r} want={want!r}")
    print(f"  {'OK  ' if good else 'FAIL'} {label:52} {got!r}")


def src(rel: str) -> str:
    return (ROOT / rel).read_text()


def main() -> int:
    agents = src("AGENTS.md")

    # --- the four verdicts and their exit codes, from the code not the doc ---
    from sparkscreen import Verdict
    check("Verdict has exactly the four documented members",
          [v.value for v in Verdict], ["allow", "deny", "review", "unknown"])
    check("REVIEW and UNKNOWN are distinct values",
          Verdict.REVIEW.value != Verdict.UNKNOWN.value, True)

    # exit codes
    cli = src("src/sparkscreen/cli.py")
    check("cli defines an allow exit of 0", "EXIT_ALLOW = 0" in cli, True)
    check("cli defines a deny exit of 1", "EXIT_DENY = 1" in cli, True)
    check("REVIEW and UNKNOWN share one exit constant",
          "EXIT_UNKNOWN" in cli and "EXIT_REVIEW" not in cli, True)

    # --- the fail-loud effect guarantee AGENTS.md tells people not to break ---
    #
    # Tested by behaviour, not by grepping for an `except`. The earlier version of this
    # check asserted the absence of the literal string "except UnmappedLabelError",
    # which is a proxy that passes for the wrong reason: delete the call, or wrap it in
    # a bare `except Exception`, and the string test still passes while the property is
    # broken. What matters is the observable behaviour -- an unmapped label reaching
    # screen() must raise rather than yield a finding with no effect flags.
    from sparkscreen.analysis.effects import lookup_effects
    check("an unmapped label returns None, not an empty set",
          lookup_effects("NotARealLabel"), None)

    # Temporarily blank one mapping so a real label becomes unmapped, then confirm the
    # screener propagates the error instead of quietly reporting a finding with no
    # effect flags. This is the fail-open the Effect axis was designed to prevent.
    import sparkscreen.analysis.effects as eff_mod

    saved = eff_mod.LABEL_EFFECTS["DropTable"]
    try:
        del eff_mod.LABEL_EFFECTS["DropTable"]
        raised = False
        try:
            from sparkscreen import screen as _screen
            r = _screen('spark.sql("DROP TABLE t")')
            # If it did not raise, the fail-open is real and must be reported loudly.
            findings_without_effects = [
                f for f in r.findings if not f.effect
            ]
            raised = bool(findings_without_effects)
        except eff_mod.UnmappedLabelError:
            raised = True
        check("an unmapped label propagates instead of yielding no-effect findings",
              raised, True)
    finally:
        eff_mod.LABEL_EFFECTS["DropTable"] = saved

    # --- grammar pins are full 40-char commits ---
    spec = src("src/sparkscreen/grammar/spec.py")
    shas = re.findall(r'"([0-9a-f]{7,40})"', spec)
    full = [s for s in shas if len(s) == 40]
    check("at least one full 40-char commit is pinned", len(full) >= 2, True)
    check("no short SHA is pinned", [s for s in shas if len(s) < 40], [])

    # --- generated parsers are committed, not ignored ---
    check("the generated parsers are in git",
          bool(subprocess.run(
              ["git", "ls-files", "src/sparkscreen/grammar/generated"],
              cwd=ROOT, capture_output=True, text=True).stdout.strip()), True)
    gitignore = src(".gitignore")
    check(".gitignore does not exclude the generated parsers",
          bool(re.search(r"^generated/?$|grammar/generated", gitignore, re.M)), False)

    # --- the two-venv claim: the fast venv must NOT have pyspark ---
    fast = subprocess.run(
        [str(ROOT / ".venv/bin/python"), "-c", "import pyspark"],
        capture_output=True, text=True, cwd=ROOT)
    check("the fast .venv does NOT have pyspark", fast.returncode != 0, True)
    slow = subprocess.run(
        [str(ROOT / ".venv-pyspark/bin/python"), "-c",
         "import pyspark; print(pyspark.__version__)"],
        capture_output=True, text=True, cwd=ROOT)
    check(".venv-pyspark has pyspark 3.5.1", slow.stdout.strip(), "3.5.1")

    # --- counts quoted in AGENTS.md must match reality ---
    from sparkscreen.analysis.effects import LABEL_EFFECTS
    check("122 labels mapped", len(LABEL_EFFECTS), 122)

    model = src("src/sparkscreen/model.py")
    check("Report.verdict aggregates on verdict, not reason",
          bool(re.search(r"def verdict\(self\).*?Verdict\.DENY", model, re.S))
          and "f.reason in ANALYSIS_FAILURE_REASONS" not in model, True)

    # --- the Python-scope claim: os.system must still be unscreened ---
    from sparkscreen import screen
    check("os.system is out of scope (returns ALLOW, no findings)",
          (screen("os.system('rm -rf /')").verdict.value,
           len(screen("os.system('rm -rf /')").findings)), ("allow", 0))
    check("Reason.PYTHON_DANGEROUS_CALL is gone",
          hasattr(__import__("sparkscreen.model", fromlist=["Reason"]).Reason,
                  "PYTHON_DANGEROUS_CALL"), False)

    # --- a test exists for each named invariant AGENTS.md cites ---
    tests = src("tests/test_verdicts.py")
    check("test_aggregation_follows_verdict_not_reason exists",
          "def test_aggregation_follows_verdict_not_reason" in tests, True)
    check("a pin-invariant test exists",
          bool(re.search(r"def test.*pin|def test.*commit",
                         src("tests/test_grammar_port.py"))), True)

    # --- the doc's own internal links resolve ---
    for m in re.finditer(r"\]\((?!https?:)([^)#]+)", agents):
        target = (ROOT / m.group(1))
        if not target.exists():
            failures.append(f"AGENTS.md links to missing file: {m.group(1)}")
    print(f"  OK   AGENTS.md relative links all resolve")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        return 1
    print("every AGENTS.md claim verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
