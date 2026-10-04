# Working in this repository

Practices that are specific to *this* codebase, most of them learned the hard way. For
conventions that any Rust/Python repo would share, see `CONTRIBUTING.md`.

---

## Verification habits

**Differential oracles are not optional here.** A test suite that only checks your own
reasoning has not found the bug you care about. Three times now, a complete and correct
hand-written corpus passed 100% while the tool was wrong in a way that mattered:

| the bug | what it took to find it |
|---|---|
| lowercase SQL rejected | running a real Spark ([F6](findings.md#f6--case-insensitivity-found-only-against-a-real-engine)) |
| `BailErrorStrategy` rationale | reading the grammar's `errorCapturingIdentifier` ([D2](decisions.md#d2--bailerrorstrategy-and-the-nuance-that-matters)) |
| destructive targets lost | asserting the *absence* of an input, not its presence ([F7](findings.md#f7--extract_namespaces-reported-columns-as-namespaces)) |

When adding detection for anything, ask what the oracle would be. If you cannot name one,
that work is not ready to merge — say so in the issue instead.

**Run the reproducer before you believe a bug report.** Including from a subagent. Three
of four times an agent "found a bug", the bug was in the test. See
[findings § Where the test suite was wrong](findings.md#where-the-test-suite-was-wrong).

**A wrong expectation is worse than a missing test.** It encodes a false belief that
survives until something depends on it. When a test fails, first ask whether the *source*
is wrong.

---

## Corpus-writing traps

Three that have each cost real time. All are in `CONTRIBUTING.md` too, repeated here
because they are the highest-yield thing to know before writing tests here.

### 1. Never write one corpus and apply it to two grammars

`tests/conftest.py` provides a `spec_key` fixture that parametrizes over all three pinned
grammars, and it will happily run a 4.2-only statement against 4.1:

```
$$abc$$                codeLiteral on 4.x, syntax error in 3.5.1
CALL sys.system_info()  4.x only
SELECT 1 |> SELECT 2    4.x only (pipe operator)
BEGIN ... END           4.x only (script)
SELECT ... QUALIFY ...  4.2 ONLY -- 4.1 and 3.5.1 reject it
```

Version-specific statements belong in `VERSION_SPECIFIC` / `VERSION_SPECIFIC_REJECTED` in
`tests/differential/corpus.py`, never in the shared list.

### 2. Write SQL in lowercase

The original suite was 100% uppercase and passed 100% while every grammar rejected
`select 1`. Real agent-written code is lowercase; real Spark accepts lowercase. An
all-uppercase corpus has a blind spot you cannot see.

### 3. Assert the input you must *not* produce

The `DROP TABLE prod.users` must still yield `prod.users` test exists because narrowing
the namespace extractor to fix over-collection could plausibly have dropped the table
instead. Over-collecting is noisy; losing a table is fail-open.

Same reasoning for `extract_string_literals`: assert a payload is *absent* on the benign
path, not merely present on the malicious one.

---

## Things that look wrong but are not

The codebase contains decisions that read as mistakes. They are not.

- **All three grammars use ANTLR 4.13.1, even though 3.5.1 pins 4.9.3.** 4.9.3 cannot build its
  own grammar for the Python target — labels `from=`, `input=`, `property=` collide with
  Python runtime attributes. See [D7](decisions.md#d7--one-antlr-version-for-both-grammars).
- **The vendored `.g4` files are Apache-2.0 and are committed.** That is deliberate:
  pinning them makes builds reproducible and offline-capable.
- **`OUTSIDE_ALLOWLIST` is not in `UNKNOWN_REASONS`.** Adding it conflates "we could not
  analyze" with "we analyzed and it is outside policy". See
  [D13](decisions.md#d13--unknown_reasons-is-classification-only-never-a-decision).
- **`_JAVA_MARKERS` is a denylist, and that is known to be incomplete.** The real guarantee
  is that generated parsers are committed and CI fails if regeneration differs, plus the
  differential suite. See `tests/test_grammar_port.py`.
- **`reason="repository specific"`-style xfails stay.** Each names the gap. Removing one
  without fixing the underlying issue silently re-opens a fail-open path.

---

## Delegation

Worktree per agent, enforced, not assumed:

```bash
git worktree add ../sparkscreen-<slug> -b <slug>
```

Three times during the initial build, concurrent agents editing adjacent files caused real
conflicts in `tests/test_screen_policy.py` that had to be unpicked by hand. The cost is
low because the pieces are genuinely disjoint — but only if the ownership boundary is
stated in the brief. **Every brief should name the files the agent may touch and the ones
it must not.**

Scratch files belong in the worktree, not `/tmp`. An agent cleaning up after itself
produced a `rm` of five files in `/tmp` that tripped the security guard and required the
maintainer to adjudicate. Version-controlled scratch is visible; `/tmp` scratch is not.

---

## Before you claim something works

The bar this repo holds itself to, from things that were believed and turned out wrong:

- "the wheel works" → install it in a clean venv and run it with `java` absent from
  `PATH`
- "the grammars agree" → run it against every `spec_key` value (there are three)
- "it matches Spark" → run the differential suite against a live session
- "the table is complete" → enumerate the grammar's labels empirically rather than
  asserting against a hand-written list
- "the policy is right" → `policy_label_drift()` and the two allowlists applied
  independently

Each of those is a case where the cheap check passed and the real one did not.