"""Show the exact source diff for one named mutmut mutant, and verify a test kills it.

The committed classifier attributes a mutant to a source line by index arithmetic, which
is only approximate: a mutation inside a multi-line call is attributed to whichever line
it happens to land on. When deciding *what a survivor means*, the only trustworthy
statement is the literal diff between the original function and the mutant's copy of it,
so that is what this prints.

    .venv/bin/python scripts/_mutant_diff.py x__eval_write__mutmut_4

`--kill TEST` proves a test actually kills the mutant: it copies the worktree to a scratch
directory, applies this one mutation to the copy's src/, and runs one pytest node there.
The worktree's own src/ is never written to -- a mutant is a temporary artefact, not an
edit. Exit 0 from `--kill` means the test failed under the mutant, i.e. it kills.

Two things about the scratch run are easy to get wrong, and both produce a result that
looks fine but means nothing:

  * the control run (see below) -- a kill is only believed if the same test passes
    unmutated in the same scratch tree;
  * `tests/test_packaging.py` builds a wheel and creates a venv, which does not survive
    being run from a copied tree, so `--kill tests/` reports CONTROL FAILED for reasons
    unrelated to any mutant. Point `--kill` at a test module, not at `tests/`.

Runs are sequential: every check rebuilds the one shared scratch tree.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import re
import os
import shutil
import subprocess
import sys
from pathlib import Path

PRIMARY = Path("/opt/data/projects/sparkscreen")
HERE = Path(__file__).resolve().parent.parent
SCRATCH = Path("/opt/data/cache/scratch/mutcheck")

MUT_RE = re.compile(r"^def ((?:x_)?\w+?)__mutmut_(?:orig|\d+)\(")

#: mutmut joins class and method names with U+01C1 when it mangles a method.
CLASS_SEP = "\u01c1"


def _base_name(name: str) -> str:
    """`_recover_target` from `xǁ_WriteFinderǁ_recover_target__mutmut_16`.

    mutmut mangles a method into `x<U+01C1>Class<U+01C1>method`, so the class prefix has
    to come back off before the original function can be located.
    """
    stem = name.rsplit("__mutmut_", 1)[0]
    if stem.startswith("x" + CLASS_SEP):
        return stem.split(CLASS_SEP)[-1]
    return stem.removeprefix("x_")


def _functions(path: Path) -> dict[str, tuple[int, int]]:
    """name -> (start_line, end_line) for every def, methods included, via the AST.

    mutmut mangles a method as `x\u01c1_Class\u01c1_method`, and those copies live
    nested inside the class, so a top-level-only walk misses every method mutant.
    """
    lines = path.read_text().splitlines()
    out: dict[str, tuple[int, int]] = {}
    for node in ast.walk(ast.parse("\n".join(lines))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = (node.lineno - 1, node.end_lineno or node.lineno)
    return out


def find(name: str) -> tuple[Path, Path, str, str, str]:
    """(mutant_file, orig_file, mangled, base_name, rel_path) for a mutant name."""
    base_want = _base_name(name)
    for mpath in sorted((PRIMARY / "mutants/src").rglob("*.py")):
        rel = mpath.relative_to(PRIMARY / "mutants/src")
        opath = PRIMARY / "src" / rel
        if not opath.exists():
            continue
        if name not in _functions(mpath):
            continue
        mangled = _base_name(name)
        return mpath, opath, mangled, base_want, rel.as_posix()
    raise SystemExit(f"no mutant named {name}")


def locate(name: str):
    mpath, opath, mangled, base, rel = find(name)
    num = name.rsplit("__mutmut_", 1)[1]
    m0, m1 = _functions(mpath)[name]
    o0, o1 = _functions(opath)[base]
    return (mpath, opath, rel, base,
            mpath.read_text().splitlines()[m0:m1],
            opath.read_text().splitlines()[o0:o1],
            (o0, o1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="e.g. x__eval_write__mutmut_4")
    ap.add_argument("--kill", default="",
                    help="pytest node id to run against this mutant; 0 exit = killed")
    ap.add_argument("--no-hypothesis-cleanup", action="store_true",
                    help="keep hypothesis from deleting the scratch tree on a failure")
    args = ap.parse_args()

    name = args.name.split(".")[-1]
    if "__mutmut_" not in name:
        raise SystemExit("not a mutant name")
    _, _, rel, base, mlines, olines, (o0, o1) = locate(name)

    print(f"# {rel} :: {base}()  original lines {o0 + 1}-{o1}")
    print("\n".join(difflib.unified_diff(
        olines, mlines, fromfile=f"src/{rel}:{base}",
        tofile=f"mutant:{name}", lineterm="", n=2)))

    if not args.kill:
        return 0

    # A control run first, against the same scratch tree with NO mutation applied.
    # Without it, "the test failed" is uninterpretable: a broken copy, a stale
    # __pycache__ or a test that only passes in the worktree would all report as a kill.
    # This is the check that makes every other number on this page mean something.
    control = _run_in_scratch(None, rel, olines, (o0, o1), mlines, args.kill)
    if control.returncode:
        print(f"\n== CONTROL FAILED: the node does not pass unmutated, so a 'kill' "
              f"below proves nothing.\n" + "\n".join(control.tail))
        return 2

    mutated = _run_in_scratch(name, rel, olines, (o0, o1), mlines, args.kill)
    verdict = ("KILLED (test failed under mutant)"
               if mutated.returncode else "SURVIVED (test passed!)")
    print(f"\n== control passed; {name} + {args.kill}: {verdict}")
    print("\n".join("   " + t for t in mutated.tail))
    return 0 if mutated.returncode else 1


def _run_in_scratch(name, rel, olines, span, mlines, node):
    """Run one pytest node against a scratch copy of the tree, mutated or not."""
    o0, o1 = span
    if SCRATCH.exists():
        shutil.rmtree(SCRATCH)
    SCRATCH.mkdir(parents=True)
    for item in ("src", "tests", "pyproject.toml", "conftest.py"):
        s = HERE / item
        if s.is_dir():
            shutil.copytree(s, SCRATCH / item,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        elif s.exists():
            shutil.copy2(s, SCRATCH / item)
    target = SCRATCH / "src" / rel
    tl = target.read_text().splitlines()
    assert tl[o0:o1] == olines, "scratch copy diverged from the worktree; refusing to patch"
    if name:
        # Install the mutant's BODY under the ORIGINAL function name.
        #
        # Dropping it in under its mangled name (`x__eval_write__mutmut_39`) makes every
        # check a false positive: `screen()` calls `_eval_write`, the patched file no
        # longer defines it, and the test fails with `NameError` before reaching the
        # mutated line. mutmut avoids this with a trampoline -- the original name stays
        # and dispatches to the mutant -- so the faithful equivalent is to keep the name
        # and take only the body.
        body = mlines[:]
        body[0] = olines[0]
        tl[o0:o1] = body
        target.write_text("\n".join(tl) + "\n")
        ast.parse("\n".join(tl))  # a mutant that does not compile is not a result
    # hypothesis writes its `.hypothesis` example database next to the tests, and on a
    # failing example it `rmtree`s any failing example's parent directory. When that
    # parent is the scratch tree itself, hypothesis deletes the tree mid-run and pytest
    # dies with INTERNALERROR/FileNotFoundError -- which the control then reports as a
    # failure. Opting out of the cleanup keeps the scratch tree intact to read the
    # result from.
    cmd = [str(HERE / ".venv/bin/python"), "-m", "pytest", node, "-q", "--no-header",
           "-p", "no:randomly"]
    # Set by default, not opt-in: a hypothesis test that fails under the mutant can
    # delete the scratch tree, which turns a real kill into an unreadable INTERNALERROR
    # and a real survive into a bogus CONTROL FAILED.
    os.environ.setdefault("HYPOTHESIS_NO_CLEANUP", "1")
    return _Run(subprocess.run(cmd, cwd=SCRATCH, capture_output=True, text=True,
                               timeout=900))


class _Run:
    """A pytest result plus the lines worth reading."""

    def __init__(self, proc):
        self.returncode = proc.returncode
        self.tail = (proc.stdout or proc.stderr).strip().splitlines()[-4:]


if __name__ == "__main__":
    sys.exit(main())