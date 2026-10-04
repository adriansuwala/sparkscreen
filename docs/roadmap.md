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

Every row below is **closed**. This table is a pointer to where each decision landed,
because `docs/decisions.md` is where the reasoning actually lives. Open work is filed as
`br` issues, not here — run `br list` for that, and treat its output as authoritative
over anything written here.

| id | | decided in |
|---|---|---|
| `sparkscreen-rn6` | DataFrame write sinks (`saveAsTable`, `save`, `jdbc`) | shipped — see "Where we are" below |
| `sparkscreen-dhe` | Split `UNKNOWN` into `REVIEW` vs `UNKNOWN` | [D18](decisions.md#d18--split-unknown-into-review-and-unknown-the-exit-code-stays-binary) |
| `sparkscreen-120` | Should unbounded `DELETE`/`MERGE` carry `DESTROY_DATA`? | [D15](decisions.md#d15--destroy_data-on-deleteupdatemerge-is-a-slowdown-flag-not-a-damage-estimate) — yes |
| `sparkscreen-r50` | `LOAD DATA` `READ_LOCAL_FS` over-approximation | [D17](decisions.md#d17--keep-the-over-approximation-when-the-false-positive-is-cheaper) — keep it |
| `sparkscreen-znf` | `dbutils` / dangerous Python call screening | [D16](decisions.md#d16--a-never-emitted-reason-is-a-defect) — out of scope |

Open work is filed as `br` issues, not in this file. If you are adding a row here, it is
because the decision was made and you are recording where; the reverse — deciding a
question *in* this table — is what made it drift in the first place.

---

## Where we are

`master` is green: **3,833 tests passing in ~49s** with a JVM (3,806 without — the extra
27 are the pin-identity guard, which generates parsers), 20 skipped, 10 xfailed (all
documented gaps, none accidental). **90 differential tests collect against a live Spark
3.5.1** — 87 pass, 3 skip — across four differential modules; 18 engine-specific
expectations (6 per engine) are recorded for 3.5.1, 4.1.3 and 4.2.0, all three verified
against live engines. The wheel ships the parsers and runs with no JVM. A 20-statement
file screens in **11.8 ms** warm (median, measured by `scripts/bench.py`).

Detected today, and only this:

- `spark.sql(...)` sinks, positional and keyword forms
- SQL recovered by constant folding through f-strings, `+`, `%`, `.format()`, `join()`,
  format specs — with real Python scoping, so shadowing and rebinding fail closed
- Parsed with Spark's genuine grammar, three pinned versions, strict
- Statement labels, table/namespace targets, string literals extracted from the parse tree
- **DataFrame write sinks**: `saveAsTable`, `save`, `jdbc`, `insertInto`, with write
  mode recovered (`overwrite` destroys, default errors out — both verified live), including
  writers bound through an alias or through both arms of an `if`
- **`Effect` flags per operation**: 122 mapped labels, orthogonal and fail-loud on an
  unmapped label (the label universe derived across all three grammars is 112)
- Declarative policy with namespace allowlists and resource limits
- **Four verdicts** — `ALLOW` / `DENY` / `REVIEW` / `UNKNOWN` — so "we analysed it and a
  human should look" is distinguishable from "we could not analyse it"

Not detected: everything else. See
[known blind spots](findings.md#known-blind-spots).

---

## Blind-spot work

The DataFrame API is the largest remaining gap and the reason the `Effect` axis exists.

### 1. ~~`Effect` axis~~ — done

(The commit SHAs this file used to cite here do not resolve in this repository's history;
cited by name instead, with the code and the test as the evidence.)

Shipped. 122 labels mapped, derived from the generated parser classes rather than a
corpus, and `effects_for_label()` raises `UnmappedLabelError` rather than returning an
empty set.

Separate *what an operation does* from *what the policy decided*, per
[D4](decisions.md#d4--effect-is-a-set-of-flags-not-an-enum). Orthogonal flags, derived
from the labels we already resolve, so it is additive rather than a rewrite.

Everything else in this section has somewhere to put its results, which is why this goes
first.

### 2. ~~DataFrame write detection~~ — done

Shipped, with the oracle converted into assertions. `tests/differential/
test_dataframe_writes.py` runs against a live Spark 3.5.1 and checks that an overwrite
takes a target from 3 rows to 2, that an append takes it to 4, and that default mode
raises `TABLE_OR_VIEW_ALREADY_EXISTS` and changes nothing. Aliased writers
(`w = df.write`) are covered, including one bound in **both arms of an `if`**, which was
the last fail-open here and is now closed — see [F16](findings.md#f16--a-writer-bound-in-both-arms-of-an-if-loses-its-binding-savejdbc-report-allow).

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

### 4. ~~Interprocedural constant propagation~~ — done

Shipped in `e8e27d9`. `def run(tbl): spark.sql(f"drop table {tbl}")` is `DENY` when every
call site in the file passes a literal and all of them agree, and `for t in ["a","b"]`
unrolls to two sinks. No execution, no shim. This was the cheap, high-value version of
[T1](threads.md#t1--the-inverse-kernel-pseudo-executing-code-so-variation-resolves-itself).

What is still unresolved is listed in
[threads.md](threads.md#interprocedural-folding--done-e8e27d9): recursion, decorators,
generators, methods, `*args`/`**kwargs`, non-literal defaults and disagreeing call sites.
Resolution is per-file, which is the same trade T1 accepted.

---

## Deferred

### ~~`REVIEW` / `UNKNOWN` verdict split~~ — done

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

### Mutation testing — mutmut, running at full scale

Agreed and in progress. The argument is exactly your framing: this tests the tests.
For a screener the mutation that matters is `DENY` -> `ALLOW`, and no amount of coverage
finds it, because the line still executes — it just returns the wrong answer.

```bash
.venv/bin/mutmut run --max-children 4   # ~3,200 mutants over the decision logic
.venv/bin/mutmut results                 # survivors = assertions that do not bite
```

**Correction: mutmut was never broken here.** It appeared to deadlock — seven processes at
0% CPU in `futex_do_wait`, no output past "Generating mutants` — and I concluded the tool
did not scale, then built `scripts/mutate.py` to replace it. The actual cause was never
reading the linked section of mutmut's own documentation, which says to set
`process_isolation = "forkserver"` when a run hangs. With that one line it runs without
difficulty. The lesson is recorded as F15: *a tool failing in a specific way is a
hypothesis about a configuration, not a fact about the tool.*

Two genuine test bugs surfaced while wiring it up, both worth having found either way:

- `pythonpath = ["src"]` is now set in `[tool.pytest.ini_options]`. The suite passed by
  hand only because `PYTHONPATH=src` was set in the shell; any tool spawning pytest as a
  subprocess inherited nothing and got `ModuleNotFoundError`.
- Four tests assert properties of *the git checkout* (`.gitignore`, the index, a built
  wheel), so they fail in any copied tree. They now skip outside a checkout via
  `is_source_checkout()`.

`scripts/mutate.py` stays as a lighter cross-check, but its numbers are the less
trustworthy of the two: its generator is line-based and skipped 127 of 244 mutants, where
mutmut's libcst rewrite applies all of them and generates 3,209.

Read survivors as a *measurement*, not a gate: a high count in a module means its tests
need strengthening, not that the module is wrong. A survivor in `model.py` or `policy.py`
is a real screener bug; a survivor in `screen.py` is the expensive kind, since that is the
aggregation path which has already produced one production bug (F1).


### Publishing — versioning and release scripts, no PyPI

**Decided 2026-10-04: not publishing to PyPI.** Installation is via a GitHub link, which
is what the wheel was already verified to support — it builds, installs into a clean venv
and runs with `java` off `PATH`, carrying the generated parsers.

What that makes necessary, and what it does not:

- **Still needed** — a tagged release (`git tag` + a release entry) and whatever scripting
  makes a release repeatable: version bump, changelog, tag, and the CI checks that must
  pass first. "Installable from a GitHub link" still requires a tag to point at, and the
  version is currently only `0.8.0` in `pyproject.toml`.
- **No longer needed** — PyPI credentials, a `.pypirc`, trusted publishing, and the
  name-availability question.

The version is the single source of truth in `pyproject.toml`, read from there by
`src/sparkscreen/__init__.py`. A release script should read it rather than parse it twice,
so there is one number.

The shortest path from "works on my machine" to "installable", and the only item here that
is not blocked on a design question.

## Explicitly not doing

- **Rust.** [D11](decisions.md#d11--rust-is-not-the-answer-dropped). 11.8 ms for a
  20-statement file, against a job that takes seconds. Revisit only if screening becomes a
  hot path.
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

Every seeded issue is closed, with its reasoning inline on the issue itself rather than
only in this file — which is the point of having a tracker. Closed: `rn6` (DataFrame
writes), `120` (`DELETE`/`MERGE` destruction), `r50` (`LOAD DATA`), `znf` (Python
scope), `dhe` (verdict split).

Open, as of 2026-10-04: `kie` (versioning and release scripts, no PyPI) and `sxl` (the
real-snippet corpus — deliberately non-synthetic). Run `br list` for current state.