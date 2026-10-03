"""Shared test helpers.

Not a package: `tests/` has no `__init__.py`, and pytest's default import mode puts the
test file's own directory on `sys.path`, so `import _helpers` works from any test module.
That is why this exists rather than a function in `conftest.py` -- conftest symbols are
injected as fixtures, not as plain names, and referencing one from a test raises
`NameError` at collection time.
"""
from pathlib import Path


def is_source_checkout(root: Path | str) -> bool:
    """True if `root` is a git checkout, not a copy of the source tree.

    Several tests assert things about *the repository* rather than about the code: that
    the ANTLR jar is gitignored, that `egg-info` is untracked. Those facts live in
    `.gitignore` and the git index, neither of which exists in a copied tree.

    This matters for mutation testing. `mutmut` copies `src/`, `tests/` and
    `pyproject.toml` into `mutants/` and runs the suite there; four of those tests failed
    in that directory with nothing actually wrong. Skipping is the honest response -- the
    assertion is still real, it simply has nothing to assert against outside a checkout.
    """
    root = Path(root)
    return (root / ".gitignore").exists() and (root / ".git").exists()