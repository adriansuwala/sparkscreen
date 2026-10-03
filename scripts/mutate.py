#!/usr/bin/env python3
"""Mutation testing for the decision logic, with per-mutant test selection.

## Why this exists instead of `mutmut run`

Two reasons, both measured rather than assumed.

**1. `mutmut` 3.8.0 deadlocks on this project.** Started with `--max-children 4`, it
stalled in `futex_do_wait` across all seven processes with 0% CPU and no log output past
"Generating mutants". It works fine on a small project (verified on a 6-mutant toy
repro), so this is scale-related, not a broken install. It was killed rather than left
to burn hours.

**2. It cannot subset tests, and that is the actual cost driver.** There are ~790
mutants across the four decision modules, and the full suite is ~43 s / 2,488 tests. No
single test dominates the wall clock (the slowest is 3.5 s), so the cost is genuinely
irreducible per mutant: 790 x 43 s / 4 workers is roughly 2.4 hours of pure test
execution.

But a mutant in `model.py` is not sensitive to the DataFrame detector or the grammar
port. Running `tests/test_dataframe_writes.py` against a changed verdict comparison is
wasted work. This runner picks the tests that import the mutated module (plus the
fail-closed invariant sweeps, which are the real contract), so a `model.py` mutant costs
seconds instead of a minute.

## What it reports

    killed  -- a test caught it. The assertion bites.
    survived -- no test in the selection caught it. This is the interesting category:
                it means either the code is unreachable, or a test asserts too little.

`survived` is a *measurement*, not a failure of this tool. A survivor in `screen.py` is
the expensive kind -- that is the aggregation path which has already produced one
production bug (F1, `Report.verdict` aggregating on `reason`).

## Usage

    python scripts/mutate.py                      # all four modules
    python scripts/mutate.py --module model.py    # one module
    python scripts/mutate.py --jobs 4 --limit 40  # bounded sample
    python scripts/mutate.py --dry-run            # count without running

Mutation operators are the ones that matter for decision logic: comparison operator
flips, boolean operator flips, constant swaps, and `return None` injection. Arithmetic
and string mutations are skipped deliberately -- for a screener, "returns the wrong
branch" is the interesting bug class, not "concatenates slightly differently".
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Modules whose mutation changes a verdict, and the tests that guard each.
#:
#: The mapping is by hand rather than derived, because "which tests guard this module"
#: is a judgement: `test_verdicts.py` and `test_fail_closed` sweeps guard model.py, but a
#: DataFrame-write mutant is only caught by tests that actually construct DataFrame code.
#: Deriving it by import graph would include tests that merely import the module without
#: asserting anything about its logic.
MODULE_TESTS: dict[str, list[str]] = {
    # test_effects.py belongs here because model.py owns Effect, effect_names,
    # effect_flags, has_effect, by_effect and by_verdict -- and test_effects.py is where
    # all of those are asserted. Omitting it produced 5 false survivors on the first run,
    # which is exactly the failure mode a mutation harness has: a survivor that means
    # "I did not run the right test", not "this code is untested".
    "model.py": [
        "tests/test_verdicts.py",
        "tests/test_effects.py",
        "tests/test_screen_policy.py",
        "tests/test_dataframe_writes.py",
    ],
    "policy.py": [
        "tests/test_screen_policy.py",
        "tests/test_verdicts.py",
        "tests/test_effects.py",
    ],
    "screen.py": [
        "tests/test_verdicts.py",
        "tests/test_screen_policy.py",
        "tests/test_dataframe_writes.py",
        "tests/test_folding.py",
        "tests/test_effects.py",
    ],
    "analysis/treewalk.py": [
        "tests/test_treewalk.py",
        "tests/test_screen_policy.py",
        "tests/test_verdicts.py",
    ],
}

#: Always included, for any mutant. These are the fail-closed sweeps, and they are the
#: property most worth protecting: no input may yield ALLOW unless it genuinely is safe.
ALWAYS_TESTS = ["tests/test_verdicts.py::TestFailClosedInvariants"]

COMPARE_FLIP = {
    ast.Lt: ast.LtE, ast.LtE: ast.Lt,
    ast.Gt: ast.GtE, ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
    ast.In: ast.NotIn, ast.NotIn: ast.In,
}
BOOL_FLIP = {ast.And: ast.Or, ast.Or: ast.And}


class Mutant:
    __slots__ = ("path", "lineno", "kind", "detail")

    def __init__(self, path: str, lineno: int, kind: str, detail: str):
        self.path, self.lineno, self.kind, self.detail = path, lineno, kind, detail

    def key(self) -> str:
        return f"{self.path}:{self.lineno}:{self.kind}:{self.detail}"

    def __repr__(self) -> str:
        return f"{self.path}:{self.lineno} {self.kind} {self.detail}"


def find_mutants(rel_path: str) -> list[Mutant]:
    """Enumerate mutants by source transformation, without executing anything."""
    src_path = ROOT / "src/sparkscreen" / rel_path
    tree = ast.parse(src_path.read_text())
    out: list[Mutant] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = node.ops[0]
            if type(op) in COMPARE_FLIP:
                name = type(op).__name__
                out.append(Mutant(rel_path, node.lineno, "compare",
                                  f"{name}->{COMPARE_FLIP[type(op)].__name__}"))
        elif isinstance(node, ast.BoolOp):
            op = type(node.op)
            if op in BOOL_FLIP:
                out.append(Mutant(rel_path, node.lineno, "boolop",
                                  f"{op.__name__}->{BOOL_FLIP[op].__name__}"))
        elif isinstance(node, ast.If) and not isinstance(node.test, ast.BoolOp):
            # `if X:` -> `if not X:` is the same shape as a boolop flip but is a separate
            # operator in the AST, so it needs its own rule or it is silently untested.
            out.append(Mutant(rel_path, node.lineno, "ifnegate", "not"))
        elif isinstance(node, ast.Return) and node.value is not None:
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                out.append(Mutant(rel_path, node.lineno, "return_none", "None"))
    return out


def apply_mutation(src_path: pathlib.Path, mutant: Mutant) -> str | None:
    """Return mutated source, or None if this mutation cannot be applied to this line.

    Line-based editing is used deliberately over AST round-tripping: `ast.unparse` would
    normalise the whole file and produce a diff nobody can read, which defeats the point
    of being able to eyeball a survivor.
    """
    lines = src_path.read_text().splitlines(keepends=True)
    idx = mutant.lineno - 1
    if idx >= len(lines):
        return None
    line = lines[idx]

    if mutant.kind == "ifnegate":
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        # Only safe when the condition starts the line; a multi-line `if (\n` form is
        # skipped rather than guessed at.
        if not stripped.startswith("if "):
            return None
        lines[idx] = indent + "if not " + stripped[3:]
        return "".join(lines)

    if mutant.kind == "return_none":
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        if not stripped.startswith("return "):
            return None
        lines[idx] = indent + "return None\n"
        return "".join(lines)

    if mutant.kind == "compare":
        old, new = mutant.detail.split("->")
        op_src = {
            "Lt": "<", "LtE": "<=", "Gt": ">", "GtE": ">=",
            "Eq": "==", "NotEq": "!=", "Is": "is", "IsNot": "is not",
            "In": "in", "NotIn": "not in",
        }
        if op_src[old] in line:
            lines[idx] = line.replace(op_src[old], op_src[new], 1)
            return "".join(lines)
        return None

    if mutant.kind == "boolop":
        old, new = mutant.detail.split("->")
        tok = " and " if old == "And" else " or "
        alt = " or " if old == "And" else " and "
        if tok in line:
            lines[idx] = line.replace(tok, alt, 1)
            return "".join(lines)
        return None

    return None


def run_one(args_tuple) -> tuple[Mutant, str, str]:
    """Run one mutant in an isolated copy. Never raises."""
    mutant, tests = args_tuple
    src_path = ROOT / "src/sparkscreen" / mutant.path
    original = src_path.read_text()
    mutated = apply_mutation(src_path, mutant)
    if mutated is None:
        return mutant, "skipped", ""

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="mut-"))
    try:
        # Copy the package tree, not the whole repo: the repo contains .venvs, mutants/
        # and a dist/ that would multiply the copy cost by orders of magnitude.
        pkg_dst = tmp / "sparkscreen"
        shutil.copytree(src_path.parent, pkg_dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

        # The mutant file may be nested (analysis/treewalk.py)
        target = pkg_dst / mutant.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(mutated)

        env = dict(os.environ)
        env["PYTHONPATH"] = str(tmp)
        cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider",
               "--no-header", *tests]
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True,
                              text=True, timeout=300)
        # -x stops at the first failure, so a nonzero exit means "a test caught it".
        if proc.returncode == 0:
            return mutant, "survived", ""
        tail = proc.stdout.strip().splitlines()[-1:] or [""]
        return mutant, "killed", tail[0][:160]
    except subprocess.TimeoutExpired:
        return mutant, "timeout", "exceeded 300s"
    except Exception as exc:  # noqa: BLE001
        return mutant, "error", f"{type(exc).__name__}: {exc}"[:160]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--module", action="append", dest="modules",
                    help="module to mutate (repeatable); default all")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--limit", type=int, help="stop after N mutants (sampling)")
    ap.add_argument("--dry-run", action="store_true", help="count without running")
    args = ap.parse_args()

    modules = args.modules or list(MODULE_TESTS)
    all_mutants: list[Mutant] = []
    for mod in modules:
        if mod not in MODULE_TESTS:
            print(f"unknown module {mod!r}; known: {list(MODULE_TESTS)}")
            return 2
        ms = find_mutants(mod)
        print(f"{mod:26} {len(ms):4} mutants")
        all_mutants.extend(ms)

    print(f"{'TOTAL':26} {len(all_mutants):4} mutants")

    if args.limit:
        all_mutants = all_mutants[: args.limit]
        print(f"sampling to the first {args.limit}")

    if args.dry_run:
        return 0

    # Preserve source order so mutants and their tests are paired consistently.
    work = [(m, sorted(set(MODULE_TESTS[m.path] + ALWAYS_TESTS))) for m in all_mutants]
    print(f"running with {args.jobs} workers\n")

    counts = {"killed": 0, "survived": 0, "timeout": 0, "error": 0, "skipped": 0}
    survivors: list[Mutant] = []
    errors: list[tuple[Mutant, str]] = []
    done = 0

    with concurrent.futures.ProcessPoolExecutor(max_workers=args.jobs) as ex:
        for mutant, status, detail in ex.map(run_one, work):
            done += 1
            counts[status] = counts.get(status, 0) + 1
            if status == "survived":
                survivors.append(mutant)
                print(f"  SURVIVED  {mutant!r}")
            elif status == "killed":
                if done % 25 == 0:
                    print(f"  ... {done}/{len(work)} "
                          f"(killed {counts['killed']}, "
                          f"survived {counts['survived']})")
            else:
                errors.append((mutant, detail))
                print(f"  {status.upper():9} {mutant!r}  {detail}")

    total = sum(counts.values())
    print("\n" + "=" * 68)
    print(f"killed    {counts['killed']:5} / {total}  "
          f"({counts['killed'] / total * 100:.1f}% caught)")
    print(f"survived  {counts['survived']:5}")
    print(f"skipped   {counts['skipped']:5}   (mutation not applicable to that line)")
    print(f"timeout   {counts['timeout']:5}")
    print(f"error     {counts['error']:5}")
    print("=" * 68)

    if survivors:
        print("\nSURVIVORS -- no test in the selection caught these.")
        print("Each is either unreachable code or an under-specified test. Inspect with:")
        for m in survivors:
            print(f"  src/sparkscreen/{m.path}:{m.lineno}  "
                  f"{m.kind} {m.detail}")
    if errors:
        print(f"\n{len(errors)} non-kill outcomes -- read these before trusting the ratio.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
