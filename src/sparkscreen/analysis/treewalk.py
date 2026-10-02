"""Walk a Spark parse tree to recover the facts a policy needs.

Two jobs:

* `top_level_statement_contexts` -- the labeled alternatives of the statements in a
  script. This is the rule vocabulary: `DropTable`, `InsertOverwriteTable`,
  `MergeIntoTable`, `LoadData`, `ManageResource`, `Call`, and so on. Policies match on
  these names rather than on SQL text.

* identifier extraction -- table/namespace/path names, normalised to a
  `NamespaceRef`. Spark's grammar represents identifiers several ways (plain,
  backquoted, `IDENTIFIER('literal')`, multipart with dots), and a policy that matches
  on raw text would be trivially bypassed with `` `prod`.`users` ``. We resolve them to
  a canonical dotted string so allowlists cannot be evaded by quoting.

Everything here is best-effort *extraction*, not validation. A missing identifier is
reported as `None`, never guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

from antlr4.ParserRuleContext import ParserRuleContext
from antlr4.tree.Tree import ParseTree


@dataclass(frozen=True)
class NamespaceRef:
    """A resolved table or namespace name.

    `parts` is the dot-separated identifier, with backquotes and any
    `IDENTIFIER('literal')` wrapper removed. `quoted` records whether any component was
    quoted, so policies can optionally be stricter about quoted forms.
    """

    parts: tuple[str, ...]
    quoted: bool = False

    @property
    def name(self) -> str:
        return ".".join(self.parts)

    @property
    def namespace(self) -> tuple[str, ...]:
        """All but the last part -- the database/catalog this lives in."""
        return self.parts[:-1]

    @property
    def table(self) -> str | None:
        return self.parts[-1] if self.parts else None

    def matches(self, pattern: str) -> bool:
        """Match against a dotted pattern, honouring `*` wildcards per component."""
        pat = tuple(p for p in pattern.split(".") if p != "")
        if len(pat) != len(self.parts):
            # allow `db.*` to match a bare table with no db part, and vice versa
            if "*" not in pat:
                return False
        return all(p == "*" or p == part for p, part in zip(pat, self.parts)) and (
            len(pat) == len(self.parts) or "*" in pat
        )

    def __str__(self) -> str:
        return self.name


def top_level_statement_contexts(tree: ParseTree) -> Iterator[ParseTree]:
    """Yield each top-level statement context in a parsed tree.

    Handles three root shapes across the grammars we pin:
      * Spark 4.0  `compoundOrSingleStatement -> singleStatement | singleCompoundStatement`
      * Spark 3.5.1 `singleStatement`  (parsed directly -- the root IS the statement list)
      * BEGIN...END scripts, whose body holds several `;`-separated statements
    """
    root_cls = type(tree).__name__

    if root_cls in _STATEMENT_LIST_CONTEXTS:
        holder = tree
    else:
        holder = (
            _child(tree, "singleStatement")
            or _child(tree, "singleCompoundStatement")
        )
        if holder is None:
            # A grammar change could produce a shape we don't know. Yield anything that
            # looks like a statement rather than silently returning nothing.
            yield from _children(tree, "statement")
            return

    if type(holder).__name__ == _ctx_name("singleStatement"):
        yield from _statementish(holder)
        return

    body = _child(holder, "compoundBody")
    if body is None:
        return
    for st in _children(body, "compoundStatement"):
        yield from _statementish(st)


def _statementish(node: ParseTree):
    """Yield the statement contexts directly under `node`.

    There is no generic `StatementContext`: ANTLR gives every labeled alternative its
    own context class (`DropTableContext`, `InsertOverwriteTableContext`, ...), and a
    rule with only labeled alternatives generates no accessor at all. So instead of
    matching rule names we take the parser-rule-context children and skip terminals --
    which is exactly "the statements of this statement-list".
    """
    for c in getattr(node, "children", []) or []:
        if isinstance(c, ParserRuleContext):
            yield c


def _ctx_name(rule: str) -> str:
    """`singleStatement` -> `SingleStatementContext`."""
    return rule[0].upper() + rule[1:] + "Context"


#: Contexts that are themselves a statement *list* rather than a statement.
_STATEMENT_LIST_CONTEXTS = frozenset({
    _ctx_name("singleStatement"),
    _ctx_name("singleCompoundStatement"),
})


def _child(node: ParseTree, rule: str):
    want = _ctx_name(rule)
    for c in getattr(node, "children", []) or []:
        if type(c).__name__ == want:
            return c
    return None


def _children(node: ParseTree, rule: str) -> list:
    want = _ctx_name(rule)
    return [c for c in (getattr(node, "children", []) or []) if type(c).__name__ == want]


def statement_label(ctx: ParseTree) -> str:
    """`DropTableContext` -> `DropTable`."""
    cls = type(ctx).__name__
    return cls[:-len("Context")] if cls.endswith("Context") else cls


#: Labels that merely wrap a more specific statement and carry no policy meaning of
#: their own. `DmlStatement` wraps insertInto/fromClause; the actual statement kind
#: (e.g. `InsertOverwriteTable`) is one level down. Policies must key on the inner
#: label, otherwise INSERT OVERWRITE and INSERT INTO look identical -- which they are
#: not.
WRAPPER_LABELS = frozenset({
    "DmlStatement",       # ctes? dmlStatementNoWith
    "SingleInsertQuery",  # dmlStatementNoWith : insertInto (query|...)
    "MultiInsertQuery",
    "SingleCompoundStatement",
    "Ctes",               # a WITH clause wrapper
    "CtesGlobal",
})

#: Label prefixes for the query rule hierarchy (query / queryTerm / queryPrimary /
#: querySpecification and their labeled alternatives). A plain SELECT descends all the
#: way through these, so matching on the prefix avoids enumerating every level -- and
#: keeps working when Spark adds a query level we have never seen.
QUERY_LABEL_PREFIXES = ("Query",)

#: Label reported for a plain read-only query, whichever wrapper we descended through.
QUERY_LABEL = "StatementDefault"


def _is_wrapper(label: str) -> bool:
    """True if `label` is a pass-through rule rather than a statement kind."""
    return label in WRAPPER_LABELS or label.startswith(QUERY_LABEL_PREFIXES)


def find_labeled_context(node: ParseTree, label: str) -> ParseTree | None:
    """Depth-first search for the descendant context whose label is `label`."""
    want = label + "Context"
    for c in getattr(node, "children", []) or []:
        if isinstance(c, ParserRuleContext):
            if type(c).__name__ == want:
                return c
            found = find_labeled_context(c, label)
            if found is not None:
                return found
    return None


def effective_label(ctx: ParseTree) -> str:
    """The most specific statement label under `ctx`.

    `INSERT OVERWRITE TABLE t SELECT 1` parses as
    `DmlStatement > SingleInsertQuery > InsertOverwriteTable`, so the top-level label is
    useless for policy. This descends through wrapper labels and returns the label a
    rule should actually match on. Descent that bottoms out on the query rules yields
    `StatementDefault`, which is the "read-only SELECT" label policies expect.
    """
    label = statement_label(ctx)
    if not _is_wrapper(label):
        return label
    for child in getattr(ctx, "children", []) or []:
        if isinstance(child, ParserRuleContext):
            inner = effective_label(child)
            if inner != label:
                return inner
    return QUERY_LABEL


# ---------------------------------------------------------------------------
# identifier extraction
# ---------------------------------------------------------------------------

# Rule contexts that hold a single table/namespace reference.
_IDENTIFIER_REF_RULES = (
    "IdentifierReferenceContext",
    "MultipartIdentifierContext",
    "ErrorCapturingIdentifierContext",
    "TemporalTableIdentifierReferenceContext",
)

#: Token *names* that are identifiers. A plain identifier reaches the tree as a bare
#: terminal (possibly with DOT between parts), so terminals must be collected too --
#: collecting only rule contexts silently finds nothing for `DROP TABLE prod.users`
#: while finding plenty for the backquoted spelling.
IDENTIFIER_TOKEN_NAMES = ("IDENTIFIER", "BACKQUOTED_IDENTIFIER")


def identifier_token_types(grammar_key: str | None = None) -> frozenset[int]:
    """Resolve identifier token names to numeric types for one pinned grammar.

    ANTLR assigns token numbers per grammar, and the two pinned grammars number them
    differently, so this is cached per grammar key. Resolving against the default
    grammar silently yields an empty target list when analysing the other one.
    """
    from sparkscreen.grammar.parser import get_parser

    parser = get_parser(grammar_key)
    lexer_cls = parser._load()[0].SqlBaseLexer
    names = lexer_cls.symbolicNames
    return frozenset(
        names.index(name) for name in IDENTIFIER_TOKEN_NAMES if name in names
    )


_TOKEN_TYPES: dict[str | None, frozenset[int]] = {}


def _identifier_types(grammar_key: str | None = None) -> frozenset[int]:
    if grammar_key not in _TOKEN_TYPES:
        _TOKEN_TYPES[grammar_key] = identifier_token_types(grammar_key)
    return _TOKEN_TYPES[grammar_key]

# Contexts whose identifier is a path/URI rather than a table name.
_STRING_PATH_RULES = (
    "LocationSpecContext",
    "InsertOverwriteHiveDirContext",
    "InsertOverwriteDirContext",
)


def extract_namespaces(ctx: ParseTree, *, grammar_key: str | None = None) -> list[NamespaceRef]:
    """Every table/namespace identifier appearing under `ctx`.

    Only whole identifier references are collected, and each is normalised to a dotted
    `NamespaceRef`. Aliases and column names are deliberately not reported as targets:
    a policy that has to reason about `FROM t AS x` shouldn't be handed `x`.
    """
    found: list[NamespaceRef] = []
    _walk(ctx, found, collect=True, grammar_key=grammar_key)
    seen: set[NamespaceRef] = set()
    out: list[NamespaceRef] = []
    for n in found:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _walk(node: ParseTree, found: list[NamespaceRef], collect: bool = True,
          *, grammar_key: str | None = None) -> None:
    cls = type(node).__name__
    if collect and cls in _IDENTIFIER_REF_RULES:
        ref = _resolve_ref(node, grammar_key=grammar_key)
        if ref is not None:
            found.append(ref)
            # Stop descending: `IdentifierReference > MultipartIdentifier >
            # ErrorCapturingIdentifier` nest three levels deep, and collecting each
            # level yields the same table three times. Only the outermost reference is
            # a target.
            return
    for c in getattr(node, "children", []) or []:
        _walk(c, found, collect, grammar_key=grammar_key)


def _is_identifier_token(node: ParseTree) -> bool:
    """True for a leaf whose token is an identifier."""
    if not hasattr(node, "getSymbol"):
        return False
    tok = node.getSymbol()
    return bool(tok) and tok.type in _identifier_types()


def _resolve_ref(node: ParseTree, *, grammar_key: str | None = None) -> NamespaceRef | None:
    """Turn an identifier subtree into a canonical NamespaceRef."""
    parts, quoted = _parts(node, grammar_key=grammar_key)
    if not parts:
        return None
    return NamespaceRef(parts=tuple(parts), quoted=quoted)


def _parts(node: ParseTree, *, grammar_key: str | None = None) -> tuple[list[str], bool]:
    """Collect the identifier components under `node`, in order.

    Handles all the ways Spark's grammar spells an identifier:
      * bare IDENTIFIER terminals joined by DOT        -> prod.users
      * BACKQUOTED_IDENTIFIER terminals                -> `prod`.`users`
      * IDENTIFIER('literal')                          -> IDENTIFIER('prod')
    Quoting sets `quoted=True` so a policy can be stricter about quoted forms.
    """
    out: list[str] = []
    quoted = False

    def rec(n: ParseTree, depth: int = 0) -> None:
        nonlocal quoted
        if depth > 12:
            return
        cls = type(n).__name__
        if cls in ("QuotedIdentifierContext", "BackQuotedIdentifierContext",
                   "SingleQuotedIdentifierContext"):
            t = _text(n)
            if t:
                quoted = True
                out.append(_unquote(t))
            return
        if cls == "IdentifierLiteralContext":
            # IDENTIFIER('literal') -- the literal is the identifier
            lit = None
            for c in getattr(n, "children", []) or []:
                if type(c).__name__ == "StringLitContext":
                    lit = _text(c)
            if lit:
                out.append(_unquote(lit.strip()))
            return
        if cls == "TerminalNodeImpl":
            tok = n.getSymbol()
            if tok is not None and tok.type in _identifier_types(grammar_key):
                out.append(_unquote(tok.text))
            return
        for c in getattr(n, "children", []) or []:
            rec(c, depth + 1)

    rec(node)
    return [p for p in out if p], quoted


def _text(node: ParseTree) -> str:
    t = getattr(node, "getText", None)
    if t is None:
        return ""
    try:
        return t()
    except Exception:
        return ""


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == "`" and s[-1] == "`":
        return s[1:-1].replace("``", "`")
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('""', '"')
    return s


def extract_string_literals(ctx: ParseTree) -> list[str]:
    """Every STRING_LITERAL text under `ctx`, unquoted.

    Used to catch `LOAD DATA ... INPATH '/etc/passwd'`, `ADD JAR /tmp/x.jar`,
    `LOCATION 's3://prod-bucket'`.
    """
    out: list[str] = []

    def rec(n: ParseTree) -> None:
        if type(n).__name__ in ("StringLitContext", "SingleStringLitWithoutMarkerContext"):
            t = _text(n)
            if t:
                out.append(_unquote(t))
            return
        for c in getattr(n, "children", []) or []:
            rec(c)

    rec(ctx)
    return out


def executed_immediate_sql(ctx: ParseTree) -> list[str]:
    """SQL hidden inside EXECUTE IMMEDIATE '...'.

    These are real statements Spark will run but which never appear as top-level
    statements, so a policy that only looks at top level misses them entirely.
    """
    out: list[str] = []
    for c in getattr(ctx, "children", []) or []:
        if type(c).__name__ == "VisitExecuteImmediateContext":
            for lit in extract_string_literals(c):
                out.append(lit)
    return out