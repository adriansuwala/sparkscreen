"""Detect PySpark DataFrame write operations from the Python AST.

`spark.sql("...")` is only one way an agent writes data. The DataFrame API is the other,
and it is the harder one for a screener because the operation never becomes SQL text —
there is no string to fold, no grammar to parse, no statement label to classify. What
there is instead is a method name and a save *mode*, and the effect of the call depends
on both.

This module finds those calls and describes what they do. It deliberately answers a
different question from the SQL path, and the distinction is the whole design:

  - The SQL path is conservative about the *target* because `spark.sql(q)` may be any
    query, and an unresolved `q` genuinely cannot be classified. It has no such doubt
    about the *effect*, because the effect comes from the parsed statement.
  - The DataFrame path is the mirror image. `df.write.mode("overwrite").saveAsTable(x)`
    is a destructive write **whatever `x` resolves to** — the effect is knowable even
    when the target is not. That is a stronger guarantee than the SQL path can offer,
    and it means an unresolvable `x` here is not a reason to give up on the effect. It
    is only a reason to stop applying namespace allowlists to the target.

The live-Spark oracle that pins these semantics is
`tests/differential/probe_dataframe_oracle.py`; `tests/differential/test_dataframe_
writes.py` asserts the classification against a real session.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from ..model import Effect
from .folding import SinkKey, StringFolder

#: Terminal methods that perform a write. The names are matched on the *last* attribute
#: of the call, so `df.write.saveAsTable(...)` ends in `saveAsTable`.
#:
#: `saveAsTable` and `insertInto` are PySpark-specific enough to be treated as sinks on
#: their own -- no `.write` in the receiver chain required -- because nothing else in
#: the Python world has a method by that name. `save` and `jdbc` are common enough
#: (file writers, database drivers, unrelated libraries) that matching them bare would
#: flag half of any codebase, so they are only sinks when the receiver chain actually
#: shows a `.write`.
WRITE_METHODS_PYSPARK_SPECIFIC = frozenset({"saveAsTable", "insertInto"})
WRITE_METHODS_NEEDING_WRITE = frozenset({"save", "jdbc"})
WRITE_METHODS = WRITE_METHODS_PYSPARK_SPECIFIC | WRITE_METHODS_NEEDING_WRITE

#: Methods that configure a writer. Walked through to recover the save mode and to
#: confirm a `.write` sits somewhere upstream in the chain.
CHAIN_METHODS = frozenset({"mode", "format", "partitionBy", "option", "options"})

#: The one save mode that destroys existing data. Spark's modes are append / overwrite /
#: error / errorifexists / ignore; only `overwrite` replaces what is there.
OVERWRITE_MODE = "overwrite"


@dataclass(frozen=True)
class DataFrameWrite:
    """One DataFrame write call, and what it does.

    `target` is the resolved destination when we can prove it (a table name, or a path);
    `target_known` is False when the expression is dynamic. Note that a False
    `target_known` does **not** make the effect unknown — see the module docstring.
    `mode_known` is separate again: it tells us whether we could resolve `.mode(...)`, not
    whether we could resolve the destination.
    """

    key: SinkKey
    operation: str            # saveAsTable | save | jdbc | insertInto
    effects: frozenset[Effect]
    overwrites: bool          # mode resolves to `overwrite`
    mode_known: bool          # we could resolve (or establish the absence of) the mode
    target: str | None        # resolved table name or path, when provable
    target_known: bool
    chain: str                # the receiver expression, for error messages


def find_dataframe_writes(
    tree: ast.AST, folder: StringFolder
) -> list[DataFrameWrite]:
    """Find every DataFrame write in `tree`, in source order.

    `folder` must already have visited `tree`, so that constant assignments made
    earlier in the file are available for resolving `.mode(...)` and destination names.
    Sharing the folder is what makes `mode = "overwrite"; df.write.mode(mode)...` work
    and, more importantly, what makes a shadowed variable resolve to UNKNOWN rather than
    to a stale binding from an outer scope.
    """
    finder = _WriteFinder(folder)
    finder.visit(tree)
    return finder.writes


class _WriteFinder(ast.NodeVisitor):
    def __init__(self, folder: StringFolder) -> None:
        self.folder = folder
        self.writes: list[DataFrameWrite] = []
        self._ordinal = 0

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in WRITE_METHODS:
            if self._is_writer_chain(f):
                self.writes.append(self._describe(node, f))
        self.generic_visit(node)

    # -- recognising a writer ----------------------------------------------

    def _is_writer_chain(self, f: ast.Attribute) -> bool:
        """True if this call's receiver chain is (or came from) a `.write`.

        Walks up the chain looking for a `.write` attribute. A PySpark-specific method
        (`saveAsTable`, `insertInto`) is trusted without one, because the name is
        specific enough to stand on its own; `save`/`jdbc` are not.
        """
        if f.attr in WRITE_METHODS_PYSPARK_SPECIFIC:
            return True
        return self._chain_has_write(f.value)

    def _chain_has_write(self, node: ast.AST | None) -> bool:
        """Look for a `.write` attribute anywhere in a receiver chain."""
        depth = 0
        while node is not None and depth < 32:
            depth += 1
            if isinstance(node, ast.Attribute):
                if node.attr == "write":
                    return True
                node = node.value
            elif isinstance(node, ast.Call):
                node = node.func.value if isinstance(node.func, ast.Attribute) else None
            elif isinstance(node, ast.Subscript):
                node = node.value
            else:
                return False
        return False

    # -- describing the call ------------------------------------------------

    def _describe(self, node: ast.Call, f: ast.Attribute) -> DataFrameWrite:
        operation = f.attr
        # `jdbc` takes its mode as a keyword argument; everything else takes it from the
        # `.mode(...)` chain. Spark's DataFrameWriter has no `.mode()` chain form for
        # jdbc, so reading only the chain would silently report every jdbc as a
        # default-mode append -- the fail-open direction for a `mode="overwrite"`.
        keyword_mode, kw_seen = self._recover_keyword_mode(node)
        if kw_seen:
            mode = keyword_mode
        else:
            mode, _ = self._recover_mode(f)

        # Three states, and the distinction matters:
        #   _UNSET         no .mode() anywhere -> Spark's default (errorifexists),
        #                  which is a *known* default and cannot destroy silently
        #   "overwrite"    read from a folded constant -> known
        #   None           a .mode(x) we could not fold -> genuinely unknown
        mode_known = mode is not None
        overwrites = mode == OVERWRITE_MODE
        effects = self._effects(operation, overwrites)
        target, target_known = self._recover_target(node, operation)
        self._ordinal += 1
        return DataFrameWrite(
            key=SinkKey(node.lineno, node.col_offset, self._ordinal),
            operation=operation,
            effects=effects,
            overwrites=overwrites,
            # An absent `.mode()` is a *known* default; an unresolvable `.mode(x)` is
            # not. Both are common and they mean different things -- one we can reason
            # about, the other we cannot.
            mode_known=mode_known,
            target=target,
            target_known=target_known,
            chain=_src(node.func),
        )

    def _recover_keyword_mode(self, node: ast.Call) -> tuple[object, bool]:
        """(mode, seen) from a `mode=` keyword argument.

        Only `jdbc` accepts one, but reading it generically is harmless and keeps this
        from having to know the signature of every writer method.
        """
        for kw in node.keywords:
            if kw.arg == "mode":
                return (self.folder.fold(kw.value) if kw.value is not None else None), True
        return _UNSET, False

    def _effects(self, operation: str, overwrites: bool) -> frozenset[Effect]:
        """What this write does. Independent of the target, by design.

        `save` and `jdbc` reach outside the cluster (a filesystem path, a remote
        database), so they carry REACHES_EXTERNAL even when overwriting. `saveAsTable`
        and `insertInto` write into the warehouse and do not.

        Overwrite adds DESTROY_DATA because it replaces rows that already exist. This
        is the same flag the SQL axis gives `INSERT OVERWRITE`, and for the same reason
        (D15): the question is whether a person needs to look, not how much is lost.
        A default-mode `saveAsTable` does *not* get DESTROY_DATA — Spark refuses it
        outright when the table exists (`TABLE_OR_VIEW_ALREADY_EXISTS`), which the
        oracle confirms, so it cannot silently destroy anything.
        """
        effects: set[Effect] = {Effect.WRITE_DATA}
        if operation in WRITE_METHODS_NEEDING_WRITE:
            effects.add(Effect.REACHES_EXTERNAL)
        if overwrites:
            effects.add(Effect.DESTROY_DATA)
        return frozenset(effects)

    def _recover_mode(self, f: ast.Attribute) -> tuple[object, bool]:
        """(mode, seen) from the `.mode(...)` chain; `(None, True)` if unresolvable.

        Walks up the receiver chain; the *innermost* (last applied) `.mode(...)` wins,
        matching how the writer builder actually behaves -- each call returns self, so
        `df.write.mode("append").mode("overwrite")` is an overwrite.
        """
        node: ast.AST | None = f.value
        depth = 0
        while node is not None and depth < 32:
            depth += 1
            if isinstance(node, ast.Attribute) and node.attr == "write":
                return _UNSET, True  # reached the writer: no .mode() on the chain
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "mode":
                    if node.args:
                        return self.folder.fold(node.args[0]), True
                    return None, True  # .mode() with no argument: mode is unreadable
                node = node.func.value
                continue
            if isinstance(node, ast.Attribute):
                node = node.value
                continue
            return _UNSET, True
        return _UNSET, True

    def _chain_has_mode(self, node: ast.AST | None) -> bool:
        depth = 0
        while node is not None and depth < 32:
            depth += 1
            if isinstance(node, ast.Attribute):
                if node.attr == "write":
                    return False
                node = node.value
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "mode":
                    return True
                node = node.func.value
            else:
                return False
        return False

    def _recover_target(
        self, node: ast.Call, operation: str
    ) -> tuple[str | None, bool]:
        """(target, known) for the destination, folded when provable.

        An unresolvable destination yields `(None, False)` and is *not* an error. The
        effect already answers "does this mutate", which is the question that matters;
        only the namespace allowlists need a name, and they simply cannot be applied.
        """
        if operation == "jdbc":
            # jdbc(url, table, ...) — the table is the second positional or `table=`.
            arg = None
            if len(node.args) >= 2:
                arg = node.args[1]
            for kw in node.keywords:
                if kw.arg == "table":
                    arg = kw.value
                    break
        elif node.args:
            arg = node.args[0]
        else:
            return None, False
        if arg is None:
            return None, False
        folded = self.folder.fold(arg)
        if folded is None:
            return None, False
        return folded, True


class _Unset:
    """Distinguishes "no mode() call" from "mode() we could not read"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<unset>"


_UNSET = _Unset()


def _src(node: ast.AST | None) -> str:
    """Best-effort source text for an expression, for error messages only."""
    if node is None:
        return "?"
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - ast.unparse is total in 3.9+
        return "<expr>"
