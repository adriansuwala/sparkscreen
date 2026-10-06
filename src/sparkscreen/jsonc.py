"""JSONC: JSON with comments and trailing commas.

The default policy files ship as `.jsonc` because a policy is the one artefact in this
package a human is expected to *edit*, and a wall of uncommented rule ids teaches nobody
what `literal_prefixes` does. Comments are the documentation.

The loader is a character scanner rather than a regex, and that is not incidental. The
shipped defaults contain literal string values that look exactly like comment syntax:

    "literal_prefixes": ["s3://", "hdfs://", "file:/", "gs://"]

A `//`-to-end-of-line regex strips the rest of every one of those lines and the policy
then silently fails to match the very paths it was written to catch. It still parses, it
still loads, and it is wrong -- which is this project's favourite shape of bug. So the
scanner tracks string state and only treats `//` and `/*` as comments outside a string,
and it treats `\\` as an escape so that `"a\\"` does not swallow the rest of the file.

Trailing commas are removed for the same ergonomics reason: a commented JSON file whose
last rule ends in `,` should not be a syntax error, because that is what every editor
does when you comment a line out.

Deliberately NOT supported, because a permissive parser that half-supports a format is
worse than one that refuses it:

* single-quoted strings, unquoted keys, hex numbers -- JSON5 extensions, not JSONC
* `NaN` / `Infinity` / `-Infinity`

Those turn a typo into a silently different policy rather than an error. The last one
needs saying explicitly because it is not a formatting nicety: `json.loads` accepts
`NaN` and `Infinity` out of the box, so without `parse_constant` below this module would
have accepted a float where a count belongs, and the failure would surface much later as
a nonsense limit rather than as a bad policy file. json5 accepts all of these too.

On the choice of handroll over json5 (the obvious alternative, and the one that also
handles everything here): see `tests/test_jsonc.py::test_handroll_agrees_with_json5_on_the
_cases_that_matter`, which runs both against the same corpus so the claim stays checkable
rather than being a one-time assertion in a commit message.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["loads", "strip_comments", "JsoncError"]


def _reject_constant(name: str) -> Any:
    """Refuse NaN / Infinity, which `json.loads` accepts unless told otherwise.

    A policy's limits and severities are counts and enum values. A `NaN` there is not a
    lenient parse, it is a policy that will behave unpredictably at comparison time, and
    it is exactly the sort of thing an LLM or a careless editor produces.
    """
    raise JsoncError(
        f"{name} is not valid JSONC: it is not a JSON number, and accepting it would "
        f"give a policy a non-comparable value"
    )


class JsoncError(ValueError):
    """Raised for input that is not JSONC.

    A `ValueError`, so the existing `except (OSError, ValueError, json.JSONDecodeError)`
    in the CLI already catches it and reports `bad policy: ...` rather than a traceback.
    """


def strip_comments(text: str) -> str:
    """Remove comments and trailing commas, preserving everything else verbatim.

    Comment characters are replaced with spaces rather than deleted. Two reasons:

    1. A `json.JSONDecodeError` reports a line and column. Deleting the comment shifts
       every subsequent line up, so the error points at the wrong line -- and the user
       is editing this file by hand, so a wrong line number costs them real time.
    2. It keeps the output a pure character-for-character substitution, which makes this
       function's behaviour checkable: `strip_comments(s)` has the same length as `s`.

    Newlines inside a comment are preserved for the same reason.
    """
    out = list(text)
    i = 0
    n = len(text)
    # Stack of string states. None = outside a string; True = inside a double-quoted
    # string. A list rather than a bool because a `bool` invites writing `in_string`
    # where a tuple was meant; the extra bracket costs nothing and the intent is clearer.
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            if ch == "\\":
                # Skip the escaped character so `"\\"` does not look like an unterminated
                # string. Stepping i by 2 is enough; the escaped char cannot itself be a
                # backslash-then-terminator in a way this misses.
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            out[i] = out[i + 1] = " "
            i += 2
            while i < n:
                if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
                    out[i] = out[i + 1] = " "
                    i += 2
                    break
                # Preserve the newline so a multi-line comment does not shift line numbers.
                if text[i] != "\n":
                    out[i] = " "
                i += 1
            else:
                raise JsoncError(
                    "unterminated block comment: /* with no matching */"
                )
            continue
        i += 1
    return "".join(_drop_trailing_commas(out))


def _drop_trailing_commas(chars: list[str]) -> list[str]:
    """Blank out a `,` that is followed only by whitespace then `}` or `]`.

    Operates on the already-comment-stripped text, so a comma that only looked like it
    followed a comment has already become whitespace. Scans backwards from each closer,
    which is what makes it correct for nested structures: the innermost trailing comma is
    found first, and blanking it cannot affect an outer one's search.
    """
    out = list(chars)
    for idx in range(len(out) - 1, -1, -1):
        if out[idx] not in "}]":
            continue
        j = idx - 1
        while j >= 0 and out[j].isspace():
            j -= 1
        if j >= 0 and out[j] == ",":
            out[j] = " "
    return out


def loads(text: str) -> Any:
    """Parse JSONC text. Raises `JsoncError` (a `ValueError`) on anything malformed."""
    try:
        return json.loads(strip_comments(text), parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise JsoncError(f"invalid JSONC: {e}") from e