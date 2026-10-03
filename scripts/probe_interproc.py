"""Verify interprocedural folding against cases the subagent did not write.

The agent reported two headline reproducers and a list of things it deliberately left
unresolved. Both matter, and neither is self-verifying:

  * the two headline cases must actually resolve now;
  * every case it says it does NOT resolve must still be UNKNOWN, not silently ALLOW.

The second is the one that can hide a fail-open. "I did not resolve X" is only reassuring
if X is visible somewhere. The right outcome for an unresolvable case is UNKNOWN, so this
probe distinguishes ALLOW (fail-open) from UNKNOWN (correct refusal) for each.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sparkscreen import screen  # noqa: E402

# Must resolve to a DROP finding.
MUST_RESOLVE: dict[str, str] = {
    "param, literal arg": 'def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.users")',
    "param, two call sites agree": (
        'def drop(t):\n    spark.sql(f"DROP TABLE {t}")\n'
        'drop("prod.users")\ndrop("prod.users")'
    ),
    "param with default": (
        'def drop(t="prod.users"):\n    spark.sql(f"DROP TABLE {t}")\ndrop()'
    ),
    "loop over literal list": (
        'for t in ["prod.users", "prod.orders"]:\n    spark.sql(f"DROP TABLE {t}")'
    ),
    "loop over literal tuple": 'for t in ("prod.users",):\n    spark.sql(f"DROP TABLE {t}")',
    "intraprocedural (regression)": (
        'table = "prod.users"\nspark.sql(f"DROP TABLE {table}")'
    ),
}

# The agent said it does NOT resolve these. Each must come back UNKNOWN (or at worst
# REVIEW/never ALLOW). An ALLOW on any of these is a fail-open.
MUST_NOT_RESOLVE: dict[str, str] = {
    # Single call site, literal arg, no cycle: resolvable, and DENY is the right answer.
    # Resolution here is not a bug -- the agent's list of exclusions is about constructs it
    # cannot bound, not a blanket refusal.
    "recursion, single literal call site": (
        'def f(n):\n    spark.sql(f"DROP TABLE prod.t{n}")\nf(1)'
    ),
    # Must contain a real sink, or ALLOW is right for the trivial reason that there is
    # nothing to find. An earlier version of this probe had no spark.sql at all and
    # "correctly" reported a fail-open.
    "mutual recursion": (
        'def a(t):\n    b(t)\ndef b(t):\n    spark.sql(f"DROP TABLE {t}")\na("prod.users")'
    ),
    "decorator": (
        '@deco\ndef drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.users")'
    ),
    "generator (body does not run)": (
        'def drop(t):\n    spark.sql(f"DROP TABLE {t}")\n    yield 1\nlist(drop("prod.users"))'
    ),
    "kwargs": 'def drop(*args, **kwargs):\n    spark.sql(f"DROP TABLE {args[0]}")\ndrop("prod.users")',
    "non-literal default": 'def drop(t=os.environ["T"]):\n    spark.sql(f"DROP TABLE {t}")\ndrop()',
    "nested call argument": 'def h(x):\n    return x\ndef drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop(h("prod.users"))',
    "call sites disagree": (
        'def drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.users")\ndrop("prod.orders")'
    ),
    "function never called": 'def drop(t):\n    spark.sql(f"DROP TABLE {t}")',
    # Module-level t is shadowed by the parameter inside the body, so DROP prod.users is
    # what this code really does. Resolving it is correct.
    "module name shadowed by its own param": (
        't = "safe"\ndef drop(t):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.users")'
    ),
    "module name used inside body": (
        't = "safe"\ndef drop(x):\n    spark.sql(f"DROP TABLE {t}")\ndrop("prod.users")'
    ),
    "loop over runtime name": 'for t in tables:\n    spark.sql(f"DROP TABLE {t}")',
    "loop over set": 'for t in {"prod.users"}:\n    spark.sql(f"DROP TABLE {t}")',
    "loop over enumerate": 'for i, t in enumerate(tables):\n    spark.sql(f"DROP TABLE {t}")',
    "loop body has break": (
        'for t in ["prod.users", "prod.orders"]:\n'
        '    if t:\n        break\n    spark.sql(f"DROP TABLE {t}")'
    ),
    "loop body has continue": (
        'for t in ["prod.users", "prod.orders"]:\n'
        '    if t:\n        continue\n    spark.sql(f"DROP TABLE {t}")'
    ),
    "loop body has nested loop": (
        'for t in ["prod.users"]:\n'
        '    for u in ["prod.orders"]:\n        spark.sql(f"DROP TABLE {t}")'
    ),
    "loop over comprehension": (
        'for t in [x for x in tables]:\n    spark.sql(f"DROP TABLE {t}")'
    ),
    "empty iterable": 'for t in []:\n    spark.sql(f"DROP TABLE {t}")',
    "one unknown element poisons": 'for t in ["prod.users", unknown()]:\n    spark.sql(f"DROP TABLE {t}")',
    "method, not module fn": (
        'class C:\n    def drop(self, t):\n        spark.sql(f"DROP TABLE {t}")\nC().drop("prod.users")'
    ),
    "call site inside the body": (
        'def drop(t):\n    drop("prod.users")\n    spark.sql(f"DROP TABLE {t}")\ndrop("x")'
    ),
    "async def": 'async def drop(t):\n    spark.sql(f"DROP TABLE {t}")',
}

# Nested rebinding that must NOT resurrect a folded constant (the agent says it fixed 14
# of these shapes). Each reports DROP prod.a today on the unfixed baseline.
STALE_REBIND: dict[str, str] = {
    "del": 't = "prod.a"\nif c:\n    del t\nspark.sql(f"DROP TABLE {t}")',
    "reassign": 't = "prod.a"\nif c:\n    t = input()\nspark.sql(f"DROP TABLE {t}")',
    "import rebinds": 't = "prod.a"\nif c:\n    import t\nspark.sql(f"DROP TABLE {t}")',
    "with-as": 't = "prod.a"\nwith open(t) as t:\n    pass\nspark.sql(f"DROP TABLE {t}")',
    "except-as": 't = "prod.a"\ntry:\n    pass\nexcept E as t:\n    pass\nspark.sql(f"DROP TABLE {t}")',
    "augmented": 't = "prod.a"\nif c:\n    t += "x"\nspark.sql(f"DROP TABLE {t}")',
    "walrus": 't = "prod.a"\nif (t := other()):\n    pass\nspark.sql(f"DROP TABLE {t}")',
    "loop target": 't = "prod.a"\nfor t in others:\n    pass\nspark.sql(f"DROP TABLE {t}")',
}


def main() -> int:
    failures: list[str] = []

    print("must resolve:")
    for label, code in MUST_RESOLVE.items():
        rep = screen(code)
        resolved = any(f.effect and "DROP" in (f.message or "").upper() for f in rep.findings) or rep.verdict.value == "deny"
        mark = "ok  " if resolved else "MISS"
        if not resolved:
            failures.append(f"[must-resolve] {label}")
        print(f"  {mark} {label:32} {rep.verdict.value}")

    print("\nmust NOT resolve (UNKNOWN or REVIEW, never ALLOW):")
    for label, code in MUST_NOT_RESOLVE.items():
        rep = screen(code)
        bad = rep.verdict.value == "allow"
        mark = "FAIL" if bad else "ok  "
        if bad:
            failures.append(f"[must-not-resolve] {label} -> ALLOW")
        print(f"  {mark} {label:32} {rep.verdict.value}")

    print("\nstale rebindings (must not report a DROP they cannot execute):")
    for label, code in STALE_REBIND.items():
        rep = screen(code)
        claims_drop = any(
            "DROP" in (f.message or "").upper() and "prod.a" in (f.message or "")
            for f in rep.findings
        )
        mark = "FAIL" if claims_drop else "ok  "
        if claims_drop:
            failures.append(f"[stale-rebind] {label} still reports DROP prod.a")
        print(f"  {mark} {label:32} {rep.verdict.value}")

    print()
    if failures:
        print(f"FAIL ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
        return 1
    print("OK: resolves what it claims, refuses the rest, no stale rebinding survives")
    return 0


if __name__ == "__main__":
    sys.exit(main())