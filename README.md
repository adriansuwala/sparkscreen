# sparkscreen

A safety screener for agent-written PySpark. It answers **ALLOW**, **DENY**, **REVIEW**,
or **UNKNOWN** — and never guesses.

MIT licensed.

```python
from sparkscreen import screen

report = screen('''
df = spark.table("prod.events")
df.write.mode("overwrite").saveAsTable("prod.events_v2")
''')
# DENY  — an overwrite of a production table, caught with no SQL text in sight
```

```bash
pip install sparkscreen     # 462 KB, no JVM, Python >= 3.10
sparkscreen job.py          # exit 0 / 1 / 2
```

## What it catches

| code | verdict |
|---|---|
| `spark.sql("DROP TABLE prod.t")` | `DENY` |
| `spark.sql(f"DROP TABLE {t}")`, `t = "prod.t"` | `DENY` |
| `df.write.mode("overwrite").saveAsTable("prod.t")` | `DENY` |
| `df.write.jdbc("jdbc:postgresql://h", "t", mode="overwrite")` | `DENY` |
| `spark.sql("delete from t where id = 1")` | `REVIEW` |
| `spark.sql("select * from secret.s")` outside your allowlist | `REVIEW` |
| `spark.sql(f"drop table {unresolved}")` | `UNKNOWN` |

## Why it isn't a regex

Text-matching is adversarial to text-matching. `"DR" + "OP TABLE t"`,
`DROP /* x */ TABLE t`, `f"drop table {tbl}"`, and a `spark.sql(q)` where `q` was built
three statements ago all slip past or trip false positives. So there are two real parsers:

1. **Python's `ast`** finds the sinks and recovers the SQL by constant propagation
   through f-strings, `+`, `%`, `.format()`, `join()` and format specs, with real Python
   scoping — so a shadowed or rebound variable fails closed instead of producing a
   confident, wrong answer.
2. **Spark's own ANTLR grammar**, pinned per version, ported to a Python target and
   shipped in the wheel. Rules key on labeled alternatives like `DropTable`, never on
   substrings. `INSERT OVERWRITE` and `INSERT INTO` are different labels; no substring
   rule distinguishes them.

## The four verdicts

| verdict | meaning | exit |
|---|---|---|
| `ALLOW` | resolved, parsed, nothing objected | 0 |
| `DENY` | a deny rule matched | 1 |
| `REVIEW` | we analysed it, a person should decide | 2 |
| `UNKNOWN` | we could not analyse it | 2 |

Two verdicts would force a lie: when the SQL can't be resolved, a binary screener must
pick between crying wolf on every dynamic query or lying about it. **Fail-closed** is the
design —

> **Nothing in this package has a code path from "something went wrong" to `ALLOW`.**

`REVIEW` and `UNKNOWN` share exit 2 deliberately: the exit status is a binary gate, and
the distinction lives in the `verdict` field. They need different remedies — `REVIEW` is a
policy decision, `UNKNOWN` is screener health. A dashboard that conflates them either
pages someone about a parser bug or buries a real review.

## What it deliberately does not do

- **`delete`/`update`/`merge` are `REVIEW` regardless of `WHERE`.** `delete where id=3`
  still removes a record, and an auto-approval is a decision someone will rely on. This is
  a deliberate slowdown, not a damage estimate.
- **It is not a general Python screener.** `os.system`, `shutil.rmtree` and `dbutils`
  return `ALLOW` with zero findings — out of scope by decision, because the expensive
  failure in an ephemeral pod is a wrong write to a warehouse someone else owns, which is
  the Spark problem this tool solves.
- **Only literal-call-site parameters and literal-loop elements resolve.** `def run(t):
  spark.sql(f"drop table {t}")` is `DENY` when every call site in the file passes a literal
  and all of them agree, and `for t in ["prod.users"]` is unrolled. Recursion, decorators,
  generators, methods, `*args`/`**kwargs`, nested calls like `drop(h("x"))`, and call sites
  that disagree are all still `UNKNOWN`. A function resolved from its visible call sites is
  analysed per-file: a caller in another module is out of reach, so its statement is missed.

The full, measured list is in [findings](docs/findings.md#known-blind-spots).

## Effects are separate from verdicts

`Effect` is a set of orthogonal flags, because operations routinely do several things at
once — `DROP COLUMN` is both `WRITE_SCHEMA` and `DESTROY_DATA`:

```python
from sparkscreen import screen, Effect

r = screen('df.write.mode("overwrite").saveAsTable(name)')
r.verdict                          # REVIEW  — `name` is dynamic, so no namespace check ran
Effect.DESTROY_DATA in r.effects   # True    — the mode is a literal, so this IS an overwrite
```

An unknown destination does not erase a known effect. 122 statement labels are mapped
across all three grammars, and an unmapped label raises rather than quietly producing a
finding with no effects.

## Policy

Policies are data, so a company can add rules without forking.

```python
from sparkscreen import screen, Policy, default_policy

policy = Policy(
    name="corp",
    rules=default_policy().rules,
    writable_namespaces=("staging.*", "analytics.*"),
    readable_namespaces=("prod.*", "staging.*"),
)
report = screen(source, policy)
```

Or `sparkscreen --policy company.json`.

Namespace patterns are **whole-component wildcards**: `*` matches exactly one component,
so `prod.*` matches `prod.users` but not `production.users`, and a bare `"*"` means no
restriction. Two known sharp edges are documented in
[findings](docs/findings.md#f14--sql-namespace-extraction-drops-a-component-named-x-and-can-truncate-a-3-part-name):
SQL namespace extraction currently drops any component literally named `x`, and can
truncate some three-part names, so an allowlist can be checked against a shortened name.

| limit | default | on exceed |
|---|---|---|
| `max_code_chars` | 20,000 | DENY |
| `max_sql_chars` | 10,000 | DENY |
| `max_statements` / `max_literals` / `max_targets` | 200 / 100 / 100 | UNKNOWN |

The length caps `DENY` rather than returning `UNKNOWN` because they express reviewer
intent ("don't ask me to eyeball five screens"), which is different from "we couldn't
tell".

## Spark versions

`spark-4.2`, `spark-4.1` and `spark-3.5.1`, each pinned to a **full immutable commit** of
Spark's own grammar — never a branch, never a movable tag. All generated with ANTLR
4.13.1. Each pin is the release it names, so `spark-4.1` rejects the 4.2-only syntax
(`QUALIFY`, `CHANGES`, `APPROX`/`EXACT NEAREST`) that a real 4.1 engine also rejects.

```bash
sparkscreen --list-grammars
sparkscreen --spark 3.5.1 job.py
sparkscreen --spark 4.1 job.py
```

Grammar acceptance is not semantic validity — Spark's analyzer applies further checks
after parsing. Every behavioural claim here was verified against a live engine, because
twice the grammar was the wrong oracle: case-insensitivity, and the fact that
`mode("overwrite")` destroys while the default mode *errors* rather than replacing.

## Performance

~12 ms for a 20-statement file, warm (measure it yourself with `scripts/bench.py`). Screening
is not a bottleneck in any pipeline where
a Spark job takes seconds — which is why this is Python, not Rust.

## Development

```bash
./scripts/setup.sh              # fast env, ~15s, no JVM
./scripts/setup.sh --full       # + differential env (PySpark 3.5.1) and a discovered JVM
./scripts/setup.sh --check      # verify an existing env, change nothing
./scripts/setup.sh --engine 4.1.3   # + a second engine, in .venv-pyspark-4.1.3
```

Three Spark lines ship grammars and the CI matrix tests all three. `--full` provisions the
oldest by default because it is the cheapest useful one; `--engine` provisions any other.
Each engine venv is ~500MB (462MB of it Spark's own jars), so ask for the ones you need.

Two environments on purpose. The fast one has **no PySpark**, which is what keeps
~2,500 tests at 45 seconds; the differential one has PySpark 3.5.1 and needs a JVM.

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q

export JAVA_HOME=$(ls -d /opt/data/home/.jre/*)
PATH="$JAVA_HOME/bin:$PATH" PYTHONPATH=src .venv-pyspark/bin/python \
    -m pytest tests/differential/ -q
```

Regenerating the parsers is a maintainer operation needing Java and the ANTLR jar:
`python -m sparkscreen.grammar.build --all`. The committed parsers mean you never need it
to build, test, or release.

## Documentation

| | |
|---|---|
| [Usage guide](docs/user-docs/README.md) | for people using the tool |
| [AGENTS.md](AGENTS.md) | orientation for agents and new contributors |
| [Findings](docs/findings.md) | **read this first** if deciding whether to trust it |
| [Decisions](docs/decisions.md) | numbered, each with what would change our mind |
| [Threads](docs/threads.md) | open questions, deliberately unsettled |
| [Roadmap](docs/roadmap.md) | current state and next work |

Findings is the one to start with. Almost every bug recorded there was a **fail-open** —
the screener reported ALLOW, reported nothing, or reported something confident and wrong,
and none of them crashed.

## Status

**0.8.0. Not 1.0 yet**, for two concrete reasons: `Verdict` gained a member recently (a
downstream exhaustive `match` would raise), and this has only ever been installed by its
author. Exit codes `0`/`1`/`2` are stable.

Verified: ~2,500 tests, 51 differential expectations against a live Spark 3.5.1, mutation
testing over the decision logic, and the built wheel installed into a clean environment
with Java absent from `PATH`.

## Licence

MIT — see [LICENSE](LICENSE).

The vendored Spark `.g4` grammar files under `src/sparkscreen/grammar/vendored/` are
Apache-2.0 and used as inputs, not relicensed. They are **not** shipped in the wheel:
only the generated parsers are, which is why no JVM is needed at runtime.
