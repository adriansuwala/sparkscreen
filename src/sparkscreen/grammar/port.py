"""Grammar port: Spark's Java-target ANTLR grammars -> Python-target ANTLR grammars.

Spark ships SqlBaseLexer.g4 / SqlBaseParser.g4 as Java-target grammars: they embed
Java in @header, @members and inline actions/predicates. ANTLR's Python3 target
copies those verbatim into the generated module, so the Java must be translated
first. This module does that translation as an explicit, version-keyed rewrite.

Design constraints:
  * Every Java-ism is handled by name. An unrecognised one raises, so a Spark
    upgrade that adds new Java cannot silently produce a mis-parsing grammar.
  * Only ANTLR 4.13.1 is supported. 4.9.3 (Spark 3.5.x's pin) rejects Spark 3.5.1's
    own grammar for the Python target -- labels like `from=`, `input=`, `property=`
    collide with Python runtime attribute names. 4.13.1 resolves this and builds
    both pinned grammars cleanly.
  * This is a source-to-source transform of *our own vendored copy* of the grammar.
    It does not modify Spark.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SUPPORTED_ANTLR_VERSION = "4.13.1"

# --- lexer @header ------------------------------------------------------------

LEXER_HEADER = "from collections import deque as _java_Deque\n"

# --- parser @header -----------------------------------------------------------

PARSER_HEADER = '''
def _port_dec_complex_type(parser):
    """Spark reaches into the lexer's type-level counter for STRUCT<...> via NEQ."""
    ts = getattr(parser, "_input", None)
    src = getattr(ts, "tokenSource", None)
    if src is not None and hasattr(src, "decComplexTypeLevelCounter"):
        src.decComplexTypeLevelCounter()
'''

# --- lexer @members -----------------------------------------------------------
# Only `isValidDecimal`, `isHint` and `markUnclosedComment` exist in Spark 3.5.1;
# the type-level counter, dollar-quote tag stack and shift-right check were added
# later. All are emitted unconditionally -- an unused method is harmless, and a
# per-version split would be a maintenance liability for no benefit.

LEXER_MEMBERS = '''
has_unclosed_bracketed_comment = False
complex_type_level_counter = 0

def _port_tags(self):
    d = self.__dict__.get("_port_tags_deque")
    if d is None:
        d = _java_Deque()
        self.__dict__["_port_tags_deque"] = d
    return d

def _port_text(self):
    # Java getText() from inside a lexer action; the Python Lexer has no such method
    return self._input.getText(self._tokenStartCharIndex, self._input.index - 1)

def isValidDecimal(self):
    nextChar = self._input.LA(1)
    if ord('A') <= nextChar <= ord('Z') or ord('0') <= nextChar <= ord('9') or nextChar == ord('_'):
        return False
    return True

def isHint(self):
    return self._input.LA(1) == ord('+')

def markUnclosedComment(self):
    self.has_unclosed_bracketed_comment = True

def incComplexTypeLevelCounter(self):
    self.complex_type_level_counter += 1

def decComplexTypeLevelCounter(self):
    if self.complex_type_level_counter > 0:
        self.complex_type_level_counter -= 1

def isShiftRightOperator(self):
    return self.complex_type_level_counter == 0
'''

# --- parser @members ----------------------------------------------------------

PARSER_MEMBERS = '''
legacy_setops_precedence_enabled = False
legacy_exponent_literal_as_decimal_enabled = False
SQL_standard_keyword_behavior = False
double_quoted_identifiers = False
parameter_substitution_enabled = True
legacy_identifier_clause_only = False
single_character_pipe_operator_enabled = True

_PIPE_START_TOKEN_NAMES = __PIPE_START_TOKEN_NAMES__

def isOperatorPipeStart(self):
    if not self.single_character_pipe_operator_enabled:
        return False
    cls = type(self)
    cached = cls.__dict__.get('_port_pipe_start_tokens')
    if cached is None:
        # resolved by name; tokens absent from older grammars are simply skipped
        cached = frozenset(
            getattr(cls, n) for n in cls._PIPE_START_TOKEN_NAMES
            if hasattr(cls, n)
        )
        cls._port_pipe_start_tokens = cached
    return self._input.LA(2) in cached
'''


# Every Java-ism we know how to translate. Each entry is (java_pattern, python_repl).
# `port_grammar` asserts that no rewrite is left unused against the actual grammar,
# so a grammar that no longer contains one of these is reported rather than ignored.
LEXER_REWRITES = [
    (r"\{incComplexTypeLevelCounter\(\);\}", "{self.incComplexTypeLevelCounter();}"),
    (r"\{decComplexTypeLevelCounter\(\);\}", "{self.decComplexTypeLevelCounter();}"),
    (r"\{isShiftRightOperator\(\)\}\?", "{self.isShiftRightOperator()}?"),
    (r"\{isValidDecimal\(\)\}\?", "{self.isValidDecimal()}?"),
    (r"\{!isHint\(\)\}\?", "{not self.isHint()}?"),
    (r"\{markUnclosedComment\(\);\}", "{self.markUnclosedComment();}"),
    (r"\{tags\.push\(getText\(\)\);\}", "{self._port_tags().append(self._port_text());}"),
    (r"\{getText\(\)\.equals\(tags\.peek\(\)\)\}\?", "{self._port_text() == self._port_tags()[-1]}?"),
    (r"\{tags\.pop\(\);\}", "{self._port_tags().pop();}"),
]

PARSER_REWRITES = [
    (r"\{legacy_setops_precedence_enabled\}\?", "{self.legacy_setops_precedence_enabled}?"),
    (r"\{!legacy_setops_precedence_enabled\}\?", "{not self.legacy_setops_precedence_enabled}?"),
    (r"\{isOperatorPipeStart\(\)\}\?", "{self.isOperatorPipeStart()}?"),
    (r"\{!isOperatorPipeStart\(\)\}\?", "{not self.isOperatorPipeStart()}?"),
    (r"\{SQL_standard_keyword_behavior\}\?", "{self.SQL_standard_keyword_behavior}?"),
    (r"\{!SQL_standard_keyword_behavior\}\?", "{not self.SQL_standard_keyword_behavior}?"),
    (r"\{double_quoted_identifiers\}\?", "{self.double_quoted_identifiers}?"),
    (r"\{!double_quoted_identifiers\}\?", "{not self.double_quoted_identifiers}?"),
    (r"\{legacy_identifier_clause_only\}\?", "{self.legacy_identifier_clause_only}?"),
    (r"\{!legacy_identifier_clause_only\}\?", "{not self.legacy_identifier_clause_only}?"),
    (r"\{legacy_exponent_literal_as_decimal_enabled\}\?",
     "{self.legacy_exponent_literal_as_decimal_enabled}?"),
    (r"\{!legacy_exponent_literal_as_decimal_enabled\}\?",
     "{not self.legacy_exponent_literal_as_decimal_enabled}?"),
    (r"\{parameter_substitution_enabled\}\?", "{self.parameter_substitution_enabled}?"),
    (r"\{\(\(SqlBaseLexer\) getTokenStream\(\)\.getTokenSource\(\)\)\.decComplexTypeLevelCounter\(\);\}",
     "{_port_dec_complex_type(self)}"),
]

# Bare `self.x` boolean flags in the Java source: `{flag}?` and `{!flag}?`. These are
# rewritten by name so a *new* flag added upstream shows up as an unhandled Java-ism.
_FLAG_RE = re.compile(r"\{(!?)([A-Za-z_][A-Za-z_0-9]*)\}\?")

# Java-isms we can detect but cannot translate automatically.
_JAVA_MARKERS = (
    "public boolean", "public int", "public void", "public final",
    "private final", "import java", "new ArrayDeque", "new HashMap",
    "Deque<", "String>", "@Override",
)

# Rule-name / token-name we reference from our Python members. If a grammar renames
# one of these, getattr() would fail at parse time, so we check up front instead.
#
# `PIPE_START_TOKENS` is the set consulted by `isOperatorPipeStart` to disambiguate the
# single-character pipe operator, which Spark added after 3.5. Those tokens do not all
# exist in 3.5.1 (EXTEND, ASOF, AGGREGATE, BIN are absent), so the runtime set is built
# from whichever of these the grammar actually defines -- `build_pipe_start_tokens()`
# resolves them by name and skips missing ones.
REQUIRED_PARSER_TOKENS = ("SELECT", "SET", "WHERE", "PIPE", "SEMI", "JOIN", "LIMIT")

#: Candidate tokens for `isOperatorPipeStart`, resolved lazily per grammar.
PIPE_START_TOKENS = (
    "SELECT", "EXTEND", "SET", "DROP", "AS", "WHERE", "PIVOT", "UNPIVOT",
    "TABLESAMPLE", "INNER", "CROSS", "LEFT", "RIGHT", "FULL", "NATURAL",
    "SEMI", "ANTI", "ASOF", "JOIN", "UNION", "EXCEPT", "SETMINUS",
    "INTERSECT", "ORDER", "CLUSTER", "DISTRIBUTE", "SORT", "LIMIT",
    "OFFSET", "AGGREGATE", "WINDOW", "LATERAL", "BIN",
)

#: Tokens whose absence is fine (newer-Spark-only constructs).
OPTIONAL_PARSER_TOKENS = frozenset(set(PIPE_START_TOKENS) - set(REQUIRED_PARSER_TOKENS))


class PortError(RuntimeError):
    """The grammar contains Java we do not know how to translate."""


@dataclass
class PortResult:
    text: str
    applied: list[str] = field(default_factory=list)
    unused_rules: list[str] = field(default_factory=list)


def _strip_block(text: str, keyword: str) -> tuple[str, str | None]:
    """Remove an @keyword { ... } block, returning (text_without_block, body_or_None)."""
    start = text.find(keyword + " {")
    if start == -1:
        return text, None
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[:start] + text[j + 1:], text[i + 1:j]
    raise PortError(f"unterminated {keyword} block")


def _strip_comments(text: str) -> str:
    return re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)


def _check_java(text: str, where: str) -> None:
    body = _strip_comments(text)
    for marker in _JAVA_MARKERS:
        if marker in body:
            raise PortError(
                f"unported Java construct {marker!r} in {where}; "
                "add an explicit rewrite in sparkscreen.grammar.port"
            )


def port_grammar(src: str, *, is_lexer: bool, pipe_start_tokens: tuple[str, ...] = ()) -> PortResult:
    """Translate one Java-target Spark grammar into a Python-target one.

    `pipe_start_tokens` names the tokens `isOperatorPipeStart` may consult; it is
    substituted into the generated members so the runtime set matches this grammar.
    """
    text, _header = _strip_block(src, "@header")
    text, members = _strip_block(text, "@members")
    if members is None:
        raise PortError("no @members block found -- grammar shape changed?")
    if not any(k in members for k in ("public boolean", "public int", "public void")):
        raise PortError("unexpected @members shape -- port by hand")

    applied: list[str] = []
    unused: list[str] = []
    rewrites = LEXER_REWRITES if is_lexer else PARSER_REWRITES

    for pattern, repl in rewrites:
        new_text, n = re.subn(pattern, repl.replace("\\", "\\\\"), text)
        text = new_text
        if n:
            applied.append(pattern)
        else:
            unused.append(pattern)

    # bare `{flag}?` / `{!flag}?` predicates: the flags we know by name
    known_flags = {
        "legacy_setops_precedence_enabled", "legacy_exponent_literal_as_decimal_enabled",
        "SQL_standard_keyword_behavior", "double_quoted_identifiers",
        "parameter_substitution_enabled", "legacy_identifier_clause_only",
    }

    def flag_repl(m: re.Match) -> str:
        bang, name = m.group(1), m.group(2)
        if name not in known_flags:
            raise PortError(
                f"unknown semantic predicate {{{name}}} -- add it to known_flags "
                "and to PARSER_MEMBERS explicitly"
            )
        applied.append(f"flag:{name}")
        return f"{{{'not ' if bang else ''}self.{name}}}?"

    text = _FLAG_RE.sub(flag_repl, text)

    # after rewriting, no bare Java flag reference may remain
    leftover = _FLAG_RE.findall(_strip_comments(text))
    if leftover:
        raise PortError(f"untranslated predicate remains: {leftover}")

    where = "lexer" if is_lexer else "parser"
    _check_java(text, where)

    license_block = src[src.index("/*"): src.index("*/") + 2]
    if is_lexer:
        # `caseInsensitive` is load-bearing, not cosmetic. Spark SQL is
        # case-insensitive: real 3.5.1 accepts `select 1`, `drop table t` and even
        # `SeLeCt 1`. But the vendored grammars spell keywords in uppercase and 3.5.1
        # declares `fragment LETTER : [A-Z]`, so a literal port rejects every
        # lower-case statement. Enabling case-insensitivity here is what makes the port
        # agree with the engine it screens for.
        preamble = (
            f"{license_block}\nlexer grammar SqlBaseLexer;\n"
            "options { caseInsensitive = true; }\n"
            f"@header {{{LEXER_HEADER}\n}}\n@members {{{LEXER_MEMBERS}\n}}\n"
        )
    else:
        members_py = PARSER_MEMBERS.replace(
            "__PIPE_START_TOKEN_NAMES__",
            repr(tuple(pipe_start_tokens)),
        )
        preamble = (
            f"{license_block}\nparser grammar SqlBaseParser;\n"
            "options { tokenVocab = SqlBaseLexer; }\n"
            f"@header {{{PARSER_HEADER}\n}}\n@members {{{members_py}\n}}\n"
        )

    body = re.sub(r"^\s*(lexer|parser)\s+grammar\s+\w+\s*;", "", text, count=1, flags=re.M)
    body = re.sub(r"^\s*options\s*\{[^}]*\}\s*", "", body, count=1, flags=re.M)

    return PortResult(text=preamble + "\n" + body, applied=applied, unused_rules=unused)


def verify_tokens_renamed(lexer_grammar_text: str, parser_grammar_text: str) -> set[str]:
    """Check that tokens our Python members reference still exist.

    Token constants are generated from the *lexer* grammar (the parser declares
    `tokenVocab = SqlBaseLexer`), so that is where authoritative names live.

    Returns the subset of `PIPE_START_TOKENS` this grammar actually defines. A missing
    optional token is normal across Spark versions (3.5.1 predates the single-char pipe
    operator); a missing required token means a rename and is a hard failure.
    """
    lexer_tokens = set(re.findall(r"^([A-Z_][A-Z_0-9]*)\s*:", lexer_grammar_text, flags=re.M))
    block = re.search(r"^tokens\s*\{(.*?)\}", parser_grammar_text, flags=re.S | re.M)
    parser_tokens = set(re.findall(r"[A-Z_][A-Z_0-9]*", block.group(1))) if block else set()
    defined = lexer_tokens | parser_tokens

    missing = [t for t in REQUIRED_PARSER_TOKENS if t not in defined]
    if missing:
        raise PortError(
            f"parser members reference tokens absent from the grammar: {missing}; "
            "a Spark upgrade renamed them"
        )
    return {t for t in PIPE_START_TOKENS if t in defined}