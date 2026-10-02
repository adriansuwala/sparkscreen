"""Rewrite Spark's SqlBaseLexer/Parser .g4 Java-isms into valid Python-target code.

ANTLR's Python3 target copies @members / @header / inline actions verbatim into
the generated .py, so the Java in Spark's grammars must be translated first.
Only ~30 Java-isms exist across both files; each one is handled explicitly here
rather than with a generic source-to-source transform, so an unexpected new
Java-ism fails loudly instead of silently mis-parsing.
"""

import re
import sys

# --- lexer @header: java.util imports -> Python equivalents -------------------

LEXER_HEADER = '''
from collections import deque as _java_Deque
'''

# --- lexer @members: hand-port of Spark's Java helpers ------------------------

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

# --- parser @members ---------------------------------------------------------

PARSER_MEMBERS = '''
legacy_setops_precedence_enabled = False
legacy_exponent_literal_as_decimal_enabled = False
SQL_standard_keyword_behavior = False
double_quoted_identifiers = False
parameter_substitution_enabled = True
legacy_identifier_clause_only = False
single_character_pipe_operator_enabled = True

_PIPE_START_TOKEN_NAMES = (
    'SELECT', 'EXTEND', 'SET', 'DROP', 'AS', 'WHERE', 'PIVOT', 'UNPIVOT',
    'TABLESAMPLE', 'INNER', 'CROSS', 'LEFT', 'RIGHT', 'FULL', 'NATURAL',
    'SEMI', 'ANTI', 'ASOF', 'JOIN', 'UNION', 'EXCEPT', 'SETMINUS',
    'INTERSECT', 'ORDER', 'CLUSTER', 'DISTRIBUTE', 'SORT', 'LIMIT',
    'OFFSET', 'AGGREGATE', 'WINDOW', 'LATERAL', 'BIN',
)

def isOperatorPipeStart(self):
    if not self.single_character_pipe_operator_enabled:
        return False
    cls = type(self)
    cached = cls.__dict__.get('_port_pipe_start_tokens')
    if cached is None:
        cached = {getattr(cls, n) for n in cls._PIPE_START_TOKEN_NAMES}
        cls._port_pipe_start_tokens = cached
    return self._input.LA(2) in cached
'''

# inline action/predicate rewrites: exact source -> Python target.
# Applied to both grammar bodies (lexer rules use lexer helpers, parser rules
# reference lexer state through the token stream, handled by port_grammar below).
INLINE_REWRITES = [
    # lexer side
    (r'\{incComplexTypeLevelCounter\(\);\}', '{self.incComplexTypeLevelCounter();}'),
    (r'\{decComplexTypeLevelCounter\(\);\}', '{self.decComplexTypeLevelCounter();}'),
    (r'\{isShiftRightOperator\(\)\}\?', '{self.isShiftRightOperator()}?'),
    (r'\{isValidDecimal\(\)\}\?', '{self.isValidDecimal()}?'),
    (r'\{!isHint\(\)\}\?', '{not self.isHint()}?'),
    (r'\{markUnclosedComment\(\);\}', '{self.markUnclosedComment();}'),
    (r'\{tags\.push\(getText\(\)\);\}', '{self._port_tags().append(self._port_text());}'),
    (r'\{getText\(\)\.equals\(tags\.peek\(\)\)\}\?', '{self._port_text() == self._port_tags()[-1]}?'),
    (r'\{tags\.pop\(\);\}', '{self._port_tags().pop();}'),
    # parser side
    (r'\{legacy_setops_precedence_enabled\}\?', '{self.legacy_setops_precedence_enabled}?'),
    (r'\{!legacy_setops_precedence_enabled\}\?', '{not self.legacy_setops_precedence_enabled}?'),
    (r'\{isOperatorPipeStart\(\)\}\?', '{self.isOperatorPipeStart()}?'),
    (r'\{!isOperatorPipeStart\(\)\}\?', '{not self.isOperatorPipeStart()}?'),
    (r'\{SQL_standard_keyword_behavior\}\?', '{self.SQL_standard_keyword_behavior}?'),
    (r'\{!SQL_standard_keyword_behavior\}\?', '{not self.SQL_standard_keyword_behavior}?'),
    (r'\{double_quoted_identifiers\}\?', '{self.double_quoted_identifiers}?'),
    (r'\{!double_quoted_identifiers\}\?', '{not self.double_quoted_identifiers}?'),
    (r'\{legacy_identifier_clause_only\}\?', '{self.legacy_identifier_clause_only}?'),
    (r'\{!legacy_identifier_clause_only\}\?', '{not self.legacy_identifier_clause_only}?'),
    (r'\{legacy_exponent_literal_as_decimal_enabled\}\?', '{self.legacy_exponent_literal_as_decimal_enabled}?'),
    (r'\{!legacy_exponent_literal_as_decimal_enabled\}\?', '{not self.legacy_exponent_literal_as_decimal_enabled}?'),
    (r'\{parameter_substitution_enabled\}\?', '{self.parameter_substitution_enabled}?'),
    # parser reaches into the lexer's type-level counter for STRUCT<...> via NEQ
    (r'\{\(\(SqlBaseLexer\) getTokenStream\(\)\.getTokenSource\(\)\)\.decComplexTypeLevelCounter\(\);\}',
     '{_port_dec_complex_type(self)}'),
]

# helper referenced by the rewritten parser action above
PARSER_EXTRA = '''
def _port_dec_complex_type(parser):
    ts = getattr(parser, "_input", None)
    src = getattr(ts, "tokenSource", None)
    if src is not None and hasattr(src, "decComplexTypeLevelCounter"):
        src.decComplexTypeLevelCounter()
'''


def _strip_block(text, keyword):
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
    raise ValueError(f"unterminated {keyword} block")


def _rewrite_inline(text):
    for pat, rep in INLINE_REWRITES:
        text = re.sub(pat, rep.replace("\\", "\\\\"), text)
    return text


def port_grammar(src, is_lexer):
    text, header = _strip_block(src, "@header")
    text, members = _strip_block(text, "@members")
    if members is None:
        raise ValueError("no @members block found")
    leftover = re.findall(r"\{[^{}]*\}", members)
    # the @members body is replaced wholesale; only sanity-check it was Java
    if "public boolean" not in members and "public int" not in members and "public void" not in members:
        raise ValueError("unexpected @members shape -- port by hand")
    del leftover

    text = _rewrite_inline(text)

    # any surviving Java-ish token inside a rule means the port is incomplete
    body_wo_comments = re.sub(r"//[^\n]*|/\*.*?\*/", "", text, flags=re.S)
    for bad in ("public ", "import java", "private final", "new ArrayDeque"):
        if bad in body_wo_comments:
            raise ValueError(f"unported Java construct remains: {bad!r}")

    header_py = LEXER_HEADER if is_lexer else PARSER_EXTRA
    members_py = LEXER_MEMBERS if is_lexer else PARSER_MEMBERS
    return header_py, text, members_py


def build(in_dir, out_dir, lexer_src, parser_src):
    import os
    os.makedirs(out_dir, exist_ok=True)
    for name, src_name, is_lexer in (
        ("SqlBaseLexer", lexer_src, True),
        ("SqlBaseParser", parser_src, False),
    ):
        src = open(os.path.join(in_dir, src_name)).read()
        header_py, body, members = port_grammar(src, is_lexer)

        # rebuild the grammar with a Python-target preamble, keeping the original
        # license comment block that sits above the `grammar X;` declaration.
        lic_start = src.index("/*")
        lic_end = src.index("*/", lic_start) + 2
        license_block = src[lic_start:lic_end]

        if is_lexer:
            preamble = (
                license_block + "\nlexer grammar SqlBaseLexer;\n"
                "@header {" + header_py + "\n}\n"
                "@members {" + members + "\n}\n"
            )
        else:
            preamble = (
                license_block + "\nparser grammar SqlBaseParser;\n"
                "options { tokenVocab = SqlBaseLexer; }\n"
                "@header {" + header_py + "\n}\n"
                "@members {" + members + "\n}\n"
            )

        # drop the original grammar declaration + options block from the body;
        # the Python-target preamble replaces them.
        body = re.sub(r"^\s*(lexer|parser)\s+grammar\s+\w+\s*;", "", body, count=1, flags=re.M)
        body = re.sub(r"^\s*options\s*\{[^}]*\}\s*", "", body, count=1, flags=re.M)

        out = preamble + "\n" + body
        dst = os.path.join(out_dir, name + ".g4")
        open(dst, "w").write(out)
        print("wrote", dst)


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])