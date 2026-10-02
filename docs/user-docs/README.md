# Documentation

## For users

Start here.

| | |
|---|---|
| [README](../README.md) | what the tool is, install, quick start |
| [usage](usage.md) | verdicts, CLI, library API, policies, limits, troubleshooting |

## For contributors and agents

Verbose, with the reasoning and the dead ends.

| | |
|---|---|
| [WORKING.md](../WORKING.md) | index, and the two things to know before trusting anything here |
| [agents.md](../agents.md) | conventions, corpus traps, verification bar |
| [findings.md](../findings.md) | every bug, with a live reproducer |
| [decisions.md](../decisions.md) | D1–D14 and what the alternatives cost |
| [threads.md](../threads.md) | open questions and feasibility notes |
| [roadmap.md](../roadmap.md) | current state and what is next |
| [issues.md](../issues.md) | the issue ledger |
| [CONTRIBUTING](../CONTRIBUTING.md) | branch policy, worktrees, test conventions |

---

## The short version

`sparkscreen` decides whether a piece of agent-written PySpark is safe to execute, using
Python's `ast` to find `spark.sql()` sinks and **Spark's real ANTLR grammar** to parse
what it finds. It returns `ALLOW`, `DENY`, or `UNKNOWN`, and has no code path from
"something went wrong" to `ALLOW`.

It currently only sees SQL reached through `spark.sql()`. DataFrame API writes
(`df.write.saveAsTable`, `.save`, `.jdbc`) are **not detected** — the largest known gap,
and the top roadmap item. If your agent writes via the DataFrame API, instruct it to use
`spark.sql()`.