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

Aliased writers
---------------

`w = df.write` followed by `w.saveAsTable("prod.t")` is the same write written
differently, and PySpark's own examples use that shape. A receiver that is a bare
`Name` therefore has to be resolved -- but only when it is *provable*, because the
alternative is to read a stale binding and report a clean, specific, wrong finding.

`_WriteFinder` resolves those names by reusing the constant folder's scope machinery
rather than reimplementing it (see its docstring for why subclassing beats a second
copy). It inherits the folder's answer on the question that decides whether an alias
is trustworthy at all: a name rebound by a loop target, a `with`, a parameter, an
augmented assignment, a `global` or a `del` loses its value. So a parameter, a loop
variable, a dict lookup and a rebinding all stay unresolved, and an unresolved alias
produces exactly what the code produced before aliases existed -- never an ALLOW.

Resolving an alias also *creates* a hazard that direct `df.write...` chains do not
have: a DataFrameWriter is a mutable builder, so `w.mode("overwrite")` on its own line
mutates the object `w` still points at, and a later `w.save(path)` really is an
overwrite. A chain walk bottoms out at a bare `Name` and cannot see that, so when it
does, the mode is reported unknown -- which routes to REVIEW, never to ALLOW. Missing
the mode would be a false ALLOW on a destructive write, which is the one failure this
tool exists to prevent.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from ..model import Effect
from .folding import MISSING, SinkKey, StringFolder

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


#: How far to walk a `.write` receiver before giving up. Matches the receiver-chain
#: depth limit in `_chain_has_write`; both guard against a pathological AST rather than
#: against anything real, and both fail in the safe direction (no alias resolved).
MAX_ALIAS_DEPTH = 32


class _Writer:
    """The folded value of an expression that provably produces a DataFrameWriter."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<DataFrameWriter>"


#: Folded result for `df.write` and friends. Distinct from every string constant, so a
#: binding can never be mistaken for one.
_WRITER = _Writer()


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

    Writer aliases (`w = df.write; w.save(...)`) are resolved from the finder's own
    scope machinery, which it borrows from the constant folder rather than
    reimplementing -- see `_WriteFinder`.
    """
    finder = _WriteFinder(folder)
    finder.visit(tree)
    return finder.writes


class _WriteFinder(StringFolder):
    """Find DataFrame writes, and track the writer aliases they arrive through.

    Subclasses the constant folder rather than reimplementing scope tracking, and that
    is the whole design. The question "may I trust this binding?" is not specific to
    strings -- it is the project's central rule that *a stale binding is worse than no
    binding*, and the folder already answers it for every construct that can rebind a
    name: loop targets, `with`, parameters, augmented assignment, `global`/`nonlocal`,
    `del`, imports, `match` captures, comprehension variables, and bodies that may
    never run. A second implementation of that list here would be a second place for
    it to drift, and the drift would be fail-open.

    Inheriting is also why this is one pass rather than two. The finder and the alias
    binder both need source order, and a whole-tree traversal of a DataFrame-heavy file
    is most of this module's cost; running the binder as its own visitor doubled it.

    Note what is *not* inherited. This instance's frame tables hold writers, not SQL
    text, and it never calls `fold()` on its own state -- `self.folder` is the separate
    constant folder `screen()` passed in, and that one does the SQL folding. Mixing them
    would let `spark.sql(w)` fold a writer to the string `"<DataFrameWriter>"` and be
    reported as resolved SQL: a false assurance of exactly the kind this tool exists to
    refuse.
    """

    def __init__(self, folder: StringFolder) -> None:
        super().__init__()
        #: The constant folder for SQL text. Never used for writer bindings.
        self.folder = folder
        self.writes: list[DataFrameWrite] = []
        self._ordinal = 0

    # -- writer aliases ----------------------------------------------------

    def _value(self, node: ast.AST, depth: int) -> object:
        """Fold a whole right-hand side to a writer, or to nothing.

        Depth 0 is the binding statement's right-hand side, which is the only position
        anything is ever bound from. Nested positions (an f-string field, a `+` operand)
        deliberately do not resolve: nothing binds through them, and letting the
        sentinel leak into a string there would put a fabricated constant in the table.
        """
        if depth == 0 and self._is_writer_expr(node):
            return _WRITER
        return MISSING

    def _is_writer_expr(self, node: ast.AST, depth: int = 0) -> bool:
        """True if `node` provably evaluates to a DataFrameWriter."""
        if depth > MAX_ALIAS_DEPTH:
            return False
        if isinstance(node, ast.Attribute):
            # Only the `.write` attribute itself. `self.df.write` and
            # `spark.table("x").write` are both this branch -- the receiver's shape
            # does not matter, which is the same trust the chain walk places in a
            # `.write` anywhere in a receiver.
            #
            # Deliberately *not* "any attribute on a writer": `w.format` is a bound
            # method, not a writer, and admitting it would let `x = w.format` resolve
            # as one.
            return node.attr == "write"
        if isinstance(node, ast.Name):
            # An alias of an alias (`w = df.write; w2 = w`). Still provable, still the
            # same object, so it stays a writer.
            return self._lookup(node.id) is _WRITER
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # The writer builder returns self from every configuration call, so
            # `df.write.mode("overwrite")` is the writer -- but the mode is *not*
            # carried, and deliberately so: knowing the object does not tell us which
            # mode it was last put into, so the caller reports the mode unknown.
            if node.func.attr not in CHAIN_METHODS:
                return False
            return self._is_writer_expr(node.func.value, depth + 1)
        return False

    def _is_writer_name(self, node: ast.AST | None) -> bool:
        """True if `node` is a `Name` that provably holds a writer right here.

        Asked live rather than from a table built in an earlier pass, and the timing is
        what makes it correct: this runs while the enclosing scopes are still on the
        frame stack, so a function parameter shadows the module binding for the length
        of the function body and no longer. `w = df.write` followed by
        `def g(w): w.save(...)` therefore does not resolve `g`'s parameter -- which is
        the whole point, since the module binding is stale inside `g`.

        It is asked from `visit_Call`, which runs *before* `generic_visit` descends to
        the receiver, so nothing here may depend on the receiver having been visited.
        """
        return isinstance(node, ast.Name) and self._lookup(node.id) is _WRITER

    # -- recognising a write ----------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        # Deliberately does *not* call the folder's `visit_Call`, so no SQL sinks are
        # collected here: this pass answers one question about objects, and `self.folder`
        # already collected the SQL. Collecting them twice would be pure duplicated work.
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
        """Look for a `.write` attribute -- or a name bound to one -- in a chain.

        The second case is what makes `w = df.write; w.save(path)` reachable. `save`
        and `jdbc` are common method names in the wider Python world, so they are only
        trusted when the receiver provably came from a `.write`; an alias is provable
        only when the binder says so, and an unresolvable name is no more of a writer
        than an unrecognised expression is.
        """
        depth = 0
        while node is not None and depth < 32:
            depth += 1
            if isinstance(node, ast.Attribute):
                if node.attr == "write":
                    return True
                node = node.value
            elif isinstance(node, ast.Name):
                return self._is_writer_name(node)
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
            if isinstance(node, ast.Name) and self._is_writer_name(node):
                # The chain bottomed out at a resolved alias, which is a different
                # question from "reached a fresh `.write`" above.
                #
                # A DataFrameWriter is a mutable builder and every configuration call
                # returns *self*, so `w.mode("overwrite")` on its own line mutates the
                # object `w` still names, and the `w.save(path)` after it really is an
                # overwrite. A fresh `df.write` cannot have been mutated -- it was just
                # constructed -- which is why that branch is a known default and this
                # one is not. Reporting the default here would be a false ALLOW on a
                # destructive write, so the mode stays unknown and the verdict degrades
                # to REVIEW.
                #
                # An *unresolved* name is deliberately not handled here: it keeps
                # falling through to the `_UNSET` below, so a name we cannot resolve
                # degrades to exactly the behaviour that existed before aliases were
                # tracked, rather than to a new and different one.
                return None, True
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
