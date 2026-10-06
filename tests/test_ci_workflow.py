"""Every `run:` block in the CI workflow must be valid shell.

F25. The `docs` job died with

    /home/runner/work/_temp/a955c4fc-....sh: line 10: syntax error near
    unexpected token `}'

which is GitHub Actions' own generated script, not anything in this repository. The step
had

    if grep -qE '...' /tmp/usage.txt; then
      ...
    }

A brace is not `fi`. Two mistakes of the same shape sat in one block: the `|| { ... }`
above it needed a command before the brace, and the `if` needed `fi`. Bash rejected the
whole step at parse time, so the guard it was written to provide never ran.

**Why nothing caught it.** The step is shell embedded as a YAML string, so `bash -n` on
the repository's own scripts does not see it, and the file is not executable, so nothing
runs it locally. This test is the only thing that ever parses it.

The extraction is the load-bearing part, and it is deliberately dumb: the YAML is parsed
with `yaml.safe_load`, every `run:` string is written to a file verbatim, and `bash -n`
parses it. GitHub wraps the block in a generated script whose default shell is
`bash --noprofile --norc -eo pipefail {0}`, so `bash -n` is the same parser that rejected
the real step.

`${{ ... }}` expressions are substituted with a placeholder first. They are not shell,
they would confuse the parser, and a substitution cannot hide a brace mistake -- the
construct being checked is the surrounding control flow.

**The `importorskip` here is deliberate, and it is also the trap this file was nearly
born into.** PyYAML is in the `dev` extra, so the import succeeds on a runner. But a
skip is not a pass: the first version of this file used `pytest.importorskip("yaml")`
against an environment that never installed it, so the guard skipped in *both* venvs and
in CI -- a check that examined zero steps and reported SUCCESS, which is precisely the
outcome it exists to prevent. So the skip below fails the module rather than passing it
quietly: if PyYAML ever goes missing again, the suite goes red instead of blind. See
`test_this_module_will_not_skip_itself_unchecked`, which asserts the dependency is
present, and F25 in docs/findings.md.

This asserts the *steps* parse. It does not assert they do the right thing; that is what
running them is for, and the differential and wheel checks cover their own steps by
executing ci_checks.py rather than by parsing YAML.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

try:
    import yaml
except ImportError as _exc:  # pragma: no cover - depends on the environment
    # A bare `import yaml` is the right failure here: the tests in this module are the
    # only thing that parses the workflow's shell, so a missing parser means the guard
    # is not running at all. Importing it directly makes that a collection error rather
    # than a skip, which a runner would report as a green job that verified nothing.
    raise RuntimeError(
        "PyYAML is required by tests/test_ci_workflow.py, which is the only guard that "
        "parses the CI workflow's run: blocks as shell. It is declared in the `dev` "
        "extra in pyproject.toml; install it with `pip install -e \".[dev]\"`. This is "
        "raised instead of pytest.importorskip() on purpose -- a skip here means the "
        "workflow's shell is never checked, which is the exact failure this module "
        f"exists to catch. (original: {_exc})"
    ) from _exc

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
#: Every workflow's run: blocks get the same bash -n treatment. F25's lesson applies
#: per file: shell embedded in ANY YAML file is seen by nothing else in this repo, so
#: a new workflow (e.g. fuzz-deep.yml) must not have to opt in to being checked.
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _run_blocks(text: str) -> list[tuple[str, str]]:
    """Every `run:` value in the workflow, with a path to identify each one."""
    data = yaml.safe_load(text)
    found: list[tuple[str, str]] = []

    def walk(node: object, trail: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "run" and isinstance(value, str):
                    found.append((trail or "<workflow>", value))
                else:
                    walk(value, f"{trail}.{key}" if trail else str(key))
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{trail}[{i}]")

    walk(data, "")
    return found


def _as_shell(block: str) -> str:
    """Neutralise GitHub expressions so `bash -n` sees only shell.

    An expression is not shell and can contain braces that mean nothing to bash. The
    control flow around it is what this test is checking, and substituting the
    expression cannot hide an unbalanced brace in the surrounding block.
    """
    out = []
    i = 0
    while i < len(block):
        if block.startswith("${{", i):
            end = block.find("}}", i)
            if end == -1:
                out.append("__UNCLOSED_EXPRESSION__")
                break
            out.append("__GHA_EXPR__")
            i = end + 2
        else:
            out.append(block[i])
            i += 1
    return "".join(out)


BLOCKS = [
    (f"{wf.name}:{trail}", block)
    for wf in WORKFLOWS
    for trail, block in _run_blocks(wf.read_text())
] if WORKFLOWS else []


def test_the_workflow_has_run_blocks_to_check():
    """A guard that silently checks nothing is the failure mode it exists to prevent.

    This is the assertion that would have caught the guard itself being wrong: if the
    extraction ever stopped matching the workflow's shape, the parse test below would
    pass having examined zero steps.
    """
    assert WORKFLOW.exists(), f"{WORKFLOW} is missing; the parse guard cannot run"
    assert len(WORKFLOWS) >= 2, (
        f"expected more than one workflow under .github/workflows/, found "
        f"{[wf.name for wf in WORKFLOWS]}; a new workflow must not be unchecked"
    )
    assert len(BLOCKS) >= 5, f"expected the CI steps, found only {len(BLOCKS)}: {BLOCKS}"
    # The two jobs that were broken, so a rename cannot quietly drop them from the count.
    joined = "\n".join(block for _, block in BLOCKS)
    assert "setup.sh --help" in joined, "the setup.sh --help guard is no longer checked"
    assert "ci_checks.py audits" in joined, "the docs audits are no longer checked"


@pytest.mark.parametrize(
    "trail,block",
    BLOCKS,
    ids=[trail for trail, _ in BLOCKS],
)
def test_every_run_block_is_valid_shell(trail: str, block: str, tmp_path: Path):
    """`bash -n` each step the way the runner's own generated script would.

    Non-zero exit means the runner would reject the step before running a single
    command in it, so the step's checks never happen and the job fails with a parse
    error instead of a diagnosis.
    """
    script = tmp_path / "step.sh"
    script.write_text(_as_shell(block))
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-n", str(script)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"{trail}: the run block is not valid shell, so GitHub Actions will reject the "
        f"step before executing any of it.\n"
        f"bash said: {proc.stderr.strip()}\n"
        f"block was:\n{_as_shell(block)}"
    )


def test_this_module_will_not_skip_itself_unchecked():
    """A guard that skipped would be the failure it exists to catch.

    The module raises at import if PyYAML is absent, so reaching this test already proves
    `yaml` imported. Asserting the rest closes the other direction: if the extraction
    ever returns nothing (a renamed `run:` key, a workflow moved to a different path),
    the parametrised test below passes having examined zero steps. This is the same
    vacuous-success shape as the F24 `.venv` bug and the differential `importorskip`
    guard, and it is worth naming rather than assuming pytest cannot do it.
    """
    assert BLOCKS, (
        "no run: blocks were extracted from the workflow, so the shell-parsing guard "
        "would pass without checking a single step"
    )


def test_the_workflow_parses_as_yaml():
    """Separate from the shell check, and earlier: a YAML error reads far worse.

    Every workflow file, not just ci.yml: a new workflow that fails to parse would
    otherwise be discovered by Actions, not by this suite.
    """
    for wf in WORKFLOWS:
        data = yaml.safe_load(wf.read_text())
        assert isinstance(data, dict), f"{wf.name}: the workflow is not a YAML mapping"
        assert "jobs" in data, f"{wf.name}: the workflow declares no jobs"
    data = yaml.safe_load(WORKFLOW.read_text())
    for job in ("fast", "wheel", "differential", "docs", "grammar-build"):
        assert job in data["jobs"], f"the {job} job is missing from the workflow"


def test_python_is_the_interpreter_ci_uses():
    """The audits must run under the job's own Python, not a hardcoded `.venv`.

    F24: `_verify_refs.py` shelled out to `ROOT/.venv/bin/python`, which does not exist
    on a runner, and the `docs` job died before checking a single literal. The workflow
    installs `.[dev]` into the job's own interpreter and never creates `.venv`, so any
    step naming `.venv/bin/python` is a step that cannot work here.

    This is a cheap tripwire over the workflow's own text rather than a runtime check --
    the runtime check is the CI-shaped tree, which cannot be built from inside a runner
    that does have a `.venv`.
    """
    text_by_file = {wf.name: wf.read_text() for wf in WORKFLOWS}
    for name, text in text_by_file.items():
        assert ".venv/bin/python" not in text, (
            f"{name}: the workflow names .venv/bin/python, but no CI job creates a "
            ".venv; CI installs \".[dev]\" into the job's own interpreter"
        )
    # And the shape that would reintroduce it: a run step invoking an audit directly
    # rather than through ci_checks.py, which does the interpreter lookup itself.
    for trail, block in BLOCKS:
        assert "_verify_" not in block, (
            f"{trail}: runs a _verify_*.py audit directly. Route it through "
            f"scripts/ci_checks.py so the interpreter lookup stays in one place."
        )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))