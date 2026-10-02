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

Measured, not guessed. All currently return ALLOW with zero findings:

| input | why it is missed |
|---|---|
| `df.write.mode("overwrite").saveAsTable("prod.t")` | DataFrame API — never becomes SQL text |
| `df.write.mode("overwrite").save("s3://bucket/x")` | same |
| `df.write.jdbc("jdbc:postgresql://prod", "t", mode="overwrite")` | same, and reaches an external system |
| `dbutils.fs.rm("/", recurse=True)` | Python-level, not analysed |
| `shutil.rmtree("/data")` | Python-level |
| `os.system("rm -rf /")` | Python-level |

`Reason.PYTHON_DANGEROUS_CALL` was defined in the enum and **never raised** — a reason we
never emitted, advertising capability we did not have. Removed 2026-10-02
(`sparkscreen-znf`): the coverage is out of scope, and a dead enum member implies it
exists. Python screening is a separate tool ([T4](threads.md#t4--pluggable-operation-cataloques),
[T6](threads.md#t6--should-python-level-calls-be-screened-here)).

The lesson generalises: **an enum is a promise about what the code can produce.** A
member that is never emitted makes the tool look more capable than it is.

---

## Process lessons

Worth more than any individual bug, since they are what would have prevented the above:

1. **Do not write one corpus and apply it to two grammars.** `$$abc$$` is a `codeLiteral`
   in 4.0 and a syntax error in 3.5.1. `CALL`, `|>`, `BEGIN…END` are 4.0-only. This
   produced several false failures during development.
2. **Write test SQL in lowercase.** An all-uppercase corpus has a blind spot you cannot
   see — see F6.
3. **A wrong expectation is worse than a missing test.** It encodes a false belief that
   survives until something depends on it.
4. **Verify a subagent's claim before accepting it**, and ask for a reproducer. Three of
   the four times an agent "found a bug", the bug was in the test.
5. **A test suite that disagrees with you is working.** If every test passes the first
   time, one of you is not trying.