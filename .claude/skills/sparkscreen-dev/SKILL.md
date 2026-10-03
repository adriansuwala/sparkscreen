---
name: sparkscreen-dev
description: Use when changing sparkscreen internals -- adding a label, policy rule, grammar version, or folding capability. Covers the invariants and the two traps that recur.
---

# Working on sparkscreen

## Two venvs

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q     # fast, no JVM, ~16s

export JAVA_HOME=$(ls -d /opt/data/home/.jre/*)
PATH="$JAVA_HOME/bin:$PATH" PYTHONPATH=src .venv-pyspark/bin/python \
    -m pytest tests/differential/ -q                     # needs pyspark 3.5.1
```

The fast `.venv` has no pyspark on purpose. The differential suite needs the other one
plus a JRE. Do not "fix" that by installing pyspark into the fast venv — it slows the
feedback loop for a suite that should stay instant.

## Invariants with tests named after them

- **Every sink is resolved or unresolved, never neither, never both.** Keyed by per-sink
  ordinal, not line number: `spark.sql("DROP TABLE a"); spark.sql("DROP TABLE b")` is
  one line and two sinks.
- **A stale binding is worse than no binding.** Any rebinding invalidates. A stale
  constant produces a clean, specific, *wrong* finding.
- **Aggregate on `verdict`, never on `reason`.** Doing otherwise let
  `DELETE`/`UPDATE`/`MERGE`/`INSERT` report ALLOW.
- **`effects_for_label()` raises `UnmappedLabelError`.** Never return an empty set, never
  add a try/except "to keep the CLI usable".
- **Grammar pins are full 40-char commits.** Not short SHAs, branches, or movable tags.
- **An enum member that is never emitted is a bug.** `PYTHON_DANGEROUS_CALL` was removed
  for this reason.

## The recurring trap: a property held by an exception

Three separate bugs had this shape — F8 (`elif` between the two allowlist checks), F9
(`EXECUTE IMMEDIATE` not under direct parser children), F13 (`OUTSIDE_ALLOWLIST` correct
only by being left out of a set, with a comment saying it must stay out). The behaviour was
right for the cases anyone had thought of and wrong for the next one.

When a property is "this case is absent" or "this case happens to be here", express it as
a rule over the whole domain, and test it by name rather than by example.

## Adding a statement label

1. The label universe is **derived from the generated parser classes**, not a corpus — a
   corpus only proves labels you thought to write SQL for.
2. Add it to `LABEL_EFFECTS` or it raises at screening time.
3. Run `policy_label_drift()`; it reports labels a rule covers but `DESTRUCTIVE_LABELS`
   omits, and the reverse.
4. Corpus tests run *in addition* to the structural derivation, to catch a bug in the
   derivation itself.

## Adding a Spark version

Add to `SPECS` with a **full commit SHA**, re-fetch, generate. Put version-specific
expectations in `VERSION_SPECIFIC` / `VERSION_SPECIFIC_REJECTED` — never in the shared
corpus. Known divergences: 3.5.1's root is `singleStatement` (4.0's is
`compoundOrSingleStatement`), 3.5.1 has no `EXECUTE` token, and token numbers differ per
grammar so identifiers must be resolved via the spec.

## Test a claim against a real engine before believing it

The grammar is a good oracle for shape and a poor one for meaning. Case-insensitivity and
overwrite-vs-error both looked like grammar defects and were not — one needed a live Spark,
the other needed row counts.

In-memory catalog does not support `REPLACE TABLE`; seed via `saveAsTable`.

## Regression tests: name the bug

Existing ones follow "the X used to do Y" with the reason. The strongest are tests that
*delete a guard to prove the guard bites* — e.g. removing a label mapping and asserting the
screener raises. An assertion that cannot fail is worse than no test.

## Mutation testing

```bash
.venv/bin/python scripts/mutate.py   # ~244 mutants over the decision logic
.venv/bin/python scripts/mutate.py --module model.py
```

Use this, **not** `mutmut run`: mutmut 3.8.0 deadlocks on this project at scale, and it
cannot subset tests per mutant, which is the actual cost driver.

Scoped to decision logic on purpose. A survivor in `model.py` is a screener bug that could
turn `DENY` into `ALLOW` — exactly the thing coverage cannot see, because the line still
executes and just returns the wrong answer.

## Recording decisions

- `docs/decisions.md` — numbered D-numbers, with a "what would change our mind"
- `docs/findings.md` — F-numbers, the defect catalogue
- `docs/threads.md` — T-numbers, open musings
- Issue ledger via `br` CLI. `br list` reads its SQLite DB, so a hand-edited
  `issues.jsonl` will silently disagree.
