# Roadmap

Current state, and what is next. Effort estimates are ranges of focused work, and are
optimistic in the way noted at the end — see [How to read these](#how-to-read-these).

---

## Issue ledger

`br` (beads) tracks the open work in this repository, in `./.beads/`. The database is
gitignored by `br` itself; `issues.jsonl` is tracked, so the ledger travels with the
repo.

```bash
br list          # everything open
br ready         # unblocked, in priority order
br show <id>     # full issue with discussion
br create --title "..." --priority 0 --description-file notes.md
br dep add <issue> <depends-on>   # "issue is blocked by depends-on"
br dep tree <issue>
br dep cycles
```

| id | | |
|---|---|---|
| `sparkscreen-rn6` | **P0** | Detect DataFrame write sinks (`saveAsTable`, `save`, `jdbc`) |
| `sparkscreen-120` | P1 | Decision: should unbounded `DELETE`/`MERGE` carry `DESTROY_DATA`? |
| `sparkscreen-r50` | P2 | Decision: `LOAD DATA` `READ_LOCAL_FS` over-approximation |
| `sparkscreen-dhe` | P2 | Split `Verdict.UNKNOWN` into `REVIEW` vs `UNKNOWN` — blocked by `rn6` |
| `sparkscreen-znf` | P3 | `dbutils` and dangerous Python call screening — scope decision first |

Each issue carries its reasoning inline, so the "why" survives without needing
`docs/roadmap.md` open alongside it.

---

## Where we are

`master` is green: **2,479 tests passing in ~16s**, 16 skipped, 8 xfailed (all documented
gaps, none accidental). **51 differential expectations re-verified against a live Spark
3.5.1** — 40 SQL plus 11 DataFrame, the latter asserting row counts actually drop on
overwrite. Wheel ships the parsers and runs with no JVM. A 20-statement file screens in
**5.5 ms** warm.

Detected today, and only this:

- `spark.sql(...)` sinks, positional and keyword forms
- SQL recovered by constant folding through f-strings, `+`, `%`, `.format()`, `join()`,
  format specs — with real Python scoping, so shadowing and rebinding fail closed
- Parsed with Spark's genuine grammar, two pinned versions, strict
- Statement labels, table/namespace targets, string literals extracted from the parse tree
- **DataFrame write sinks**: `saveAsTable`, `save`, `jdbc`, `insertInto`, with write
  mode recovered (`overwrite` destroys, default errors out — both verified live)
- **`Effect` flags per operation**: 122 mapped labels across both grammars, orthogonal
  and fail-loud on an unmapped label
- Declarative policy with namespace allowlists and resource limits
- **Four verdicts** — `ALLOW` / `DENY` / `REVIEW` / `UNKNOWN` — so "we analysed it and a
  human should look" is distinguishable from "we could not analyse it"

Not detected: everything else. See
[known blind spots](findings.md#known-blind-spots).

---

## Blind-spot work

The DataFrame API is the largest remaining gap and the reason the `Effect` axis exists.

### 1. ~~`Effect` axis~~ — done (`0cc01f6`)

Shipped. 122 labels mapped across both grammars, derived from the generated parser
classes rather than a corpus, and `effects_for_label()` raises `UnmappedLabelError`
rather than returning an empty set.

Separate *what an operation does* from *what the policy decided*, per
[D4](decisions.md#d4--effect-is-a-set-of-flags-not-an-enum). Orthogonal flags, derived
from the labels we already resolve, so it is additive rather than a rewrite.

Everything else in this section has somewhere to put its results, which is why this goes
first.

### 2. ~~DataFrame write detection~~ — done (`22ed358`)

Shipped, with the oracle converted into assertions. `tests/differential/
test_dataframe_writes.py` runs against a live Spark 3.5.1 and checks that an overwrite
takes a target from 3 rows to 2, that an append takes it to 4, and that default mode
raises `TABLE_OR_VIEW_ALREADY_EXISTS` and changes nothing. One limitation recorded as
[T5b](threads.md#t5b--dataframe-writes-known-gap): an aliased writer (`w = df.write`) is
missed for `.save`/`.jdbc`.

`df.write.mode(...).saveAsTable(...)`, `.save(...)`, `.insertInto(...)`, `.jdbc(...)`,
`.write.partitionBy(...).save(...)`.

Effect is determinate; only the target may be unresolved
([D5](decisions.md#d5--the-dataframe-api-effect-is-determinate-even-when-the-target-is-not)).
`.mode()` is captured separately, since `overwrite` clobbers and default-mode errors out.

**Now verified against a live engine.** `tests/differential/probe_dataframe_oracle.py`
observes the real effect of each write against a scratch warehouse. Converting that probe
into assertions is the natural first step.

### 3. ~~`dbutils` and Python-level calls~~ — closed as out of scope

`sparkscreen-znf`, decided 2026-10-02. `dbutils.fs.rm`, `shutil.rmtree` and
`os.system` remain unscreened **by decision**. The unused
`Reason.PYTHON_DANGEROUS_CALL` was deleted rather than wired, so the enum no longer
advertises coverage that does not exist.

Rationale: the target environment is an ephemeral pod, where the expensive failure is a
wrong write to a warehouse that belongs to someone else — and the `spark.sql()` path
already covers that. Python screening is a separate tool
([T4](threads.md#t4--pluggable-operation-cataloques),
[T6](threads.md#t6--should-python-level-calls-be-screened-here)).

### 4. Interprocedural constant propagation — ~4h

`def run(tbl): spark.sql(f"drop table {tbl}")` is UNKNOWN today even when every caller
passes a literal. This is the cheap, high-value version of [T1](threads.md#t1--the-inverse-kernel-pseudo-executing-code-so-variation-resolves-itself)
and needs no execution.

Expect this to *increase* the UNKNOWN rate initially while making the remaining UNKNOWNs
far more trustworthy.

---

## Deferred

### ~~`REVIEW` / `UNKNOWN` verdict split~~ — done (`080fb93`)

Shipped. See [D18](decisions.md#d18--split-unknown-into-review-and-unknown-the-exit-code-stays-binary).
The exit-code constraint held: `REVIEW` and `UNKNOWN` both exit 2, as the deferral note
required. Doing it earlier than planned was justified by finding that
`UNSUPPORTED_STATEMENT` had been sitting in the analysis-failure set all along, inflating
the "could not analyse" rate with ordinary policy gaps.

### Adding Spark versions

Add to `SPECS` with a full commit SHA, re-fetch, generate, and let the differential suite
tell you whether it agrees with the engine. The version-specific corpus entries
(`VERSION_SPECIFIC`, `VERSION_SPECIFIC_REJECTED`) are where the expected differences go —
do not put a 4.0-only statement in the shared corpus.

### Mutation testing — running

Agreed and in progress. The argument is exactly your framing: this tests the tests.
For a screener the mutation that matters is `DENY` -> `ALLOW`, and no amount of coverage
finds it, because the line still executes — it just returns the wrong answer.

```bash
.venv/bin/mutmut run       # policy / model / screen / treewalk, ~790 mutants
.venv/bin/mutmut results   # survivors = assertions that do not bite
```

Roughly 790 mutants at ~50s per full-suite run. **Expect hours, not minutes.** A survivor
in `model.py` or `policy.py` is a real screener bug; a survivor in `screen.py` is the
expensive kind, since that is the aggregation path that has already produced one
production bug (F1). The next extensions are `folding.py` and `calls.py`, which decide
what SQL gets screened at all.

Read it as a *measurement*, not a gate: a high survivor count in a module means its
tests need strengthening, not that the module is wrong.

`mutmut-config.toml` exists and is scoped to the decision logic rather than the grammar
port. Worth a real run to find assertions that do not bite.

### Publishing — ~1h, no new code

The wheel builds and is verified in a clean venv with `java` off `PATH`, carrying both
grammar pairs. What is missing is the boring part: a real version number (it is still
`0.1.0`), a `LICENSE`/author block check, a tagged release, and PyPI credentials. This is
the shortest path from "works on my machine" to "installable", and it is the only item
here that is not blocked on a design question.

`mutmut-config.toml` exists and is scoped to the decision logic rather than the grammar
port. Worth a real run to find assertions that do not bite.

---

## Explicitly not doing

- **Rust.** [D11](decisions.md#d11--rust-is-not-the-answer-dropped). 8.1 ms of a 10.4 ms
  budget, against a job that takes seconds. Revisit only if screening becomes a hot path.
- **A mocked runtime.** [T1](threads.md#t1--the-inverse-kernel-pseudo-executing-code-so-variation-resolves-itself).
  The static approximation is already good; interprocedural propagation captures most of
  the value for far less risk.
- **Fixing the `UNKNOWN_REASONS` set.** It is correct as-is.
  [D13](decisions.md#d13--unknown_reasons-is-classification-only-never-a-decision).

---

## How to read these

Estimates assume someone who knows the codebase. They are **optimistic for anything
without an oracle**, and that distinction is the one worth internalising:

- **SQL parsing** had an oracle from the start — Spark's real grammar, then a live engine.
  Two bugs were found only by the live engine ([F6](findings.md#f6--case-insensitivity-found-only-against-a-real-engine)),
  and three more by reading the grammar directly.
- **The DataFrame API has an oracle too, but it had to be built**
  ([D5](decisions.md#d5--the-dataframe-api-effect-is-determinate-even-when-the-target-is-not)).
  It is behavioural rather than a parse decision, and slower per case, but it exists.
- **Python-level call screening has no oracle at all.** There is no ground truth for "is
  `os.system` dangerous in an ephemeral pod" — that is a policy question, not a fact. This
  is the one piece where tests can only check our reasoning, not our correctness, and it
  is why [T6](threads.md#t6--should-python-level-calls-be-screened-here) argues for
  scoping it out rather than guessing.

The general rule this project keeps rediscovering: **a test suite that can only check
your reasoning has not found the bug you care about.** F6 was invisible to a complete,
correct, 100%-passing corpus. Differential oracles are not a luxury here| id | | |
|---|---|---|
| _(none open)_ | | the ledger is empty; all five seeded issues are closed |

Every seeded issue is closed, with its reasoning inline on the issue rather than only in
this file — which is the point of having a tracker. Closed: `rn6` (DataFrame writes),
`120` (`DELETE`/`MERGE` destruction), `r50` (`LOAD DATA`), `znf` (Python scope),
`dhe` (verdict split)..