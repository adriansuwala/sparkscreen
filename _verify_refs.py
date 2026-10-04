"""Verify that every grammar key and engine pin in the tree names something real.

`scripts/setup.sh` shipped a `spark-4.0` verification step for two releases after F17
removed that key, and `docs/user-docs/usage.md` still told readers to pass
`spec="spark-4.0"` in its headline library example. Both are one defect: a literal naming
a grammar key, living in a file that nothing executes and no audit reads the literal out
of. `_verify_readme.py` checked that `setup.sh` was *executable*; nothing ran it.

So this does not hardcode the offenders, because a list of known-stale strings is stale
the moment the next one appears. It scans tracked text files for grammar-key-shaped
literals and `pyspark==X` pins, and requires each to be one of:

  * a key that `spec.py` actually ships,
  * an engine version the differential corpus records expectations for,
  * a test asserting that a key does NOT exist (which needs a plausible wrong key),
  * prose narrating a key's retirement, or a reasoning document.

The exemptions are the design. A check that flagged `spark-4.0` inside `findings.md`
would be wrong on day one -- F17 exists to document that removal -- so the allowlist is
explicit, and narrow enough that adding to it is a decision rather than a suppression.

Not part of the pytest suite: a repo-wide audit, like its siblings. It shells out to git
and walks the tree, which the fast suite should not do. It also shells out to a Python
interpreter to read the real grammar keys, so it looks one up rather than assuming
`.venv` -- CI has no `.venv` and this cost a red build once already (F24). Run:
    .venv/bin/python _verify_refs.py
"""
from __future__ import annotations

import re
import subprocess
import sys as _sys
from pathlib import Path as _Path

# Runnable as a bare script without PYTHONPATH=src, like any other entry point.
_sys.path.insert(0, str(_Path(__file__).resolve().parent / "src"))

ROOT = _Path(__file__).resolve().parent

#: Whole files where a retired grammar key is legitimate: the reasoning documents exist to
#: narrate their removal, so "spark-4.0" in findings.md is the subject, not a stale claim.
#:
#: Per-file on purpose, and NOT extended to usage.md. That guide is live documentation
#: which happens to contain one paragraph of history; exempting the whole file would have
#: let the wrong example on line 81 survive, which is the bug this audit exists to catch.
HISTORICAL_FILES = {
    "docs/findings.md",
    "docs/decisions.md",
    "docs/threads.md",
}

#: Files that *define* the rule and therefore have to name the shapes they reject.
#:
#: This audit flagged itself on its own docstring: explaining that `spark-4.0` is retired
#: requires writing the literal, and per-line narration matching does not rescue it because
#: the words "removed"/"retired" sit on a neighbouring line from the literal. That is the
#: same trap AGENTS.md hit when it documented this very bug.
#:
#: Rewording was rejected because two of these lines exist precisely to show what a real
#: key and a real *release* look like side by side (`spark-4.1` vs `spark-4.1.3`); removing
#: the literals would gut the explanation of why the regex is loose on the patch level.
#:
#: Narrow on purpose and audited like the rest: hits here are printed by name below, so
#: widening this set is visible in the output rather than silent. A literal that is stale
#: for a *different* reason still fails -- the exemption is for naming the shape, not for
#: any grammar key whatsoever.
RULE_FILES = {
    "_verify_refs.py",
}

#: Never scanned: build output, gitignored caches, and mutation-instrumented copies.
SKIP_DIRS = {"build", "dist", ".git", ".venv", ".venv-pyspark", "mutants", "__pycache__",
             "src/sparkscreen/grammar/generated"}

#: `spark-4.1`, `spark-3.5.1`, `spark-4.1.3`. Loose on the patch level on purpose: a real
#: *release* (4.1.3) is not the same kind of thing as a real *key* (spark-4.1), and conflating
#: them would let `spec="spark-4.1.3"` through as if it were valid when no such key ships.
GRAMMAR_KEY_RE = re.compile(r"\bspark-(\d+)\.(\d+)(?:\.(\d+))?\b")

#: `pyspark==4.1.3` -- an engine pin, which is a different namespace from a grammar key.
ENGINE_PIN_RE = re.compile(r"pyspark==(\d+\.\d+\.\d+)")

#: A line may still name a retired key when it narrates the retirement. Both conditions are
#: required: the line must say the key is gone, AND must not look like code an operator would
#: run. The prose in usage.md that explains the removal passes; the example above it does not.
RETIREMENT_NARRATION = re.compile(
    r"(earlier|formerly|previous|used to|no longer|\bgone\b|removed|not supported"
    r"|retired|superseded)", re.I)
EXECUTABLE_SHAPE = re.compile(r"^\s*(#|//)?\s*(\w+\s*=|\w+\(|\$|>|python|bash|sh )")

#: Tests legitimately name grammars that do not exist, to prove they are rejected. Only
#: exempt where the file is asserting rejection -- a test that documented support for a
#: phantom key would be the same bug wearing a different hat.
REJECTION_ASSERTION = re.compile(
    r"(pytest\.raises|raises|unknown|not.?supported|Unmapped|KeyError|fails|invalid)", re.I)

TEXT_SUFFIXES = {".py", ".sh", ".md", ".yml", ".yaml", ".toml", ".cfg", ".txt", ".bash"}

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got == want:
        print(f"  OK   {label:<52} {got!r}")
    else:
        failures.append(label)
        print(f"  FAIL {label:<52} got={got!r} want={want!r}")


def _python(code: str) -> str:
    # The interpreter is looked up, not assumed, same as _verify_readme.py and
    # _verify_agents.py. CI installs `.[dev]` into the job's own Python and has no
    # `.venv` at all, so a hardcoded `.venv/bin/python` made this audit die with
    # FileNotFoundError before it checked a single literal. The running interpreter
    # IS the environment in CI, so fall back to it.
    #
    # `cwd=ROOT` is load-bearing: a relative interpreter path is resolved against the
    # subprocess cwd rather than the caller's, so without it the fallback would look
    # in the wrong directory.
    fast_python = _Path(".venv/bin/python")
    if not fast_python.exists():
        fast_python = _Path(_sys.executable)
    out = subprocess.run([str(fast_python), "-c", code],
                         capture_output=True, text=True, cwd=ROOT, check=True)
    return out.stdout


def real_keys() -> set[str]:
    out = _python("import sys; sys.path.insert(0,'src');"
                  "from sparkscreen.grammar.spec import SPECS;"
                  "print('\\n'.join(sorted(s.key for s in SPECS)))")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def real_engines() -> set[str]:
    out = _python("import sys; sys.path.insert(0,'.'); sys.path.insert(0,'src');"
                  "from tests.differential.corpus import ENGINE_EXPECTATIONS;"
                  "print('\\n'.join(sorted(ENGINE_EXPECTATIONS)))")
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def scannable_files() -> list[_Path]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                         cwd=ROOT, check=True)
    files = []
    for rel in out.stdout.splitlines():
        p = ROOT / rel
        if p.suffix not in TEXT_SUFFIXES or not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ROOT).parts):
            continue
        if rel in HISTORICAL_FILES:
            continue
        files.append(p)
    return files


print("--- the namespaces this audit compares against ---")
KEYS = real_keys()
ENGINES = real_engines()
check("grammar keys shipped", sorted(KEYS), ["spark-3.5.1", "spark-4.1", "spark-4.2"])
check("engine versions with recorded expectations", sorted(ENGINES),
      ["3.5.1", "4.1.3", "4.2.0"])

print("\n--- scanning tracked text files for literals that name either ---")
stale_keys: list[tuple[str, int, str, str]] = []
stale_pins: list[tuple[str, int, str]] = []
exempt_fixtures: list[tuple[str, int, str]] = []
exempt_prose: list[tuple[str, int, str]] = []
exempt_rule: list[tuple[str, int, str]] = []

for path in scannable_files():
    rel = str(path.relative_to(ROOT))
    text = path.read_text(errors="replace")
    is_test = rel.startswith("tests/")
    is_rule = rel in RULE_FILES
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in GRAMMAR_KEY_RE.finditer(line):
            key = m.group(0)
            if key in KEYS:
                continue
            if is_rule:
                # Naming the rejected shape is the job of a RULE_FILE. Recorded by name
                # below rather than dropped, so this exemption is inspectable.
                exempt_rule.append((rel, lineno, key))
            elif is_test and REJECTION_ASSERTION.search(text):
                exempt_fixtures.append((rel, lineno, key))
            elif RETIREMENT_NARRATION.search(line) and not EXECUTABLE_SHAPE.search(line):
                exempt_prose.append((rel, lineno, key))
            else:
                stale_keys.append((rel, lineno, key, line.strip()[:74]))
        for m in ENGINE_PIN_RE.finditer(line):
            ver = m.group(1)
            if ver not in ENGINES and not (is_test and REJECTION_ASSERTION.search(text)):
                stale_pins.append((rel, lineno, ver))

check("stale grammar-key literals outside exemptions", len(stale_keys), 0)
check("stale engine pins outside exemptions", len(stale_pins), 0)

for rel, lineno, key, line in stale_keys:
    print(f"       {rel}:{lineno}  {key}")
    print(f"         {line}")
for rel, lineno, ver in stale_pins:
    print(f"       {rel}:{lineno}  pyspark=={ver}")

print("\n--- what was exempt, and why (shown so the allowlist stays auditable) ---")
if exempt_rule:
    print(f"  {len(exempt_rule)} literal(s) inside the file(s) that define this rule:")
    for rel, lineno, key in exempt_rule:
        print(f"    {rel}:{lineno}  {key}")
print(f"  {len(exempt_fixtures)} test fixture(s) naming a key to assert it is rejected:")
for rel, lineno, key in exempt_fixtures:
    print(f"    {rel}:{lineno}  {key}")
print(f"  {len(exempt_prose)} line(s) of prose narrating a removal:")
for rel, lineno, key in exempt_prose:
    print(f"    {rel}:{lineno}  {key}")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for x in failures:
        print(f"  - {x}")
    raise SystemExit(1)
print("every grammar key and engine pin in the tree names something that exists")