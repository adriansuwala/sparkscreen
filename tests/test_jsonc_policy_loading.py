"""`load_policy` routes `.jsonc` through the comment-stripping parser.

Separate from `test_jsonc.py` because the question here is not "is the scanner correct"
but "does anything actually call it". A perfectly good parser that no code path reaches is
the shape of bug that survives a green suite, so the tests below drive the loader through
the CLI rather than through `policy_from_dict`.

The load-bearing case is the last one: a `.json` file containing `//` must still be
rejected. Sniffing content instead of switching on the suffix would quietly accept a
broken `.json`, and a policy that loads when it should not is a fail-open.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sparkscreen import jsonc
from sparkscreen.model import Verdict
from sparkscreen.policy import load_policy

SRC = str(Path(__file__).resolve().parents[1] / "src")

#: A policy that exercises every field `policy_from_dict` reads, with comments and a
#: trailing comma so the file is only valid as JSONC.
POLICY_JSONC = """{
  // the name is what shows up in the summary line
  "name": "jsonc-loaded",

  /* namespace lists are independent checks, not alternatives */
  "writable_namespaces": ["prod.*",],
  "readable_namespaces": [],

  "rules": [
    {
      "id": "deny.drop",
      "verdict": "deny",
      "reason": "deny_rule",
      "message": "drops a table",
      "severity": "critical",
      "labels": ["DropTable"],
      // comment inside an array, where a naive stripper is most likely to break
      "target_patterns": ["prod.*"],
    },
    {
      "id": "allow.query",
      "verdict": "allow",
      "reason": "no_matching_rule",
      "message": "read-only",
      "severity": "info",
      "labels": ["StatementDefault"],
    },
  ],

  "limits": {
    "max_code_chars": 1234,
    "max_sql_chars": 10000,
    "max_statements": 200,
    "max_literals": 100,
    "max_targets": 100,
  },
}
"""


#: The same policy, but with URL-scheme values that are indistinguishable from comment
#: syntax. Written out in full rather than derived from `POLICY_JSONC` by string surgery:
#: a `.replace()` that silently fails to match leaves the test passing on the wrong input,
#: which is worse than no test. A dedicated literal also keeps the shipped default's own
#: value list visible here, since that is the file this hazard was found in.
SCHEME_LIKE_PREFIXES = """\
{
  "name": "scheme-like-prefixes",
  "rules": [
    {
      "id": "deny.create-location",
      "verdict": "deny",
      "reason": "deny_rule",
      "message": "creates a table outside the warehouse",
      "severity": "critical",
      "labels": ["CreateTable"],
      // every one of these opens with a comment marker
      "literal_prefixes": [
        "s3://",
        "s3a://",
        "hdfs://",
        "gs://",
        "file:/",
      ],
      "target_patterns": ["s3://", "hdfs://", "gs://"],
    },
  ],
}
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


class TestJsoncRouting:
    def test_a_commented_policy_loads_from_disk(self, tmp_path):
        pol = load_policy(str(_write(tmp_path, "p.jsonc", POLICY_JSONC)))
        assert pol.name == "jsonc-loaded"
        assert pol.writable_namespaces == ("prod.*",)
        assert pol.readable_namespaces == ()
        assert [r.id for r in pol.rules] == ["deny.drop", "allow.query"]
        assert pol.rules[0].verdict is Verdict.DENY
        assert pol.rules[0].target_patterns == ("prod.*",)
        assert pol.limits.max_code_chars == 1234

    def test_the_same_document_without_comments_is_identical(self, tmp_path):
        """Stability, not just tolerance: stripping must not alter the meaning.

        If the stripper blanked something inside a string value the two would differ, and
        the difference would be a rule that quietly matches nothing.
        """
        plain = json.dumps(jsonc.loads(POLICY_JSONC))
        a = load_policy(str(_write(tmp_path, "a.jsonc", POLICY_JSONC)))
        b = load_policy(str(_write(tmp_path, "b.json", plain)))
        assert a.name == b.name
        assert a.rules == b.rules
        assert a.limits == b.limits
        assert a.writable_namespaces == b.writable_namespaces

    def test_string_values_that_look_like_comments_survive(self, tmp_path):
        """The fail-open this whole module exists to prevent, at the loader boundary.

        A stripper that eats `"s3://"` produces a policy that loads cleanly, matches no
        path, and reports ALLOW. Asserted through `load_policy`, because a scanner unit
        test would not catch a loader that never called the scanner.
        """
        pol = load_policy(str(_write(tmp_path, "p.jsonc", SCHEME_LIKE_PREFIXES)))
        assert pol.rules[0].target_patterns == ("s3://", "hdfs://", "gs://")

    @pytest.mark.parametrize("suffix", [".json", ".JSON"])
    def test_plain_json_still_works_and_suffix_case_does_not_matter(self, tmp_path, suffix):
        plain = json.dumps({"name": "plain", "rules": []})
        pol = load_policy(str(_write(tmp_path, f"p{suffix}", plain)))
        assert pol.name == "plain"

    def test_a_json_file_containing_comments_is_still_rejected(self, tmp_path):
        """Switch on the suffix, not on a sniff of the content.

        If `load_policy` sniffed for `//` instead, this would pass and a genuinely broken
        `.json` would be accepted. A policy that loads when it should not is a fail-open,
        so the refusal is the property worth pinning.
        """
        path = _write(tmp_path, "broken.json", '{\n  // a comment\n  "name": "x"\n}')
        with pytest.raises(json.JSONDecodeError):
            load_policy(str(path))

    def test_malformed_jsonc_raises_a_value_error_the_cli_already_catches(self, tmp_path):
        """`JsoncError` is a `ValueError`, so the CLI prints `bad policy`, not a traceback."""
        path = _write(tmp_path, "bad.jsonc", '{"name": }')
        with pytest.raises(ValueError):
            load_policy(str(path))


class TestThroughTheCli:
    """The reason the change was made: `--policy file.jsonc` has to actually work."""

    def _run(self, tmp_path, policy_name, policy_text, code):
        _write(tmp_path, policy_name, policy_text)
        src = _write(tmp_path, "code.py", code)
        import os

        env = dict(os.environ, PYTHONPATH=SRC)
        return subprocess.run(
            [sys.executable, "-m", "sparkscreen.cli", "--policy",
             str(tmp_path / policy_name), "--spark", "3.5.1", str(src)],
            capture_output=True, text=True, env=env, cwd=str(Path(SRC).parent),
        )

    def test_cli_accepts_a_jsonc_policy(self, tmp_path):
        r = self._run(tmp_path, "p.jsonc", POLICY_JSONC,
                      "spark.sql('DROP TABLE prod.users')")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "DENY" in r.stdout
        assert "jsonc-loaded" in r.stdout

    def test_cli_reports_bad_jsonc_as_bad_policy_not_a_traceback(self, tmp_path):
        r = self._run(tmp_path, "bad.jsonc", '{"name": }', "spark.table('x')")
        assert "bad policy" in (r.stdout + r.stderr)
        assert "Traceback" not in r.stderr

    def test_shipped_default_policy_loads_through_the_cli(self, tmp_path):
        """The file that motivated the change, end to end.

        Guards against the shipped defaults drifting out of JSONC -- if a rule file were
        edited into something only strict JSON accepts, this fails here rather than in a
        user's terminal.
        """
        shipped = Path(SRC) / "sparkscreen" / "policies" / "default-spark-3.5.1.jsonc"
        if not shipped.exists():  # pragma: no cover - file is added in the same branch
            pytest.skip("shipped default policy not present")
        import os

        src = _write(tmp_path, "code.py", "spark.sql('DROP TABLE prod.users')")
        env = dict(os.environ, PYTHONPATH=SRC)
        r = subprocess.run(
            [sys.executable, "-m", "sparkscreen.cli", "--policy", str(shipped),
             "--spark", "3.5.1", str(src)],
            capture_output=True, text=True, env=env, cwd=str(Path(SRC).parent),
        )
        assert r.returncode == 1, r.stdout + r.stderr
        assert "DENY" in r.stdout
