# sparkscreen — working documents

Everything here is **context for agents and contributors**, not for users of the tool.
The register is deliberately different: verbose, hedged where the uncertainty is real,
and willing to record a wrong turn and why it was wrong.

For user documentation, see [user-docs](user-docs/) and the [README](../README.md).

| document | what it is | read it when |
|---|---|---|
| [findings](findings.md) | every bug found, with a live reproducer, ordered by severity | deciding whether to trust the tool; touching the area a bug lived in |
| [decisions](decisions.md) | D1–D14, numbered, with what the rejected alternative cost | about to change something a decision already settled |
| [threads](threads.md) | T1–T6, open questions and feasibility notes | looking for work that is *not* decided yet |
| [roadmap](roadmap.md) | current state and what is next | picking something up |
| [agents.md](agents.md) | how to work in this repo: conventions, traps, verification habits | before delegating to a subagent |
| [issues.md](issues.md) | the issue ledger and how it relates to the roadmap | before creating or closing work |

---

## What this repository is, for a reader with no context

`sparkscreen` answers one question: **is this PySpark safe to execute?** It is a static
screener, intended to sit in an agent's execution path.

Two real parsers, because regexes fail on adversarial text:

1. Python's `ast` finds `spark.sql(...)` sinks and recovers the SQL by constant
   propagation through f-strings, concatenation, `%`, `.format()` and format specs.
2. **Spark's actual ANTLR grammar**, pinned per version and translated to a Python
   target, parses the recovered SQL. Rules key on labeled alternatives like `DropTable`
   and `InsertOverwriteTable`, never on substrings.

Three verdicts: `ALLOW`, `DENY`, `UNKNOWN`. The third one is the entire design. See
[D1](decisions.md#d1--three-verdicts-not-two).

---

## The thing to know before trusting any of it

**Almost every bug ever found in this repository was a fail-open.** The screener reported
`ALLOW`, reported nothing, or reported something confident and wrong — and none of them
crashed. That is the specific failure mode of a security tool: a crash is visible, and a
plausible wrong answer gets trusted.

[findings.md](findings.md) has twelve of them, each with a reproducer you can run.

The corollary, and it has bitten repeatedly: **a test suite that can only check your
reasoning has not found the bug you care about.** The case-insensitivity bug
([F6](findings.md#f6--case-insensitivity-found-only-against-a-real-engine)) was invisible
to a complete, correct, 100%-passing hand-written corpus. It took a live Spark.

---

## Vocabulary

Three terms that were conflated early and are now load-bearing. Do not re-conflate them.

- **Effect** — what an operation *does*. A set of orthogonal flags (`WRITE_DATA`,
  `DESTROY_DATA`, `LOAD_CODE`, …), derived from the statement label. See
  [D4](decisions.md#d4--effect-is-a-set-of-flags-not-an-enum).
- **Confidence** — whether we were able to determine the effect. Distinct from effect
  itself.
- **Verdict** — what the *policy* decided about that effect. `ALLOW` / `DENY` /
  `REVIEW` / `UNKNOWN`.

Deriving a verdict from anything other than the verdict itself is a bug with a history:
see [F1](findings.md#f1--verdict-aggregated-on-reason-instead-of-verdict) and
[D3](decisions.md#d3--aggregate-on-verdict-never-on-reason).