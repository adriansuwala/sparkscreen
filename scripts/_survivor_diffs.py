"""Ground-truth survivor list, read from mutmut's own results file.

`mutmut results` prints one line per mutant that did NOT die, fully qualified. This
script turns each into the literal source diff of the mutated function, so a triage
decision is made on the change itself rather than on the committed classifier's guess at
which line it landed on. The classifier reads the *original* line's text, so it misses
mutmut's keyword-argument mutations (`severity=Severity.CRITICAL` -> `severity=None`) --
which are exactly the ones that change a verdict.

    .venv/bin/python scripts/_survivor_diffs.py --module screen --summary
    .venv/bin/python scripts/_survivor_diffs.py --grep "verdict"

Reads the gitignored `mutants/` tree and the cached results file in the primary
worktree; writes nothing.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import re
import sys
from pathlib import Path

PRIMARY = Path("/opt/data/projects/sparkscreen")
HERE = Path(__file__).resolve().parent.parent
CACHE = Path("/opt/data/cache/scratch/results.txt")

LINE = re.compile(r"\s*([\w.]+?):\s+([a-z ]+)$")


def survivors() -> dict[str, str]:
    """mutant fully-qualified name -> outcome, straight from `mutmut results`."""
    if not CACHE.exists():
        raise SystemExit(f"missing {CACHE}; regenerate with `mutmut results`")
    out: dict[str, str] = {}
    for line in CACHE.read_text().splitlines():
        m = LINE.match(line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _spans(path: Path) -> dict[str, tuple[int, int]]:
    """Every def in the file, methods included, name -> (start, end) line index."""
    lines = path.read_text().splitlines()
    out: dict[str, tuple[int, int]] = {}
    for node in ast.walk(ast.parse("\n".join(lines))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = (node.lineno - 1, node.end_lineno or node.lineno)
    return out


def locate(full: str):
    """(module, short mutant name, changed lines, original line no) for one survivor."""
    tail = full.rpartition(".")[2]
    if not tail.startswith("x"):
        return None
    mod, name = full[: len(full) - len(tail) - 1], tail
    rel = "src/" + mod.replace(".", "/") + ".py"
    opath, mpath = PRIMARY / rel, PRIMARY / "mutants" / rel
    if not opath.exists() or not mpath.exists():
        return None
    base = name.rsplit("__mutmut_", 1)[0].removeprefix("x_")
    os_, ms = _spans(opath), _spans(mpath)
    if name not in ms:
        return None
    # mutmut mangles methods as `xǁClassǁmethod`, so match on the suffix.
    cand = [k for k in os_ if k == base or k.endswith(base) or base.endswith(k)]
    if not cand:
        return None
    (o0, o1), (m0, m1) = os_[cand[0]], ms[name]
    changed = [
        l for l in list(difflib.unified_diff(
            opath.read_text().splitlines()[o0:o1],
            mpath.read_text().splitlines()[m0:m1],
            fromfile="orig", tofile=name, lineterm="", n=0))[2:]
        if l[:1] in "+-" and not l.startswith(("---", "+++"))
        and "__mutmut_" not in l
    ]
    return mod, name, changed, o0 + 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", default="")
    ap.add_argument("--grep", default="")
    ap.add_argument("--only", default="", help="substring the MUTANT name must contain")
    ap.add_argument("--summary", action="store_true",
                    help="one line per survivor instead of the full diff")
    args = ap.parse_args()

    shown = 0
    sv = survivors()
    for full, outcome in sorted(sv.items()):
        if args.module and args.module not in full:
            continue
        if args.only and args.only not in full:
            continue
        got = locate(full)
        if got is None:
            continue
        mod, name, changed, lineno = got
        if not changed:
            continue
        blob = "\n".join(changed)
        if args.grep and args.grep not in blob:
            continue
        shown += 1
        if args.summary:
            one = " | ".join(c.strip() for c in changed)
            print(f"{outcome:<8} {mod}.{name} L{lineno}: {one[:160]}")
        else:
            print(f"### {outcome} {mod}.{name}  (orig L{lineno})")
            print(blob)
            print()
    print(f"{shown} shown of {len(sv)} survivors")
    return 0


if __name__ == "__main__":
    sys.exit(main())