# Documentation

Written records of what this project is, what it decided, and what it got wrong. The
point is that reasoning survives past the conversation it happened in — several entries
here exist because an insight was made once and would otherwise have been re-derived.

| document | what it is |
|---|---|
| [findings](findings.md) | every bug found, with a live reproducer, ordered by severity |
| [decisions](decisions.md) | numbered decisions, the rationale, and what the rejected alternative cost |
| [threads](threads.md) | open questions and musings — feasibility notes, not commitments |
| [roadmap](roadmap.md) | current state, what is next, and what was deliberately dropped |

For the tool itself, start with the [README](../README.md).

---

## Two things worth knowing before reading

**Almost every bug was a fail-open.** The screener reported ALLOW, reported nothing, or
reported something confident and wrong. None of them crashed. That is the specific danger
of a security tool: a crash is visible, a plausible wrong answer gets trusted.

**Three times, a subagent found that the maintainer's test was wrong, not the code.**
Those are recorded in [findings](findings.md#where-the-test-suite-was-wrong) rather than
quietly fixed, because they are the most useful entries in the file — a test suite that
disagrees with you is working, and if every test passes first time, one of you is not
trying.

---

## Conventions used here

- **Reproducers are live.** Each finding in `findings.md` includes code that returns the
  wrong answer on the code before its fix. Re-run them if you touch that area.
- **Decisions are numbered** (D1–D14) so code comments can cite them without a URL.
- **Threads are explicitly not decisions.** They record what we currently believe and what
  would change our mind, so a future reader can tell a settled call from an open question.
- **Estimates are marked by oracle availability.** The general rule this project keeps
  rediscovering is that a test suite which can only check your reasoning has not found the
  bug you care about. See [roadmap § How to read these](roadmap.md#how-to-read-these).