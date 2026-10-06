# experiments/

Scratch work kept for provenance: the reasoning behind the implementation, including
code that did not ship. These files are not imported by the package and are not
tested. They exist so a future reader can see *why* the implementation looks the way
it does, and so the dead ends are not rediscovered.

## spike/port_prototype.py

The first working Java→Python port of Spark's `SqlBaseLexer.g4` / `SqlBaseParser.g4`,
written before `sparkscreen.grammar.port` existed. Provenance for two findings that
shaped the real implementation:

1. **ANTLR copies Java verbatim into the Python target.** `@members`, `@header` and
   inline actions are pasted unchanged, so the grammar must be translated first.
   Spark's grammars contain 24 Java-isms at commit `3c28a9c0` and 6 at `v3.5.1`.

2. **ANTLR 4.13.1 cannot be replaced by Spark 3.5.1's own pin.** ANTLR 4.9.3 rejects
   the 3.5.1 grammar for the Python target — labels `from=`, `input=`, `property=`
   collide with Python runtime attribute names. 4.13.1 builds both pinned grammars.

Two members here are deliberate bug fixes over the obvious translation:

- `isShiftRightOperator` must test `complex_type_level_counter == 0`, not "the next
  character is `>`". The obvious version silently mis-lexes `MAP<INT, ARRAY<INT>>`.
- `Lexer.getText()` does not exist in the Python runtime; the equivalent is
  `self._input.getText(self._tokenStartCharIndex, self._input.index - 1)`.

## spike/smoke_prototype.py

68-case accept/reject corpus used to validate the port. Two results from it drove the
design:

- ANTLR's **default error strategy accepts invalid SQL.** `INSERT INTO t SELECT * FROM`
  and `SELECT * FROM t WHERE` both produce usable parse trees, because Spark's grammar
  deliberately routes malformed identifiers through `errorCapturingIdentifier` to give a
  nicer error later. A screener must install `BailErrorStrategy` or it will analyse SQL
  Spark would never run.
- Some apparent failures are correct behaviour: `INSERT OVERWRITE DIRECTORY '/tmp/x'`
  without a query is invalid per the grammar, and `$$...$$` is a `codeLiteral` (only
  valid in `CREATE METRIC VIEW`), not a general string literal.

## spike/probe_python.py

Established that ~7 of 9 `spark.sql()` call sites in agent-style code have a
non-literal first argument. This is the finding that made constant folding mandatory
rather than optional: regex-over-source scores near zero on that corpus.

Note this probe also *misclassifies* `"..." % tbl` as a literal, because `%` formatting
is `ast.Mod` and not `ast.Add`. That is the class of error that makes text-matching
screeners unreliable, and it is why the real folder tracks node types.

## spike/probe_fold.py

Measures what a constant-propagation pass can actually recover. It recovers f-strings,
`+`, `%` (scalar and tuple), `.format()`, `"".join`, format specs (`{n:04d}`) and
`!r`/`!a` conversions, and correctly gives up on `input()`, loop variables, function
parameters and dict lookups. Those give-ups are the UNKNOWN verdict — see
`sparkscreen.model.Reason.UNRESOLVED_DYNAMIC_SQL`.

## spike/fuzz_no_false_allow.py

The first fuzz sweep, JVM-free: 1500 seeded token-level mutants of the corpus across all
three pinned grammars, plus probes of every documented folding path. Zero verdict-level
violations — destructive mutants never ALLOW, unparseable mutants never ALLOW, and all
eight folding paths recovered a planted `drop table prod.users`. Also the source of a
test bug worth keeping: its first draft asserted "nonempty effects ⇒ never ALLOW" and
flagged 264 *correct* ALLOWs, because `READ_DATA`-only and additive `WRITE_SCHEMA`
findings are legitimately allowed under the default policy. The property was sharpened to
`DESTROY_DATA`/`LOAD_CODE` before promotion into `tests/test_properties.py` section 5.

## spike/fuzz_differential.py

The same mutation generator pointed at live engines via `parsePlan` (the parse-only
oracle — it throws on syntax errors and never executes, so CREATE/INSERT mutants cannot
touch the warehouse). Same seeded case list on every engine, so results are comparable.
First sweep: 540 cases × 3.5.1 / 4.1.3 / 4.2.0, zero false rejects and six false
accepts — all one class, now F27. This is what found F27; the corpus and the 34
pre-existing property tests had both passed everything for weeks.

## spike/probe_engine_messages.py

Ran the F27 reproducers back through `parsePlan` to capture the engine's own exception
text. That text is what settled the mechanism: `INVALID_SET_SYNTAX` and
`UNSUPPORTED_DATATYPE` are AST-builder rejections against a grammar-level accept, not
grammar drift — the ports agree with the `.g4` files, and the `.g4` files agree with the
engine's grammar. Without the messages, the natural wrong move is "fix the port".

## Case sensitivity (found later, against a real Spark install)

Both ports initially rejected every lowercase statement — `select 1`, `drop table t`,
`SeLeCt 1` — because the vendored grammars spell keywords uppercase and 3.5.1 declares
`fragment LETTER : [A-Z]`. Real Spark 3.5.1 accepts all of them, verified by running
`spark.sql()` under PySpark 3.5.1. `grammar/port.py` therefore emits
`options { caseInsensitive = true; }`. This is why `tests/differential/` exists: the
corpus above was not sufficient to find it.