"""Does BailErrorStrategy actually change our accept/reject decisions?

The original rationale for BailErrorStrategy was "ANTLR's default error strategy
accepts invalid SQL". That turns out to be only half true, and the distinction matters
for how the tool is documented:

  * Spark's grammar deliberately accepts some malformed input. `errorCapturingIdentifier`
    exists so the parser can attach a better error message later, so
    `INSERT INTO t SELECT * FROM` is genuinely *in* the language as far as the grammar
    is concerned. Real Spark accepts it too and fails later, during analysis. We match
    that, and should not pretend otherwise.
  * Error *recovery* is a separate matter: the default strategy deletes or invents
    tokens and keeps going, so a tree comes back for input no engine would run.

This script isolates the second category: for each input, does the default strategy
return a tree that the strict one rejects?
"""
import sys

from antlr4 import BailErrorStrategy, CommonTokenStream, InputStream
from antlr4.error.ErrorStrategy import DefaultErrorStrategy
from antlr4.error.Errors import ParseCancellationException

from sparkscreen.grammar.parser import get_parser

CASES = [
    # genuinely broken -- recovery is the only reason a tree appears
    "SELECT * FROM t JOIN",
    "SELECT $$abc$$",
    "SELECT * FROM t WHERE AND OR",
    "DROP TABLE",
    "SELECT ((((1))))))",
    "INSERT INTO",
    "CREATE TABLE",
    "SELECT * FROM t GROUP",
    "MERGE INTO t",
    "DROP TABLE t WHERE",
    "SELECT * FROM",
    "SELECT * FROM t WHERE",
    "INSERT INTO t SELECT * FROM",
]


def try_parse(spec_key, sql, strategy):
    lexer_mod, parser_mod = get_parser(spec_key)._load()
    lexer = lexer_mod.SqlBaseLexer(InputStream(sql))
    parser = parser_mod.SqlBaseParser(CommonTokenStream(lexer))
    parser._errHandler = strategy()
    try:
        return parser, parser.compoundOrSingleStatement()
    except (ParseCancellationException, RecursionError):
        return parser, None


def main() -> int:
    spec_key = sys.argv[1] if len(sys.argv) > 1 else "spark-4.0"
    strict_only = 0
    print(f"grammar: {spec_key}\n")
    print(f"{'input':38} {'default':>10} {'strict':>10}  verdict")
    print("-" * 72)
    for sql in CASES:
        _, default_tree = try_parse(spec_key, sql, DefaultErrorStrategy)
        _, strict_tree = try_parse(spec_key, sql, BailErrorStrategy)
        d = "tree" if default_tree is not None else "error"
        s = "tree" if strict_tree is not None else "error"
        if d == "tree" and s == "error":
            verdict = "RECOVERED (strict rejects)"
            strict_only += 1
        elif d == "tree" and s == "tree":
            verdict = "both accept"
        else:
            verdict = "both reject"
        print(f"{sql!r:38} {d:>10} {s:>10}  {verdict}")

    print(f"\n{strict_only} of {len(CASES)} inputs are accepted only via error recovery.")
    print("Those are exactly the inputs the default strategy invents tokens for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())