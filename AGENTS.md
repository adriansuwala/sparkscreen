# AGENTS.md — sparkscreen

A static safety screener for agent-written PySpark. If you are an agent or new
contributor, this file is the orientation you need. Everything here is deliberately
stable; if you find something here that is no longer true, that is a bug worth
reporting, not a thing to quietly work around.

## What this tool is

Agent-written PySpark frequently hides the SQL in an f-string, so screening the literal
text of `spark.sql(...)` misses most real code. `sparkscreen` recovers the SQL statically,
parses it with Spark's *actual* ANTLR grammar, and applies a declarative policy. It ships
the generated parsers, so **no JVM is needed at runtime**.

Two design commitments that explain most of the codebase:

1. **Fail closed.** Anything it cannot prove safe is `UNKNOWN`, never `ALLOW`. A screener
   that reports "no issues" on code it could not analyse is a false assurance with a
   confident voice, which is how a dangerous tool gets trusted.
2. **Parse, don't match.** Statements are identified by their parse tree, not by string
   matching. `"DR" + "OP TABLE t"` is identical to `"DROP TABLE t"`, and there is no
   text to evade.

## The four verdicts

| verdict | meaning | exit |
|---|---|---|
| `ALLOW` | resolved, parsed, nothing objected | 0 |
| `DENY` | a deny rule matched | 1 |
| `REVIEW` | we analysed it, a person should decide | 2 |
| `UNKNOWN` | we could not analyse it | 2 |

`REVIEW` and `UNKNOWN` both exit 2 deliberately — the exit code is the binary gate, and
which of the two it was is in the `verdict` field. Do not "fix" this by splitting exit
codes.

## Setup and the commands that matter

Two virtualenvs. This trips up everyone.

```bash
# .venv — fast suite, no JVM, ~16s. Use this for everything by default.
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q

# .venv-pyspark — only for the differential suite. Has pyspark==3.5.1.
# The main .venv deliberately has NO pyspark.
export JAVA_HOME=$(ls -d /opt/data/home/.jre/*)
PATH="$JAVA_HOME/bin:$PATH" PYTHONPATH=src .venv-pyspark/bin/python \
    -m pytest tests/differential/ -q
```

Docs audits are separate scripts, not part of the suite:

```bash
PYTHONPATH=src .venv/bin/python _verify_docs.py    # 45 claims in docs/user-docs/
PYTHONPATH=src .venv/bin/python _verify_effect.py  # 24 Effect-design claims
```

Issue ledger is `br` (beads). `br list --status open`; the DB is gitignored but
`issues.jsonl` is tracked, so **use the CLI, not hand-edits** — `br list` reads its
SQLite DB and will silently disagree with a hand-edited JSONL.

```bash
/opt/data/.local/bin/br list --status open
```

## Current state (2026-10-02)

- 2,479 tests passing, 16 skipped, 8 xfailed (all documented gaps)
- 51 differential expectations against live Spark 3.5.1
- 122 statement labels mapped to `Effect` flags across both pinned grammars
- 5.5 ms for a 20-statement file, warm

## Invariants you must not break

These are the project's load-bearing properties. Each has a test named after it; if you
find yourself deleting or weakening one, stop.

- **Every sink is either resolved or unresolved — never neither, never both.** A sink
  dropped on the floor is a false ALLOW. Keyed by per-sink ordinal, not line number: one
  line can hold two sinks.
- **A stale binding is worse than no binding.** Any rebinding (loop target, `with`,
  function param, augmented assign, `global`, `del`) invalidates the value. A stale
  constant produces a clean, specific, *wrong* finding.
- **`Report.verdict` aggregates on `verdict`, never on `reason`.** Aggregating on reason
  let `DELETE`/`UPDATE`/`MERGE`/`INSERT` report ALLOW. This has now been done by accident
  once; the guard is `test_aggregation_follows_verdict_not_reason`.
- **`effects_for_label()` raises `UnmappedLabelError` on a miss.** It must never return an
  empty set. `screen()` deliberately does not catch it. Resist the urge to add a
  try/except "to keep the CLI usable".
- **Grammar pins are full 40-character commits.** Never a short SHA, a branch, or a
  movable tag. Enforced by `tests/test_grammar_port.py`.
- **No reachable label without an effect mapping.** The label universe is derived from
  the generated parser classes, not from a corpus.
- **An enum member that is never emitted is a bug.** `Reason.PYTHON_DANGEROUS_CALL` was
  removed for exactly this reason.

## Traps that have actually cost time

- **Case-insensitivity is real.** Spark SQL is case-insensitive; the ports set
  `caseInsensitive = true`. Do not "fix" this — it was established against a live engine,
  not guessed.
- **Spark 3.5.1's root rule is `singleStatement`, 4.0's is `compoundOrSingleStatement`.**
  The tree walk handles both. 3.5.1 has no `EXECUTE` token at all.
- **Token numbers differ per grammar.** Resolve identifiers to token types via the spec,
  never by integer comparison.
- **The generated parsers are committed on purpose.** They ship in the wheel so runtime
  users need no JVM. Do not gitignore them.
- **In-memory catalog doesn't support `REPLACE TABLE`.** Seed differential tables via the
  DataFrame API (`saveAsTable`), not `CREATE OR REPLACE TABLE`.
- **A correctness property held by an exception is not held.** Three separate bugs had
  this shape (F8, F9, F13). Prefer a rule over the domain, tested by its name.

## Adding a feature

1. Check `docs/roadmap.md` and `br list` first — it may already be reasoned about.
2. Decisions go in `docs/decisions.md` with a number (D19, D20...) and a "why we might be
   wrong". Findings go in `docs/findings.md` as F-numbers. Open musings go in
   `docs/threads.md` as T-numbers.
3. **Verify against a real engine before believing a semantic claim.** The grammar is a
   good oracle for *shape* and a poor one for *meaning*. Case-insensitivity and
   overwrite-vs-error both looked like grammar defects and were not.
4. Tests that are not useful are worse than no tests. Prefer a differential assertion
   against real Spark over a self-consistent unit test where you can.

## Repository map

| path | what |
|---|---|
| `src/sparkscreen/screen.py` | the pipeline; SQL sinks + DataFrame writes |
| `src/sparkscreen/model.py` | `Verdict`, `Effect`, `Reason`, `Finding`, `Report` |
| `src/sparkscreen/policy.py` | declarative rules, namespace allowlists, limits |
| `src/sparkscreen/analysis/folding.py` | scope-aware constant propagation |
| `src/sparkscreen/analysis/calls.py` | DataFrame write detection |
| `src/sparkscreen/analysis/effects.py` | label → `Effect` mapping, fail-loud |
| `src/sparkscreen/grammar/` | spec (pins), port, build, generated parsers |
| `docs/user-docs/usage.md` | the user guide |
| `docs/WORKING.md` | index of the reasoning documents |
| `docs/agents.md` | deeper contributor/agent practices |

## Scope

Screening Spark SQL and Spark DataFrame writes. **Not** general Python
(`os.system`, `shutil.rmtree`, `dbutils` are out of scope *by decision* — see
`sparkscreen-znf`). Not Rust; 5.5 ms against a multi-second job is not a bottleneck.
