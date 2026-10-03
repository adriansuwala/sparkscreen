# Contributing

## Branch policy

`master` is the release line. It is **prod/release ready** at every commit: it builds,
the wheel imports with no JVM, and the fast test suite passes. Nothing lands on `master`
that is half-finished or knowingly buggy.

```
master                  <- release line. always green. tagged releases only.
  |
  +-- test-suite        <- integration branch for the test suite + folding fixes
        |
        +-- worktree: fix-folding       (fail-open sink bugs)
        +-- worktree: test-treewalk     (walker diagnosis + tests)
        +-- worktree: test-properties   (fuzz + grammar-port tests)
```

Long-lived integration branches are fine; feature branches off them are expected. The
rule is narrow: **`master` never carries a failing test or a known fail-open path.**

### Worktrees

Parallel work uses `git worktree` so branches do not block each other:

```bash
git worktree add ../sparkscreen-fix-folding -b fix-folding
cd ../sparkscreen-fix-folding && pytest tests/ -q
```

The reason to insist on this here is concrete rather than stylistic. Several bugs in this
project's history were found by *two agents editing adjacent files at once* — and twice,
that caused real conflicts that had to be unpicked by hand (`tests/test_screen_policy.py`
was mid-edit from three directions). One agent per worktree removes the class of problem
entirely.

## Commit messages

Explain the failure, not the diff. The most useful commits in this repo's history are the
ones that record a wrong belief and what corrected it:

- `f3c1f39` — "aggregate verdicts on verdict, not reason — DELETE/MERGE/INSERT were
  ALLOW". The body carries the two live reproducers.
- The case-insensitivity commit records that the original port was *wrong* and that only
  differential testing against a real engine found it.

That second kind is the point. A commit that says only "fix bug in policy.py" is worth
less than the time it cost.

If a commit fixes a bug, include a reproducer. If a commit corrects an earlier claim,
say which claim and why it was wrong.

## Tests

```bash
pytest tests/ -q                        # fast suite; must pass before pushing
.venv/bin/mutmut run --max-children 4  # mutation testing
.venv/bin/mutmut results                # survivors = assertions that do not bite
```

Mutation testing is not optional for a change to `policy.py`, `model.py`, `screen.py` or
`treewalk.py` — those decide verdicts. It has caught real gaps that 2,500 tests could not,
including an untested public API and an off-by-one on a limit boundary. Survivors are a
measurement, not a failure: read them and decide whether the code is unreachable or the
test is too weak.

Config is in `[tool.mutmut]` in `pyproject.toml` — mutmut 3 reads it only there, since
`mutmut run` accepts no `--config` flag. `process_isolation = "forkserver"` is load-bearing:
the default `fork` mode deadlocked on this project, and forkserver is mutmut's documented
remedy for exactly that symptom.

`scripts/mutate.py` also exists as a lighter cross-check with per-mutant test selection.
Prefer `mutmut`; see the header of that file for why its own numbers are less trustworthy
(its generator is line-based and skipped 127 of 244 mutants).

Differential tests need a real engine and are opt-in:

```bash
uv pip install --python .venv-pyspark/bin/python pyspark==3.5.1 \
    "antlr4-python3-runtime==4.13.1" pytest
JAVA_HOME=/path/to/jre .venv-pyspark/bin/python -m pytest \
    tests/differential -q
```

### Writing test corpora

Two lessons that cost real time here, both worth respecting in review:

1. **Never write a corpus against one grammar and apply it to both.** `SELECT $$abc$$` is
   a `codeLiteral` in 4.0 and a syntax error in 3.5.1; `CALL` and the pipe operator exist
   only in 4.0. Use the `spec_key` fixture and branch where the *grammar* differs, not
   where your expectation differs.

2. **Write corpora in lowercase.** The original suite was written entirely in uppercase
   SQL, which is why it passed while both grammars rejected `select 1`. Real agent-written
   code is lowercase; real Spark accepts lowercase. If a corpus is all-uppercase it has a
   blind spot you cannot see.

3. **A wrong expectation is worse than a missing test.** When a test fails, first ask
   whether the *source* is wrong. Twice during development the test was right and the
   expectation was wrong — `INSERT INTO t SELECT * FROM` genuinely *is* accepted by both
   Spark's grammar and real Spark, via `errorCapturingIdentifier`. Do not weaken an
   assertion to make a test pass; record the observation instead.

## Grammar changes

The generated parsers are committed and ship in the wheel, so they are part of the source
tree, not build output. If you touch `grammar/port.py` or the vendored `.g4` files:

```bash
python -m sparkscreen.grammar.build --generate
git diff --exit-code -- src/sparkscreen/grammar/generated   # CI enforces this
```

Never hand-edit anything under `src/sparkscreen/grammar/generated/`.

## The fail-closed rule

This is the invariant any change must preserve: **nothing in this package has a code path
from "something went wrong" to `ALLOW`.**

Every new failure mode you add must land on `Verdict.UNKNOWN`. If you are tempted to
report a confidently-wrong result instead of UNKNOWN, don't — a plausible but incorrect
finding is worse than no finding, because the operator sees a clean, specific verdict for
something that will never actually run.

`tests/test_verdicts.py` enforces this and should grow whenever a new failure mode is
introduced.