"""Probe: which label does each candidate FUZZ_SEED yield, per pinned grammar?

Feeds the seed-completeness guard (tests/test_properties.py). JVM-free.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from sparkscreen.grammar.parser import get_parser
from sparkscreen.grammar.spec import SPECS

CANDIDATES = [
    "delete from t where id = 1",
    "update t set a = 1",
    "merge into prod.t using s on t.id = s.id when matched then update set *",
    "drop view prod.v",
    "drop index i on t",
    "drop index if exists i on t",
    "drop schema prod.s",
    "drop database if exists s",
    "drop function f",
    "drop function if exists f",
    "alter table t drop column a",
    "alter table t drop if exists partition (a = 1)",
    "alter table t drop primary key",
    "alter table t drop constraint c",
    "alter table t replace columns (a int)",
    "insert overwrite table prod.t select 1",
    "insert overwrite directory '/tmp/x' select 1",
    "insert overwrite directory '/tmp/x' row format delimited fields terminated by ',' select 1",
    "insert into t replace where a > 1 select 1",
    "insert into t replace using (a) select 1",
    "insert into t replace on a > 1 values (2)",
    "create function f as 'x.y'",
    "create or replace function f as 'x.y' using jar '/tmp/j.jar'",
    "add jar /tmp/x.jar",
    "add file /tmp/x.txt",
    "from s insert into t1 select 1 insert into t2 select 2",
]

for sql in CANDIDATES:
    cells = []
    for spec in SPECS:
        try:
            st = get_parser(spec.key).try_parse(sql)
            top = st.label if st else "REJECT"
            tight = [s.label for s in st.statements] if st else []
            cells.append(f"{spec.key}: {top} -> {tight}")
        except Exception as e:
            cells.append(f"{spec.key}=RAISE:{type(e).__name__}")
    print(f"{sql[:60]:<60} {' ;; '.join(cells)}")