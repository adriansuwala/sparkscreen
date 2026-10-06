"""JSONC parsing, and the shape of the comment-stripper.

Two things are being guarded here, and they are not the same thing.

1. Correctness of the scanner. The default policy files contain string values that look
   exactly like comment syntax -- `"s3://"`, `"hdfs://"` -- and a stripper that treats
   those as comments produces a policy that loads cleanly, matches nothing, and reports
   ALLOW. That is a fail-open with a confident voice, which is the failure mode this
   whole project exists to prevent, so it gets an explicit test per construct.

2. The decision to handroll rather than depend on json5. That is a judgement call, and a
   judgement call that is only recorded in a commit message is not checkable later. The
   oracle test at the bottom runs json5 against the same corpus, so if a future json5
   disagrees -- or if json5 is dropped from the dev extra and this test silently stops
   running -- the reason is visible rather than folklore.

No JVM required.
"""

from __future__ import annotations

import json

import pytest

from sparkscreen import jsonc
from sparkscreen.jsonc import JsoncError


class TestCommentSyntax:
    def test_line_comment_is_removed(self):
        assert jsonc.loads('{\n // hi\n "a": 1\n}') == {"a": 1}

    def test_block_comment_is_removed(self):
        assert jsonc.loads('{ /* note */ "a": 1 }') == {"a": 1}

    def test_multiline_block_comment_is_removed(self):
        """The licence-header shape, which every generated file wants to have."""
        assert jsonc.loads('{ /* one\ntwo\nthree */ "a": 1 }') == {"a": 1}


class TestStringsThatLookLikeComments:
    """The reason this module exists rather than a regex.

    Each of these is a value the shipped default policies actually contain or plausibly
    could. A stripper that gets any of them wrong still returns valid JSON, so nothing
    downstream complains -- the rule simply stops matching.
    """

    def test_url_scheme_prefix_survives(self):
        """`literal_prefixes: ["s3://"]` is in the shipped default policy.

        This is the single most important assertion in the file. `s3://` after a `"` is a
        string; the same two characters outside a string start a comment, and the only
        thing distinguishing them is the string state the scanner tracks.
        """
        assert jsonc.loads('{"literal_prefixes": ["s3://", "hdfs://"]}') == {
            "literal_prefixes": ["s3://", "hdfs://"]
        }

    @pytest.mark.parametrize("value", ["s3://", "hdfs://", "gs://", "abfss://", "wasbs://"])
    def test_every_shipped_external_prefix_survives(self, value):
        assert jsonc.loads(json.dumps([value])) == [value]

    def test_double_slash_inside_a_string_is_not_a_comment(self):
        assert jsonc.loads('{"m": "a // b"}') == {"m": "a // b"}

    def test_block_comment_markers_inside_a_string_are_not_comments(self):
        assert jsonc.loads('{"m": "a /* b */ c"}') == {"m": "a /* b */ c"}

    def test_escaped_quote_does_not_end_the_string(self):
        r"""A `"` preceded by `\` is data. Getting this wrong truncates the string early
        and then treats the rest of the file as code, which is how a policy ends up
        half-parsed rather than rejected."""
        assert jsonc.loads(r'{"m": "she said \"hi\""}') == {"m": 'she said "hi"'}

    def test_trailing_backslash_before_the_closing_quote(self):
        r"""The nastiest case: a string ending in an escaped backslash. `\\` then `"` --
        a stripper that handles `\"` but not `\\` decides the string is still open and
        swallows the remainder of the document as a comment."""
        assert jsonc.loads(r'{"m": "ends with a backslash: \\"}') == {
            "m": "ends with a backslash: \\"
        }

    def test_comment_markers_after_a_string_are_still_comments(self):
        """The other direction: string state must end, not swallow the file.

        The failure this guards against is the stripper seeing `"x"` and then treating the
        `//` as being *inside* a string, which would blank the rest of the document -- and
        a policy that blanks its own rules loads cleanly and matches nothing.

        Note what this deliberately does NOT assert: that a second JSON document on the
        following line is ignored. That is multi-document JSON, which this module does not
        support, and `loads` raising `Extra data` on trailing content is the correct
        behaviour rather than a gap. Silently discarding everything after the first closing
        brace would be the dangerous version -- it would hide a second policy in the file.
        """
        text = '{"a": "x"} // trailing note\n{"b": 2}'
        stripped = jsonc.strip_comments(text)
        # The comment is gone and the second document is still there, untouched.
        assert "trailing note" not in stripped
        assert '{"b": 2}' in stripped
        # Which means the parse refuses, and says why.
        with pytest.raises(JsoncError, match="Extra data"):
            jsonc.loads(text)

    def test_a_trailing_comment_at_end_of_file_is_fine(self):
        """The supported version of the case above: one document, comment at the end."""
        assert jsonc.loads('{"a": "x"} // trailing note') == {"a": "x"}
        assert jsonc.loads('{"a": "x"}\n// trailing note\n') == {"a": "x"}


class TestTrailingCommas:
    @pytest.mark.parametrize("text,expected", [
        ('{"a": 1,}', {"a": 1}),
        ('{"a": [1, 2,],}', {"a": [1, 2]}),
        ('{"a": [{"b": 1,},],}', {"a": [{"b": 1}]}),
        ('[{"a": 1,}, {"b": 2,},]', [{"a": 1}, {"b": 2}]),
    ])
    def test_trailing_commas_are_dropped(self, text, expected):
        assert jsonc.loads(text) == expected

    def test_a_comma_is_not_dropped_when_a_value_follows(self):
        """The scanner must not become a comma-deleting machine."""
        assert jsonc.loads('{"a": 1, "b": 2}') == {"a": 1, "b": 2}


class TestPositionsArePreserved:
    def test_stripping_preserves_length(self):
        """Comments become spaces, not deletions.

        `json.JSONDecodeError` reports a line and column, and the person fixing the file is
        looking at a terminal. Deleting the comment shifts every later line up, so the
        error points at the wrong line -- in a file nobody has read yet, because it is
        generated. Length preservation also makes this function trivially auditable: the
        output is a character-for-character substitution of the input.
        """
        text = '{\n  // a comment\n  "a": 1\n}\n'
        assert len(jsonc.strip_comments(text)) == len(text)

    def test_newlines_inside_a_block_comment_are_kept(self):
        text = '{\n/* one\ntwo */\n"a": 1\n}'
        assert jsonc.strip_comments(text).count("\n") == text.count("\n")

    def test_error_line_number_points_at_the_real_line(self):
        """The reason for length preservation, asserted as behaviour.

        The malformed token has to be a real token rather than a bare `,`, because a comma
        sitting immediately before a closer is legitimately treated as a trailing comma and
        blanked out -- so `{"b": ,}` loses the comma and the parse error moves to the `}`
        on the next line. That is the trailing-comma feature working, not a position bug,
        and it has its own tests above. With an unremovable token the reported line is the
        line the token is actually on, which is the property that matters when somebody is
        staring at a terminal trying to fix a hand-edited policy file.
        """
        text = '{\n  "a": 1,\n  // comment\n  "b": @\n}\n'
        with pytest.raises(JsoncError) as exc:
            jsonc.loads(text)
        # Line 4 is `"b": @`; if the comment had been deleted rather than blanked, the
        # error would report line 3.
        assert "line 4" in str(exc.value)

    def test_a_bare_comma_before_a_closer_is_a_trailing_comma_not_a_position_bug(self):
        """Pins the interaction the test above deliberately avoids.

        `"b": ,` is a comma before a closer, so it is dropped as a trailing comma and the
        remaining `"b":` is what fails. The error therefore names the following line. Worth
        a named test because "the error line moved" looks like a regression in the
        stripper's position handling and is actually the trailing-comma pass.
        """
        text = '{\n  "a": 1,\n  // comment\n  "b": ,\n}\n'
        stripped = jsonc.strip_comments(text)
        assert ',' not in stripped.split('"b"')[1]  # the comma really was dropped
        with pytest.raises(JsoncError) as exc:
            jsonc.loads(text)
        assert "line 5" in str(exc.value)


class TestRejection:
    """A permissive parser that half-supports a format is worse than one that refuses it.

    Every case here is a typo or an LLM-authored file that should be an error, not a
    policy that quietly means something else.
    """

    @pytest.mark.parametrize("text", [
        '{"a": NaN}',
        '{"a": Infinity}',
        '{"a": -Infinity}',
        '{"max_code_chars": NaN}',
    ])
    def test_non_finite_numbers_are_rejected(self, text):
        """`json.loads` accepts these by default; `parse_constant` is what stops it.

        A limit of NaN would not raise here -- it would compare false against every
        statement, so the cap would never fire and the failure would surface as "why is
        nobody being screened past 20,000 characters" much later.
        """
        with pytest.raises(JsoncError, match="not a JSON number"):
            jsonc.loads(text)

    @pytest.mark.parametrize("text", ['{a: 1}', "{'a': 1}", '{"a": 0x1F}'])
    def test_json5_extensions_are_rejected(self, text):
        """JSON5 syntax, which json5 the library accepts and JSONC does not."""
        with pytest.raises(JsoncError):
            jsonc.loads(text)

    def test_unterminated_block_comment_is_reported(self):
        """Silently accepting the text up to EOF would drop every rule after it."""
        with pytest.raises(JsoncError, match="unterminated block comment"):
            jsonc.loads('{"a": 1 /* never closed')

    def test_truncated_document_is_rejected(self):
        with pytest.raises(JsoncError):
            jsonc.loads('{"a": 1,')

    def test_empty_input_is_rejected(self):
        with pytest.raises(JsoncError):
            jsonc.loads("")


class TestErrorType:
    def test_jsonc_error_is_a_value_error(self):
        """The CLI catches `(OSError, ValueError, json.JSONDecodeError)` and prints
        `bad policy: ...` rather than a traceback. A new exception type that is not a
        `ValueError` would turn a bad policy file into an unhandled crash."""
        assert issubclass(JsoncError, ValueError)

    def test_message_mentions_the_problem(self):
        with pytest.raises(JsoncError, match="invalid JSONC"):
            jsonc.loads("{bad}")


# ---------------------------------------------------------------------------
# oracle
# ---------------------------------------------------------------------------

#: The same inputs, asserted against json5 where json5 is installed. json5 is a dev-only
#: dependency: it is the reference implementation this handroll was checked against, not
#: something the package needs at runtime.
JSON5_CORPUS = [
    '{"literal_prefixes": ["s3://", "hdfs://"]}',
    '{"m": "a // b"}',
    '{"m": "a /* b */ c"}',
    r'{"m": "she said \"hi\""}',
    r'{"m": "ends with a backslash: \\"}',
    '{\n // hi\n "a": 1\n}',
    '{ /* one\ntwo */ "a": 1 }',
    '{"a": 1,}',
    '{"a": [1, 2,],}',
    '{"a": [{"b": 1,},],}',
]


@pytest.mark.parametrize("text", JSON5_CORPUS)
def test_handroll_agrees_with_json5_on_the_cases_that_matter(text):
    """Why not `import json5`?

    json5 handles every case in this corpus correctly and is a mature library, so the
    honest summary is: the handroll was chosen for control over *rejection*, not for
    capability. json5 accepts unquoted keys, single-quoted strings, hex numbers and
    `NaN`/`Infinity`, and this project would rather refuse a policy file than interpret a
    typo in one -- a policy is a security artefact, and its whole value is that what it
    says is what Spark will be allowed to do.

    Vendoring json5 to get the same behaviour is not worth it: the scanner is ~60 lines,
    it is exercised by the cases above plus the ones above them, and vendoring a parser to
    then forbid most of its features is a strange trade.

    If json5 is not installed the test skips rather than fails. `json5` is in the `dev`
    extra, so in CI it is present; a contributor running a bare venv gets a skip, which is
    the honest outcome -- the assertions that matter do not depend on it.
    """
    json5 = pytest.importorskip("json5", reason="json5 is a dev-only reference oracle")
    assert jsonc.loads(text) == json5.loads(text)


def test_json5_would_accept_what_we_refuse():
    """The specific divergence, asserted rather than assumed.

    If a future json5 tightened these, this test fails and the docstring gets updated --
    which is the point of writing it down. Both directions are checked because a test that
    only proves json5 is laxer would still pass if json5 became stricter and this handroll
    kept the behaviour.
    """
    json5 = pytest.importorskip("json5")
    with pytest.raises(JsoncError):
        jsonc.loads('{"a": NaN}')
    # json5 parses it, which is the behaviour being declined. If this ever starts raising,
    # the handroll and the library have converged and the rationale needs revisiting.
    assert json.loads(json.dumps(json5.loads('{"a": NaN}')))["a"] != json5.loads('{"a": NaN}')["a"]