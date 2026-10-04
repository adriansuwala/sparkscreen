"""Every statement label the pinned grammars can produce, derived from the parsers.

The effect table in `effects.py` has to be *total* over this set -- a label with no
entry would be analysed and then reported with no effect flags, which is fail-open.
So the set itself has to be derived mechanically, not from a SQL corpus.

A corpus is not sufficient on its own, and this is the reason: a corpus can only
demonstrate the labels you thought to write SQL for. `CreatePipelineDataset` and
`CreateFlowAutoCdc` were real, reachable top-level statements in the post-4.2 master
grammar this module was written against -- that snapshot is gone now (F17 re-pinned the
4.x lines to the actual 4.1 and 4.2 releases, and neither construct appears in either),
but they are the standing example of why a corpus cannot stand in for a derivation. The
argument does not depend on those two labels still existing: any label reachable only
through a rule the corpus never fires is invisible to a corpus-based coverage test, and
that test would pass with it unmapped. The generated parser classes, by contrast,
already contain the complete answer: ANTLR emits one `<Label>Context` class per
labeled alternative of every rule, and inheritance encodes which rule each belongs to.

## The derivation

1. **Entry shapes.** `top_level_statement_contexts` (in `treewalk`) can yield contexts
   under exactly three holders: `SingleStatementContext` (all grammars),
   `CompoundBodyContext` and `CompoundStatementContext` (the 4.x BEGIN...END scripts).
   Starting anywhere else would collect labels that can never reach a policy.

2. **Labeled alternatives of a rule** are the direct subclasses of that rule's
   context class. `DropTableContext` subclasses `StatementContext`, so `statement`
   can emit `DropTable` and `DropTableContext` is the only way to know that.

3. **Wrapper descent.** `effective_label()` walks *through* wrapper labels
   (`WRAPPER_LABELS`) to reach the real statement kind, so the label set is not closed
   under the entry rule alone: `DmlStatement > SingleInsertQuery > InsertOverwriteTable`
   means the screener reports `InsertOverwriteTable`. Descent therefore follows any
   label that `_is_wrapper` accepts.

4. **Query collapse.** `effective_label()` returns `QUERY_LABEL` ("StatementDefault")
   for anything under a `Query*` label -- the whole query hierarchy collapses to one
   read-only label. Descending into it would enumerate `QueryTermDefault`,
   `QueryPrimaryDefault` and every other level of a construct that produces exactly one
   policy label. Those are not collected, and `StatementDefault` is added instead.

Steps 3 and 4 use the same predicate `_is_wrapper` applies at runtime, imported from
`treewalk` rather than reimplemented. A second copy of "what counts as a wrapper"
would drift from the first, and the drift would be invisible: the universe would grow
with labels `effective_label` can never return, and the coverage test would demand
effect entries for them forever.
"""

from __future__ import annotations

import importlib
from typing import Iterator

from ..grammar.spec import SPECS, get_spec
from .treewalk import QUERY_LABEL, QUERY_LABEL_PREFIXES, WRAPPER_LABELS

#: Contexts that `top_level_statement_contexts` can hang statements off. Keep in step
#: with that function's three root shapes; a grammar that adds a fourth entry rule will
#: not be picked up here, which is why the coverage test also runs a SQL corpus.
_ENTRY_HOLDERS = (
    "SingleStatementContext",
    "CompoundBodyContext",
    "CompoundStatementContext",
)

#: Inherited context methods that are not rule references.
_NOT_RULES = frozenset({"getRuleIndex", "copyFrom", "accept", "enterRule", "exitRule"})


def _context_classes(parser_cls) -> dict[str, type]:
    """`{"StatementContext": <class>, ...}` for every context in a generated parser."""
    out: dict[str, type] = {}
    for name in dir(parser_cls):
        if not name.endswith("Context"):
            continue
        obj = getattr(parser_cls, name, None)
        if isinstance(obj, type):
            out[name] = obj
    return out


def _rule_name_to_context(rule: str) -> str:
    """`dmlStatementNoWith` -> `DmlStatementNoWithContext`."""
    return rule[0].upper() + rule[1:] + "Context"


def _label_of(ctx_cls: type) -> str:
    name = ctx_cls.__name__
    return name[:-len("Context")] if name.endswith("Context") else name


def _labeled_alternatives(rule_ctx: type) -> set[str]:
    """Labels of the alternatives of the rule whose context class this is.

    Direct subclasses only. A grandchild belongs to a nested rule, not this one:
    `InsertIntoTableContext` subclasses `InsertIntoContext`, and treating it as an
    alternative of `insert` would be wrong even though it is reachable.
    """
    return {
        _label_of(sub)
        for sub in rule_ctx.__subclasses__()
        if sub.__name__.endswith("Context")
    }


def _child_rule_contexts(rule_ctx: type, classes: dict[str, type]) -> list[type]:
    """The context classes of the rules this rule references.

    ANTLR generates a zero-arg accessor per referenced sub-rule, so the accessor names
    *are* the sub-rule list. Token accessors (`LEFT_PAREN`, `COMMA`) are upper-case and
    filtered by the `name[0].islower()` check.
    """
    out: list[type] = []
    for name, attr in vars(rule_ctx).items():
        if name.startswith("_") or name in _NOT_RULES:
            continue
        if not callable(attr) or not name or not name[0].islower():
            continue
        sub = classes.get(_rule_name_to_context(name))
        if sub is not None:
            out.append(sub)
    return out


def _is_wrapper(label: str) -> bool:
    """The same predicate `treewalk.effective_label` descends through."""
    return label in WRAPPER_LABELS or label.startswith(QUERY_LABEL_PREFIXES)


def labels_for_grammar(grammar_key: str | None = None) -> frozenset[str]:
    """Every label `effective_label()` can return under one pinned grammar.

    `grammar_key=None` means the default grammar, matching `get_spec(None)`.
    """
    spec = get_spec(grammar_key)
    module = importlib.import_module(f"{spec.python_module('SqlBaseParser')}")
    classes = _context_classes(module.SqlBaseParser)

    labels: set[str] = set()
    seen: set[type] = set()
    frontier: list[str] = []

    def absorb(rule_ctx: type) -> None:
        if rule_ctx in seen:
            return
        seen.add(rule_ctx)
        new = _labeled_alternatives(rule_ctx)
        labels.update(new)
        frontier.extend(l for l in new if _is_wrapper(l))

    for holder in _ENTRY_HOLDERS:
        ctx = classes.get(holder)
        if ctx is not None:
            for rule_ctx in _child_rule_contexts(ctx, classes):
                absorb(rule_ctx)

    while frontier:
        label = frontier.pop()
        if label.startswith(QUERY_LABEL_PREFIXES):
            # The whole query hierarchy collapses to one read-only policy label, and
            # this is that label. Descending further would enumerate the query
            # hierarchy's own labeled alternatives, none of which a policy ever sees.
            labels.add(QUERY_LABEL)
            continue
        ctx = classes.get(_rule_name_to_context(label))
        if ctx is None:
            # A label whose context class is absent cannot be constructed by this
            # parser, so it cannot appear. Skipping is safe; raising would be noise.
            continue
        for rule_ctx in _child_rule_contexts(ctx, classes):
            absorb(rule_ctx)

    return frozenset(labels)


def grammar_labels() -> Iterator[str]:
    """Sorted union over every pinned grammar.

    A union, not an intersection: sparkscreen can be asked to screen with either
    grammar, so a label only one of them can emit still has to be classified. The
    grammars disagree substantially -- `CALL` and the pipe operator exist only in 4.0,
    `SET TIME ZONE` only in 3.5.1 -- which is exactly why this cannot be a per-grammar
    table.
    """
    out: set[str] = set()
    for spec in SPECS:
        out |= labels_for_grammar(spec.key)
    yield from sorted(out)