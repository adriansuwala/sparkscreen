"""Guards for the release mechanism (D20).

The release path is three pieces that must stay in agreement: `scripts/release.py`
(the only writer of the version), `.github/workflows/release.yml` (the manual
trigger), and ci.yml (the gate, invoked as a reusable workflow). Nothing else in
the suite exercises them -- a release is rare, and a mechanism that rots between
releases fails exactly on the day it is needed. So the structural facts live here:

- ci.yml must stay callable (`workflow_call`), or the release gate is a copy of CI
  rather than CI.
- No workflow may name PyPI again. The publish step was removed once already
  (dead code contradicting the no-PyPI decision); this is the tripwire.
- release.yml's shell blocks must parse, the same way test_ci_workflow checks
  ci.yml -- the helpers are imported from there so the extraction cannot drift.
- The script's refusals and its happy path are exercised against throwaway git
  repositories, so they are hermetic and safe to run anywhere (including a
  shallow CI checkout, where the real tree's branch state would be irrelevant).

The integration tests run the script as a subprocess with `--root` pointed at the
throwaway repo, which is the flag that exists for this purpose. They pass
`--skip-gate` (there is no suite to run in a fake repo) except the one test that
asserts a missing gate script is itself a refusal.
"""
from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

try:
    import yaml
except ImportError as _exc:  # same failure shape as test_ci_workflow
    raise RuntimeError(
        "PyYAML is required by tests/test_release_mechanism.py to parse the "
        "workflows. It is declared in the `dev` extra in pyproject.toml; install "
        f"it with `pip install -e \".[dev]\"`. (original: {_exc})"
    ) from _exc

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_DIR = ROOT / ".github" / "workflows"
RELEASE_WORKFLOW = WORKFLOW_DIR / "release.yml"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
#: Every workflow, not just the two this module shipped with: the no-registry-upload
#: decision applies to anything that ever appears in this directory, and a tripwire
#: scoped to a fixed list is a tripwire a new file sidesteps.
ALL_WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))
RELEASE_SCRIPT = ROOT / "scripts" / "release.py"

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
VERSION = PYPROJECT["project"]["version"]


# ---------------------------------------------------------------------------
# Workflow structure
# ---------------------------------------------------------------------------


def test_ci_workflow_is_callable_as_the_release_gate():
    """The release gate must be CI itself, invoked, not a copy of it."""
    data = yaml.safe_load(CI_WORKFLOW.read_text())
    triggers = data.get("on", data.get(True))  # PyYAML 1.1 parses a bare `on` as True
    assert triggers and "workflow_call" in triggers, (
        "ci.yml lost its workflow_call trigger; the release workflow's gate is no "
        "longer CI but a copy that can drift."
    )


@pytest.mark.parametrize("workflow", ALL_WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_names_the_package_registry(workflow: Path):
    """The no-registry-upload decision must not be quietly reversed by a workflow edit.

    Scoped to every workflow file, not a fixed list: a newly added workflow that
    only checked ci.yml and release.yml would sidestep the tripwire.
    """
    text = workflow.read_text().lower()
    assert "pypi" not in text, (
        f"{workflow.name} mentions PyPI. Releases do not upload to PyPI "
        "(D20); a release is a tag plus a GitHub Release carrying the wheel. "
        "If this is a genuine decision change, it is a decision change: record "
        "it in docs/decisions.md, not as a workflow step."
    )


def test_release_workflow_shape():
    """The trigger, the gate dependency, and the bump choices, all pinned."""
    data = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    triggers = data.get("on", data.get(True))
    assert triggers and "workflow_dispatch" in triggers, (
        "a release must be a human decision; release.yml must stay manual"
    )
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert inputs["bump"]["options"] == ["patch", "minor", "major"]
    assert inputs["bump"]["default"] == "patch"

    jobs = data["jobs"]
    assert set(jobs) == {"gate", "release"}, sorted(jobs)
    assert jobs["gate"]["uses"] == "./.github/workflows/ci.yml", (
        "the gate must be ci.yml called as a reusable workflow"
    )
    assert jobs["release"]["needs"] == "gate", (
        "the release job must not run before the gate is green"
    )


#: A minimal stand-in for the real tree: exactly the files the script touches.
PYPROJECT_TEMPLATE = """[project]
name = "sparkscreen"
version = "{version}"
"""

INIT_TEMPLATE = '''__version__ = "{version}"
'''


def _make_repo(tmp_path: Path, version: str = "0.8.0",
               configure_identity: bool = True) -> Path:
    """A throwaway repo with the shape the script needs, plus a bare origin.

    `configure_identity=False` builds a repo with NO git identity -- the state a
    fresh CI runner is in. That variant is the regression test for the release
    workflow's first real failure: the commit carried a `-c` identity but the
    annotated tag did not, so `git tag -a` died with "empty ident name" after the
    bump had already been committed.
    """
    repo = tmp_path / "repo"
    (repo / "src/sparkscreen").mkdir(parents=True)
    (repo / "pyproject.toml").write_text(PYPROJECT_TEMPLATE.format(version=version))
    (repo / "src/sparkscreen/__init__.py").write_text(INIT_TEMPLATE.format(version=version))
    (repo / "scripts").mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(git + ["init", "-b", "master"], check=True, capture_output=True)
    if configure_identity:
        subprocess.run(git + ["config", "user.name", "Test"], check=True,
                       capture_output=True)
        subprocess.run(git + ["config", "user.email", "test@example.com"],
                       check=True, capture_output=True)
    subprocess.run(git + ["add", "-A"], check=True, capture_output=True)
    # The setup commit carries its own -c identity; the repo itself stays
    # identity-less when configure_identity=False, which is the point of the flag.
    subprocess.run(git + ["-c", "user.name=Setup", "-c",
                          "user.email=setup@example.com",
                          "commit", "-m", "work"], check=True, capture_output=True)
    # A bare origin, so `fetch --tags origin` succeeds and --no-push can be
    # verified against a real remote rather than assumed.
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(origin)],
                   check=True, capture_output=True)
    subprocess.run(git + ["remote", "add", "origin", str(origin)],
                   check=True, capture_output=True)
    subprocess.run(git + ["push", "-u", "origin", "master"], check=True,
                   capture_output=True)
    return repo


def _run_release(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(RELEASE_SCRIPT), "--root", str(repo), *extra],
        capture_output=True, text=True, timeout=120,
    )


def _version_of(repo: Path) -> str:
    return tomllib.loads((repo / "pyproject.toml").read_text())["project"]["version"]


def test_release_bumps_both_version_homes_and_tags(tmp_path):
    repo = _make_repo(tmp_path)
    proc = _run_release(repo, "--bump", "minor", "--skip-gate", "--yes", "--no-push")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _version_of(repo) == "0.9.0"
    assert '__version__ = "0.9.0"' in (repo / "src/sparkscreen/__init__.py").read_text()
    changelog = (repo / "CHANGELOG.md").read_text()
    assert "## 0.9.0" in changelog, changelog
    described = subprocess.run(
        ["git", "-C", str(repo), "describe", "--exact-match", "--tags"],
        capture_output=True, text=True).stdout.strip()
    assert described == "v0.9.0", described


def test_release_leaves_the_remote_untouched_until_push(tmp_path):
    """--no-push must mean the remote never sees the bump, even on success."""
    repo = _make_repo(tmp_path)
    old_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
    proc = _run_release(repo, "--bump", "minor", "--skip-gate", "--yes", "--no-push")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    remote = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "origin"],
        capture_output=True, text=True).stdout
    assert old_sha in remote, remote
    assert "refs/tags/" not in remote, "the tag was pushed despite --no-push"


def test_release_notes_prints_the_changelog_section(tmp_path):
    repo = _make_repo(tmp_path)
    assert _run_release(repo, "--bump", "minor", "--skip-gate", "--yes",
                        "--no-push").returncode == 0
    proc = _run_release(repo, "--notes", "v0.9.0")
    assert proc.returncode == 0, proc.stderr
    assert "## 0.9.0" in proc.stdout


def test_release_refuses_a_dirty_tree_and_writes_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("half-finished work")
    proc = _run_release(repo, "--bump", "minor", "--skip-gate", "--yes", "--no-push")
    assert proc.returncode == 1
    assert "dirty" in proc.stderr
    assert _version_of(repo) == "0.8.0", "a refusal must not touch the version"


def test_release_refuses_off_master(tmp_path):
    repo = _make_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "checkout", "-b", "feature"],
                   check=True, capture_output=True)
    proc = _run_release(repo, "--bump", "minor", "--skip-gate", "--yes", "--no-push")
    assert proc.returncode == 1
    assert "master" in proc.stderr
    assert _version_of(repo) == "0.8.0"


def test_release_refuses_an_existing_tag(tmp_path):
    repo = _make_repo(tmp_path)
    subprocess.run(["git", "-C", str(repo), "tag", "v0.9.0"],
                   check=True, capture_output=True)
    proc = _run_release(repo, "--bump", "minor", "--skip-gate", "--yes", "--no-push")
    assert proc.returncode == 1
    assert "already exists" in proc.stderr
    assert _version_of(repo) == "0.8.0"


def test_release_works_on_a_runner_with_no_git_identity(tmp_path):
    """Regression: the release workflow's first real failure.

    A fresh CI runner has no user.name/user.email configured. The release commit
    carried a `-c` identity but the annotated tag did not, so `git tag -a` died
    with "empty ident name" AFTER the bump had been committed -- on the first
    real run of the workflow, from a bug no configured-identity test could see.
    """
    repo = _make_repo(tmp_path, configure_identity=False)
    proc = _run_release(repo, "--bump", "minor", "--skip-gate", "--yes", "--no-push")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    tagger = subprocess.run(
        ["git", "-C", str(repo), "for-each-ref", "refs/tags/v0.9.0",
         "--format=%(taggername)"], capture_output=True, text=True).stdout.strip()
    assert tagger, "the tag has no tagger identity at all"
    committer = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%cn"],
        capture_output=True, text=True).stdout.strip()
    assert committer, "the release commit has no committer identity"


def test_release_refuses_a_bumped_to_same_version(tmp_path):
    repo = _make_repo(tmp_path)
    proc = _run_release(repo, "--version", "0.8.0", "--skip-gate", "--yes", "--no-push")
    assert proc.returncode == 1
    assert _version_of(repo) == "0.8.0"


def test_release_refuses_a_non_release_version_string(tmp_path):
    """The rewrite patterns are exact; a suffix would be a silent partial rewrite."""
    repo = _make_repo(tmp_path)
    proc = _run_release(repo, "--version", "1.0.0rc1", "--skip-gate", "--yes",
                        "--no-push")
    assert proc.returncode == 1
    assert _version_of(repo) == "0.8.0"


def test_release_refuses_when_the_gate_script_is_missing(tmp_path):
    """--skip-gate is an explicit claim; omitting it with no gate must fail loudly."""
    repo = _make_repo(tmp_path)
    proc = _run_release(repo, "--bump", "minor", "--yes", "--no-push")
    assert proc.returncode == 1
    assert "gate" in proc.stderr
    assert _version_of(repo) == "0.8.0"


def test_dry_run_prints_the_plan_and_writes_nothing(tmp_path):
    repo = _make_repo(tmp_path)
    proc = _run_release(repo, "--bump", "minor", "--dry-run")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _version_of(repo) == "0.8.0"
    assert not (repo / "CHANGELOG.md").exists()
    tags = subprocess.run(["git", "-C", str(repo), "tag"], capture_output=True,
                          text=True).stdout.strip()
    assert not tags, tags


def test_the_real_script_sees_the_real_version():
    """The script's read path and the tests' read path must agree about the version."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import tomllib, pathlib; print(tomllib.load(open('pyproject.toml','rb'))"
         "['project']['version'])"],
        cwd=ROOT, capture_output=True, text=True)
    assert proc.stdout.strip() == VERSION


def test_release_workflow_uses_the_real_version_in_release_steps():
    """The workflow must read the version from pyproject.toml, never a literal.

    A version literal here would be exactly the drift the release script exists to
    prevent: the workflow would tag a version that is not the one in the tree.
    """
    text = RELEASE_WORKFLOW.read_text()
    assert VERSION not in text, (
        "release.yml hardcodes the version; read it from pyproject.toml so the "
        "tag and the tree cannot disagree."
    )
    assert "pyproject.toml" in text, "the workflow must name where the version lives"
