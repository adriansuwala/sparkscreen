"""Spike: mutation fuzz of SQL + generated PySpark, asserting the fail-closed contract.

Throwaway probe for the fuzzing-test exploration. Read-only against src/.

P1  TOTALITY:      parse()/screen() over mutated SQL never raise an undeclared type.
P2  NO FALSE ALLOW: screen() verdict == ALLOW  =>  the SQL parses under that grammar.
P3  FOLD RECOVERY: a destructive literal reaching spark.sql() through each path the
    folder documents (plain, f-string, +, %, .format(), loop element, interproc call)
    yields DENY -- and the shadowed variants yield never-ALLOW.
"""
import random
import sys
import traceback
from collections import Counter

sys.path.insert(0, "src")

from sparkscreen import screen
from sparkscreen.analysis.effects import effects_for_label
from sparkscreen.grammar.parser import SqlSyntaxError, get_parser
from sparkscreen.grammar.spec import SPECS
from sparkscreen.model import Verdict

KEYS = [s.key for s in SPECS]
PARSERS = {k: get_parser(k) for k in KEYS}

SEEDS = [
    "select 1",
    "select a from t1 where a > 1 group by a having count(*) > 1 order by a limit 5",
    "with x as (select 1) select * from x",
    "drop table prod.users",
    "drop table if exists prod.users purge",
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
]

KEYWORDS = ["drop", "select", "from", "where", "table", "insert", "union", "join",
            "delete", "values", "as", "on", "by", "not"]

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
    """One random mutation: delete / duplicate / swap / insert keyword / nest parens."""
    toks = tokenize(sql)
    op = rng.choice(["del", "dup", "swap", "kw", "parens", "tail"])
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
        toks.append(rng.choice([";", "limit 1", "union", "-- x", ")", "'"])).__class__
    return "".join(toks).strip()


def pywrap(sql):
    return 'import pyspark\nspark.sql("' + sql.replace('"', "'") + '")\n'


violations = []
counts = Counter()
N_MUT = 1500

for n in range(N_MUT):
    seed = SEEDS[rng.randrange(len(SEEDS))]
    m = mutate(seed)
    if not m:
        continue
    key = KEYS[n % len(KEYS)]
    parser = PARSERS[key]
    # P1: totality of parse
    try:
        st = parser.try_parse(m)
        if st is not None:
            counts["parsed"] += 1
            counts["label:" + st.label] += 1
            # label must map to effects without raising
            try:
                eff = effects_for_label(st.label)
                assert isinstance(eff, frozenset), type(eff)
                from sparkscreen.model import Effect
                must_not_allow = eff & {Effect.DESTROY_DATA, Effect.LOAD_CODE}
                if must_not_allow:
                    counts["destructive"] += 1
            except Exception as e:
                violations.append(("effects-raise", key, m, repr(e)))
                counts["V_effects"] += 1
                continue
            # P2: ALLOW only for parseable SQL
            rep = screen(pywrap(m), spec=key)
            if rep.verdict is Verdict.ALLOW:
                counts["allow"] += 1
                if must_not_allow:
                    violations.append(("allow-destructive", key, m,
                                       f"label={st.label} eff={sorted(map(str,eff))}"))
                    counts["V_allow_destructive"] += 1
            # P1b: totality of screen
        else:
            counts["unparsed"] += 1
            rep = screen(pywrap(m), spec=key)
            if rep.verdict is Verdict.ALLOW:
                violations.append(("allow-unparseable", key, m, rep.summary()))
                counts["V_false_allow"] += 1
    except Exception as e:
        violations.append(("raise", key, m, traceback.format_exc()[-300:]))
        counts["V_raise"] += 1

# P3: fold recovery -- destructive literal through every documented path
DEST = "drop table prod.users"
fold_paths = {
    "plain":      'import pyspark\nspark.sql("' + DEST + '")\n',
    "fstring":    'import pyspark\nq = "' + DEST + '"\nspark.sql(f"{q}")\n',
    "concat":     'import pyspark\nq = "drop table " + "prod.users"\nspark.sql(q)\n',
    "pct":        'import pyspark\nspark.sql("drop table %s" % "prod.users")\n',
    "format":     'import pyspark\nspark.sql("drop table {}".format("prod.users"))\n',
    "join":       'import pyspark\nq = "".join(["drop table ", "prod.users"])\nspark.sql(q)\n',
    "loopelem":   'import pyspark\nfor q in ["' + DEST + '"]:\n    spark.sql(q)\n',
    "interproc":  'import pyspark\ndef f(t):\n    spark.sql(f"drop table {t}")\nf("prod.users")\n',
}
unknown_paths = {
    "shadow":     'import pyspark\nq = "' + DEST + '"\nfor q in get_thing():\n    pass\nspark.sql(q)\n',
    "param":      'import pyspark\ndef f(q):\n    spark.sql(q)\n',
}

for name, src in fold_paths.items():
    try:
        rep = screen(src, spec="spark-4.2")
        if rep.verdict is not Verdict.DENY:
            violations.append(("fold-not-deny", name, src, rep.summary()))
            counts["V_fold"] += 1
        else:
            counts["fold_ok:" + name] += 1
    except Exception as e:
        violations.append(("fold-raise", name, src, traceback.format_exc()[-300:]))
        counts["V_fold_raise"] += 1

for name, src in unknown_paths.items():
    try:
        rep = screen(src, spec="spark-4.2")
        if rep.verdict is Verdict.ALLOW:
            violations.append(("unknown-path-allow", name, src, rep.summary()))
            counts["V_unknown_allow"] += 1
        else:
            counts["unknown_ok:" + name] += 1
    except Exception as e:
        violations.append(("unknown-raise", name, src, traceback.format_exc()[-300:]))
        counts["V_unknown_raise"] += 1

print("counts:", dict(sorted(counts.items())))
print(f"violations: {len(violations)}")
for v in violations[:15]:
    print("---", v[0], v[1], repr(v[2])[:120])
    print("   ", str(v[3])[:400])