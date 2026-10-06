"""Version and packaging invariants.

These are the checks that stop a release from shipping a lie. A screener that reports
`__version__ == "1.0"` when the code is pre-release is exactly the kind of quiet
overclaim this project exists to avoid, applied to itself.

No JVM required.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import sparkscreen

from _helpers import is_source_checkout

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())


class TestVersionAgreement:
    def test_declared_version_matches_the_fallback(self):
        """pyproject.toml and __init__.py must not disagree.

        `__version__` reads packaging metadata when installed, so the literal in
        `__init__.py` is only the source-checkout fallback -- but that fallback is what
        a contributor running from a clone sees, and it silently drifted once already.
        """
        src = (ROOT / "src/sparkscreen/__init__.py").read_text()
        fallback = re.search(r'__version__ = "([^"]+)"', src)
        assert fallback, "the source-checkout fallback version is missing"
        assert fallback.group(1) == PYPROJECT["project"]["version"]

    def test_reported_version_is_a_real_version(self):
        assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-.]?[A-Za-z0-9.]+)?",
                            sparkscreen.__version__), sparkscreen.__version__

    def test_egg_info_is_gitignored(self):
        if not is_source_checkout(ROOT):
            pytest.skip(f"not a source checkout: {ROOT}")
        ignore = (ROOT / ".gitignore").read_text()
        assert "*.egg-info/" in ignore
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout
        assert "egg-info" not in tracked, (
            "egg-info is committed. It is a build artefact that changes on every build "
            "and shadows the declared version via importlib.metadata."
        )

    def test_egg_info_is_a_build_artefact_not_a_source_file(self):
        """The stale-egg-info check above is about *committed* state, not local state.

        `python -m build` writes `src/sparkscreen.egg-info` as a side effect, so a test
        that merely asserts the directory is absent would fail on any machine that has
        ever built a wheel -- including CI, which builds one for every other test here.
        That is a test that cannot pass, which is worse than no test.

        So the guarantee is two-part and both parts are checked: the artefact is
        gitignored and untracked (above), and when the package *is* installed, the
        version it reports is the declared one (below). The local directory existing is
        fine; it existing *and being committed* is the bug.
        """
        check_git_clean = subprocess.run(
            ["git", "status", "--porcelain", "src/"], cwd=ROOT,
            capture_output=True, text=True).stdout
        assert "egg-info" not in check_git_clean, (
            f"egg-info shows as untracked/modified, so it is not ignored:\\n{check_git_clean}"
        )


class TestPreReleaseStance:
    def test_version_notes_state_the_stability_contract(self):
        """The claim "not yet 1.0" must be in the code, not just in a commit message."""
        assert sparkscreen.VERSION_NOTES
        assert "exit codes" in sparkscreen.VERSION_NOTES.lower()

    def test_version_notes_are_exported(self):
        assert "VERSION_NOTES" in sparkscreen.__all__


# Module-scoped, not class-scoped. A class-scoped fixture that calls pytest.skip() (or
# that raises during teardown) trips an assertion inside pytest's own fixture machinery
# when the suite's autouse fixtures finalise, and the resulting error is about pytest
# internals rather than about packaging. Module scope avoids the interaction entirely.
@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory):
    """Build the wheel once. Skipped rather than failed if the toolchain is absent."""
    out = tmp_path_factory.mktemp("wheel")
    try:
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(out)],
            cwd=ROOT, check=True, capture_output=True, timeout=900,
        )
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot build a wheel here: {exc}")
    wheels = list(out.glob("*.whl"))
    assert wheels, "build produced no wheel"
    return wheels[0]


class TestWheelContents:
    """The wheel must carry the generated parsers, or runtime users need a JVM."""

    def test_wheel_carries_both_generated_parsers(self, built_wheel):
        import zipfile

        names = zipfile.ZipFile(built_wheel).namelist()
        for spec in ("spark_3_5_1", "spark_4_2"):
            lexer = f"sparkscreen/grammar/generated/{spec}/SqlBaseLexer.py"
            parser = f"sparkscreen/grammar/generated/{spec}/SqlBaseParser.py"
            assert lexer in names, f"{lexer} missing from the wheel"
            assert parser in names, f"{parser} missing from the wheel"

    def test_wheel_contains_no_grammar_grammars_or_antlr_tooling(self, built_wheel):
        """The vendored `.g4` sources and the ANTLR jar must not ship.

        They are maintainer inputs: ~1 MB of grammar text and a multi-megabyte jar that
        no runtime user needs. Shipping them would make the wheel an order of magnitude
        larger for no benefit.
        """
        import zipfile

        names = zipfile.ZipFile(built_wheel).namelist()
        assert not [n for n in names if n.endswith(".g4")], "grammar sources leaked"
        assert not [n for n in names if n.endswith(".jar")], "ANTLR jar leaked"

    def test_wheel_ships_every_default_policy_the_checkout_has(self, built_wheel):
        """`--init-policy` reads default policies from the installed package.

        A wheel without them breaks the documented starting point for every user who
        does not copy out of the repo -- and would do so silently, since the failure
        surfaces only when someone runs `--init-policy`. The expectation is derived from
        the checkout rather than hardcoded, so adding a policy file for a new pin needs
        no edit here; this test then verifies the new file actually ships.
        """
        import zipfile

        src_policies = ROOT / "src/sparkscreen/policies"
        if not src_policies.is_dir():
            pytest.skip("no policies directory in this checkout")
        expected = sorted(p.name for p in src_policies.glob("default-*.jsonc"))
        assert expected, "checkout ships no default policies; nothing to verify"

        names = zipfile.ZipFile(built_wheel).namelist()
        shipped = {n.rsplit("/", 1)[-1] for n in names
                   if n.startswith("sparkscreen/policies/") and n.endswith(".jsonc")}
        missing = [n for n in expected if n not in shipped]
        assert not missing, f"default policies missing from the wheel: {missing}"

    def test_wheel_is_importable_with_no_jvm_on_path(self, built_wheel, tmp_path):
        """The real end-to-end check: install the artefact, hide java, screen code."""
        import os
        import subprocess as sp
        import sys as _sys

        # During a mutation run this whole directory IS mutmut's instrumented copy: every
        # file in src/ has had `from mutmut.mutation.trampoline import ...` spliced into
        # it. Building a wheel from here bakes that import into the artefact, and the
        # freshly-created venv then dies with `No module named 'mutmut'`.
        #
        # The assertion is still worth having, but not from an instrumented tree -- it
        # would be testing the trampoline, not the wheel.
        if not is_source_checkout(ROOT):
            pytest.skip("mutation run: src/ is instrumented, so this wheel is not the real one")

        venv = tmp_path / "venv"
        # stdlib venv, not `uv`. `uv` is a local convenience and is not installed on the
        # CI runner, so shelling out to it failed with FileNotFoundError: 'uv' before the
        # wheel was ever installed -- a test that could not run where it matters most.
        # `python -m venv` is everywhere Python is.
        # Both subprocesses run with PYTHONPATH stripped. The venv interpreter would
        # otherwise import sparkscreen from the CHECKOUT rather than from the wheel, and
        # the test would pass while proving nothing about the artefact. pyproject sets
        # `pythonpath = ["src"]` for pytest and anything wrapping this does the same, so
        # this is the normal state of the world, not an exotic one.
        install_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        sp.run([_sys.executable, "-m", "venv", str(venv)], check=True,
               capture_output=True, env=install_env)
        sp.run([str(venv / "bin/python"), "-m", "pip", "install", "-q",
                str(built_wheel)], check=True, capture_output=True, env=install_env)

        # PATH is an EMPTY directory, not /usr/bin:/bin. GitHub's ubuntu runners
        # preinstall a JDK at /usr/bin/java, so a PATH that keeps the usual system
        # directories finds java and the `which('java') is None` assertion below fails --
        # or, worse, a probe that tolerated java would prove nothing about the no-JVM
        # path. Same reasoning as the `wheel` job in ci.yml, which uses /tmp/empty-bin.
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir()
        # PYTHONPATH must be dropped, not merely unset. The parent process may have src/
        # on it (pyproject sets `pythonpath = ["src"]` for pytest, and any wrapper that
        # exports it does the same), and the venv interpreter below would then import
        # sparkscreen from the CHECKOUT instead of from the wheel just installed -- which
        # is the one thing this test must not do. Verified: with PYTHONPATH pointing at
        # src/ this test fails with ModuleNotFoundError, because the checkout's imports
        # drag in packages the bare venv does not have.
        clean_env = {"PATH": str(empty_bin), "HOME": os.environ.get("HOME", "/tmp")}
        clean_env.pop("PYTHONPATH", None)
        code = (
            "import shutil;"
            "assert shutil.which('java') is None, 'java leaked onto PATH';"
            "from sparkscreen import screen;"
            "assert screen('spark.sql(\"DROP TABLE prod.t\")').verdict.value == 'deny';"
            "assert screen('df.write.mode(\"overwrite\").saveAsTable(\"prod.t\")')"
            ".verdict.value == 'deny';"
            "assert screen('spark.sql(\"delete from t where id=1\")')"
            ".verdict.value == 'review';"
            "print('ok')"
        )
        result = sp.run([str(venv / "bin/python"), "-c", code],
                        capture_output=True, text=True, env=clean_env, timeout=300)
        assert result.returncode == 0, (
            f"wheel failed with java absent from PATH:\n{result.stdout}\n{result.stderr}"
        )
        assert "ok" in result.stdout
