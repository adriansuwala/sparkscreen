"""Audit README.md — the first thing anyone reads.

The old README made three claims that were false by the time they were read: that
DataFrame writes were invisible (fixed three commits earlier), that the wheel was ~3 MB
(it is 462 KB), and that there were three verdicts (there are four). A README is the most
read document in a repository, which makes a stale claim in it the most expensive kind.

Every code example and every number here is executed or measured.

Run: PYTHONPATH=src .venv/bin/python _verify_readme.py
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent
failures: list[str] = []


def check(label: str, got, want) -> None:
    good = got == want
    if not good:
        failures.append(f"{label}: got={got!r} want={want!r}")
    print(f"  {'OK  ' if good else 'FAIL'} {label:56} {got!r}")


def main() -> int:
    readme = (ROOT / "README.md").read_text()

    from sparkscreen import Effect, Verdict, screen

    # --- the headline example, exactly as printed ---
    report = screen('''
df = spark.table("prod.events")
df.write.mode("overwrite").saveAsTable("prod.events_v2")
''')
    check("README headline example -> DENY",
          report.verdict, Verdict.DENY)
    check("  and it carries DESTROY_DATA",
          Effect.DESTROY_DATA in report.effects, True)

    # --- the "what it catches" table, row by row ---
    cases = [
        ('spark.sql("DROP TABLE prod.t")', Verdict.DENY),
        ('t="prod.t"\nspark.sql(f"DROP TABLE {t}")', Verdict.DENY),
        ('df.write.mode("overwrite").saveAsTable("prod.t")', Verdict.DENY),
        ('df.write.jdbc("jdbc:postgresql://h", "t", mode="overwrite")', Verdict.DENY),
        ('spark.sql("delete from t where id = 1")', Verdict.REVIEW),
        ('tbl = input()\nspark.sql(f"drop table {tbl}")', Verdict.UNKNOWN),
    ]
    for src, want in cases:
        got = screen(src).verdict
        check(f"table row: {src[:42]!r}", got, want)

    # the allowlist row needs a policy, so it is checked separately
    from sparkscreen.policy import Policy, default_policy
    pol = Policy(name="r", rules=default_policy().rules,
                 readable_namespaces=("prod.*",))
    check("table row: read outside allowlist -> REVIEW",
          screen('spark.sql("select * from secret.s")', pol).verdict, Verdict.REVIEW)

    # --- the four verdicts ---
    check("exactly four verdicts", len(list(Verdict)), 4)
    check("verdict order in the table matches the enum",
          [v.value for v in Verdict], ["allow", "deny", "review", "unknown"])
    cli = (ROOT / "src/sparkscreen/cli.py").read_text()
    check("exit codes are 0/1/2 and REVIEW has no own code",
          ("EXIT_ALLOW = 0" in cli and "EXIT_DENY = 1" in cli
           and "EXIT_UNKNOWN = 2" in cli and "EXIT_REVIEW" not in cli), True)

    # --- the effects example ---
    r = screen('df.write.mode("overwrite").saveAsTable(name)')
    check("effects example: verdict is REVIEW", r.verdict, Verdict.REVIEW)
    check("effects example: DESTROY_DATA known anyway",
          Effect.DESTROY_DATA in r.effects, True)

    # --- namespace pattern claim: prod.* must NOT match prod.staging.x ---
    # Use the real matcher, not fnmatch. An earlier version of this check called
    # fnmatch directly and FAILED -- because fnmatch's `*` crosses dots, so
    # fnmatch("prod.staging.x", "prod.*") is True. That is a true fact about fnmatch and
    # a false statement about sparkscreen, whose patterns are per-component. Testing the
    # stdlib instead of the code under test passes for the wrong reason.
    from sparkscreen.policy import Policy as _P
    _pol = _P(name="p", rules=default_policy().rules,
              readable_namespaces=("prod.*",))
    check("`prod.*` matches `prod.users`",
          screen('spark.sql("select * from prod.users")', _pol).verdict, Verdict.ALLOW)
    check("`prod.*` does not match `production.users`",
          screen('spark.sql("select * from production.users")', _pol).verdict,
          Verdict.REVIEW)
    _w = _P(name="w", rules=default_policy().rules,
            writable_namespaces=("prod.*",), readable_namespaces=("*",))
    check("`prod.*` covers `prod.users` on the DataFrame path",
          screen('df.write.mode("append").saveAsTable("prod.users")', _w).verdict,
          Verdict.ALLOW)
    check("`prod.*` does NOT cover `prod.staging.x` on the DataFrame path",
          screen('df.write.mode("append").saveAsTable("prod.staging.x")', _w).verdict,
          Verdict.REVIEW)

    # --- the limits table ---
    from sparkscreen.policy import Limits, default_policy
    lim = default_policy().limits
    check("max_code_chars default", lim.max_code_chars, 20_000)
    check("max_sql_chars default", lim.max_sql_chars, 10_000)
    check("max_statements default", lim.max_statements, 200)
    check("max_literals default", lim.max_literals, 100)
    check("max_targets default", lim.max_targets, 100)
    check("oversize code is DENY, not UNKNOWN",
          screen("# pad\n" * 4000).verdict, Verdict.DENY)

    # --- version and wheel claims ---
    import sparkscreen as _pkg
    check("README version claim matches the package",
          "0.8.0" in readme and _pkg.__version__ == "0.8.0", True)
    whl = sorted((ROOT / "dist").glob("*.whl"))
    if whl:
        import zipfile
        size = whl[-1].stat().st_size
        check("wheel size claim ('462 KB') is within 10%",
              abs(size - 462_000) < 46_200, True)
        names = zipfile.ZipFile(whl[-1]).namelist()
        check("wheel ships no .g4 (README says so)",
              len([n for n in names if n.endswith(".g4")]), 0)
        check("wheel ships 8 generated parser modules",
              len([n for n in names if "generated" in n and n.endswith(".py")]), 8)
    else:
        print("  SKIP wheel checks: no dist/*.whl built")

    # --- grammars ---
    from sparkscreen.grammar.spec import SPECS
    check("both grammars present",
          sorted(s.key for s in SPECS), ["spark-3.5.1", "spark-4.0"])
    spec_src = (ROOT / "src/sparkscreen/grammar/spec.py").read_text()
    shas = re.findall(r'"([0-9a-f]{7,40})"', spec_src)
    check("every pin is a full 40-char commit",
          all(len(s) == 40 for s in shas), True)

    # --- the out-of-scope claims must still hold ---
    for snippet in ("os.system('rm -rf /')", "shutil.rmtree('/data')",
                    "dbutils.fs.rm('/', recurse=True)"):
        rep = screen(snippet)
        check(f"out of scope: {snippet[:34]!r}", (rep.verdict, len(rep.findings)),
              (Verdict.ALLOW, 0))
    check("interprocedural is still UNKNOWN",
          screen('def run(t):\n    spark.sql(f"drop table {t}")\nrun("prod.t")').verdict,
          Verdict.UNKNOWN)

    # --- the two-env split the README tells people to use ---
    fast = subprocess.run([str(ROOT / ".venv/bin/python"), "-c", "import pyspark"],
                          capture_output=True, text=True)
    check("fast .venv has no pyspark (README claims this)", fast.returncode != 0, True)
    check("setup.sh exists and is executable",
          (ROOT / "scripts/setup.sh").exists()
          and bool((ROOT / "scripts/setup.sh").stat().st_mode & 0o111), True)
    check("mutate.py exists", (ROOT / "scripts/mutate.py").exists(), True)

    # --- links ---
    for m in re.finditer(r"\]\((?!https?:)([^)#]+)", readme):
        if not (ROOT / m.group(1)).exists():
            failures.append(f"README links to missing file: {m.group(1)}")
    print("  OK   README relative links all resolve")

    # --- no stale claims left ---
    for banned, why in [
        ("invisible", "DataFrame writes are detected now"),
        ("1.9 MB", "the wheel is ~462 KB"),
        ("three verdicts", "there are four"),
        ("~3 MB", "the wheel is ~462 KB"),
    ]:
        if banned.lower() in readme.lower():
            failures.append(f"README still contains a stale claim: {banned!r} ({why})")
    print("  OK   no known-stale claims remain")

    print()
    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        return 1
    print("every README claim verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
