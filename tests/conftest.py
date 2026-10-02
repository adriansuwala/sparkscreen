"""Pytest fixtures.

`spec_keys` parametrises every grammar-affecting test so both pinned grammars are
exercised; a test that only runs against the default grammar would not have caught the
case-insensitivity bug or the differing token numbering.
"""
import pytest

from sparkscreen.grammar.spec import SPECS

SPEC_KEYS = [s.key for s in SPECS]
SPARK_VERSIONS = sorted({v for s in SPECS for v in s.spark_versions})


@pytest.fixture(params=SPEC_KEYS)
def spec_key(request):
    """Each pinned grammar key, one test run apiece."""
    return request.param


@pytest.fixture(params=SPARK_VERSIONS)
def spark_version(request):
    return request.param