#!/usr/bin/env python
"""Run the checks CI runs, locally, with the same code paths.

`ci.yml` used to inline the interesting steps as heredoc shell, so they could only be
run by a GitHub runner. Six behaviours existed in exactly one place -- the workflow --
and were therefore unreviewable, untestable locally, and impossible to reproduce when a
job went red. This moves them into `scripts/ci_checks.py`, which the workflow calls.

Nothing here is a reimplementation of a CI step: each function is the step's logic, and
`ci.yml` now runs these functions. One implementation, two callers.

Usage:
    scripts/ci_checks.py <check> [<check> ...]     # named checks
    scripts/ci_checks.py --list                   # what is available
    scripts/ci_checks.py --all                    # everything runnable here

Exit code is 0 only when every selected check passed, so `&&` and pre-commit both work.
"""
from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Engines the differential matrix covers. Kept here so a local run and the CI matrix
#: cannot disagree about what "supported" means; the test suite asserts this list matches
#: the grammar specs and the workflow's matrix.
SUPPORTED_ENGINES = ("3.5.1", "4.1.3", "4.2.0")

#: Graders that must be present on every PR.
AUDIT_SCRIPTS = ("_verify_docs.py", "_verify_effect.py", "_verify_readme.py",
                 "_verify_agents.py", "_verify_refs.py")


class CheckFailure(Exception):
    """A check ran and failed. Carries the reason for the report."""


def _python() -> str:
    """The interpreter to run checks with.

    Prefers the project venv when it exists, because the audits import from `src/` and a
    bare system python fails on a missing module -- which says nothing about the check.
    Falls back to the running interpreter, which is what CI has.
    """
    venv_py = ROOT / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else sys.executable


def _env_with_src(base: dict | None = None) -> dict:
    """A copy of `base` with the checkout's src/ importable.

    A checkout is not an installed package: `python -m sparkscreen...` fails with
    ModuleNotFoundError unless src/ is on the path. CI hides this by running
    `pip install -e .` first, so a local run without it would fail for a reason that has
    nothing to do with the check being run.

    Scoped to commands that run *in this checkout*. Anything probing an installed
    artifact must use a clean environment instead -- see `check_wheel_installs_and_screens`,
    where inheriting PYTHONPATH would let the checkout satisfy an import the wheel is
    supposed to satisfy, which is the exact claim under test.
    """
    env = dict(base if base is not None else os.environ)
    src = str(ROOT / "src")
    existing = env.get("PYTHONPATH", "")
    if src not in existing.split(os.pathsep):
        env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    return env


def _run(cmd: list[str], *, cwd: Path = ROOT, env: dict | None = None,
         what: str = "") -> subprocess.CompletedProcess:
    # A checkout is not an installed package: `python -m sparkscreen...` fails with
    # ModuleNotFoundError unless src/ is importable. CI hides this by running
    # `pip install -e .` first, so a local run without it would fail for a reason that
    # has nothing to do with the check. Put src/ on the path instead of requiring an
    # editable install -- it costs nothing and makes the entry point work in a bare tree.
    env = _env_with_src(env)

    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-25:]
        raise CheckFailure(
            f"{what or ' '.join(cmd)}\n  exit {proc.returncode}\n"
            + "\n".join(f"  | {ln}" for ln in tail)
        )
    return proc


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_pyspark_absent() -> str:
    """The fast suite must stay engine-free, or it is no longer the fast suite.

    `dev` must not pull in pyspark: a fast job that quietly grows a Spark dependency
    keeps its runtime and loses its point. This is a `pip install` claim, so it is
    checked rather than assumed -- the skill note about a step that names a module it
    never installed is the same failure one level up.
    """
    code = "import pyspark"  # noqa: F841
    proc = subprocess.run([_python(), "-c", code], capture_output=True, text=True)
    if proc.returncode == 0:
        raise CheckFailure(
            "pyspark is importable in the fast environment.\n"
            "  The fast suite is defined by having no engine; if pyspark is now a dev "
            "dependency, tests that importorskip it will silently stop skipping and the "
            "~20s suite becomes a Spark suite. Remove it from the `dev` extra, or move "
            "this check."
        )
    return "pyspark is absent from the fast environment (as required)"


def check_fast_suite() -> str:
    """The whole suite, run the way the fast job runs it: no engine, no JVM.

    The same invocation CI uses, so a local run is evidence about CI rather than a
    similar-looking test.
    """
    _run([_python(), "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider"],
         what="fast suite")
    return "fast suite passed with no engine present"


def check_audits() -> str:
    """Every documented claim, re-derived by the script that owns it.

    Five scripts, all of which assert things about the repo rather than about a
    fixture, so they are the only checks that catch prose drifting away from the code.

    `_verify_refs.py` is the one that catches literals: a grammar key or engine pin
    written into a file nothing executes. `setup.sh` shipped a verification step two
    releases after F17 removed one key, and the user guide told readers to pass a retired
    key in its headline example -- both invisible until a scan for grammar-key-shaped
    literals rather than a list of known-bad strings.
    """
    for script in AUDIT_SCRIPTS:
        path = ROOT / script
        if not path.exists():
            raise CheckFailure(f"audit script missing: {script}")
        _run([_python(), script], what=script)
    return f"all {len(AUDIT_SCRIPTS)} documentation audits passed"


def check_grammar_regenerates_clean() -> str:
    """Regenerating from the pinned grammars must reproduce exactly what is committed.

    The committed generated parsers are what ship in the wheel, so a pin edit that does
    not regenerate them leaves the package and the grammar disagreeing. Previously only
    CI could notice.

    Worth knowing what this does and does not catch, because both were verified by
    tampering rather than assumed. The build *wipes and regenerates* the generated tree
    before the diff, so:

    - A committed file whose content disagrees with the pin is repaired by the build and
      the check passes. That is the intended behaviour, not a gap: the question is
      "does regenerating reproduce what is committed", and it does.
    - A stray untracked file in the tree is deleted by the build, so the untracked check
      only fires on files the build did not clean. It is a backstop, not the main guard.
    - A pin that no longer resolves, or an upstream fetch that fails, does fail here --
      that is the case worth catching, and it is caught by the build's own exit code.
    """
    java_home = os.environ.get("JAVA_HOME") or os.environ.get("JRE_HOME")
    has_java = bool(java_home and (Path(java_home) / "bin" / "java").exists())
    if not has_java and shutil.which("java") is None:
        return "SKIPPED: no JVM (grammar regeneration needs one)"

    env = dict(os.environ)
    if java_home:
        env["PATH"] = f"{java_home}/bin" + os.pathsep + env.get("PATH", "")
    _run([_python(), "-m", "sparkscreen.grammar.build", "--fetch", "--generate"],
         env=env, what="regenerate parsers")

    # Scope the diff to the generated tree. A bare `git diff --exit-code` also reports
    # whatever else happens to be uncommitted -- an edited workflow, a work-in-progress
    # doc -- and then blames the grammar for it. This check is about generated code
    # tracking its pin, and nothing else.
    diff = subprocess.run(
        ["git", "diff", "--exit-code", "--stat", "--",
         "src/sparkscreen/grammar/generated", "src/sparkscreen/grammar/vendored"],
        cwd=ROOT, capture_output=True, text=True)
    if diff.returncode != 0:
        raise CheckFailure(
            "regenerating the parsers produced a diff in the generated grammar tree:\n"
            + "\n".join(f"  | {ln}" for ln in diff.stdout.strip().splitlines()[:25])
            + "\n  The committed generated code no longer matches the pinned grammars. "
              "Commit the regenerated output, or fix the pin."
        )
    # Untracked files in the same tree are equally a mismatch: a freshly generated parser
    # that was never committed ships nothing.
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "--",
         "src/sparkscreen/grammar/generated", "src/sparkscreen/grammar/vendored"],
        cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if untracked:
        raise CheckFailure(
            f"untracked files in the generated grammar tree:\n  | {untracked}\n"
            "  A parser that exists but is not committed will not ship in the wheel."
        )
    return "parsers regenerate byte-identically from the pinned grammars"


def check_wheel_contents(wheel: Path | None = None) -> str:
    """The wheel must ship parsers and no grammar sources.

    Generated parsers are needed at runtime and are committed on purpose, so they ship.
    The `.g4` sources and the ANTLR jar must not: the jar alone is ~2 MB per grammar and
    would put a JVM dependency in the install footprint of every user.
    """
    if wheel is None:
        with tempfile.TemporaryDirectory() as tmp:
            _run([_python(), "-m", "build", "--wheel", "--outdir", tmp], what="build wheel")
            built = sorted(glob.glob(f"{tmp}/*.whl"))
            if not built:
                raise CheckFailure("python -m build produced no wheel")
            return _audit_wheel(Path(built[0]))
    return _audit_wheel(wheel)


def _audit_wheel(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()

    leaked = [n for n in names if n.endswith(".g4")]
    if leaked:
        raise CheckFailure(f"grammar sources shipped in the wheel: {leaked[:5]}")
    jars = [n for n in names if n.endswith(".jar")]
    if jars:
        raise CheckFailure(f"ANTLR jar shipped in the wheel: {jars[:5]}")

    missing = [
        f"sparkscreen/grammar/generated/{spec}/{mod}"
        for spec in ("spark_3_5_1", "spark_4_1", "spark_4_2")
        for mod in ("SqlBaseLexer.py", "SqlBaseParser.py")
        if f"sparkscreen/grammar/generated/{spec}/{mod}" not in names
    ]
    if missing:
        raise CheckFailure(f"generated parsers missing from the wheel: {missing}")
    return f"wheel ships all three parsers, no .g4, no .jar ({len(names)} entries)"


def check_wheel_installs_and_screens(wheel: Path | None = None) -> str:
    """Install the wheel into a clean venv and screen with no JVM available.

    Two claims in one: the wheel is installable on its own, and the tool refuses to need
    a JVM at import or screening time. The second is why the empty-PATH trick below uses
    a freshly made directory -- `PATH=/usr/bin:/bin` still finds the runner's preinstalled
    JDK, so the assertion would prove nothing.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        if wheel is None:
            _run([_python(), "-m", "build", "--wheel", "--outdir", str(tmpdir)],
                 what="build wheel")
            wheel = Path(sorted(glob.glob(f"{tmpdir}/*.whl"))[0])

        venv_dir = tmpdir / "venv"
        venv.create(venv_dir, with_pip=True)
        vpy = venv_dir / "bin" / "python"
        # Deliberately NOT via _run(): _run puts the checkout's src/ on PYTHONPATH,
        # which is right for in-tree commands and wrong here. The point of this check is
        # that the WHEEL satisfies the import, so the installer must not be able to see
        # the checkout at all.
        proc_install = subprocess.run(
            [str(vpy), "-m", "pip", "install", "--quiet", str(wheel)],
            capture_output=True, text=True, cwd=ROOT)
        if proc_install.returncode != 0:
            raise CheckFailure(
                "installing the wheel into a clean venv failed:\n"
                + "\n".join(f"  | {ln}" for ln in
                            (proc_install.stdout + proc_install.stderr)
                            .strip().splitlines()[-15:])
            )

        empty_bin = tmpdir / "empty-bin"
        empty_bin.mkdir()
        # Built from scratch rather than copied from os.environ: an inherited PYTHONPATH
        # would let the checkout satisfy the import the freshly-installed wheel is
        # supposed to satisfy, which is the claim under test.
        clean_env = {"PATH": str(empty_bin), "HOME": str(tmpdir)}
        probe = (
            "import sparkscreen, sys\n"
            "r = sparkscreen.screen('import pyspark\\nspark.sql(\"SELECT 1\")\\n')\n"
            "print('verdict:', r.verdict.value)\n"
            "assert shutil_which('java') is None, 'java was reachable'\n"
            "print('screened with no JVM on PATH')\n"
        ).replace("shutil_which", "__import__('shutil').which")
        proc = subprocess.run([str(vpy), "-c", probe], capture_output=True, text=True,
                              env=clean_env)
        if proc.returncode != 0:
            raise CheckFailure(
                "screening from the installed wheel with no JVM failed:\n"
                + "\n".join(f"  | {ln}" for ln in
                            (proc.stdout + proc.stderr).strip().splitlines()[-15:])
            )
        return "wheel installs standalone and screens with no JVM on PATH"


def check_differential(engine: str | None = None,
                        interpreter: str | None = None) -> str:
    """Run the differential suite against one engine.

    `engine` defaults to whatever pyspark the given interpreter has. The matrix value is
    explicit in CI; locally the installed engine is the useful default.
    """
    py = interpreter or _python()
    if engine:
        proc = subprocess.run([py, "-c", "import pyspark; print(pyspark.__version__)"],
                              capture_output=True, text=True)
        found = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "?"
        if found != engine:
            raise CheckFailure(
                f"interpreter has pyspark {found}, not {engine}.\n"
                f"  Pass --interpreter pointing at a venv with pyspark=={engine}."
            )
    proc = subprocess.run([py, "-m", "pytest", "tests/differential/", "-q",
                           "-p", "no:cacheprovider"], capture_output=True, text=True)
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise CheckFailure(f"differential suite failed:\n" + "\n".join(
            f"  | {ln}" for ln in out.splitlines()[-25:]))

    # pytest exits 0 on an all-skipped run, which for an importorskip-gated suite means
    # "the engine never loaded". Count what was collected instead of trusting the exit.
    collect = subprocess.run([py, "-m", "pytest", "tests/differential/", "-q",
                              "-p", "no:cacheprovider", "--collect-only"],
                             capture_output=True, text=True)
    total = 0
    for line in collect.stdout.splitlines():
        if ".py: " in line and line.rsplit(": ", 1)[-1].isdigit():
            total += int(line.rsplit(": ", 1)[-1])
    if total < 90:
        raise CheckFailure(
            f"only {total} engine tests collected; the engine did not load, so nothing "
            f"was checked. pytest reported success because every module skipped."
        )
    # `-q` plus a redirect can leave the summary on stdout or stderr depending on the
    # version; search both rather than printing an empty line.
    summary = next((ln.strip() for ln in out.splitlines()
                    if "passed" in ln and ("skipped" in ln or "failed" in ln)), "")
    if not summary:
        summary = f"{total} engine tests collected"
    return f"differential vs pyspark {engine or 'installed'}: {summary}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CHECKS = {
    "no-pyspark": check_pyspark_absent,
    "fast": check_fast_suite,
    "audits": check_audits,
    "grammar-clean": check_grammar_regenerates_clean,
    "wheel-contents": check_wheel_contents,
    "wheel-install": check_wheel_installs_and_screens,
}

#: Checks that need something a bare checkout will not have. `all` runs the rest and
#: reports these as skipped rather than failing.
NEEDS_EXTERNAL = ("differential",)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("checks", nargs="*", help="check names, or --all / --list")
    ap.add_argument("--all", action="store_true", help="run every locally-satisfiable check")
    ap.add_argument("--list", action="store_true", help="list checks and exit")
    ap.add_argument("--engine", help="pyspark version for the differential check")
    ap.add_argument("--interpreter", help="python with pyspark installed (default: project venv)")
    args = ap.parse_args(argv)

    if args.list:
        for name, fn in CHECKS.items():
            print(f"  {name:<16} {(fn.__doc__ or '').strip().splitlines()[0]}")
        print(f"  {'differential':<16} run the differential suite (--engine, --interpreter)")
        return 0

    if args.all or not args.checks:
        selected = list(CHECKS)
        if args.engine or args.interpreter:
            selected.append("differential")
    else:
        selected = args.checks
        unknown = [c for c in selected if c not in CHECKS and c not in NEEDS_EXTERNAL]
        if unknown:
            ap.error(f"unknown check(s): {', '.join(unknown)}; try --list")

    failures, skipped = [], []
    for name in selected:
        label = f"{name}" + (f" [{args.engine}]" if name == "differential" and args.engine else "")
        print(f"── {label}", flush=True)
        try:
            if name == "differential":
                print(f"   {check_differential(args.engine, args.interpreter)}")
            else:
                print(f"   {CHECKS[name]()}")
        except CheckFailure as exc:
            failures.append((label, str(exc)))
            print(f"   FAILED\n{exc}", flush=True)
        else:
            continue

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:", flush=True)
        for label, _ in failures:
            print(f"  - {label}")
        return 1
    print(f"all {len(selected)} check(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())