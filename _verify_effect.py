"""Verify the design claims the Effect axis makes about itself.

Several of these are non-obvious claims in docstrings. A docstring that asserts a
safety property is only worth as much as its verification, so this checks the ones
that matter rather than restating them.

Not part of the pytest suite: a design audit. Promote it if the Effect API churns.
Run: .venv/bin/python _verify_effect.py
"""
from __future__ import annotations
import sys as _sys
from pathlib import Path as _Path
# Runnable as a bare script without PYTHONPATH=src, like any other entry point.
_sys.path.insert(0, str(_Path(__file__).resolve().parent / "src"))


from sparkscreen import (
    Effect,
    Finding,
    Reason,
    Severity,
    Verdict,
    effect_names,
    read_only_policy,
    screen,
)
from sparkscreen.analysis.effects import UnmappedLabelError, effects_for_label


failures: list[str] = []


def check(label: str, got, want) -> None:
    good = got == want
    if not good:
        failures.append(f"{label}: got={got!r} want={want!r}")
    print(f"  {'OK  ' if good else 'FAIL'} {label:50} {got!r}")


def names(effects) -> list[str]:
    """Sorted member names. Uses str() because a composite Flag's .name is a joined
    string, and .name is None for a value with no single name."""
    return sorted(str(e) for e in effects if e)


print("--- the str-mixin hazard ---")
# Docstring claims: with a `str` mixin a bare string could enter an effect set and be
# compared as if it were the flag. Verify plain Flag actually blocks that.
check("Effect is not a str subclass", issubclass(Effect, str), False)
try:
    Effect("DESTROY_DATA")
    check("str rejected on construction", "accepted silently", "ValueError")
except ValueError:
    check("str rejected on construction", "ValueError", "ValueError")

print("\n--- no Effect.UNKNOWN (absence is the signal) ---")
check("no UNKNOWN member", hasattr(Effect, "UNKNOWN"), False)
check("empty effect on unparseable SQL",
      screen("spark.sql('SELCT 1')").findings[0].effect, frozenset())
check("...but a real statement has one",
      screen("spark.sql('SELECT 1')").findings[0].effect,
      frozenset({Effect.READ_DATA}))

print("\n--- composite expansion ---")
# Iterating a Flag yields members, but a set can hold a composite pseudo-member whose
# .name is the joined string. effect_names must expand rather than emit that.
check("effect_names expands composite", effect_names({Effect(3)}),
      ["WRITE_DATA", "WRITE_SCHEMA"])
check("effect_names of a plain set",
      effect_names({Effect.LOAD_CODE, Effect.DESTROY_DATA}),
      ["DESTROY_DATA", "LOAD_CODE"])
check("effect_names of empty", effect_names(frozenset()), [])
check("effect_names drops zero", effect_names({Effect(0)}), [])

print("\n--- str rendering ---")
check("combination str",
      str(Effect.WRITE_SCHEMA | Effect.DESTROY_DATA), "WRITE_SCHEMA|DESTROY_DATA")
check("single member str", str(Effect.READ_LOCAL_FS), "READ_LOCAL_FS")
check("no repr noise", "Effect." in str(Effect.WRITE_DATA), False)

print("\n--- has_effect requires ALL, not any ---")
f = Finding(
    verdict=Verdict.DENY,
    reason=Reason.DESTRUCTIVE_STATEMENT,
    message="drops a column",
    severity=Severity.CRITICAL,
    statement="DropTableColumns",
    effect=frozenset({Effect.WRITE_SCHEMA, Effect.DESTROY_DATA}),
)
check("has both", f.has_effect(Effect.WRITE_SCHEMA, Effect.DESTROY_DATA), True)
check("missing one -> False", f.has_effect(Effect.WRITE_SCHEMA, Effect.LOAD_CODE), False)
check("single flag present", f.has_effect(Effect.WRITE_SCHEMA), True)
check("effect_flags() collapses", f.effect_flags(),
      Effect.WRITE_SCHEMA | Effect.DESTROY_DATA)

print("\n--- effect is independent of verdict ---")
# The central claim: a DROP TABLE is DESTROY_DATA whichever policy ran.
d = screen("spark.sql('DROP TABLE prod.t')")
u = screen("spark.sql('DROP TABLE prod.t')", read_only_policy())
check("DENY verdict, DESTROY_DATA",
      (d.verdict, names(d.findings[0].effect)), (Verdict.DENY, ["DESTROY_DATA"]))
check("UNKNOWN verdict, still DESTROY_DATA",
      (u.verdict, names(u.findings[0].effect)),
      (Verdict.UNKNOWN, ["DESTROY_DATA"]))
check("same effect, different verdict",
      d.findings[0].effect == u.findings[0].effect, True)

print("\n--- the DROP COLUMN question, both halves ---")
add = screen("spark.sql('ALTER TABLE t ADD COLUMN a INT DEFAULT 0')").findings[0]
drop = screen("spark.sql('ALTER TABLE t DROP COLUMN a')").findings[0]
check("ADD COLUMN is additive only", names(add.effect), ["WRITE_SCHEMA"])
check("DROP COLUMN destroys", names(drop.effect), ["DESTROY_DATA", "WRITE_SCHEMA"])
check("policy can distinguish them", drop.has_effect(Effect.DESTROY_DATA), True)
check("...and ADD COLUMN is not destructive",
      add.has_effect(Effect.DESTROY_DATA), False)

print("\n--- unknown labels raise, never silently empty ---")
try:
    effects_for_label("TotallyMadeUpStatement")
    check("unknown label raises", "no raise", "UnmappedLabelError")
except UnmappedLabelError:
    check("unknown label raises", "UnmappedLabelError", "UnmappedLabelError")

print("\n--- Report.effects is a union summary, not a verdict ---")
r = screen("spark.sql('DROP TABLE prod.t')\nspark.sql('SELECT 1')")
check("union across findings", names(r.effects), ["DESTROY_DATA", "READ_DATA"])
check("empty when nothing analyzed", screen("spark.sql('SELCT 1')").effects, frozenset())
check("by_effect(DESTROY_DATA) count", len(r.by_effect(Effect.DESTROY_DATA)), 1)

print("\n--- JSON serialises as sorted names ---")
check("to_dict() effect field", d.findings[0].to_dict()["effect"], ["DESTROY_DATA"])

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for x in failures:
        print(f"  - {x}")
    raise SystemExit(1)
print("every Effect design claim verified")
