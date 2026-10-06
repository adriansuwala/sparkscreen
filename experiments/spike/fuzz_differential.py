"""Spike: differential fuzz -- generated SQL, sparkscreen parser vs the live engine.

Throwaway probe for the fuzzing exploration. Run per engine:

    JAVA_HOME=$(ls -d /opt/data/home/.jre/*) PATH="$JAVA_HOME/bin:$PATH" \\
        <venv-with-pyspark>/bin/python experiments/spike/fuzz_differential.py --key spark-4.2

The SAME seeded case list is used for every engine, so results are comparable across
versions. The engine side uses `sessionState().sqlParser().parsePlan(sql)`, which
throws only on a syntax error and never executes the statement -- safer than
`spark.sql(...).collect()`, which would actually run CREATE/INSERT mutants.

Verdict asymmetry (mirrors tests/differential/test_against_real_spark.py):
  FAIL    we accept what the engine rejects   -> false negative, must fix
  NOTE    we reject what the engine accepts   -> screener health, count and inspect
"""
import argparse
import random
import sys
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from sparkscreen.grammar.parser import SqlSyntaxError, get_parser  # noqa: E402

SEEDS = [
    "select 1",
    "select a from t1 where a > 1 group by a having count(*) > 1 order by a limit 5",
    "with x as (select 1) select * from x",
    "drop table prod.users",
    "insert overwrite table prod.t select 1",
    "insert into t values (1)",
    "delete from t where id = 1",
    "alter table t add column b int",
    "create table t (a int) using parquet",
    "set spark.sql.shuffle.partitions=200",
    "show tables",
    "select cast(x as struct<a: int>) from t",
    "explain select 1",
    "truncate table t",
    "select a from t1 join t2 on t1.a = t2.b",
    "select * from (values (1, 2), (3, 4)) as v(x, y)",
]

KEYWORDS = ["drop", "select", "from", "where", "table", "insert", "union", "join",
            "delete", "values", "as", "on", "by", "not", "over", "partition"]

rng = random.Random(20261006)


def tokenize(s):
    out, cur = [], ""
    for ch in s:
        if ch == " ":
            if cur:
                out.append(cur)
                cur = ""
            out.append(" ")
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def mutate(sql):
    toks = tokenize(sql)
    op = rng.choice(["del", "dup", "swap", "kw", "parens", "tail", "quote", "case"])
    if not toks:
        return sql
    i = rng.randrange(len(toks))
    if op == "del":
        del toks[i]
    elif op == "dup":
        toks.insert(i, toks[i])
    elif op == "swap" and i < len(toks) - 1:
        toks[i], toks[i + 1] = toks[i + 1], toks[i]
    elif op == "kw":
        toks.insert(i, rng.choice(KEYWORDS))
    elif op == "parens":
        toks.insert(i, "(")
        toks.append(")")
    elif op == "tail":
        toks.append(rng.choice([";", "limit 1", "union", "group by a", "'unterminated"]))
    elif op == "quote":
        toks.insert(i, rng.choice(["`t`", '"t"', "prod.t", "`prod`.`t`"]))
    elif op == "case":
        toks[i] = toks[i].swapcase()
    return "".join(toks).strip()


def build_cases(n_mutants, n_seeds):
    cases = []
    for _ in range(n_seeds):
        cases.append(SEEDS[rng.randrange(len(SEEDS))])
    for _ in range(n_mutants):
        seed = SEEDS[rng.randrange(len(SEEDS))]
        m = mutate(seed)
        if m:
            cases.append(m)
    return cases


def main():
    import tempfile

    ap = argparse.ArgumentParser()
    ap.add_argument("--key", required=True, choices=["spark-3.5.1", "spark-4.1", "spark-4.2"])
    ap.add_argument("--mutants", type=int, default=500)
    ap.add_argument("--seeds", type=int, default=40)
    ap.add_argument("--warehouse-dir", default=None,
                    help="Spark warehouse dir; default: a fresh temp dir")
    args = ap.parse_args()

    cases = build_cases(args.mutants, args.seeds)
    ours_cache = {}
    counts = Counter()

    from pyspark.sql import SparkSession

    wh = args.warehouse_dir or tempfile.mkdtemp(prefix=f"sparkscreen-fuzz-wh-{args.key}-")
    session = (
        SparkSession.builder.master("local[1]")
        .appName("sparkscreen-fuzz-diff")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.warehouse.dir", wh)
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    jparser = session._jsparkSession.sessionState().sqlParser()

    def engine_accepts(sql):
        try:
            jparser.parsePlan(sql)
            return True
        except Exception:
            return False

    def ours_accepts(sql):
        if sql not in ours_cache:
            try:
                st = get_parser(args.key).parse(sql)
                ours_cache[sql] = st is not None
            except SqlSyntaxError:
                ours_cache[sql] = False
            except Exception:
                traceback.print_exc()
                ours_cache[sql] = False
                counts["ours-raise"] += 1
        return ours_cache[sql]

    fails, notes = [], []
    try:
        for sql in cases:
            engine = engine_accepts(sql)
            ours = ours_accepts(sql)
            if ours and not engine:
                fails.append(sql)
                counts["FAIL_we_accept_engine_rejects"] += 1
            elif engine and not ours:
                notes.append(sql)
                counts["NOTE_engine_accepts_we_reject"] += 1
            else:
                counts["agree:accept" if engine else "agree:reject"] += 1
    finally:
        session.stop()

    print(f"== {args.key}: {len(cases)} cases")
    print("counts:", dict(sorted(counts.items())))
    for s in fails[:10]:
        print("FAIL:", repr(s))
    for s in notes[:10]:
        print("NOTE:", repr(s))


if __name__ == "__main__":
    main()