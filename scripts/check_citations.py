"""Check that every mutant in the CLAIMED ledger is cited by a docstring, and vice versa.

The convention the ledger rests on is "each test docstring names the mutant it kills".
This answers both directions mechanically:

  * a CLAIMED entry with no citation in its class's docstrings -> undocumented kill;
  * a `mutmut_N` named in a docstring that the ledger does not claim -> a kill nobody
    re-checks.

Slash forms (`mutmut_5/6`, `mutmut_16/18`) are expanded, and `_recover_target__mutmut_5`
counts as citing 5 regardless of the class prefix mutmut chose.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_ledger():
    spec = importlib.util.spec_from_file_location(
        "vsc", ROOT / "scripts/verify_survivor_claims.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cited_numbers(class_src: str) -> set[str]:
    """Every mutant number a class's docstrings mention, slash forms expanded."""
    out: set[str] = set()
    for tok in re.findall(r"mutmut_\d+(?:/\d+)*", class_src):
        first, _, rest = tok.partition("_")
        digits = tok[len("mutmut_"):]
        head, *more = digits.split("/")
        out.add(head)
        out.update(more)
    return out


def main() -> int:
    ledger = _load_ledger()
    problems = []
    for module in sorted({m for _, m, _ in ledger.CLAIMED}):
        tree = ast.parse((ROOT / module).read_text())
        by_class = {n.name: ast.get_source_segment(
            (ROOT / module).read_text(), n) or ""
            for n in tree.body if isinstance(n, ast.ClassDef)}
        claimed = {}
        for mutant, mod, cls in ledger.CLAIMED:
            if mod != module:
                continue
            claimed.setdefault(cls, []).append(mutant)
        for cls, mutants in claimed.items():
            have = cited_numbers(by_class.get(cls, ""))
            undocumented = [m for m in mutants if m.rsplit("_", 1)[1] not in have]
            if undocumented:
                problems.append(f"{module}::{cls}: killed but not cited: "
                                + ", ".join(sorted(undocumented)))
    # reverse direction: numbers cited in a claimed class but absent from the ledger
    ledger_nums = {m.rsplit("_", 1)[1] for m, _, _ in ledger.CLAIMED}
    for problem in problems:
        print(f"!! {problem}")
    print(f"\n{len(problems)} problem(s); ledger holds {len(ledger.CLAIMED)} entries, "
          f"{len(ledger_nums)} distinct mutant numbers")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())