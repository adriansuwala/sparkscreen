# Using sparkscreen

Task-oriented. For background and rationale, see the
[working documents](../WORKING.md).

## Install

```bash
pip install sparkscreen
```

No JVM. The Spark SQL parsers are built into the wheel. Python 3.10+.

## The four verdicts

| verdict | meaning | exit code |
|---|---|---|
| `ALLOW` | we resolved the SQL, parsed it with Spark's grammar, and no policy objected | 0 |
| `DENY` | …and a deny rule matched | 1 |
| `REVIEW` | we analysed it and a person should decide | 2 |
| `UNKNOWN` | we could not analyse it | 2 |

`UNKNOWN` is the point rather than a cop-out. A screener that reports "no issues" on code
it could not analyse is a false assurance with a confident voice, which is how a dangerous
tool gets trusted. So every internal failure lands on `UNKNOWN`, and a gate should treat
any non-zero exit as "do not execute without review".

### `REVIEW` and `UNKNOWN` are different, on purpose

Both mean "a human is needed", and they need different humans. `UNKNOWN` is screener
health — the tool could not do its job, and the fix is a better screener or a smaller
input. `REVIEW` is operator workload — the tool did its job and reached a defensible
conclusion, and the fix is a policy decision. A dashboard that conflates them either
pages someone about a parser bug or hides a genuine review behind it.

| | `REVIEW` | `UNKNOWN` |
|---|---|---|
| the statement was parsed | yes | no |
| labels and targets are known | yes | no |
| what the screener could not do | lacked a *policy opinion* | lacked *analysis* |

So `DELETE FROM t WHERE id = 3` is `REVIEW` — parsed, classified, deliberately routed to
a human — while `spark.sql(f"drop table {x}")` with an unresolved `x` is `UNKNOWN`, because
no statement exists to rule on. `drop table secret.salaries` outside your
`readable_namespaces` is `REVIEW`: the check ran and returned "not permitted".

`REVIEW` and `UNKNOWN` share exit code 2. The exit status is the tool's binary gate —
safe to run, or a human looks at it. Which of the two it was is in the `verdict` field,
not the exit code.

## Command line

```bash
sparkscreen job.py                     # exit 0 / 1 / 2
sparkscreen --spark 3.5.1 job.py       # bare version or grammar key
sparkscreen --policy company.json job.py
sparkscreen --json job.py | jq .verdict
cat job.py | sparkscreen -
sparkscreen --list-grammars
```

Example:

```
$ sparkscreen etl.py
DENY: 1 deny, 0 unknown, 0 review, 0 allow

  DENY    line 14: overwrites existing data (INSERT OVERWRITE / REPLACE)
          reason=destructive_statement severity=critical
          statement=InsertOverwriteTable
          rule=deny.overwrite
          targets=prod.events_v2
          sql='INSERT OVERWRITE TABLE prod.events_v2 SELECT * FROM staging.events'
```

## Library

```python
from sparkscreen import screen, load_policy, read_only_policy, Verdict

report = screen(source_code, policy=None, spec="spark-4.2")

report.ok         # True only when verdict is ALLOW with no unknowns
report.verdict    # Verdict.ALLOW / DENY / REVIEW / UNKNOWN
report.findings   # per-statement detail
report.summary()  # "DENY: 1 deny, 0 unknown, 0 review, 0 allow"

for f in report.findings:
    print(f.verdict, f.reason, f.statement, f.targets, f.line)

# What it *does*, independent of what the policy decided (see below)
from sparkscreen import Effect
report.effects                      # every effect in the file, unioned
f.effect                            # frozenset[Effect] for one finding
f.has_effect(Effect.DESTROY_DATA)   # True only if the flag is present
```

Gating on `report.ok` is the intended use. It is strict: one unanalysable statement
anywhere in the snippet makes it `False`.

`read_only_policy()` allows queries only, for analysis-only agents.

---

## What it actually looks at

Currently **only** SQL reached through `spark.sql()`, including:

- the SQL as a literal, an f-string, `+`, `%`, `.format()`, `join()`, format specs
- keyword forms — `spark.sql(query=...)`
- statements built with variables, as long as the folder can prove their value
- `BEGIN ... END` scripts (Spark 4.0 grammar only)

Constant folding is scope-aware. If a name is shadowed by a loop variable, a function
parameter, or reassigned, the statement becomes `UNKNOWN` rather than a guess — reporting
a confident but wrong SQL string would be worse than reporting nothing.

### The DataFrame API

Writes through `df.write` are detected, even though they never become SQL text:

```python
df.write.mode("overwrite").saveAsTable("prod.t")   # DENY    destroys existing rows
df.write.mode("overwrite").save("s3://bucket/x")   # DENY    + reaches outside the cluster
df.write.jdbc(url, "prod.t", mode="overwrite")     # DENY    + reaches an external system
df.write.mode("append").saveAsTable("staging.t")   # ALLOW   if staging is writable
df.write.saveAsTable("prod.t")                     # REVIEW  Spark refuses if it exists
df.write.mode(some_var).saveAsTable("prod.t")      # REVIEW  mode could be an overwrite
```

The save mode decides the effect, and the mode is read from the `.mode(...)` chain or,
for `jdbc`, from the `mode=` keyword. An unreadable mode is the one case that degrades,
and it degrades to REVIEW rather than to ALLOW.

**An unknown table name does not hide an overwrite.** `saveAsTable(x)` for an
unresolvable `x` still carries `WRITE_DATA | DESTROY_DATA` — the operation is knowable
even when the destination is not. That is a stronger guarantee than the SQL path can
offer, where an unresolvable `spark.sql(q)` genuinely is unknown.

`dbutils.fs.rm`, `os.system` and `shutil.rmtree` are **not** analysed, by decision.

---

## What an operation does

`verdict` is a policy decision. `effect` is a fact about the operation, and the two are
separate on purpose — the same `DROP TABLE` is `DENY` under one policy and `REVIEW`
under another, but it destroys the table either way.

| effect | |
|---|---|
| `WRITE_SCHEMA` | structure changed: columns, constraints, tables |
| `WRITE_DATA` | rows added or changed |
| `DESTROY_DATA` | rows or durable objects removed |
| `READ_DATA` | rows read |
| `READ_LOCAL_FS` | the driver's local filesystem is read |
| `LOAD_CODE` | code that will be executed is loaded |
| `REACHES_EXTERNAL` | a filesystem path, catalog, or opaque procedure |
| `CHANGE_CONFIG` | session or runtime configuration changed |

A statement carries several at once, which is the point:

```sql
ALTER TABLE t DROP COLUMN a   -- WRITE_SCHEMA | DESTROY_DATA
ALTER TABLE t ADD COLUMN a INT DEFAULT 0   -- WRITE_SCHEMA   (additive)
LOAD DATA LOCAL INPATH '/etc/passwd' ...  -- WRITE_DATA | READ_LOCAL_FS | REACHES_EXTERNAL
```

`DESTROY_DATA` on `DELETE`/`UPDATE`/`MERGE` is a deliberate slowdown flag, not a damage
estimate: a `DELETE ... WHERE id = 3` still removes a record, so it is never certified
harmless. Allow it per namespace if that suits your workflow.

An **empty** effect set means either "analysed, nothing durable" (`use prod`) or "we
could not analyse it". `report.analysis_failures` tells the two apart.

## Policies

Policies are data, so you can add rules without forking.

```json
{
  "name": "prod",
  "writable_namespaces": ["staging.*", "scratch"],
  "readable_namespaces": ["prod.*", "staging.*"],
  "rules": [
    {"id": "no-raw-jars", "verdict": "deny", "reason": "deny_rule",
     "message": "no raw jar loading", "labels": ["ManageResource"]}
  ],
  "limits": {"max_code_chars": 20000, "max_sql_chars": 10000}
}
```

```python
from sparkscreen import screen, load_policy
report = screen(source, load_policy("company.json"))
```

The two namespace lists are **independent** checks. `writable_namespaces` tightens
statements that change data; `readable_namespaces` tightens everything that reads. A table
can be writable and not readable, and both objections will be raised.

If you add a statement label to a rule, add it to `DESTRUCTIVE_LABELS` too, or the
namespace check will not see it. `policy_label_drift()` reports the mismatch and is
tested.

### Limits

| limit | default | on exceed |
|---|---|---|
| `max_code_chars` | 20,000 | DENY |
| `max_sql_chars` | 10,000 | DENY |
| `max_statements` | 200 | UNKNOWN |
| `max_literals` | 100 | UNKNOWN |
| `max_targets` | 100 | UNKNOWN |

`max_code_chars` is a review-budget control and denies rather than returning `UNKNOWN`:
it expresses intent ("don't ask me to eyeball five screens of code"), whereas `UNKNOWN`
means "we couldn't tell".

---

## Supported Spark versions

| grammar key | Spark | ANTLR |
|---|---|---|
| `spark-4.2` | 4.2.x | 4.13.1 |
| `spark-4.1` | 4.1.x | 4.13.1 |
| `spark-3.5.1` | 3.5.1 | 4.13.1 |

Grammars are pinned to full 40-character commit SHAs, never a branch or a tag. This is a
correctness property, not tidiness: the grammars disagree — 3.5.1 rejects `CALL`, `|>` and
`BEGIN…END`, and 4.1 rejects `QUALIFY`, `CHANGES` and `APPROX`/`EXACT NEAREST` — so a moving
tag would change verdicts for released code with no diff to show for it.

Each pin is the release its key names. An earlier `spark-4.0` key shipped with a pin dated
three weeks after the 4.2.0 release, so it accepted syntax no 4.0 engine runs; that key is
gone, and `spec_for_spark_version` now raises for an unsupported version instead of falling
through to the newest grammar. Spark 4.0 is not supported — upstream support ended
2026-11-23.

`--spark` accepts either form — `3.5.1`, `v3.5.1`, or `spark-3.5.1`.

---

## Adding a Spark version

Maintainers only; requires a JVM.

```bash
python -m sparkscreen.grammar.build --list
python -m sparkscreen.grammar.build --fetch      # pull the pinned .g4 from GitHub
python -m sparkscreen.grammar.build --generate  # port + run ANTLR
```

Add the version to `SPECS` in `src/sparkscreen/grammar/spec.py` with a full commit SHA,
then commit the regenerated parsers. CI regenerates and fails on any diff.

Expect the differential suite to disagree initially. That is the point — it is how you
find out whether the new grammar behaves the way you assumed.

---

## Troubleshooting

**Getting `UNKNOWN` more than you expected.** Check `f.reason` and `f.message`:

| reason | meaning |
|---|---|
| `unresolved_dynamic_sql` | the SQL came from something we cannot prove — a function parameter, a loop variable, `input()` |
| `unparseable_sql` | valid SQL is fine; this is SQL Spark itself would reject |
| `unsupported_statement` | we parsed it, but the policy has no rule for that statement type |
| `resource_limit` | over a limit, so not analysed |
| `outside_allowlist` | analysed, and it touches a namespace your policy does not permit |

`report.analysis_failures` separates "we could not look" from "we looked and it is outside
policy" — useful if you are tuning how noisy your gate is.

**Seeing `REVIEW` on `DELETE`/`UPDATE`/`MERGE`.** Correct and intentional, and the
`WHERE` clause does not exempt them. Any statement that removes rows carries
`DESTROY_DATA` and needs a human, whether it removes one record or a million.

That is deliberate: the screener is not asked whether a particular `DELETE` is harmful,
only whether a person should look. A `DELETE ... WHERE id = 3` still removes a record,
and an auto-approval is a decision someone will rely on. If you want routine cleanup to
pass, allow it by namespace or by rule — don't expect narrow predicates to earn it.

To avoid the flag entirely, restructure as `INSERT OVERWRITE` into a staging table and
swap, which is atomic and recoverable from the staging copy.

---

## Known limitations

- DataFrame API writes are not detected (above) — the largest gap.
- Interprocedural constants resolve only from literal call sites that all agree, and loops unroll only over literal lists and tuples. Recursion, decorators, generators, methods, `*args`/`**kwargs`, and disagreeing call sites stay `UNKNOWN`
  even when every caller passes a literal.
- Grammar permissiveness is not Spark semantics. `BailErrorStrategy` blocks syntax
  recovery, but Spark applies further checks after parsing.
- `dbutils` and arbitrary Python calls are out of scope by decision, not oversight.

Full detail, including the twelve bugs found during development and what each one taught,
is in [findings.md](../findings.md).
