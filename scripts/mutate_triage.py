"""Split mutmut's survivors by operator class: which ones are real test gaps?

`mutmut run` reports a survival rate with no indication of *what kind* of mutation
survived. A mutant that turns a human-readable message into `None` and one that flips a
comparison in `policy.py` are both "survived", but only the second is a finding about the
tests.

Joins two things that neither gives alone:

  - `mutmut results`  : which mutants did NOT die (survived / no tests / timeout)
  - the generated tree : what each mutant actually changed, classified by operator

Usage, from the repo root, after a `mutmut run`:

    .venv/bin/python scripts/mutate_triage.py
    .venv/bin/python scripts/mutate_triage.py --module policy.py
"""
from __future__ import annotations

import argparse
import collections
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Operator classes ordered from "a survivor here is a test gap" to "a survivor here is
# noise". The dividing line is whether mutating the line could change what the screener
# REPORTS, as opposed to how it words or normalises what it reports.
#
#   cosmetic     - message text, display strings, f-strings used only for output.
#                  A survivor means no test asserts on wording. Not a safety finding.
#   defaults     - argument normalisation such as `policy = policy or default_policy()`.
#                  Mutating it changes behaviour only for callers that omit the argument.
#   plumbing     - import wiring, caching, module-level constants.
#   decision     - comparisons, boolean logic, conditionals, return values.
#                  A survivor here is a genuine assertion gap in the decision logic.
DECISION = "decision"
COSMETIC = "cosmetic"
DEFAULTS = "defaults"
PLUMBING = "plumbing"
UNKNOWN = "unclassified"

# Lines whose mutation cannot change a verdict. Matched against the ORIGINAL source.
#
# Written without re.VERBOSE on purpose. The comments in the verbose version started with
# `#`, and under VERBOSE a `#` begins a comment that runs to the end of the line -- so each
# alternative was commented out and the pattern collapsed to `(?:)`, matching every line in
# the project. That silently classified all 1,811 mutants as cosmetic and reported a 71%
# kill rate against the least interesting bucket.
COSMETIC_LINE = re.compile(
    r"^\s*(?:message\s*=|where\s*=|summary\s*=|help\s*=|note\s*=)"
)
FSTRING_ASSIGN = re.compile(r'^\s*\w+\s*=\s*f["\']')
DEFAULT_NORMALISE = re.compile(r"=\s*\w+\s+or\s+\w+\(")
FALLBACK_ARG = re.compile(r"\belse\s+\w+\(")


def classify_line(orig: str) -> str:
    """Bucket one source line by what mutating it could do to a verdict."""
    if COSMETIC_LINE.match(orig) or FSTRING_ASSIGN.match(orig):
        return COSMETIC
    if DEFAULT_NORMALISE.search(orig) or FALLBACK_ARG.search(orig):
        return DEFAULTS
    if re.search(r"^\s*(import |from .* import )", orig):
        return PLUMBING
    if re.search(r"(>=|<=|==|!=|<|>|\band\b|\bor\b|\bnot\b|return\b|if\b)", orig):
        return DECISION
    return UNKNOWN


def _body(lines: list[str], defstart: str):
    """Return (start_index, body_lines) for the function whose def line starts with defstart."""
    idx = next((i for i, l in enumerate(lines) if l.startswith(defstart)), None)
    if idx is None:
        return None
    end = next(
        (i for i in range(idx + 1, len(lines))
         if lines[i].startswith("def ") or lines[i].startswith("@")),
        len(lines),
    )
    return idx, lines[idx:end]


def generated_mutants():
    """mutant name -> (source file, original line number, original line text).

    The line is located by diffing the mutant body against the original function body,
    not by parsing the mutant's mangled name. Mutmut encodes the *function* in the name
    (`x__eval_write__mutmut_12`) but never which line inside it changed, and one function
    routinely carries dozens of mutants spread across different lines.

    Earlier attempt guessed the line from the name and produced a table nobody should
    have believed.
    """
    out = {}
    for path in sorted((ROOT / "mutants/src").rglob("*.py")):
        rel = path.relative_to(ROOT / "mutants/src")
        orig_path = ROOT / "src" / rel
        if not orig_path.exists():
            continue
        orig_lines = orig_path.read_text().splitlines()
        mut_lines = path.read_text().splitlines()

        originals = {}
        for i, line in enumerate(orig_lines):
            m = re.match(r"(\s*)def (\w+)\(", line)
            if not m:
                continue
            got = _body(orig_lines, m.group(1) + "def " + m.group(2) + "(")
            if got and got[0] == i:
                originals[m.group(2)] = got

        for line in mut_lines:
            m = re.match(r"^def ((?:x_)?\w+?)__mutmut_(\d+)\(", line)
            if not m:
                continue
            mangled, num = m.group(1), m.group(2)
            key = mangled + "__mutmut_" + num
            base = mangled.removeprefix("x_")
            mut_body = _body(mut_lines, "def " + mangled + "__mutmut_" + num + "(")
            orig = originals.get(base)
            if mut_body is None or orig is None:
                out[key] = (orig_path, -1, "")
                continue
            o_start, o_lines = orig
            _, m_lines_b = mut_body
            lineno, text = o_start + 1, ""
            if len(o_lines) == len(m_lines_b):
                for off, (a, b) in enumerate(zip(o_lines[1:], m_lines_b[1:]), start=1):
                    if a != b:
                        lineno, text = o_start + off, a
                        break
            elif len(o_lines) > 1:
                text = o_lines[1]
            out[key] = (orig_path, lineno, text)
    return out


def read_outcomes() -> dict[str, str]:
    res = subprocess.run(
        [sys.executable, "-m", "mutmut", "results"],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    outcomes: dict[str, str] = {}
    for line in res.stdout.splitlines():
        m = re.match(r"\s*([\w.]+?):\s+([a-z ]+)$", line)
        if m:
            # `results` prints fully-qualified names (sparkscreen.policy.x_eval_write__mutmut_3)
            # while the generated tree gives bare ones (x_eval_write__mutmut_3). Index both:
            # the bare key for matching, the qualified one so `--module policy` still filters.
            full = m.group(1)
            outcomes[full] = m.group(2).strip()
            outcomes.setdefault(full.rsplit(".", 1)[-1], m.group(2).strip())
    return outcomes


def selfcheck() -> None:
    """Fail loudly if the classifier stops discriminating.

    The VERBOSE-comment bug above made `classify_line` return "cosmetic" for every line in
    the project, and the report still printed a confident table -- 71% killed, all of it in
    the bucket that does not matter. A classifier that has collapsed to a constant is
    indistinguishable from a working one unless something asserts it is not.
    """
    cases = {
        "if not hasattr(node, 'g'):": DECISION,
        "    return x": DECISION,
        "message=f'hi'": COSMETIC,
        "    where = f'{a}.write'": COSMETIC,
        "policy = policy or default_policy()": DEFAULTS,
        "import os": PLUMBING,
    }
    wrong = {src: (classify_line(src), want)
             for src, want in cases.items() if classify_line(src) != want}
    if wrong:
        for src, (got, want) in wrong.items():
            print(f"  classify_line({src!r}) -> {got}, expected {want}", file=sys.stderr)
        raise SystemExit("classify_line is misclassifying; its table would be fiction")


def main() -> int:
    selfcheck()
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", default="", help="only mutants in this module, e.g. policy")
    args = ap.parse_args()

    mutants = generated_mutants()
    if args.module:
        mutants = {k: v for k, v in mutants.items() if args.module in str(v[0])}
    outcomes = read_outcomes()

    by_class = collections.defaultdict(collections.Counter)
    examples: dict[tuple[str, str], list[str]] = collections.defaultdict(list)

    for name, (path, lineno, src) in mutants.items():
        outcome = outcomes.get(name)
        if outcome is None:
            outcome = "killed"          # absent from `results` means killed
        klass = classify_line(src)
        by_class[klass][outcome] += 1
        if outcome != "killed" and len(examples[(klass, outcome)]) < 3:
            examples[(klass, outcome)].append(
                f"{path.name}:{lineno} {src.strip()[:70]}")

    total = sum(sum(c.values()) for c in by_class.values())
    killed = sum(c["killed"] for c in by_class.values())

    print(f"{total} generated, {killed} killed ({killed / total:.1%})\n")
    print(f"{'class':<12} {'total':>6} {'killed':>7} {'surv':>6} {'notests':>8}  killed%")
    for klass in (DECISION, DEFAULTS, COSMETIC, PLUMBING, UNKNOWN):
        c = by_class.get(klass)
        if not c:
            continue
        n = sum(c.values())
        k = c["killed"]
        print(f"{klass:<12} {n:>6} {k:>7} {c['survived']:>6} "
              f"{c['no tests'] + c['timeout']:>8}  {k / n:.0%}")

    decision = by_class.get(DECISION)
    if decision:
        n = sum(decision.values())
        k = decision["killed"]
        print(f"\nDECISION-logic kill rate: {k}/{n} = {k / n:.1%}")
        print("This is the only number that reflects assertion strength. The project rule:")
        print("a survivor in this class is a real gap; elsewhere it is usually operator noise.")

    print("\nexamples of non-killed mutants:")
    for (klass, outcome), items in sorted(examples.items()):
        if outcome == "killed":
            continue
        print(f"  [{klass} / {outcome}]")
        for it in items:
            print(f"    {it}")
    return 0


if __name__ == "__main__":
    sys.exit(main())