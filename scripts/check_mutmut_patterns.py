"""Which mutmut mechanism actually suppresses mutants? Verify, don't assume.

mutmut offers three ways to scope a run, and this project had assumed one of them worked
without checking:

  1. `source_paths` + `do_not_mutate`      -- by path. Known to work; used for
                                             grammar/generated/* and grammar/spec.py.
  2. `do_not_mutate_patterns`              -- regex over source lines.
  3. inline `# pragma: no mutate`          -- in the source.

This measures 2 and 3 against a fixture whose target statement is *known* to produce
mutants, and prints which ones work. Run it from the repo root:

    .venv/bin/python scripts/check_mutmut_patterns.py .venv/bin/mutmut

Two mistakes this file exists to prevent, both of which it committed on the way:

  * Counting with `mutmut results` instead of counting generated mutants. `results` lists
    only NON-killed outcomes, so a "9" there means nine survivors, not nine mutants. That
    error made the first version report "suppressed nothing" when the baseline was simply
    miscounted -- it compared a survivors list against a survivors list.
  * Using a fixture with nothing to suppress. An f-string return generates zero mutants in
    mutmut 3.8.0, so the comparison passed vacuously.

Hence the positive control: delete the target statement entirely and require the count to
drop. If it does not, the harness is broken and every other number here is noise.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRATCH = Path("/opt/data/cache/scratch")

# `doubled = n * 2` reliably produces arithmetic mutants, and no test asserts on the
# intermediate value -- the shape we want to suppress. The other functions give mutmut
# something to chew on so the counts are not trivially small.
SAMPLE = '''\
"""Scratch module for verifying mutmut's suppression mechanisms."""

NAME = "sparkscreen"


def cosmetic(n: int) -> int:
    """Mutating the body here is cosmetic: no test asserts on the intermediate value."""
    doubled = n * 2
    return doubled


def limit(n: int) -> bool:
    """A real predicate: mutating this changes behaviour."""
    return n > 10


def main(n: int) -> None:
    print(cosmetic(n), limit(n), NAME)
'''

TESTS = '''\
from pt_mod import cosmetic, limit


def test_cosmetic():
    assert cosmetic(3) == 6


def test_limit():
    assert limit(11) is True
    assert limit(9) is False
'''

SETUP = '''\
[build-system]
requires = ["setuptools"]
build-backend = "setuptools.build_meta"

[project]
name = "pt"
version = "0"
requires-python = ">=3.11"

[tool.setuptools]
packages = []
'''

TARGET = "    doubled = n * 2"


def build_project(root: Path, *, pattern: str | None = None, pragma: str | None = None) -> None:
    """Materialise a scratch project with one suppression mechanism applied."""
    source = SAMPLE
    if pragma == "block":
        source = source.replace(
            TARGET,
            f"    # pragma: no mutate start\n{TARGET}\n    # pragma: no mutate end",
        )
    elif pragma == "trailing":
        source = source.replace(TARGET, f"{TARGET}  # pragma: no mutate")
    elif pragma == "delete":
        source = source.replace(f"{TARGET}\n", "")

    (root / "pt_mod.py").write_text(source)
    (root / "test_pt.py").write_text(TESTS)

    toml = SETUP + '\n[tool.mutmut]\nsource_paths = ["pt_mod.py"]\n'
    if pattern:
        toml += f"do_not_mutate_patterns = [{pattern!r}]\n"
    (root / "pyproject.toml").write_text(toml)


def count_generated(root: Path, mutmut: str) -> int:
    """Run mutmut, then count the mutants it GENERATED.

    Deliberately not `mutmut results`: that reports only non-killed outcomes.
    """
    subprocess.run(
        [mutmut, "run", "--max-children", "1"],
        cwd=root, capture_output=True, text=True, timeout=900,
        env={"PATH": "/usr/bin:/bin", "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    total = 0
    for path in (root / "mutants").rglob("*.py"):
        total += len(re.findall(r"^def (?:x_)?\w+__mutmut_\d+\(",
                                path.read_text(), re.MULTILINE))
    return total


def measure(mutmut: str, label: str, **kwargs) -> int:
    root = Path(tempfile.mkdtemp(prefix="mm-", dir=str(SCRATCH)))
    build_project(root, **kwargs)
    n = count_generated(root, mutmut)
    print(f"  {label:<36} {n:>4}")
    return n


def main() -> int:
    mutmut = sys.argv[1] if len(sys.argv) > 1 else "mutmut"
    mutmut = str(Path(shutil.which(mutmut) or mutmut).resolve())

    assert TARGET in SAMPLE, "fixture lost its target statement"
    SCRATCH.mkdir(parents=True, exist_ok=True)

    print(f"{'variant':<40} {'mutants':>7}")
    # The number every other variant is measured AGAINST: the untouched fixture.
    baseline = measure(mutmut, "baseline (nothing suppressed)")
    control = measure(mutmut, "control: target line deleted", pragma="delete")
    counts = {
        "regex do_not_mutate_patterns":
            measure(mutmut, "do_not_mutate_patterns regex",
                    pattern=r"doubled = n \* 2"),
        "block pragma start/end":
            measure(mutmut, "# pragma: no mutate start/end", pragma="block"),
        "trailing pragma on the line":
            measure(mutmut, "# pragma: no mutate (trailing)", pragma="trailing"),
    }

    print()
    # The control bounds what suppression could possibly achieve: deleting the statement
    # outright is the most aggressive thing any mechanism can do. If a mechanism matches
    # the control, it suppressed everything on that line. If nothing beats the baseline,
    # the harness is broken.
    removed = baseline - control
    if removed <= 0:
        print("FAIL: deleting the target line changed nothing, so the target produces no "
              "mutants and this harness cannot detect suppression either way.")
        return 1
    print(f"  deleting the line removes {removed} mutants; that is the ceiling.")

    works = [n for n, c in counts.items() if c < baseline]
    inert = [n for n, c in counts.items() if c >= baseline]
    for name in works:
        got = baseline - counts[name]
        print(f"  WORKS: {name} (-{got}"
              + (", full line suppressed" if counts[name] == control else ", partial"))
    for name in inert:
        print(f"  INERT: {name} suppressed nothing")
    if not works:
        print("FAIL: no suppression mechanism had any effect")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())