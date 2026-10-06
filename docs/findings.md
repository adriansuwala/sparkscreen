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
| `w = df.write` then `w.save("/data")` | **no longer missed** — resolved since T5b and the branch-merge fix (F16); `review`. Kept here as a worked example of a row that went stale; see the note below the table |
| `if flag: w = df.write` then `w.save(...)` (single arm) | the write may not execute, so an unbound receiver is not treated as a sink — deliberate, see [F16](findings.md#f16--a-writer-bound-in-both-arms-of-an-if-loses-its-binding-savejdbc-report-allow) |
| `dbutils.fs.rm("/", recurse=True)` | Python-level, deliberately out of scope |
| `shutil.rmtree("/data")` | Python-level, out of scope |
| `os.system("rm -rf /")` | Python-level, out of scope |
| interprocedural constants (`def run(t): spark.sql(f"drop table {t}")`) | function parameters are bound when every call site in the file passes a literal and all of them agree, and `for` loops over a literal list or tuple are unrolled; anything else (recursion, decorators, generators, methods, kwargs, disagreeing callers) is UNKNOWN, never guessed |
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

**Status: FIXED.** `337b91b`, merged into `master` as `6783b0e`, covered by
`tests/test_branch_merge.py`. The reproducer below now reports `REVIEW`. Found while
verifying T5b with a probe that deliberately did not go through the code under test.

The rest of this section is kept as it was written, because the "expected fix" is what
the code ended up doing and it is worth seeing that the shape was right before it was
implemented. The tables that report `allow` are historical measurements of the defect,
not current behaviour.

## Reproducer (as it behaved when found)

    if flag:
        w = df.write
    else:
        w = df.write
    w.save("/tmp/x")

reported `ALLOW` with zero findings. `w.save("/tmp/x")` writes to the local filesystem.
`w.jdbc(url, "t")` likewise reported `ALLOW` and reached an external system.

Now: `REVIEW` on both, with the resolved target in the finding.

## Before and after T5b, measured

| input | master | with T5b |
|---|---|---|
| `w = df.write; w.save("/tmp/x")` | `allow` | `review` |
| two-arm `if`/`else`, then `w.save(...)` | `allow` | **`allow`** — now `review` |
| two-arm `if`/`else`, then `w.jdbc(...)` | `allow` | **`allow`** — now `review` |

So T5b closed the straight-line case and left the branch case open. The remaining gap was
a branch-merge limitation in the folder, not an alias-resolution one.

## Why it is narrow

Only the operations that *require* a resolved binding are affected:

| operation | verdict | why |
|---|---|---|
| `w.saveAsTable("prod.t")` | `review` | matched on method name; the receiver is not consulted |
| `w.insertInto("prod.t", ...)` | `review` | same |
| `w.mode("overwrite").saveAsTable(...)` | `deny` | `denies_regardless_of_namespace` fires on the effect |
| `w.save("/tmp/x")` | was **`allow`** | needs a resolved writer — the binding was lost at the merge. Now `review` |
| `w.jdbc(url, "t")` | was **`allow`** | same. Now `review` |

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

The precise mechanism, found during the fix: `visit_If` routed both arms through
`_block(leak=True)`, which invalidates every name a block writes on the grounds that the
block may not run. That is right for a loop or a `with`, and wrong for an `if`, where
exactly one of two *visible* arms runs and they can be intersected.

## Expected fix — implemented as described

Make the merge join bindings rather than discard them. If both arms bind a name to the same
provable value, that value survives; if the arms disagree, the result is unresolved. Never
assume a value for the disagreeing case.

Then add the reproducer as a regression test asserting both directions: arms agree ->
`review`; arms disagree -> `UNKNOWN`, not `ALLOW`.

The failure mode to avoid is fixing this by reporting `UNKNOWN` for every unresolved writer.
T5b deliberately leaves an unresolved alias silent so ordinary agent code does not drown in
findings. The fix belongs in the merge, not in the reporting.

`tests/test_branch_merge.py` pins both directions, plus the cases a too-eager fix would
break: a merge followed by a real rebinding (the rebinding wins), a sink *inside* each arm
(still found separately), and a merged binding whose `mode`/`destination` must be read
through the merge rather than assumed.

One measured deviation from the expectation above: the disagreeing case reports `review`,
not `unknown`. That is the T5b silence rule applied consistently — `save` and `jdbc` are
common method names in ordinary Python, so an unresolvable receiver stays silent rather
than crying wolf. The direction that matters is pinned by the tests: a disagreement never
produces a verdict *better* than an unresolvable name already gets, so it cannot become a
false ALLOW.

Related: interprocedural folding resolves a function from the call sites *visible in the
file*. A function resolved here and also called from another module with a different
argument will report only the statement its local callers issue. Same per-file limitation,
same trade.

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

## F17 — the `spark-4.0` grammar is not 4.0's grammar

`src/sparkscreen/grammar/spec.py` declares `spark_versions=("4.0.0", "5.0.0")` for the
`spark-4.0` spec. The pinned commit `3c28a9c093f1026d76e53d3eb2b846ffb28465c8` is dated
2026-08-03. Spark 4.2.0 was released 2026-07-11. The pin is therefore a *post-4.2 master*
grammar, about three weeks of development newer than the 4.2.0 tag, not the 4.0.0 grammar
the key names.

Measured against the upstream release grammars (rule inventory of `SqlBaseParser.g4`):

| upstream | rules | missing from vendored | extra in vendored |
|---|---|---|---|
| v3.5.1 | 184 | 4 | 121 |
| v4.0.0 | 239 | 5 | 67 |
| v4.1.3 | 272 | 1 | 30 |
| v4.2.0 | 283 | 0 | 18 |

So it is a superset of 4.2.0 carrying 18 rules that belong to no released line —
`asofJoinType`, `binByClause`, the `autoCdc*` family, `temporalTableIdentifier` and
relatives.

Two consequences, both verified through the shipped parser rather than inferred:

**It over-accepts relative to what the key promises.** `FOREIGN KEY` / `PRIMARY KEY`
constraints, `QUALIFY` and `WINDOW` all parse on it, and none of them exist in the 4.0.0
grammar. A user who selects `spark-4.0` expecting 4.0 semantics gets a looser parser than
4.0 is.

**It never rejected anything a real engine accepts.** A 56-statement corpus spanning DDL,
DML, scripts and the 4.1/4.2 feature set was parsed by real 4.1.3 and real 4.2.0 grammars
— generated from the upstream release tags through sparkscreen's own
`port_to_python`/`generate` — and by the shipped grammar. Zero statements were accepted by
a real grammar and rejected by the shipped one. For a fail-closed screener that is the
cheap direction to be wrong in: over-accepting costs precision, under-accepting costs a
false `UNKNOWN` on working production code.

This is not a new defect class. It is the same shape as the `Reason.PYTHON_DANGEROUS_CALL`
invariant: a *capability claim* that stopped being true without saying so. The pin moved;
the name and the version range did not.

**Why we might be wrong.** Those 18 extra rules are on master and could be reverted before
any release, in which case the grammar would quietly become 4.2-shaped and the pin should
move to the 4.2.0 tag instead. The finding is about the mislabelling, not about the
grammar being wrong.

### F17 resolution

The `spark-4.0` key is gone rather than renamed, and the 4.x line is now pinned to two real
releases instead of one master snapshot:

| key | commit | release | date |
|---|---|---|---|
| `spark-4.2` | `32f7299601108917fb01920a54e084595b7b3bf8` | v4.2.0 | 2026-07-11 |
| `spark-4.1` | `77bbf77e86ad48f58b5dfbc6ac882b3e70cf1989` | v4.1.3 | 2026-07-11 |
| `spark-3.5.1` | `fd86f85e181fc2dc0f50a096855acf83a6cc5d9c` | v3.5.1 | unchanged |

Both new SHAs were resolved through the GitHub API and confirmed to be the release
commits ("Preparing Spark release v4.1.3-rc1", "v4.2.0-rc6"), and all four vendored `.g4`
files hash byte-identical to the upstream release grammars.

Splitting rather than sharing was chosen on measurement, not taste. The cost of a second
grammar turns out to be near zero where it hurts and real only in bytes:

- **Effect table: no new entries.** The 4.2 label universe (111) is a subset of the old
  113, and 4.1's one extra label, `InsertIntoReplaceWhere`, was already mapped at
  `effects.py:171`. Zero uncovered labels across all three grammars.
- **Wheel: +139 KB**, 509 KB to 648 KB. Sharing would have saved that and cost a false
  negative on every 4.1 cluster.
- **The `.g4` files are 98.8% identical** but split into 85 diff hunks spread from line 56
  to line 2,657, so a shared-base scheme would need 85 hand-maintained splice points and
  would no longer correspond to any upstream commit -- losing the SHA-pin property that is
  the reason for vendoring verbatim.

Two labels, `CreateFlowAutoCdc` and `CommentColumn`, became unreachable when the master
snapshot was dropped. They are now in the documented policy-only drift set rather than
pruned, because pruning the effect table is a policy decision and not this module's.

`spec_for_spark_version` raises for an unsupported version rather than falling through to
the newest grammar: a 4.0 user asking for their own version must not be handed 4.2 syntax,
which is this same finding wearing a different hat.

## F18 — 4.1 and 4.2 grammars diverge in six user-facing features

The plan was to share one grammar between 4.1 and 4.2 on the bet that they do not diverge.
They do. Real 4.1.3 and real 4.2.0 grammars, both generated from the upstream release tags
and both queried through the same entry rule (`compoundOrSingleStatement`), disagree on six
constructs:

| feature | introduced | 4.1.3 | 4.2.0 |
|---|---|---|---|
| `QUALIFY` clause | 4.2 | rejects | accepts |
| `CHANGES FROM VERSION <int>` | 4.2 | rejects | accepts |
| `CHANGES FROM VERSION '<str>'` | 4.2 | rejects | accepts |
| `CHANGES FROM SYSTEM_VERSION a TO VERSION b` | 4.2 | rejects | accepts |
| `JOIN ... APPROX NEAREST BY DISTANCE` | 4.2 | rejects | accepts |
| `JOIN ... EXACT NEAREST BY SIMILARITY` | 4.2 | rejects | accepts |

At the rule level 4.2 adds 12 rules over 4.1 (`qualifyClause`, `changesClause`,
`streamChangesClause`, `nearestByClause`, `tableFunctionCall`, `withLocalTimeZone`,
`withoutTimeZone`, `pathElement`, `codeLiteral`, `identifiedByClause`,
`singlePathElementList`, `tableFunctionCallWithTrailingClauses`) and drops one
(`functionTable`).

All six divergences are *additive in 4.2*. One grammar can therefore serve both lines for
screening purposes — the union accepts everything either engine accepts — at the cost of
accepting `QUALIFY`/`CHANGES`/`NEAREST` against a real 4.1 cluster, where the statement
would fail at the engine. That is an imprecision in verdicts, not a false `UNKNOWN`. Since
the verdict set has no state for "this parses but the engine would reject it", the honest
description is that a shared 4.1/4.2 grammar is sound but imprecise in one direction, and
that direction is the cheap one.

The six test statements were corrected twice after first drafts failed on **both**
grammars. `CHANGES FROM VERSION => 1` is invalid — `version` is `INTEGER_VALUE |
stringLit`, with no arrow. `JOIN APPROX NEAREST BY DISTANCE` is invalid without the
`APPROX`/`EXACT` prefix that `nearestByClause` requires. A probe that reports a defect
where two independent real grammars agree that nothing is wrong is measuring the probe.
Derive the SQL from the upstream rule bodies rather than from memory of the syntax.

## F19 — Spark 3.4 needs a mapping entry, not a grammar; 3.3 and earlier are correctly absent

The 3.5.1 grammar's rule set is a strict superset of 3.4's: 184 rules against 172, and
`rules(3.4) - rules(3.5.1)` is **empty**. No 3.4-only rule survives into 3.5. A 12-statement
3.4-era corpus — CTAS with `USING`, `MERGE`, `INSERT OVERWRITE`, `INTERSECT`/`EXCEPT`,
`CAST` to `ARRAY<INT>`, `ADD COLUMNS`, `TABLESAMPLE`, `CREATE OR REPLACE TEMPORARY VIEW` —
parses 12/12 on the shipped 3.5.1 grammar.

So if a 3.4 user appears, 3.4 support is one version-mapping entry in `SPECS`: no vendored
grammar, no generated parser. It was not built now because 3.4 is past end of life
(2024-10-21), 3.3 ended 2023-12-09, and the only managed runtime still on 3.4 is Databricks
Runtime 13.3 LTS (Spark 3.4.1), which leaves support 2026-08-22.

This is the F-numbers equivalent of a measured gap list: what 3.4 support would cost is now
a number rather than an assumption, so the decision not to build it is revisitable the
moment someone asks.

**Popularity context.** pyspark PyPI downloads, last 90 days (pepy.tech; includes CI
traffic, so stale pins are over-represented): 3.5.x 25.0%, 4.2 16.8%, 3.4 7.8%, 3.3 5.0%,
4.1 4.9%, 4.0 4.1%, other 36.3%. Managed platforms skew older than that: EMR ships 3.5.x
through 7.13, GCP's default image 2.2 is Spark 3.5.3, and Databricks Runtime 15.4/16.4 LTS
are both Spark 3.5. Upstream support ends 2026-11-23 for 4.0 and runs to 2027-11-30 for
the 3.5 LTS.

## F20 — the "dollar-quoted strings" surface claim was never true, and the syntax is unreachable anyway

The pre-F17 `spec.py` described the 4.x grammar as having "dollar-quoted strings" in its
surface. Three separate things were wrong with that, and only the first is obvious.

**It was not in 4.0, or 4.1.** Counting `DOLLAR` mentions in each upstream lexer: the
spark-3.5.1 and spark-4.1 grammars have **zero**; spark-4.2 has 7. The construct postdates
4.1 entirely, so the claim was wrong for every line except 4.2 -- and no spark-4.0 grammar
ever shipped.

**It is not a string-literal position.** `SELECT $$abc$$` does not parse on any pinned
grammar. `codeLiteral` is a *statement-level* rule
(`codeLiteral: BEGIN_DOLLAR_QUOTED_STRING DOLLAR_QUOTED_STRING_BODY+ END_DOLLAR_QUOTED_STRING`),
used by `createMetricView` (`AS codeLiteral`) and by nothing else.

**In upstream 4.2.0 it is unreachable from `statement` anyway.** `codeLiteral` is defined at
line 1618 of the vendored `SqlBaseParser.g4` but appears in **no** labeled alternative of
`statement` — the rule has 91 alternatives and this is not among them. So a bare `$$abc$$`
does not parse, and `CodeLiteral` is absent from the derived label universe for all three
grammars. `screen()` returns `UNKNOWN` for every dollar-quoted form tried, on every grammar,
which is the fail-closed outcome and also the truthful one: no pinned Spark release accepts
this syntax at top level.

**Correction, found later by the label derivation.** I wrote that `codeLiteral` was used only
by `createMetricView`, which is true, and then treated the whole construct as dead. That
conclusion does not follow. The derivation for `spark-4.2` reports **`CreateMetricView` as a
reachable label**, and it is one of the nine labels 4.2 has that 4.1 does not. So the rule is
unreachable *as a bare statement*, not unreachable *full stop* — and the live engine agrees
in the same way: `CREATE METRIC VIEW` fails, but at the word `METRIC`, because that keyword
is not in the upstream lexer either. Two independent gates, neither of which is "this syntax
does not exist".

The distinction that survives: no pinned Spark release parses a dollar-quoted literal in any
position, so `UNKNOWN` remains correct for every form. But "the parser cannot reach it" and
"the syntax is not implemented" are different claims, and only the first is established. A
future release could add `METRIC` to its lexer and the rule would start being reachable with
no grammar change at all, at which point `spark-4.2` would need to be re-pinned to stay
honest. Worth knowing, since the pin-identity guard would flag exactly that move.

So the claim was a capability assertion with nothing behind it — the same shape as the
`Reason.PYTHON_DANGEROUS_CALL` invariant and as F17 itself. It was not caught by any test
because no test asserted it, and the comment that carried it was edited in the same commit
that moved the pin. Correcting it is the whole fix; there is no detection work to do, because
there is nothing reachable to detect.

**Process note.** This sat open across three findings because each investigation re-read the
comment and did not test it. The check that would have settled it in one command is counting
the token in each *pinned* lexer rather than reasoning about which grammar was intended.

### F20 resolution

The false claim is gone from `spec.py`. The per-line comment block now describes only what
each grammar actually has, and `spark-4.2`'s entry names its real 4.2 additions. There is
no detection change: `UNKNOWN` was already correct for every dollar-quoted form, on every
pinned grammar, because upstream 4.2.0 itself cannot parse one at top level.

Recorded rather than merely deleted, because the generalisable failure is the interesting
part: a comment asserting a capability is a claim, and it needs the same evidence as any
other claim. A lexer-token count across the pinned grammars settles it in one command.

## F21 — all seven 4.2 divergences confirmed against a live Spark 4.2.0 engine

Every 4.x claim up to here was grammar-level: generated parsers and read rule bodies. A
pyspark 4.2.0 engine on JDK 17 was installed into a scratch venv and used to check them
against the real Catalyst parser, so the answers no longer rest on reading a `.g4`.

**The discriminator matters.** `spark.sql()` conflates parse failure with analysis failure,
and a first pass through it reported 12 rejections that were ambiguous — `CHANGES FROM` on a
catalog without CDC, `CALL sys.system_info()` on an unresolvable routine, and `DROP TABLE t`
on a table that did not exist all "failed" while their syntax was perfectly valid. The engine's
parser called directly —

    spark._jsparkSession.sessionState().sqlParser().parsePlan(sql)

— throws only on a syntax error and returns otherwise, which is exactly the question
sparkscreen asks. A live differential that goes through `spark.sql()` will manufacture
false divergences; that is a trap worth naming, because the first version of this probe hit it.

**Confirmed, on all seven:**

| construct | parses on 4.2.0 |
|---|---|
| `QUALIFY` | yes |
| `CHANGES FROM VERSION 1` | yes |
| `CHANGES FROM SYSTEM_VERSION 1 TO VERSION 9` | yes |
| `JOIN ... APPROX NEAREST BY DISTANCE` | yes |
| `JOIN ... EXACT NEAREST BY SIMILARITY` | yes |
| `INSERT ... REPLACE WHERE` | yes |
| `INSERT ... REPLACE USING (a)` | yes |

The 4.1-era surface also parses on 4.2.0, so 4.1 is a subset in practice as well as in rule
sets: `PRIMARY KEY`, `FOREIGN KEY`, `CREATE STREAMING TABLE`, the `WINDOW` clause,
`TABLESAMPLE`, `REPLACE WHERE`, `CALL`, `EXECUTE IMMEDIATE`, and compound `BEGIN ... END`.

**F20 re-confirmed by a second route.** Dollar quoting fails to parse on a real 4.2.0 engine
in every position tried — `SELECT $$abc$$`, bare `$$abc$$`, tagged `$tag$abc$tag$`, and as an
`EXECUTE IMMEDIATE` payload. Independently, `createMetricView` (the only user of
`codeLiteral`) turns out to be dead upstream for a *second* reason: the rule exists at
`SqlBaseParser.g4:337` but its `METRIC` keyword is **not in the lexer**, so
`CREATE METRIC VIEW ...` is itself a parse error. The construct is doubly unreachable, which
is a stronger statement than F20 made from the grammar alone.

**Two probes of mine were wrong, and both were caught by the engine.**

`SELECT 1 |> double` was labelled "4.0 surface" and rejected. It is not a grammar gap: the
pipe operator is **absent from the vendored 4.2 grammar itself**, and the engine rejects it
too — it is gated on a config upstream that this build does not enable. Our grammar agrees
with the engine, which is the correct behaviour, and the label was the error. The grammar
comment about `|` vs `|>` compatibility sits at lines 58-66 of the parser grammar, so the
syntax is known upstream and deliberately not in the default surface.

`CREATE METRIC VIEW mv AS $$...$$` was rejected at the word `METRIC`, not at `$$` — a
different failure than the one I was testing for, and only visible because I checked the
error text rather than the boolean.

**What this settles.** It closes the gap that every 4.x finding so far rested on grammar
reading alone. F22 takes the same step for 4.1.3 and 3.5.1, so all three lines are now
engine-verified rather than one of three.

## F22 — spark-4.1 verified against a live 4.1.3 engine; the split is engine-justified

F21 confirmed the 4.2 side against a live 4.2.0 and left 4.1 grammar-verified only. A
pyspark 4.1.3 engine closes that.

**The differential suite runs clean on 4.1.3: 90 passed, 0 skipped** (against 84 passed /
6 skipped on 4.2.0, where the skips are the 4.2-only leg by design). All 90 collected on the
first attempt here, which also confirms the `bp4` collect-count guard is not tuned to one
engine.

**Re-derived independently rather than trusting the recorded table.** `ENGINE_EXPECTATIONS`
already asserts our grammar matches what each engine was observed to do, which is circular if
the table itself is wrong. So the 4.1 answer was taken straight from the engine's parser and
compared to the shipped `spark-4.1` grammar, 14 constructs, **0 mismatches**:

| construct | live 4.1.3 | `spark-4.1` |
|---|---|---|
| `QUALIFY` | reject | reject |
| `CHANGES FROM VERSION 1` | reject | reject |
| `CHANGES FROM SYSTEM_VERSION 1 TO VERSION 9` | reject | reject |
| `JOIN ... APPROX NEAREST` | reject | reject |
| `JOIN ... EXACT NEAREST` | reject | reject |
| `INSERT ... REPLACE ON` (4.2 half of the split) | reject | reject |
| `PRIMARY KEY` / `FOREIGN KEY` | accept | accept |
| `CREATE STREAMING TABLE` | accept | accept |
| `TABLESAMPLE` | accept | accept |
| `INSERT ... REPLACE WHERE` (4.1 half) | accept | accept |
| `CALL` / `EXECUTE IMMEDIATE` / `BEGIN ... END` | accept | accept |

This is what makes two grammars rather than one the right call, and it is now a statement
about engine behaviour on both lines instead of a reading of two `.g4` files. Had any
4.2-only construct parsed on real 4.1.3, the union grammar would have been sound after all
and the +139 KB would have bought nothing.

**And 3.5.1 too, so all three lines are engine-verified.** `.venv-pyspark` predates the hr0
rename, so its run was repeated against the current three-grammar tree rather than assumed to
still hold: **87 passed, 3 skipped** (the skips are constructs 3.5.1 is expected to reject).

| engine | result | grammar resolved |
|---|---|---|
| pyspark 3.5.1 | 87 passed, 3 skipped | `spark-3.5.1` |
| pyspark 4.1.3 | 90 passed, 0 skipped | `spark-4.1` |
| pyspark 4.2.0 | 84 passed, 6 skipped | `spark-4.2` |

`grammar_key_for_engine()` was checked to resolve each engine to its *own* grammar. That
matters because the bug it replaced was a two-way ternary that silently mapped the middle
engine onto the newest grammar -- so "the 3.5.1 leg passed" is only meaningful if the leg
really screened with `spark-3.5.1`, and that is now asserted rather than assumed.

Every engine this project supports has therefore been run against it, and the CI matrix is
reproducing locally on all three legs before its first run.
---

## F23 — the documentation outlived the fixes, three times over

Found 2026-10-04 while answering "what is next?". No code defect: every claim below was
wrong only in the documents, and the code was right in all three cases.

| document | claimed | measured |
|---|---|---|
| `findings.md` F16 | "Still open"; `save`/`jdbc` report `allow` | fixed in `337b91b`; reports `review` |
| `roadmap.md` "Where we are" | 3,146 tests, 51 differential expectations, two grammars | 3,833 with a JVM (3,806 without), 90 collected / 87 passed / 3 skipped, three grammars |
| `findings.md` blind-spot table | `w = df.write` then `w.save(...)` is missed | `review`, since T5b and the F16 fix |

The F16 row is the sharp one, because **F16's own section contains the lesson that would
have caught it**: "Measure the gap list; do not curate it... Re-derive the list from a probe
rather than maintaining it by hand, which is the same argument as the enum one: a stale
capability claim reads as a current one." That paragraph is about the blind-spot table. It
is equally true of the status line three sections above it in the same file.

**Why this is not the first time.** The blind-spot table itself was stale once already —
F16 records the three DataFrame write rows surviving a full commit after they were fixed.
So the shape has now recurred at three levels: a table row, a finding's status line, and a
whole document's summary block.

**The generalisation, which is the actual finding.** The repository has a working oracle
for code behaviour and none for prose. `_verify_refs.py` catches a stale *identifier*; the
audits catch claims they were written to catch. But "still open" versus "fixed", and a test
count, are the kind of claim that is true when written, false the moment a fix lands, and
invisible to every check in `ci_checks.py` — because no assertion anywhere compares a
document's status word to the branch history.

Worth noting what the CI checks *do* cover: all five documentation audits pass, including
the pin-identity guard, on a tree where three documents were this wrong. A green audit
means the claims someone remembered to encode are true. It says nothing about the ones
nobody encoded.

The cheapest real guard is a `br close` reason that names the commit, checked against
`git log` — the ledger already links findings to code, so the stale claim is detectable by
comparing a finding's status against the branches containing it. Not built here.

---

## F24 — the new audit hardcoded `.venv` again, on its first CI run

The CI `docs` job failed on the commit that introduced `_verify_refs.py`:

    FileNotFoundError: [Errno 2] No such file or directory:
      '/home/runner/work/sparkscreen/sparkscreen/.venv/bin/python'

It died in `real_keys()`, before checking a single literal. CI installs `.[dev]` into the
job's own Python and has no `.venv`; the audit shelled out to `ROOT/.venv/bin/python`
unconditionally.

**This is the third instance of one bug.** `_verify_readme.py` and `_verify_agents.py` were
both fixed for exactly this earlier (`d27eb07`), and both now look the interpreter up with
the same four lines. The new audit did not inherit the fix, because the fix lives in the
sibling files rather than anywhere it could be picked up.

The lesson is about *where a fix lives*, not about the bug. Three copies of the same
defect, and a fix that only ever lands in the copies that already had it. An audit is
supposed to be the thing that catches this class of assumption — so it is the worst place
for one to survive.

**What was done.** `_python()` now looks the interpreter up the same way its siblings do:
`.venv/bin/python` if it exists, else `sys.executable`. `cwd=ROOT` stays load-bearing,
because a relative interpreter path resolves against the subprocess cwd rather than the
caller's, so without it the fallback would look in the wrong directory — a bug that would
have passed locally and failed in CI again.

**How it was verified.** Not by running it here, where `.venv` exists and the original code
also worked. A CI-shaped tree was built from `git archive` — tracked files only, no
`.venv` — and the audit run there: passes, and `scripts/ci_checks.py audits` reports all
five audits green. Then a genuinely stale literal was planted in that tree to confirm the
fallback had not neutered the check: caught, exit 1.

A fix for "my script assumed this machine" that is only ever exercised on a machine with
the assumed layout has reproduced the original error. The absence of `.venv` has to be part
of the test, or it is not a test.

---

## F25 — a CI step that never ran, written to guard a step that never ran

Found 2026-10-04. The `docs` job failed with

    /home/runner/work/_temp/a955c4fc-....sh: line 10: syntax error near unexpected token `}'

The runner is quoting *its own generated script*, not anything in this repository. GitHub
wraps a `run:` block in a temp `.sh` whose default shell is
`bash --noprofile --norc -eo pipefail {0}`. The block had been written as:

```bash
grep -q -- "--engine" /tmp/usage.txt || {
  echo "::error::setup.sh --help does not mention --engine"
  exit 1
}
if grep -qE '^\s*(set |if |for |[A-Z_]+=)' /tmp/usage.txt; then
  echo "::error::setup.sh --help printed shell code; usage() range is wrong"
  exit 1
}          # <-- a brace, where bash requires `fi`
```

Two mistakes of one shape in one block. `|| { ... }` is valid only with a command before
the brace — the construct is a command *list*, not a bare block. And `if ... then` closes
with `fi`, never `}`. Bash parses the whole file before executing any of it, so the step
died at line 10 having run **nothing**: neither `bash -n scripts/setup.sh` nor
`setup.sh --help`, which is why the log shows the help text as the last output rather than
a diagnosis.

**Why nothing caught it, twice over.** The step is shell embedded as a YAML string, so
`bash -n scripts/setup.sh` — which does pass — never sees it. And the file is not
executable, so no local run reaches it either. The guard written *to protect this step*
was itself part of the broken step.

**What was done.** The block now uses `if ... then ... fi` throughout; no bare brace
remains in the workflow. `tests/test_ci_workflow.py` parses `ci.yml` with `yaml.safe_load`,
writes every `run:` string to a file verbatim, and runs `bash -n` over it — the same
parser, and the same flags, that rejected the real step. `${{ ... }}` expressions are
replaced with a placeholder first, since they are not shell.

**How it was verified.** Three ways, because a parse guard is trivially satisfied by a
guard that parses nothing.

1. Replayed the step locally in a CI-shaped `git worktree` — no `.venv`, no venv links,
   real git state — as one `bash -eo pipefail` script: exit 0.
2. Reintroduced each defect into that tree individually: the original block verbatim, `}`
   alone in place of `fi`, and `if {` with no command. All three fail with the assertion
   naming `jobs.docs.steps[4]`; the fixed file passes. A guard not shown to fail is not
   known to work.
3. `uvx --from actionlint-py actionlint` on the workflow: clean.

**The second bug, in the guard itself.** The first version of the test began
`yaml = pytest.importorskip("yaml")`, and PyYAML was in no dependency set. So in both
local venvs and in CI the module **skipped** — a guard examining zero steps, reporting
SUCCESS, which is the exact outcome it was written to prevent. This is the same
vacuous-success shape as F24 and as the differential suite's `importorskip("pyspark")`,
and it was reached by writing the very check that exists to prevent it.

Fixed at both ends: `PyYAML>=6` is in the `dev` extra, and the module now raises at import
rather than skipping, so a missing parser is a collection error a runner reports as a red
job instead of a green one that verified nothing. `test_this_module_will_not_skip_itself_unchecked`
asserts the extraction is non-empty, so the parse guard cannot pass vacuously either.

**The lesson.** A gate that is itself a gate needs its own vacuity check. `bash -n` on the
repository's own scripts is real evidence and was never in question — it passed, truthfully,
about a different file. The step that was broken was the one made of text inside YAML, seen
by no tool in this repo, including the two guards written to see it.

## F26 — a valid Spark statement crashes the CLI: `FROM`-led multi-target INSERT

**Status: open, tracked as `sparkscreen-from-clause-crash-p9f`.** Reproduced on all three
pinned grammars; no fix on this branch.

    spark.sql('FROM s INSERT INTO TABLE prod.t SELECT *')

is valid Spark SQL on 3.x and 4.x — the `FROM`-first form of `INSERT`. sparkscreen exits
with an `UnmappedLabelError` traceback ("no effect classification for statement label
'FromClause'") instead of a verdict.

**Mechanism.** The statement parses with a top-level `FromClause` context — the
statement-shaped `fromClause` alternative, which contains *both* a `Relation` and the
`insertInto`. `FromClause` is not in `treewalk.WRAPPER_LABELS`, so `effective_label()`
returns it unchanged and the statement's real kind (`InsertIntoTable`, reached through
`DmlStatement` > `SingleInsertQuery`) is never extracted. `FromClause` has no
`LABEL_EFFECTS` entry, `effects_for_label` raises, and `screen()` deliberately lets that
propagate. The CLI has no handler for it, so a parseable, years-old statement kills the
process.

**Why the derivation missed it.** `analysis/label_universe.py` derives the label universe
from the generated parsers, extending the frontier through `_is_wrapper` labels.
`FromClause` is not a wrapper, so the derivation treats it as a statement kind it would
return — but it is absent from the derived universe, because it is reached as an
*unlabeled* rule child of `singleStatement` / `compoundOrSingleStatement`, not as a labeled
alternative. The universe therefore does not contain a label the parser demonstrably
produces, and the effects-coverage test (which walks the same universe) could not see the
gap. A derivation and its runtime disagreeing is the failure shape F8/F9/F13 share.

**Why the obvious fix is wrong.** Adding `FromClause` to `WRAPPER_LABELS` makes
`effective_label` descend to the first non-wrapper inner context — which is `Relation`,
also unmapped and also not the statement kind. The kind must come from the `insertInto`
branch. This is treewalk surgery in the class that produced F8/F9/F13, and it needs
live-engine differential verification before it lands.

**Also wrong: the crash's own justification.** `_eval_one` says the raise "can only fire
when a grammar has gained a statement the table has not caught up with". It fires on a
statement that has existed since Spark 3.x. The invariant that a crash means an unmapped
*new* label is not held; the crash is reachable by plain old SQL.

**Found while reviewing** `policies/default-spark-3.5.1.jsonc` (the first file
`--init-policy` will emit): the policy file is not implicated — the crash happens before
any policy rule is consulted, under the built-in default too.

## F27 — sparkscreen accepts SQL all three engines reject at the AST layer

**Status: open, tracked as `sparkscreen-ast-layer-rejections-f27-egl` — the decision it
needs is named at the end of this finding.** Found by the
mutation fuzz on its first sweep (`experiments/spike/fuzz_differential.py`), confirmed on
live pyspark 3.5.1, 4.1.3 and 4.2.0 via `parsePlan`.

    set( spark.sql.shuffle.partitions=200)          # engine: INVALID_SET_SYNTAX
    SET (spark.sql.shuffle.partitions=200)          # engine: INVALID_SET_SYNTAX
    alter table t add column b intunion             # engine: UNSUPPORTED_DATATYPE
    alter table t add column b partitionint         # engine: UNSUPPORTED_DATATYPE
    ALTER TABLE t ADD COLUMN b unionINT             # engine: UNSUPPORTED_DATATYPE

All three are accepted by every pinned grammar (`SetConfiguration`, `AddTableColumns`)
and rejected by all three engines. This is not a port bug: the ports faithfully match the
vendored `.g4` files, and the `.g4` files match the engine's *grammar*. The rejection
happens after the grammar, in the engine's AST builder — `INVALID_SET_SYNTAX` is raised by
`visitSetConfiguration` against the shape `SET .*?` deliberately permits, and
`UNSUPPORTED_DATATYPE` is the engine naming the unsupported type after its type rule
matched an ordinary identifier. The grammar admits the input so the engine can attach a
better message; the message is a `ParseException`, so `parsePlan` counts it as rejected.

**The two failing directions are different risks, and only this one is a defect.** The
fuzz also watched the other direction — we rejecting what an engine accepts — and found
zero instances on all three engines, across 540 mutants each. Every false verdict this
class can produce is a verdict on SQL that never runs; nothing that would execute is
misanalysed. The screened verdicts stay fail-closed (`REVIEW` for the `SET` shape,
`DENY` for the alter-table shapes), so the cost is screener health: a confident verdict
on dead SQL and a differential suite that reports drift forever, not a wrong verdict on
live SQL.

**Recorded, not fixed, on this branch.** `KNOWN_AST_LAYER_REJECTIONS` in
`tests/differential/corpus.py` holds five engine-observed reproducers with their error
classes — the first three being the *complete* divergence set of the deterministic sweep
in `tests/differential/test_fuzz_against_real_spark.py` (fixed seed, 190 cases, identical
on all three engines, computed by `experiments/spike/probe_sweep_divergences.py`) — out
of `CORPUS` on purpose, since a `CORPUS` row asserts our parser already agrees, which it
does not. The parse-acceptance xfail in `tests/test_properties.py` pins the current
behaviour JVM-free, parametrized over the same table, and the sweep test treats a
we-accept/engine-reject outside the tolerated classes (or carrying
PARSE_SYNTAX_ERROR) as a hard failure, so neither the class nor its families can grow
unnoticed. New evidence rows still belong in the table, annotated with the observed
class -- not a red suite by accident.

**The class is wider than the first five rows.** The scheduled-depth sweep (2000 fresh
mutants, `experiments/spike/probe_deep_divergence_classes.py`) found ~34 more members
across the same mechanism: mangled `DROP INDEX` qualifiers, glued `ADD JAR` resource
types (`add jarjar ...`), and mangled `REPLACE COLUMNS` bodies, plus more spellings of
the SET and datatype families. The engine messages settled every one as command-layer
(`INVALID_STATEMENT_OR_CLAUSE`, unbracketed `Operation not allowed: ...`, the two known
classes) -- and, decisively, that genuine grammar-level rejections arrive bracketed as
`[PARSE_SYNTAX_ERROR]` on both 3.5.1 and 4.2.0. That is what makes the tolerance rule
closed: the sweep tolerates by error CLASS (the recorded command-layer families, plus
the unbracketed legacy shape, printed and counted rather than silently absorbed) and
fails on `PARSE_SYNTAX_ERROR` -- which would be a real port defect -- and on any
unrecognised class, which would be a family nobody classified yet. Exact-string
recording was the first draft of this rule; the class taxonomy is what scales.

**The decision this is waiting on.** `parse()`'s contract today is "the grammar accepts".
The differential suite's asymmetry demands "the engine's parser accepts". Closing the gap
means modelling AST-layer validation for the shapes the engines reject -- a SET-shape
check and a type-name check -- and both need live-engine verification of the full rule
they implement (the set shapes per engine version, the supported type universe), or the
fix will be stricter than some engine and the harness will flag it as drift in the other
direction. The widening of the class (dozens of members, four families) makes "model the
validations statement by statement" the expensive option; the alternative -- document the
divergence as a known, bounded, screener-health property and keep the classifier as its
guard -- is now the cheaper honest answer. Until that decision, the gap is tracked rather
than owned.

**Where the fuzz layer stands after this sweep.** The pre-existing property tests
replayed fixed seed pools; section 5 of `tests/test_properties.py` mutates the seeds so
Hypothesis invents shapes nobody wrote down, JVM-free, guarding the verdict-level
contract (destructive effects never ALLOW, unparseable never ALLOW, cross-grammar
destructive never ALLOW). This module adds the parse-acceptance check that needs the
engine. Between them: verdicts are fuzzed without a JVM, and parse acceptance is fuzzed
wherever an engine is installed. The first sweeps' numbers: 1500 seeded mutants × 3
grammars, 548 parsed, 203 destructive, zero verdict-level violations; 540 exploratory
mutants × 3 engines, six hits over three unique divergent shapes (this finding), zero
false rejects; and the deterministic promoted sweep, 190 cases × 3 engines, three
divergences (recorded above), zero false rejects.
