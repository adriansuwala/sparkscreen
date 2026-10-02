# Decisions

Each entry records what was decided, why, and what the rejected alternative would have
cost. Numbered so code comments can cite them (`see docs/decisions.md D3`).

Status values: **accepted** (in the codebase), **proposed** (agreed, not built),
**open** (undecided).

---

## D1 — Three verdicts, not two

**Accepted.**

`ALLOW` / `DENY` / `UNKNOWN`, where UNKNOWN means *we could not determine*.

Two verdicts force a lie. When SQL cannot be resolved or parsed, a two-valued screener
must either cry wolf on every dynamic query, or say "safe". The second option is a false
assurance delivered with a confident voice, which is precisely how a dangerous tool gets
trusted. Every internal failure — unparseable SQL, unresolved f-string, unrecognised
statement type, resource limit, Python syntax error — lands on UNKNOWN.

> **Nothing in this package has a code path from "something went wrong" to `ALLOW`.**

---

## D2 — `BailErrorStrategy`, and the nuance that matters

**Accepted.**

ANTLR's default error strategy *invents tokens and keeps going*, returning a usable tree
for input no engine would run. Measured over a 13-input corpus, 9 broken inputs parse
that way — including `INSERT INTO`, `DROP TABLE t WHERE`, and `SELECT ((((1))))))`.

The nuance, which took two attempts to get right: this is **not** the same as Spark's
`errorCapturingIdentifier` leniency. Spark's grammar *deliberately* accepts some malformed
input so the engine can attach a better error later during analysis. Real Spark accepts
`SELECT * FROM t WHERE` and then fails with `AnalysisException`.

So we reject the first category and accept the second, matching the engine. Being
stricter than Spark in the second case would be safe but would hide our agreement for no
gain. Both categories are pinned separately in `tests/test_parser.py` so the distinction
cannot rot.

*Rejected:* keeping the default strategy for better error messages. Fatal for a security
tool — the whole failure mode is analysing SQL Spark would never run.

---

## D3 — Aggregate on verdict, never on reason

**Accepted.** See [findings F1](findings.md#f1--verdict-aggregated-on-reason-instead-of-verdict).

`verdict` is the decision; `reason` is the explanation for a human. Deriving one from the
other made every `verdict=UNKNOWN, reason=DESTRUCTIVE_STATEMENT` rule aggregate as ALLOW,
and made the CLI exit 0 on a report it had just printed as UNKNOWN.

---

## D4 — `Effect` is a set of flags, not an enum

**Accepted.**

Separating *what an operation does* from *what the policy decided about it* was the
single cleanest observation made during development. The original design had the effect
living only in a `statement` string on a policy `Finding`, so nothing downstream could
answer "list everything that mutates".

A single-valued enum was proposed first and **rejected** on the question of whether
statements can be both schema- and data-affecting. They can, and Spark's grammar already
distinguishes them:

```
ALTER TABLE t DROP COLUMN a           -> DropTableColumns     (destroys data)
ALTER TABLE t ADD COLUMN a INT DEFAULT 0 -> AddTableColumns  (additive, reversible)
```

Both are "schema change" in a two-valued axis. `Effect` is therefore a set of orthogonal
flags — `WRITE_SCHEMA`, `WRITE_DATA`, `DESTROY_DATA`, `READ_DATA`, `READ_LOCAL_FS`,
`LOAD_CODE`, `REACHES_EXTERNAL`, `CHANGE_CONFIG` — and `DROP COLUMN` gets
`{WRITE_SCHEMA, WRITE_DATA, DESTROY_DATA}`.

This is what makes the natural policy expressible: `DESTROY_DATA` and `LOAD_CODE` justify
denial regardless of namespace, while `WRITE_SCHEMA` without `DESTROY_DATA` is the
"fine in staging, review in prod" case. One bucket cannot express that asymmetry.

---

## D5 — The DataFrame API effect is determinate even when the target is not

**Accepted.**

```python
df.write.mode("overwrite").saveAsTable(name)   # mutates, whatever name is
df.write.mode("overwrite").saveAsTable(what)    # mutates; `what` is UNKNOWN
```

This is *stronger* than the SQL path, which genuinely cannot resolve `spark.sql(q)` and
must fall to UNKNOWN. Every write path is a mutation regardless of how the name resolves,
so the effect is knowable even when the target is not. `.mode()` matters and is captured
separately — verified against a live Spark 3.5.1:

- `mode("overwrite")` on an existing 2-row table → still 2 rows. It clobbers.
- `saveAsTable` with **no** `.mode()` on an existing table → `AnalysisException:
  TABLE_OR_VIEW_ALREADY_EXISTS`. It refuses.

---

## D6 — Generated parsers ship in the wheel

**Accepted.**

Neither installing nor running requires a JVM. The ANTLR tool is a maintainer-side
dependency only.

Verified rather than assumed: the wheel is ~466 KB including all 8 generated parser
modules and both vendored grammar pairs, and runs from a clean venv with `java` absent
from `PATH`. CI enforces this.

---

## D7 — One ANTLR version for both grammars

**Accepted.**

Spark 3.5.1 pins ANTLR 4.9.3, which **cannot** build its own grammar for the Python
target: labels `from=`, `input=` and `property=` collide with Python runtime attribute
names. 4.13.1 builds both pinned grammars cleanly, so one version serves both.

*Rejected:* honouring each Spark version's historical pin. That would require patching
the grammar's label names, which silently changes what the grammar accepts.

---

## D8 — Grammars pinned to full immutable commit SHAs

**Accepted.** See [findings F5](findings.md#f5--grammars-pinned-to-a-short-sha-and-a-movable-tag).

A short SHA and a movable tag are not pins. Since the two grammars disagree on `CALL`,
`|>`, and `BEGIN…END`, a moved tag changes released verdicts with no diff to show for it.

*Rejected:* pinning to release tags. Convenient, and defeats the entire purpose.

---

## D9 — `caseInsensitive` is load-bearing, not cosmetic

**Accepted.** See [findings F6](findings.md#f6--case-insensitivity-found-only-against-a-real-engine).

The vendored grammars spell keywords uppercase and 3.5.1 declares `fragment LETTER :
[A-Z]`, so a literal port is case-sensitive. Spark SQL is not. Lowercase is the common
case in agent-written PySpark, so without this the screener is unusable in practice while
passing a suite written in uppercase.

---

## D10 — Differential testing against a real engine

**Accepted.**

The only way to find a bug where our parser *disagrees with the engine it screens for*.
The case-insensitivity bug was invisible to a complete, correct, 100%-passing hand-written
corpus.

`tests/differential/` runs the parser against a real PySpark 3.5.1 and compares
accept/reject. Expectations are *recorded observations*, not inferences — which is how
`INSERT INTO t SELECT * FROM` came to be correctly classified as accepted (see D2).

The direction of disagreement matters: we are allowed to reject what Spark accepts (a
spurious UNKNOWN is safe, just noisy). We are **not** allowed to accept what Spark
rejects, because that means analysing SQL the engine would never run.

---

## D11 — Rust is not the answer. Dropped.

**Accepted, on measurement.**

A 379-character realistic query parsed in ~39 ms during the spike; the full warm
end-to-end `screen()` is **10.4 ms**, of which **8.1 ms is the ANTLR parse**.

That number is irrelevant next to Spark job submission, which is seconds to minutes. A
Rust port via `antlr4rust` would genuinely be faster, and the constant-folding pass would
stay pure-Python regardless. Not worth the complexity. Revisit only if screening ever
becomes a hot path, e.g. screening 10,000 snippets interactively.

*Rejected:* speculatively porting to Rust because "39 ms sounds slow". The user called
this correctly — measure, do not guess.

---

## D12 — Fail closed on wrong answers, not just on errors

**Accepted.**

A confidently-wrong finding is worse than no finding. If the folder cannot prove a value,
it reports UNKNOWN rather than its best guess:

```python
tbl = 'prod.t'
for tbl in tables:  spark.sql(f'drop table {tbl}')   # -> UNKNOWN, not 'drop table prod.t'
```

The operator sees a clean specific verdict for a statement that will never run, and has
no reason to doubt it. This drove the `SinkKey` change and the scoping rewrite.

---

## D13 — `UNKNOWN_REASONS` is classification only, never a decision

**Accepted.**

`Finding.is_unknown` reads the verdict; `Finding.is_analysis_failure` reads the reason.
`OUTSIDE_ALLOWLIST` is deliberately **outside** `UNKNOWN_REASONS`: an allowlist rejection
means we did the analysis and it came back outside policy, which is a different thing from
we could not analyze. Keeping them separate is what lets a dashboard report the two
separately.

This was pushed back on during development by a subagent suggesting `OUTSIDE_ALLOWLIST`
be "fixed" into the set. Conflating them would have re-created exactly the bug in D3.

---

## D14 — master is the release line

**Accepted.**

`master` is green at every commit and carries no known fail-open path. Feature work goes
on branches, parallel work on `git worktree`s with one agent per worktree.

Not stylistic: concurrent agents editing adjacent files caused real conflicts in
`tests/test_screen_policy.py` twice, which had to be unpicked by hand. See
`CONTRIBUTING.md`.