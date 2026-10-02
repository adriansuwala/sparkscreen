# sparkscreen

Is this PySpark safe to run? A static screener that answers with **ALLOW**, **DENY**, or
**UNKNOWN** — and never guesses.

MIT licensed. Usable in commercial settings.

```python
from sparkscreen import screen

report = screen('''
df = spark.table("prod.events")
df.write.mode("overwrite").saveAsTable("prod.events_v2")
''')
```

## Why a real parser

The failure mode of a regex-based SQL screener is that it matches *text*, and text is
adversarial to text-matching. `"DR" + "OP TABLE t"`, `DROP /* x */ TABLE t`,
`f"drop table {tbl}"`, and `spark.sql(q)` where `q` was built three statements ago all
slip past or trigger false positives.

So there are two real parsers, not one:

1. **Python's `ast`** finds the sinks (`spark.sql(...)`) and recovers the SQL text by
   constant propagation through f-strings, `+`, `%`, `.format()`, `join()`, and format
   specs. Measured on agent-style code, roughly 7 of 9 sinks have a non-literal
   argument — so this layer is what makes the tool see anything at all.
2. **Spark's own ANTLR grammar** — the real `SqlBaseParser.g4` from the Spark source,
   pinned per version, translated to a Python target and shipped in the wheel. Rules key
   on labeled alternatives like `DropTable` and `InsertOverwriteTable`, never on
   substrings.

## Why UNKNOWN is the point

Two verdicts would force a lie. When the SQL cannot be resolved or cannot be parsed, a
two-valued screener must pick between "dangerous" (cry wolf on every dynamic query) and
"safe" (lie). `UNKNOWN` is the honest answer, and it is what makes ALLOW mean something:

> **ALLOW means we resolved the SQL, parsed it with Spark's grammar, and applied a policy.
> Nothing in this package has a code path from "something went wrong" to ALLOW.**

Every internal failure — unparseable SQL, an unresolved f-string, an unrecognised
statement type, a resource limit, a Python syntax error — becomes UNKNOWN. Exit code 2.

```
$ sparkscreen code.py
DENY: 1 deny, 0 unknown, 0 allow

  DENY   line 1  InsertOverwriteTable   overwrites existing data (INSERT OVERWRITE / REPLACE)
         DROP TABLE prod.users
```

## Install

```bash
pip install sparkscreen
```

No JVM. The generated parsers ship in the wheel (~1.9 MB for 4.0, ~1.3 MB for 3.5.1).
Python ≥ 3.10.

## CLI

Exit codes are the API: **0** allow, **1** deny, **2** unknown. A gate should treat
anything non-zero as "do not execute without review".

```bash
sparkscreen job.py                        # exit 0/1/2
sparkscreen --spark 3.5.1 job.py          # bare version or grammar key
sparkscreen --policy company.json job.py
sparkscreen --json job.py | jq .verdict
cat job.py | sparkscreen -
sparkscreen --list-grammars
```

## Policy

Policies are data, not code, so a company can add its own rules without forking.

```python
from sparkscreen import screen, load_policy

policy = load_policy("company.json")
report = screen(source, policy)
```

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

`read_only_policy()` is available for analysis-only agents.

### Limits

| limit | default | on exceed |
|---|---|---|
| `max_code_chars` | 20,000 | DENY |
| `max_sql_chars` | 10,000 | DENY |
| `max_statements` | 200 | UNKNOWN |
| `max_literals` | 100 | UNKNOWN |
| `max_targets` | 100 | UNKNOWN |

`max_code_chars` is a **review-budget** control and deliberately DENIES rather than
returning UNKNOWN: it expresses the reviewer's intent ("don't ask me to eyeball five
screens"), whereas UNKNOWN would mean "we couldn't tell".

## Supported Spark versions

Grammars are pinned to immutable full commit SHAs — never a branch, never a bare tag.

| grammar key | Spark | ANTLR |
|---|---|---|
| `spark-4.0` | 4.0.x, 5.0.x | 4.13.1 |
| `spark-3.5.1` | 3.5.1 | 4.13.1 |

```bash
sparkscreen --list-grammars
```

Both grammars are generated with ANTLR 4.13.1, including 3.5.1. Spark 3.5.1 pins 4.9.3
itself, but 4.9.3 **cannot** build that grammar for the Python target: labels such as
`from=`, `input=` and `property=` collide with Python runtime attribute names.

## Regenerating the parsers

Maintainer-only, and not needed to install or run. Requires a JVM.

```bash
python -m sparkscreen.grammar.build --list
python -m sparkscreen.grammar.build --fetch      # pull pinned .g4 from GitHub
python -m sparkscreen.grammar.build --generate  # port + run ANTLR
python -m sparkscreen.grammar.build --all
```

## Design notes

**The ported grammar sets `caseInsensitive`.** This is load-bearing. The vendored
grammars spell keywords uppercase, and 3.5.1 declares `fragment LETTER : [A-Z]`, so a
literal port rejects `select 1`, `drop table t` and `SeLeCt 1` — all of which real Spark
accepts. Lowercase SQL is the common case in agent-written PySpark, so without it the
screener would be unusable while passing a suite written in uppercase. This is the only
bug the hand-written corpus could not find; it was caught by testing against a real
PySpark install.

**`BailErrorStrategy` is mandatory, but for a narrower reason than you'd think.** ANTLR's
default error strategy invents tokens and keeps going, returning a tree for input no
engine would run. That is what we reject. Separately, Spark's grammar *deliberately*
accepts some malformed input (`errorCapturingIdentifier`) so the engine can report a
better error later — real Spark accepts `SELECT * FROM t WHERE` and then fails with
AnalysisException. We match the engine there rather than being stricter for no gain.
Both categories are pinned in `tests/test_parser.py` so the distinction cannot rot.

**Verdicts aggregate on the verdict, never the reason.** These look like the same thing
and are not. A rule may be `verdict=UNKNOWN` with `reason=DESTRUCTIVE_STATEMENT` — a
DELETE is parsed fine, we just want a human to confirm the WHERE clause. Deriving the
verdict from the reason made DELETE/UPDATE/MERGE/INSERT aggregate as ALLOW. See the
commit message on `f3c1f39`.

## Known limitations

These are real, and mostly fail closed.

- **DataFrame API writes are invisible.** `df.write.mode("overwrite").saveAsTable(...)`
  never becomes SQL text, so a text-based screener cannot see it. This is the strongest
  argument for a logical-plan backend (see below).
- **No interprocedural constant propagation.** `def run(tbl): spark.sql(f"drop table
  {tbl}")` is UNKNOWN even when every caller passes a literal. Correct, but noisier than
  a human reviewer expects on heavily-factored code.
- **Grammar permissiveness ≠ Spark semantics.** `BailErrorStrategy` blocks syntax
  recovery, but Spark's `AstBuilder` applies further checks after parsing.
- **`dbutils`, UDFs, and arbitrary Python calls are not analysed.** `os.system`,
  `subprocess`, `shutil.rmtree` and friends are outside the current scope.

## The logical-plan alternative

Not implemented; deliberately out of scope for the static backend, but worth naming.

Spark Connect ships a typed **logical plan** over the wire rather than SQL text.
Inspecting that would be strictly more accurate than parsing, and it would cover the
DataFrame-API blind spot above — but it is a different architecture (an intercepting
proxy or a custom `SparkSession` wrapper) rather than a better parser, and it only
works for code that goes through Spark Connect.

## Testing

```bash
pytest tests/ -q                       # fast
```

Differential tests against a real Spark install are opt-in:

```bash
uv pip install --python .venv-pyspark/bin/python pyspark==3.5.1 \
    "antlr4-python3-runtime==4.13.1" pytest
JAVA_HOME=/path/to/jre PYTHONPATH=src:. .venv-pyspark/bin/python -m pytest \
    tests/differential/test_against_real_spark.py -q
```

`experiments/` keeps the spike code and findings for provenance, including the dead
ends. See `experiments/README.md`.

## Licence

MIT — see [LICENSE](LICENSE).

The vendored Spark `.g4` grammar files under `src/sparkscreen/grammar/vendored/` are
Apache-2.0 and remain under that licence; they are used as inputs, not relicensed.