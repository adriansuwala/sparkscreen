# Findings

Every entry is a defect that was **actually found, reproduced, and fixed** in this
repository — not a hypothetical. Each carries a reproducer that returns the wrong answer
on the code before its fix, so the entry can be re-checked if the area is ever touched
again.

These are ordered by severity, not by date. The common thread is worth stating once:

> Almost every bug here is a **fail-open**: the screener reported ALLOW, or reported
> nothing, or reported something confident and wrong. None of them crashed. That is the
> specific danger of a security tool — a crash is visible, a plausible wrong answer is
> trusted.

Several were found by subagents contradicting the maintainer. Those are marked, because
the fact that the test suite disagreed with the person who wrote it is itself a finding.

---

## F1 — Verdict aggregated on reason instead of verdict

**Severity: critical.** Four statement classes silently waved through.

`Report.verdict` asked *"is this finding's `reason` in `UNKNOWN_REASONS`?"* rather than
*"is its `verdict` UNKNOWN?"*. Those are independent axes, and the rules carrying row
mutation have exactly the shape the old code got wrong:

```python
Rule(verdict=UNKNOWN, reason=DESTRUCTIVE_STATEMENT)   # parsed fine, want a human
```

so `DELETE` / `UPDATE` / `MERGE` / `INSERT` aggregated to ALLOW.

The CLI was worse in kind: it *printed* `UNKNOWN:` and *exited 0*, so a CI gate keyed on
the exit code approved unanalyzed code.

```python
screen('spark.sql("delete from prod.users")')            # ALLOW  ->  UNKNOWN
screen('spark.sql("insert into prod.t values (1)")')     # ALLOW  ->  UNKNOWN
```

**Fix.** Split the concepts: `Finding.is_unknown` reads the verdict and drives decisions;
`Finding.is_analysis_failure` reads the reason and is classification only. `Report.verdict`
aggregates on verdict. `cli.main` returns the exit code for the verdict it printed.

**Lesson.** Two fields that look interchangeable are not. `verdict` is the decision,
`reason` is the explanation shown to a human.

---

## F2 — Sinks dropped when two shared a line

**Severity: critical.** `resolved` / `unresolved` were dicts keyed by **line number**.

```python
screen('spark.sql("DROP TABLE prod.users"); spark.sql("select 1")')
# -> ALLOW, 1 finding. The DROP was discarded as a duplicate key.
```

A line is not a sink identity. Sinks are now keyed by `SinkKey(line, col, ordinal)`, and
an invariant test asserts every sink lands in exactly one dict and none can land in
neither.

---

## F3 — Keyword-form sinks vanished entirely

**Severity: critical.** Zero findings, not a wrong one.

```python
screen('spark.sql(query="DROP TABLE prod.users")')     # -> ALLOW, 0 findings
```

The sink was neither resolved nor unresolved, so it simply did not exist as far as the
report was concerned. `spark.sql(**kwargs)` and `args`-only calls now fail loudly with a
specific reason.

---

## F4 — Scope-unaware folding produced confident, wrong SQL

**Severity: high.** The constant table was flat, so any rebinding left a stale value and
the folder reported a *wrong but plausible* SQL string.

```python
tbl = 'prod.t'
for tbl in tables:  spark.sql(f'drop table {tbl}')
# -> reported: 'drop table prod.t'
```

This is worse than the fail-open cases above. A specific, clean-looking finding for a
statement that will never run is more dangerous than no finding, because the operator has
no reason to doubt it. Now a `_Frame` stack gives real scoping and every rebinding
invalidates — for/async-for/with/except targets, parameters, comprehension variables,
`match` captures, `AugAssign`, `del`, imports, unpacking, `global`/`nonlocal`, and writes
inside conditionals. These cases are UNKNOWN.

---

## F5 — Grammars pinned to a short SHA and a movable tag

**Severity: high.** Supply chain, not correctness.

```python
spark-4.0    commit='3c28a9c0'      # 8-char short SHA
spark-3.5.1  commit='v3.5.1'        # a tag, which can be re-pointed
```

The module docstring claimed the grammar was *"fetched by commit, never by tag or branch,
so a force-push or tag move cannot silently change what we parse with."* Neither pin
honoured that. The two grammars genuinely disagree — `spark-4.0` accepts `CALL`, `|>`, and
`BEGIN…END` scripts, `spark-3.5.1` rejects all three — so a moved tag changes the
accept/reject verdict of released code with **no diff in this repository to show for it**.

Resolved to full SHAs and verified by re-fetching: both vendored `.g4` files are
byte-identical. A test now enforces the 40-character invariant.

---

## F6 — Case-insensitivity: found only against a real engine

**Severity: high, and the most instructive entry here.**

Both ports rejected every lowercase statement — `select 1`, `drop table t`, even
`SeLeCt 1`. The vendored grammars spell keywords uppercase and 3.5.1 declares
`fragment LETTER : [A-Z]`, so a literal port is case-*sensitive*.

Real Spark accepts all of them, verified by running `spark.sql()` under PySpark 3.5.1.

This one is in the list because of *how* it was found. The hand-written corpus was 100%
correct and 100% uppercase. No amount of additional hand-written testing would have
caught it. It took running the real engine. That is the argument for
`tests/differential/` existing at all.

Fix: `options { caseInsensitive = true; }` in the ported lexer.

---

## F7 — `extract_namespaces` reported columns as namespaces

**Severity: high.** These targets feed namespace allowlists.

```python
select a as b from t                        -> [t, b]
with q as (select * from prod.t) select 1   -> [prod.t, q]
create table prod.t (a int) using parquet  -> [prod.t, a, parquet]
update prod.t set a = 1                     -> [prod.t, a]
```

`ErrorCapturingIdentifier` and `MultipartIdentifier` are Spark's *generic* identifier
rules, used for column aliases, CTE names, column definitions, table providers and
`SET` targets, so the extractor could not tell a table position from a column position.

Fixed by restricting collection to the rules only ever used in a table position.
Non-negotiable regression guard: `DROP TABLE prod.users` must still yield `prod.users`.
Losing a table is far worse than reporting a column.

---

## F8 — A policy check was silently disabled by its neighbour

`policy.py` joined two allowlist checks with `elif`, so whichever fired first ate the
other. With a realistic policy — the default rules plus both namespace lists — this is
what that produced:

```python
p = default_policy()
p.writable_namespaces = ("staging.*",)
p.readable_namespaces  = ("prod.*",)

screen('spark.sql("DROP TABLE staging.x")', p).findings[0].reason
# before: DenyRule           (staging.x passes the writable check; readable never runs)
# after : OutsideAllowlist   (staging.x is plainly outside prod.*)
```

That is precisely the policy shape a real deployment uses — "you may write staging, you
may read prod" — and it was checking half of it.

*Note for reproducers:* this one needs `default_policy()`'s rules. A bare `Policy(...)`
has no rules, so `DropTable` hits `UNSUPPORTED_STATEMENT` and returns before the
allowlist code runs at all — the `elif` is never reached and the bug does not reproduce.
That is an easy way to write a test that passes for the wrong reason.

---

## F9 — `EXECUTE IMMEDIATE` payloads were invisible

```python
screen('spark.sql("EXECUTE IMMEDIATE \'DROP TABLE prod.users\'")')
# -> no finding for the DROP at all
```

The walker iterated only the direct children of the parse-tree root, but
`VisitExecuteImmediate` sits two levels down. The *literal extractor* found the payload,
so the information existed — it just was not being walked to. Now recurses.

---

## F10 — 3.5.1-only gap: `CREATE TABLE ... LIKE` found no source

3.5.1's `createTableLike` uses `tableIdentifier` where 4.0 uses `identifierReference`,
and the 3.5.1 context was missing from the identifier-rule set. So
`CREATE TABLE t2 LIKE prod.t` yielded no source namespace on 3.5.1 and did on 4.0.

Fail-open, and only on the older grammar — the version that a default-configuration test
run never touches.

---

## F11 — `.getText()` marker missed the shape it was added for

Widening `_JAVA_MARKERS` to catch Java in grammar actions used `".getText()"` with a
leading dot, which does not match `{String s = getText();}` — valid Java with no
receiver. Now `"getText("`.

---

## F12 — `python_module()` returned an unimportable path

Interpolated `self.key` (`"spark-4.0"`, which contains a dot and is not a legal module
name) instead of `self.module_name` (`"spark_4_0"`). Latent — nothing called it — but it
would have returned a path that could never import.

---


## F13 — the two-value verdict scale had no way to say "I did my job"

**Severity.** Structural. Found while implementing the REVIEW/UNKNOWN split, and it was
already costing accuracy before any REVIEW existed.

**What happened.** `UNSUPPORTED_STATEMENT` was a member of `UNKNOWN_REASONS`, so any
report containing it incremented `Report.analysis_failures` and was counted as "the
screener could not analyse this". But `UNSUPPORTED_STATEMENT` is raised when a statement
*parsed cleanly*, its label resolved, its targets were extracted — and then no rule in
the policy matched. Every fact needed to make a decision was in hand. The only thing
missing was an opinion.

The consequence is that the two counters a dashboard would naturally show were both wrong
for this case: the analysis-failure rate was inflated by ordinary policy gaps, and the
verdict read UNKNOWN, which reads as "I could not tell" rather than "I can tell, and you
may want an opinion".

**The worse half.** `OUTSIDE_ALLOWLIST` had the same shape and was handled by *omission* —
it was simply left out of the set, with a comment explaining that it had to stay out.
A correct classification maintained by remembering not to classify something, in a set
whose membership nothing tests. `tests/test_verdicts.py` did pin it, but the pin asserted
the *absence* from a set rather than the *presence* of the right answer.

**Fix.** The verdict scale now carries the distinction directly. `UNSUPPORTED_STATEMENT`
and `OUTSIDE_ALLOWLIST` are both REVIEW; the set that drives `analysis_failures` holds
only reasons where analysis genuinely did not happen. The rule is stated in one place and
tested as a property of the scale, so a future reason cannot drift into the wrong set
without failing a test whose name says so.

**The general lesson.** Both bugs are the same shape, and it is a shape worth watching for
in this codebase specifically: *a classification held correct by an exception rather than
by a rule.* The `elif` bug in the namespace allowlists (F8) was the third instance. If a
correctness property is expressed as "this case is absent from the set", it is one new
case away from being wrong, and nothing will complain until the case exists.

## F14 — SQL namespace extraction drops a component named `x`, and can truncate a 3-part name

**Severity.** Correctness, with a fail-open direction. Found 2026-10-03 while auditing the
README's claim that namespace patterns are per-component.

**What happens.** `treewalk.extract_namespaces` does not always return the name that was
written. Two distinct behaviours, both measured:

```
prod.users        -> prod.users        fine
prod.staging.x    -> prod.staging      truncated
prod.x            -> prod              truncated
prod.x.y          -> prod.y            the middle `x` is dropped too
prod.ax           -> prod.ax           fine
```

So a component named exactly `x` (case-insensitive) is dropped wherever it appears, and
some 3-part names are truncated to 2 parts. `x` is evidently treated as an alias or
placeholder by the grammar's identifier handling — the same ambiguity that makes
`SELECT * FROM prod.staging AS x` legal SQL.

**Why it matters.** Namespace allowlists match whatever extraction produced. Both
directions of error are reachable:

- **Fail-closed:** `readable_namespaces=("prod.*",)` against `select * from prod.x` gets
  `prod`, which does not match `prod.*`, so a legitimate read comes back `REVIEW`. Annoying
  but safe.
- **Fail-open:** the same policy against `select * from prod.staging.x` gets
  `prod.staging`, which *does* match `prod.*`, so a name the operator never authorised is
  allowed.

**Fixed here.** `NamespaceRef.matches` was separately wrong: a trailing `*` absorbed any
number of components, so `prod.*` matched `prod.a.b.c.d.e` on the DataFrame path. That is
now exact-arity (`*` matches one component; a bare `"*"` still means everything), which is
the fail-closed direction and is regression-tested.

**Deliberately not fixed here.** The extraction quirk is left as-is and recorded. It lives
in grammar-derived tree walking, and `prod.staging.x` is genuinely ambiguous SQL — Spark
itself resolves it against the catalog. A correct fix needs the real Spark resolution order,
not a guess, and it should be driven by the live differential suite rather than by
reasoning about the parse tree. Attempting it here, at the end of an unrelated change, is
exactly how the "property held by an exception" bugs happen.

**The general lesson.** The README claimed a property — "patterns are per component" — that
was true for the matcher and false for the extractor feeding it. Auditing prose against code
found it; no test had. Documentation claims about *data flow* are as testable as claims
about APIs, and `_verify_readme.py` now asserts the ones that are.


## Where the test suite was wrong

Marked separately because these are the ones where the code was right and the
expectation was not. Each was verified against CPython, the grammar, or a live engine
*before* being changed.

| Claim | Reality |
|---|---|
| `f"SELECT {f'{tbl}'}"` must not be recovered | CPython folds it to `SELECT prod.t`. Two tests asserted opposite things about the same source. |
| `spark.sql(query=q)` must stay unresolved because a keyword sink is unsupported | Contradicted the brief in the same message. The actual bug was that the sink *vanished*. |
| `with q as (...) select * from q` must not yield `q` | `FROM q` **is** a real table reference (`TableName > TemporalTableIdentifierReference`). The CTE *definition* is dropped; the *use* is kept. |
| `EXECUTE IMMEDIATE` works on both grammars | 3.5.1 has no `EXECUTE` token at all — a syntax error there, not a missed payload. |
| arbitrary Java in a grammar action must always raise | Unachievable. No finite denylist is complete. Replaced with `CAUGHT_JAVA` (must raise) and `UNCAUGHT_JAVA` (documented to pass through). |

---

## Known blind spots

Measured, not guessed. All of these currently return ALLOW with zero findings:

| input | why it is missed |
|---|---|
| `w = df.write` then `w.save("/data")` | the folder tracks string constants, not object bindings; `saveAsTable`/`insertInto` are unaffected because they match on callee name |
| `dbutils.fs.rm("/", recurse=True)` | Python-level, deliberately out of scope |
| `shutil.rmtree("/data")` | Python-level, out of scope |
| `os.system("rm -rf /")` | Python-level, out of scope |
| interprocedural constants (`def run(t): spark.sql(f"drop table {t}")`) | folding is intra-procedural by design; reported UNKNOWN, never guessed |
| SQL arriving as a parameter rather than a literal | same — UNKNOWN |

The DataFrame rows that used to sit at the top of this table
(`df.write.mode("overwrite").saveAsTable(...)`, `.save(...)`, `.jdbc(...)`) were closed
2026-10-02 in `sparkscreen-rn6`; the table is kept honest by re-measuring rather than by
deleting the rows, because a stale "known gap" list is worse than none — it reads as
current coverage. See [T5](threads.md#t5--should-dataframe-writes-be-screened).

`Reason.PYTHON_DANGEROUS_CALL` was defined in the enum and **never raised** — a reason we
never emitted, advertising capability we did not have. Removed 2026-10-02
(`sparkscreen-znf`): the coverage is out of scope, and a dead enum member implies it
exists. Python screening is a separate tool ([T4](threads.md#t4--pluggable-operation-cataloques),
[T6](threads.md#t6--should-python-level-calls-be-screened-here)).

The lesson generalises: **an enum is a promise about what the code can produce.** A
member that is never emitted makes the tool look more capable than it is.

## F15 — I declared a battle-tested tool broken without reading its docs

`mutmut run` appeared to deadlock on this project: seven processes at 0% CPU, all in
`futex_do_wait`, no output past `Generating mutants`. I confirmed mutmut worked on a
6-mutant toy reproduction, concluded "scale-related, not a broken install", killed the
run, and spent real effort writing `scripts/mutate.py` to replace it — 322 lines, a custom
mutant generator, per-module test selection, a --dry-run mode.

The fix was one line of configuration: `process_isolation = "forkserver"`. Mutmut's
documentation says, in the section on process isolation, to switch to forkserver if your
run hangs. It runs this project without difficulty. 3,209 mutants versus 244.

**Why the diagnosis felt convincing, and why that is the danger.** The toy reproduction
was real evidence and it pointed the wrong way: a 6-mutant project has no fork pressure,
so "works small, hangs large" looked like a scale limit in the tool. It was equally
consistent with "there is a configuration for large runs that I did not know about". I
had evidence that ruled out *one* explanation and treated it as if it ruled out the rest.
Confirming a failure is cheap; the failure mode was skipping the confirmation.

**The replacement was worse and I could have known that.** `scripts/mutate.py`'s generator
is line-based and declines to mutate anything it cannot apply confidently. It silently
skipped 127 of its own 244 mutants — over half — and reported the survivors without ever
saying that. A hand-rolled tool competing with a mature one needs an independent check on
its own output; the absence of one is not evidence of correctness. Its five "real"
survivors in the first run were also a subset of the wrong answer, since a survivor only
means something if the mutation was actually applied.

**What I should have done, in order:** read the tool's own docs before declaring it
inadequate; run its `results`/diagnostic command rather than inferring from `ps`; and treat
"this well-known tool fails here" as a claim needing the same scepticism as "my code is
right". The general rule: **a tool failing in a specific, reproducible way is a hypothesis
about a configuration, not a fact about the tool.** A hang is a symptom; the cause is
still unknown until someone reads what the tool says about hangs.

## F16 — a writer bound in both arms of an `if` loses its binding; `save`/`jdbc` report ALLOW

Found while verifying T5b with a probe that deliberately did not go through the code under
test. Recorded here rather than fixed, because the fix is in `folding.py` and the
interprocedural-folding agent is working in that file in a parallel worktree.

## Reproducer

    if flag:
        w = df.write
    else:
        w = df.write
    w.save("/tmp/x")

reports `ALLOW` with zero findings. `w.save("/tmp/x")` writes to the local filesystem.
`w.jdbc(url, "t")` likewise reports `ALLOW` and reaches an external system.

## Before and after T5b, measured

| input | master | with T5b |
|---|---|---|
| `w = df.write; w.save("/tmp/x")` | `allow` | `review` |
| two-arm `if`/`else`, then `w.save(...)` | `allow` | **`allow`** |
| two-arm `if`/`else`, then `w.jdbc(...)` | `allow` | **`allow`** |

So T5b closes the straight-line case and leaves the branch case open. The remaining gap is
a branch-merge limitation in the folder, not an alias-resolution one.

## Why it is narrow

Only the operations that *require* a resolved binding are affected:

| operation | verdict | why |
|---|---|---|
| `w.saveAsTable("prod.t")` | `review` | matched on method name; the receiver is not consulted |
| `w.insertInto("prod.t", ...)` | `review` | same |
| `w.mode("overwrite").saveAsTable(...)` | `deny` | `denies_regardless_of_namespace` fires on the effect |
| `w.save("/tmp/x")` | **`allow`** | needs a resolved writer — the binding is lost at the merge |
| `w.jdbc(url, "t")` | **`allow`** | same |

The destructive table operations are still caught, by name-matching and effect policy, even
when alias resolution fails. The exposure is the two operations with no PySpark-specific
method name to fall back on — and `save` is a local filesystem write, which is the one the
tool's own `READ_LOCAL_FS` / `WRITE_DATA` effects exist to flag.

## Root cause, as far as it is pinned down

The folder keeps one binding per name per scope, and the branch merge does not preserve a
binding established inside a branch body. Single-arm binding fails the same way:

    if flag:
        w = df.write
    w.save("/tmp/x")           # -> allow

which is defensible on its own (the write may not execute), but it is indistinguishable
from the two-arm case, which *should* resolve. That is the bug: the two are merged.

## Expected fix

Make the merge join bindings rather than discard them. If both arms bind a name to the same
provable value, that value survives; if the arms disagree, the result is unresolved. Never
assume a value for the disagreeing case.

Then add the reproducer as a regression test asserting both directions: arms agree ->
`review`; arms disagree -> `UNKNOWN`, not `ALLOW`.

The failure mode to avoid is fixing this by reporting `UNKNOWN` for every unresolved writer.
T5b deliberately leaves an unresolved alias silent so ordinary agent code does not drown in
findings. The fix belongs in the merge, not in the reporting.

## What the probe got wrong first

The first version of the verification probe asserted that all ten rebinding constructs must
stop resolving, and reported ten failures. Ten of ten "leaks" were the probe's fault:
`saveAsTable` and `insertInto` are matched on method name with the receiver ignored, so they
never consulted the binding in the first place. Testing invalidation requires an operation
that *depends* on the binding — which is what made the real gap visible at all. A probe that
appears to find ten bugs has usually found one misunderstanding.

## Process lessons

**A correctness property held by an exception is not held.** Three bugs in this
repository — F8 (`elif` between the two allowlist checks), F13's
`OUTSIDE_ALLOWLIST` (correct only by being left out of a set, with a comment saying it
had to stay out), and F9 (`EXECUTE IMMEDIATE` not being under direct parser children) —
are the same shape: the behaviour was right for the cases anyone had thought of, and
wrong for the next one, with nothing to say so. When a property is "this case is absent
from the set" or "this case happens to be a direct child", prefer expressing it as a rule
over the whole domain, and test the property by its name rather than by its example.

**Measure the gap list; do not curate it.** The blind-spot table above still listed the
three DataFrame write forms as unhandled for a full commit after they were fixed. The
rows were not wrong when written and nothing about fixing them invalidated them — they
simply stopped being true and said so. Re-derive the list from a probe rather than
maintaining it by hand, which is the same argument as the enum one: a stale capability
claim reads as a current one.

**Ask the real engine, not the grammar.** F6 (case-insensitivity) and the DataFrame
write semantics both looked like defects in the grammar and were not. `caseInsensitive`
came from Spark's own behaviour, and overwrite-versus-error came from executing a write
and counting rows. The grammar is a good oracle for *shape* and a poor one for
*meaning*.

