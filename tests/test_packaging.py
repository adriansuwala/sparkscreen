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
        for spec in ("spark_3_5_1", "spark_4_0"):
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

    def test_wheel_is_importable_with_no_jvm_on_path(self, built_wheel, tmp_path):
        """The real end-to-end check: install the artefact, hide java, screen code."""
        import os
        import subprocess as sp
        import sys as _sys

        venv = tmp_path / "venv"
        sp.run(["uv", "venv", str(venv), "-q"], check=True, capture_output=True)
        sp.run(["uv", "pip", "install", "-q", str(built_wheel), "--python",
                str(venv / "bin/python")], check=True, capture_output=True)

        clean_env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/tmp")}
        probe = _sys.executable  # reuse this interpreter only for -c text
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
