---
name: sparkscreen-gate
description: Use when screening PySpark for safety (drop/truncate/overwrite/JDBC) or wiring a CI gate. Route SQL vs DataFrame API, pick the right verdict.
---

# Screening agent-written PySpark

`sparkscreen` recovers SQL from Python source, parses it with Spark's real ANTLR grammar,
and applies a declarative policy. No JVM at runtime.

## First: which sink is it?

They take different paths and have different blind spots.

```python
spark.sql(q)                      # SQL path -- constant-folded, then parsed
spark.sql(query=q)                # same
spark.sql(**{"query": q})         # UNKNOWN (cannot prove which kwarg)
df.write.mode(m).saveAsTable(t)   # DataFrame path -- AST, no SQL text at all
```

DataFrame writes were the original blind spot and are now closed, **except** an aliased
writer: `w = df.write; w.save(p)` is missed, because the folder tracks string constants
and not object bindings. `saveAsTable`/`insertInto` are unaffected (they match on callee
name).

## Expect four verdicts, not three

| verdict | meaning | exit |
|---|---|---|
| `ALLOW` | resolved, parsed, nothing objected | 0 |
| `DENY` | a deny rule matched | 1 |
| `REVIEW` | analysed; a person should decide | 2 |
| `UNKNOWN` | could not analyse | 2 |

`REVIEW` vs `UNKNOWN` is not cosmetic. `REVIEW` means *screener health is fine, a human
is needed*; `UNKNOWN` means *screener health is the problem*. Conflating them makes a
dashboard page someone about a parser bug, or bury a real review.

- `DELETE`/`UPDATE`/`MERGE` → `REVIEW`, always, regardless of `WHERE`. Narrow predicate or
  not, it removes rows. That is a deliberate slowdown, not a damage estimate.
- unresolved f-string → `UNKNOWN`. No statement exists to rule on.
- allowlist violation → `REVIEW`. The check ran and said no.

## Using it

```bash
sparkscreen job.py                       # 0 allow / 1 deny / 2 review-or-unknown
sparkscreen --spark 3.5.1 job.py         # bare version or grammar key
sparkscreen --policy company.json job.py
sparkscreen --json job.py | jq '.verdict, [.findings[].effect]'
```

Exit 2 covers both `REVIEW` and `UNKNOWN` on purpose — the gate is binary, and the
`verdict` field carries the nuance. Do not branch the CI gate on anything else.

## Effects are orthogonal to verdict

`Effect` is a `Flag` set, not an enum, because statements routinely do several things at
once (`DROP COLUMN` is `WRITE_SCHEMA | DESTROY_DATA`). Read effects for the *what*, verdict
for the *so what*:

```python
r = screen('df.write.mode("overwrite").saveAsTable(name)')
r.verdict            # REVIEW — we know the effect, but `name` is dynamic
Effect.DESTROY_DATA in r.effects   # True — mode is a literal, so this IS an overwrite
```

That combination is the point: a known-dangerous operation with an uncheckable
destination. Unknown target does **not** erase a known effect.

## Things that will bite

- Length limits are **DENY**, not UNKNOWN — `max_code_chars` expresses reviewer intent.
- Out-of-scope by decision: `os.system`, `shutil.rmtree`, `dbutils.fs.rm`. All `ALLOW`
  with zero findings. Do not report this as a bug.
- Grammar acceptance ≠ semantic validity. Spark accepts some things its analyzer rejects.
- `CREATE OR REPLACE TABLE` fails on the in-memory catalog; seed differential tables via
  `saveAsTable`.
- Spark SQL is case-insensitive. The ports set `caseInsensitive = true` deliberately.

## Verifying claims against a real engine

The grammar is a good oracle for *shape* and a poor one for *meaning*. Both of these
looked like grammar defects and were not:

- case-insensitivity — only a live Spark proved it
- overwrite vs default mode — only counting rows proved that default *errors* rather than
  replacing

```bash
export JAVA_HOME=$(ls -d /opt/data/home/.jre/*)
PATH="$JAVA_HOME/bin:$PATH" .venv-pyspark/bin/python \
    -m pytest tests/differential/ -q
```

Use `.venv-pyspark` (has pyspark 3.5.1). The fast `.venv` deliberately has no pyspark.
