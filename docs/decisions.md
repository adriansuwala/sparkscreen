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

---

## D15 — `DESTROY_DATA` on `DELETE`/`UPDATE`/`MERGE` is a slowdown flag, not a damage estimate

**Accepted** (`sparkscreen-120`, 2026-10-02). Reverses the table's original reasoning.

The table first classified `UPDATE`/`DELETE`/`MERGE` as `WRITE_DATA` only, on the
grounds that "both are bounded by their `WHERE` clause, and what they destroy is decided
by the predicate rather than the label." That reasoning is true and it answers the wrong
question.

A screener is not asked whether a particular `DELETE` is harmful. It is asked whether a
person needs to look. `DELETE FROM t WHERE id = 3` removes a record, and one row is not
the same thing as safe — so if the screener can certify it as harmless, a user's record
can be removed on the strength of an analysis that turned out to be wrong. That is the
exact failure this project exists to prevent, and it does not matter that the statement
looked narrow.

So `DESTROY_DATA` here means "rows are removed and a person should carry that out
deliberately", not "this will lose a lot". Policy may still auto-approve a `DELETE` in a
sandbox namespace. The cost is that routine cleanup needs an explicit allowance, and that
is the intended price.

`INSERT INTO` deliberately stays `WRITE_DATA` — it cannot remove what is stored, so
`test_insert_into_does_not_qualify` pins the asymmetry and gives the rule a counterweight
rather than letting it widen silently.

**Generalisable:** a classifier should answer the question its consumer will ask. Asking
"is this harmful?" invites a proportionate answer and returns ALLOW too often; asking
"does a human need to look?" returns something safe to gate on. The tool's output is read
by a person deciding whether to press go, so the second question is the one that matters.

---

## D16 — a never-emitted `Reason` is a defect

**Accepted** (`sparkscreen-znf`, 2026-10-02).

`Reason.PYTHON_DANGEROUS_CALL` existed and was never raised. Deleted rather than wired:
Python-level call screening is out of scope (target environments are ephemeral pods, where
the expensive failure is a wrong warehouse write — which the `spark.sql()` path covers),
and the honest options were to implement it or stop implying it.

An enum is a promise about what the code can produce. A member that never appears in
output makes the tool look more capable than it is, which is the same confident-wrong-
answer hazard as everything else in [findings](findings.md). The rule generalises: if you
cannot name a test that emits a value, it does not belong in the enum.

---

## D17 — keep the over-approximation when the false positive is cheaper


## D18 — split UNKNOWN into REVIEW and UNKNOWN; the exit code stays binary

**Decision.** `Verdict` gains a fourth member. `REVIEW` means *we analysed it and a person
should decide*; `UNKNOWN` means *we could not analyse it*. Both exit 2.

**Why.** The old three-value scale collapsed two genuinely different failures into one
word. `SELECT * FROM secret.salaries` outside `readable_namespaces` and
`spark.sql(q)` with an unbound `q` both reported UNKNOWN, and both exited 2, and both
mean "do not run this". But they are not the same event. One is the screener saying "I
did my job and the answer is no"; the other is the screener saying "I could not do my
job". A team triaging that queue has two different remedies — write a policy rule, or fix
the tool — and one number cannot tell them apart.

The old code did carry the distinction, but sideways and unreliably: it was in
`is_analysis_failure`, a *reason*-keyed property that had to be kept in sync with a
*verdict*-keyed enum by hand. `UNSUPPORTED_STATEMENT` was the tell — it reads like a
failure, and it was classified as one, even though the statement was parsed perfectly well
and all we lacked was an opinion.

**Why the exit code does not split.** A CI gate wants one bit. Any non-zero exit already
means "a human looks at this". Making `REVIEW` exit 3 would force every consumer to
update its threshold for information it was not using, and the first consumer that
mapped `!= 0` to a single error page would silently conflate them again. The richness
belongs in the `verdict` field, which every consumer already parses.

**Consequences accepted.**

- It is a breaking API change. `Verdict` has four members now, and consumers that
  exhaustively switch on it will notice. That is the point — the old switch could not
  distinguish two states that are now distinguishable.
- `UNKNOWN_REASONS` is renamed `ANALYSIS_FAILURE_REASONS` and loses
  `UNSUPPORTED_STATEMENT`. The name had to change; it was lying.
- `is_analysis_failure` is now defined as `verdict is UNKNOWN`. The old test asserted it
  was computed from the reason *independently* of the verdict. That test was asserting
  the defect. It is replaced by one asserting the two agree, and that a REVIEW carrying
  an analysis-failure-looking reason is not a failure.
- `_combine()` had to be reordered. It preferred the UNKNOWN finding as primary, so once
  the allowlist began emitting REVIEW it stopped matching and a plain DENY became primary,
  silently dropping `OUTSIDE_ALLOWLIST` from the report. It now prefers by attention
  needed rather than by one specific verdict, which makes that class of regression
  structural rather than incidental.

**Accepted** (`sparkscreen-r50`, 2026-10-02).

`LOAD DATA INPATH` and `LOAD DATA LOCAL INPATH` share the `LoadData` label, so
`READ_LOCAL_FS` fires on both. The precise fix is keying on `(label, local)` from the
parse tree. We keep the approximation.

The bias is what matters: the canonical attack is `LOAD DATA LOCAL INPATH '/etc/passwd'`,
so the direction that errs toward *not* flagging is the one we must not take. One false
positive on the non-LOCAL form is a cheaper error than one false negative on the LOCAL
form, and the flag can be waived in policy. Precise-but-fail-open would be the wrong
trade for a security tool; "don't fragment the flags into too many pieces" is the same
instinct stated more generally.