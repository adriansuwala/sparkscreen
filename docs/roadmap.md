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

`master` is green: **2,199 tests passing in ~15s**, 14 skipped, 8 xfailed (all documented
gaps, none accidental). 40 differential expectations re-verified against a live Spark
3.5.1. Wheel ships the parsers and runs with no JVM. Warm `screen()` is **10.4 ms**
end-to-end, 8.1 ms of which is the ANTLR parse.

Detected today, and only this:

- `spark.sql(...)` sinks, positional and keyword forms
- SQL recovered by constant folding through f-strings, `+`, `%`, `.format()`, `join()`,
  format specs — with real Python scoping, so shadowing and rebinding fail closed
- Parsed with Spark's genuine grammar, two pinned versions, strict
- Statement labels, table/namespace targets, string literals extracted from the parse tree
- Declarative policy with namespace allowlists and resource limits
- Three verdicts, fail-closed, with `confidence` and `analysis_failure` distinguishable

Not detected: everything else. See
[known blind spots](findings.md#known-blind-spots).

---

## Blind-spot work

The DataFrame API is the largest remaining gap and the reason the `Effect` axis exists.

### 1. `Effect` axis — in progress

Separate *what an operation does* from *what the policy decided*, per
[D4](decisions.md#d4--effect-is-a-set-of-flags-not-an-enum). Orthogonal flags, derived
from the labels we already resolve, so it is additive rather than a rewrite.

Everything else in this section has somewhere to put its results, which is why this goes
first.

### 2. DataFrame write detection — ~3h

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

### `REVIEW` / `UNKNOWN` verdict split — ~2h

Today "we analysed it and a human should look" and "we could not analyse it" are both
`UNKNOWN`. Separating them is a breaking JSON change and buys mainly better dashboard
queries — which `is_analysis_failure` already partly provides. Deferred until someone
actually wants the distinction in a query.

Must not change the CLI exit codes: REVIEW and UNKNOWN both map to 2. Same gate, different
queryable reason.

### Adding Spark versions

Add to `SPECS` with a full commit SHA, re-fetch, generate, and let the differential suite
tell you whether it agrees with the engine. The version-specific corpus entries
(`VERSION_SPECIFIC`, `VERSION_SPECIFIC_REJECTED`) are where the expected differences go —
do not put a 4.0-only statement in the shared corpus.

### Mutation testing at scale

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
correct, 100%-passing corpus. Differential oracles are not a luxury here.