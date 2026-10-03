"""Classify mutmut's mutants by operator, so we can judge whether survivors are signal.

The question this answers: `mutmut run` reports a high survival rate. Is that because the
tests are weak, or because most of the surviving mutants are operator classes that do not
matter for a screener (mutating a human-readable message string, for instance)?

Method: for each mutant function in the generated file, find the first line that differs
from the corresponding original function, and bucket the change. Not a full mutation
operator parser -- a sample classifier good enough to tell "display string" from
"comparison flip".
"""
from __future__ import annotations

import collections
import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def function_body(lines: list[str], prefix: str) -> list[str] | None:
    """Return the lines of the function whose def starts with `prefix`."""
    starts = [i for i, l in enumerate(lines) if l.startswith(prefix)]
    if not starts:
        return None
    st = starts[0]
    end = next(
        (i for i in range(st + 1, len(lines))
         if lines[i].startswith("def ") or lines[i].startswith("@")),
        len(lines),
    )
    return lines[st:end]


def classify(orig_line: str, mut_line: str) -> str:
    o, m = orig_line.strip(), mut_line.strip()
    if re.search(r"=\s*None\s*$", m) and re.search(r"[\"']", o):
        return "string literal -> None"
    if re.search(r"\bnot\b", m) != re.search(r"\bnot\b", o):
        return "not inserted/removed"
    if (" or " in m) != (" or " in o) or (" and " in m) != (" and " in o):
        return "boolean flip"
    if re.search(r"(>=|<=|==|!=|<|>)", m) and re.search(r"(>=|<=|==|!=|<|>)", o):
        return "comparison flip"
    if not m and o:
        return "statement deleted"
    if re.search(r"\bTrue\b|\bFalse\b", m) and re.search(r"\bTrue\b|\bFalse\b", o):
        return "bool constant"
    if re.search(r"\bTrue\b|\bFalse\b", m):
        return "bool constant inserted"
    return "other"


def main() -> int:
    total = collections.Counter()
    examples: dict[str, tuple[str, str]] = {}
    per_module = collections.defaultdict(collections.Counter)

    # mutmut copies the configured source_paths verbatim, so the layout under mutants/
    # mirrors src/ rather than the package root.
    for mutant_path in sorted(ROOT.glob("mutants/src/sparkscreen/**/*.py")):
        rel = mutant_path.relative_to(ROOT / "mutants/src")
        orig_path = ROOT / "src" / rel
        if not orig_path.exists():
            continue
        muts = mutant_path.read_text().splitlines()
        orig = orig_path.read_text().splitlines()

        for line in muts:
            m = re.match(r"def (x_\w+__mutmut_\d+)\(", line)
            if not m:
                continue
            name = m.group(1)
            base = name.split("__mutmut")[0].removeprefix("x_")
            ob = function_body(orig, f"def {base}(")
            nb = function_body(muts, f"def {name}(")
            if ob is None or nb is None:
                total["could not pair"] += 1
                continue
            if len(ob) != len(nb):
                total["multi-line rewrite"] += 1
                per_module[rel.name][  "multi-line rewrite"] += 1
                continue
            bucket = "identical?"
            for a, b in zip(ob[1:], nb[1:]):
                if a != b:
                    bucket = classify(a, b)
                    examples.setdefault(bucket, (a.strip()[:78], b.strip()[:78]))
                    break
            total[bucket] += 1
            per_module[rel.name][bucket] += 1

    print(f"{'count':>7}  bucket")
    for k, v in total.most_common():
        print(f"{v:7}  {k}")
    print("\nexamples:")
    for k, (a, b) in sorted(examples.items()):
        print(f"  [{k}]")
        print(f"    orig: {a}")
        print(f"    mut : {b}")
    print("\nper module:")
    for mod, counts in sorted(per_module.items()):
        print(f"  {mod}: {dict(counts)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())