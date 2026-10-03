"""Measure screening latency, so the number in the docs is measured rather than remembered.

Every speed claim in README/AGENTS/roadmap is supposed to come from here. Run:

    .venv/bin/python scripts/bench.py

The corpus is fixed and includes both SQL sinks and DataFrame writes, because quoting a
median without saying what was screened is how the previous figure drifted: an earlier
"5.5 ms" and a later "3.9 ms" were measured on different inputs, and neither matched what
this script reports.

Numbers move with machine load, so read the order of magnitude, not the last decimal.
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sparkscreen import screen  # noqa: E402

STMTS = [
    'spark.sql("select * from prod.users")',
    'spark.sql("drop table prod.staging.tmp")',
    'spark.sql("insert into prod.users values (1)")',
    'df.write.mode("overwrite").saveAsTable("prod.copy")',
    'spark.sql("delete from prod.t where id = 1")',
]


def corpus(n: int = 20) -> str:
    return "\n".join(STMTS[i % len(STMTS)] for i in range(n))


def bench(n: int = 20, repeats: int = 60) -> tuple[float, float]:
    src = corpus(n)
    screen(src)  # warm: import, parser modules, grammar caches
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        screen(src)
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples), min(samples)


def main() -> int:
    for n in (1, 20, 100):
        med, best = bench(n)
        print(f"  {n:>4} statements: median {med:6.2f} ms   best {best:6.2f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())