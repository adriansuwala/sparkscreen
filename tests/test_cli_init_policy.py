"""`--init-policy` serves the shipped default policy for a named grammar.

This is the command the shipped files advertise in their own header, so a broken
`--init-policy` is worse than a missing feature: the file points users at it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

import sparkscreen
from sparkscreen.policy import load_policy

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src"
POLICY = SRC / "sparkscreen" / "policies"


def _cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Same recipe as test_jsonc_policy_loading: the subprocess is a plain
    interpreter, so it does not inherit pytest's `pythonpath = ["src"]`."""
    import os
    env = dict(os.environ, PYTHONPATH=str(SRC))
    return subprocess.run(
        [sys.executable, "-m", "sparkscreen.cli", *args],
        capture_output=True, text=True, env=env, cwd=str(cwd or REPO_ROOT),
    )


def test_emits_the_shipped_file_verbatim_for_3_5_1():
    """The printed bytes ARE the committed file -- not a re-serialization of it.

    The comments are the editable documentation; if the command re-emitted the
    parsed JSON, users would receive a file that no longer explains itself, and
    nothing in the suite would notice.
    """
    shipped = (POLICY / "default-spark-3.5.1.jsonc").read_text()
    r = _cli("--init-policy", "--spark", "3.5.1")
    assert r.returncode == 0, r.stderr
    assert r.stdout == shipped
    assert "//" in r.stdout  # comments survived the trip


@pytest.mark.parametrize("version,key", [
    ("3.5.1", "spark-3.5.1"),
    ("spark-3.5.1", "spark-3.5.1"),
])
def test_accepts_bare_version_and_grammar_key(version, key):
    """The bare form (`3.5.1`) resolves through the same path `--spark` uses."""
    r = _cli("--init-policy", "--spark", version)
    assert r.returncode == 0, r.stderr
    assert r.stdout == (POLICY / f"default-{key}.jsonc").read_text()


def test_emitted_file_loads_and_round_trips_through_load_policy(tmp_path):
    """A fresh user's first action after `--init-policy` is to screen with it.

    The pipe must land as a policy the loader accepts, not merely as text that
    looks right. Same file on disk, then loaded: the failure this guards is
    "prints fine, loads broken" -- a trailing-comma or stripping bug upstream of
    the loader that the verbatim test above cannot see, because that test never
    parses what was printed.
    """
    out = tmp_path / "policy.jsonc"
    r = _cli("--init-policy", "--spark", "3.5.1")
    assert r.returncode == 0, r.stderr
    out.write_text(r.stdout)
    pol = load_policy(str(out))
    assert pol.name == "sparkscreen-defaults-3.5.1"
    assert pol.rules


def test_refuses_to_run_without_spark():
    """No silent default. The shipped files are per-grammar and their label sets
    differ; guessing one can hand a user a policy that misses statements on their
    engine -- and the misses fail lenient."""
    r = _cli("--init-policy")
    assert r.returncode == 2
    assert "--spark" in r.stderr
    assert r.stdout == ""


def test_unknown_grammar_is_rejected_before_any_file_lookup():
    """An invalid grammar never reaches the file lookup: the `--spark` resolver
    rejects it with the same message the screening path prints. The 'shipped
    files' listing in the helper is a defensive branch for a valid grammar that
    lacks a file -- unreachable while every pinned grammar has one, and worth
    keeping exactly so that a future grammar without a policy file fails loudly
    instead of handing users the wrong file."""
    r = _cli("--init-policy", "--spark", "9.9")
    assert r.returncode == 2
    assert "unknown Spark version or grammar '9.9'" in r.stderr
    assert r.stdout == ""


def test_helper_raises_for_a_grammar_with_no_shipped_file():
    """The defensive branch, tested directly so it is not dead on arrival."""
    from sparkscreen.cli import _default_policy_text
    with pytest.raises(FileNotFoundError):
        _default_policy_text("spark-9.9")


def test_rejects_a_path_argument():
    """`--init-policy code.py` must error, not screen code.py with the flag
    silently ignored."""
    code = tmp_path = Path("/opt/data/cache/scratch")
    target = code / "init_policy_arg_probe.py"
    target.write_text("spark.sql('DROP TABLE t')\n")
    r = _cli("--init-policy", "--spark", "3.5.1", str(target))
    assert r.returncode == 2
    assert "no path argument" in r.stderr
    # Nothing was screened: stdout is empty, not a report.
    assert "ALLOW" not in r.stdout and "DENY" not in r.stdout


def test_screen_mode_still_requires_a_path():
    """Making `path` optional for --init-policy must not have made it silently
    optional for screening."""
    r = _cli("--spark", "3.5.1")
    assert r.returncode == 2
    assert "path is required" in r.stderr
