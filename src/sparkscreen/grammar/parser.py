"""Strict Spark SQL parsing.

Two things matter here and both are easy to get wrong:

1. **No error recovery.** ANTLR's default error strategy happily produces a usable
   parse tree for invalid SQL -- `INSERT INTO t SELECT * FROM` and
   `SELECT * FROM t WHERE` both parse. A security screener that analyzes such a tree
   is analyzing something Spark would never run. We install `BailErrorStrategy` so
   malformed SQL is rejected outright.

2. **Fail closed.** Every failure mode -- lexer error, parse error, recursion limit,
   resource limit -- is an exception or an explicit rejection. There is no path that
   turns "I could not parse this" into "nothing dangerous found".

The `errorCapturingIdentifier` rule in Spark's grammar is why (1) matters: the grammar
deliberately accepts malformed identifiers so it can produce a better error message
later. We want the opposite.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass

from antlr4 import BailErrorStrategy, CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener
from antlr4.error.Errors import ParseCancellationException
from antlr4.tree.Tree import ParseTree

from .spec import GrammarSpec, get_spec

# Deeply nested expressions can blow Python's stack in the recursive-descent walker.
# Spark guards its own AstBuilder against StackOverflowError; we do the same.
DEFAULT_RECURSION_LIMIT = 20_000


class SqlSyntaxError(ValueError):
    """SQL failed to parse. Callers must treat this as 'not screened', never 'safe'."""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None):
        super().__init__(message)
        self.line = line
        self.column = column


class _CollectingListener(ErrorListener):
    """Captures syntax errors instead of printing them to stderr.

    `ErrorListener.syntaxError` is a no-op in the base class, so the override is
    load-bearing: without it every error is swallowed and every malformed string
    looks like a successful parse.
    """

    def __init__(self) -> None:
        self.errors: list[tuple[int, int, str]] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e) -> None:  # noqa: N802
        self.errors.append((line, column, msg))


@dataclass
class ParsedStatement:
    """A successfully parsed SQL string.

    `label` is the ANTLR labeled-alternative name of the top-level statement
    (`DropTable`, `InsertOverwriteTable`, ...) which is the rule vocabulary the policy
    layer matches on. `context_class` is the generated context class name, useful for
    tests and for the `visit_children` escape hatch.
    """

    sql: str
    label: str
    tree: ParseTree
    context_class: str

    @property
    def statements(self) -> list["ParsedStatement"]:
        """Flatten a BEGIN...END script into its component statements."""
        return _flatten(self)


def _flatten(p: "ParsedStatement") -> list["ParsedStatement"]:
    """Split a BEGIN...END script into its component statements."""
    from sparkscreen.analysis.treewalk import effective_label, top_level_statement_contexts

    ctxs = [
        ParsedStatement(
            sql=p.sql,
            label=effective_label(ctx),
            tree=ctx,
            context_class=type(ctx).__name__,
        )
        for ctx in top_level_statement_contexts(p.tree)
    ]
    # A statement's own subtree carries the facts (targets, literals); the wrapper
    # context only carries the label. Hand the policy the most specific context whose
    # label we matched, so extraction sees the real statement.
    return _tighten(ctxs)


def _tighten(statements: list["ParsedStatement"]) -> list["ParsedStatement"]:
    """Replace each wrapper context with the subtree its label came from.

    `effective_label` reads the real statement kind (e.g. `InsertOverwriteTable`) out of
    a wrapper like `DmlStatement`, but the wrapper node itself is what we hand to the
    policy for fact extraction. Re-pointing `tree` at the context that actually matched
    keeps the label and the subtree in agreement.
    """
    from sparkscreen.analysis.treewalk import find_labeled_context, statement_label

    out: list[ParsedStatement] = []
    for st in statements:
        if statement_label(st.tree) != st.label:
            inner = find_labeled_context(st.tree, st.label)
            if inner is not None:
                st = ParsedStatement(
                    sql=st.sql, label=st.label, tree=inner,
                    context_class=type(inner).__name__,
                )
        out.append(st)
    return out


class SqlParser:
    """Strict parser bound to one pinned grammar."""

    def __init__(self, spec: GrammarSpec | str | None = None, *, recursion_limit: int = DEFAULT_RECURSION_LIMIT):
        self.spec = spec if isinstance(spec, GrammarSpec) else get_spec(spec)
        self.recursion_limit = recursion_limit
        self._lexer_mod = None
        self._parser_mod = None

    # -- lazy module loading, so importing sparkscreen stays cheap ----------

    def _load(self):
        if self._parser_mod is not None:
            return self._lexer_mod, self._parser_mod
        key = self.spec.module_name
        # generated parsers are one package per grammar key
        pkg = f"sparkscreen.grammar.generated.{key}"
        try:
            lexer_mod = importlib.import_module(f"{pkg}.SqlBaseLexer")
            parser_mod = importlib.import_module(f"{pkg}.SqlBaseParser")
        except ImportError as e:  # pragma: no cover - packaging failure
            raise ImportError(
                f"generated parser for {key} not found; "
                f"run `python -m sparkscreen.grammar.build --fetch --generate`"
            ) from e
        self._lexer_mod, self._parser_mod = lexer_mod, parser_mod
        return lexer_mod, parser_mod

    @property
    def visitor_class(self):
        pkg = f"sparkscreen.grammar.generated.{self.spec.module_name}"
        mod = importlib.import_module(f"{pkg}.SqlBaseParserVisitor")
        return mod.SqlBaseParserVisitor

    # -- parsing ------------------------------------------------------------

    def parse(self, sql: str) -> ParsedStatement:
        """Parse a single SQL statement, strictly. Raises SqlSyntaxError on any problem."""
        if not isinstance(sql, str):
            raise SqlSyntaxError(f"expected str, got {type(sql).__name__}")
        stripped = sql.strip()
        if not stripped:
            raise SqlSyntaxError("empty SQL")

        lexer_mod, parser_mod = self._load()
        old_limit = sys.getrecursionlimit()
        if self.recursion_limit > old_limit:
            sys.setrecursionlimit(self.recursion_limit)
        try:
            errors = _CollectingListener()
            try:
                lexer = lexer_mod.SqlBaseLexer(InputStream(stripped))
            except RecursionError as e:
                raise SqlSyntaxError("lexer recursion limit exceeded") from e
            lexer.removeErrorListeners()
            lexer.addErrorListener(errors)

            tokens = CommonTokenStream(lexer)
            parser = parser_mod.SqlBaseParser(tokens)
            parser.removeErrorListeners()
            parser.addErrorListener(errors)
            parser._errHandler = BailErrorStrategy()

            # Spark 4.0 added BEGIN...END scripts and introduced
            # `compoundOrSingleStatement` as the entry rule; 3.5.x only has
            # `singleStatement`. Try the richest rule the grammar actually has.
            entry = None
            for candidate in ("compoundOrSingleStatement", "singleStatement"):
                if hasattr(parser, candidate):
                    entry = candidate
                    break
            if entry is None:
                raise SqlSyntaxError(
                    f"grammar {self.spec.key} exposes no known entry rule; "
                    "a Spark upgrade may have renamed them"
                )

            try:
                tree = getattr(parser, entry)()
            except ParseCancellationException as e:
                raise SqlSyntaxError("syntax error") from e
            except RecursionError as e:
                raise SqlSyntaxError("parser recursion limit exceeded") from e

            if errors.errors:
                line, col, msg = errors.errors[0]
                raise SqlSyntaxError(msg, line=line, column=col)
            return _wrap(stripped, tree)
        finally:
            sys.setrecursionlimit(old_limit)

    def try_parse(self, sql: str) -> ParsedStatement | None:
        """Non-raising variant. `None` means unparseable -- which is NOT 'safe'."""
        try:
            return self.parse(sql)
        except SqlSyntaxError:
            return None


def _wrap(sql: str, tree: ParseTree) -> ParsedStatement:
    """Attach the top-level statement label to a freshly parsed tree."""
    from sparkscreen.analysis.treewalk import top_level_statement_contexts

    ctxs = list(top_level_statement_contexts(tree))
    if not ctxs:
        raise SqlSyntaxError("no top-level statement found")
    first = ctxs[0]
    cls_name = type(first).__name__
    label = cls_name[:-len("Context")] if cls_name.endswith("Context") else cls_name
    return ParsedStatement(sql=sql, label=label, tree=tree, context_class=cls_name)


_default: dict[str, SqlParser] = {}


def get_parser(spec: GrammarSpec | str | None = None) -> SqlParser:
    """Cached parser for a grammar key."""
    key = spec if isinstance(spec, str) else (spec.key if spec else get_spec(None).key)
    if key not in _default:
        _default[key] = SqlParser(key)
    return _default[key]