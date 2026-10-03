# Threads

Open questions, musings, and feasibility notes. Unlike [decisions](decisions.md), these
are **not** settled — they are things worth thinking about, recorded so the reasoning
survives past the conversation it happened in.

Each says what the question is, what we currently believe, and what would change our
mind.

---

## T1 — The inverse kernel: pseudo-executing code so variation resolves itself

**Status: musing. Explicitly out of scope for the current project.**

The original framing, worth preserving because it is sharp: there is a finite set of
"syscalls" a program can make; the interesting move is that **the environment does the
policing, not the external resource**. Execute the code, but intercept every consequential
operation at the boundary, and block on the tightest channel.

What we already do is a static approximation of this: we resolve as much as we can
statically, and where we cannot, we report UNKNOWN rather than guessing. The gap is that
we give up per-sink rather than resolving the whole program.

What would actually close it is interprocedural constant propagation — `def run(tbl):
spark.sql(f"drop table {tbl}")` is UNKNOWN today even when every caller passes a literal.
That is the cheap, high-value version of this idea and needs no execution at all. See
[roadmap](roadmap.md#blind-spot-work).

**Would change our mind** if interprocedural propagation proves too noisy to be worth it;
a bounded symbolic execution would be the fallback.

---

## T2 — Logical-plan interception instead of text parsing

**Status: musing, but assessed as genuinely feasible.**

Spark Connect ships a **typed logical plan** over the wire rather than SQL text.
Inspecting that would be strictly more accurate than parsing: no constant folding, no
unresolved strings, and — critically — it covers the DataFrame API, which is the current
biggest blind spot.

Feasibility, concretely:

- **Connect-only without a JVM hook.** A proxy in front of the Connect endpoint, or a
  custom `SparkSession`/`SparkConnectClient` wrapper, sees every plan. Works for any code
  that goes through Spark Connect, including from a notebook.
- **Not universal.** Classic sessions and `local[...]` mode speak Thrift, not Connect, so
  catching those needs either a JVM-side agent or a different interception point.
- **Only covers executed code.** It cannot screen code being reviewed but not run — which
  is where pre-commit and audit use cases live.

**The strongest argument for building it as a second backend rather than replacing the
static one:** static screening is the only thing that protects code you have *not* run.
The two are complementary, not redundant. A logical-plan backend would be strictly more
accurate on the overlap and completely absent on the part that matters most for review.

**Would change our mind** if Connect adoption is too narrow in practice — then the cost of
the JVM-side agent is probably not worth it, and the static path remains the primary.

---

## T3 — Build it as an MCP server?

**Status: assessed feasible. Feasibility only; not planned.**

The same interception point as T2, packaged as a model-context-protology server, so an
agent calls `screen` as a tool and the gate sits in the execution path rather than in a
review step.

What that buys, and it is a real ergonomic gain: the policy gate becomes *structural*.
An agent cannot bypass it, because the gate is the only way to reach the session. Today a
correctly-screened snippet and an unscreened one look identical to the agent — the
screening decision lives outside the conversation.

Ergonomics, honestly assessed:

- **Good:** one round trip, structured verdict, no code review needed for the ALLOW case.
- **Costs:** the agent needs the tool to do anything at all, so it is a dependency; a
  misconfigured policy becomes a silent gate; and UNKNOWN needs somewhere to go. The last
  is the real design question — an MCP server returning "review this" has to either block
  or hand back to a human, and both are UX commitments.

---

## T4 — Pluggable operation catalogues

**Status: musing. Long-term.**

The idea: devs of other packages prepare a *categorisation of their operations*, and
sparkscreen accepts it as a plugin so their operations screen with the same policy engine.

The reason this looks reachable is the `Effect` flag set ([D4](decisions.md#d4--effect-is-a-set-of-flags-not-an-enum)).
Once effects are keyed off an operation name rather than derived from a SQL label, the
integration surface collapses to **a table mapping their operations to flags**. They ship
the table; sparkscreen ships the policy engine. That is a small, stable contract, and it
is why D4 was worth doing in the abstract rather than only for `saveAsTable`.

What would have to be true: `Effect` must stay coarse and orthogonal. If the flag set
grows operations-specific members, the contract stops being a table and starts being
code, and the plugin model dies.

**Would change our mind** if the effect taxonomy needs per-operation members to be
useful in policy — which would mean the coarse/orthogonal split was the wrong shape.

---

## T5 — The crossroads: grammar-based or logical-plan-based?

**Status: open, but leaning "both".**

The instinct is that these are alternatives and one must win. That framing is wrong, and
it is worth writing down why:

| | static / grammar | logical plan |
|---|---|---|
| code never run | **covers it** | blind |
| DataFrame API | blind | **covers it** |
| arbitrary SQL | full fidelity | full fidelity |
| setup cost | none | intercept proxy or agent |
| offline / pre-commit / audit | **works** | needs a running cluster |
| false-negative risk | higher (folding gaps) | near zero |

They fail in opposite directions. The static path is what makes this tool usable as a
review and audit gate; the plan-based path is what makes it authoritative. Building only
the second would make it unusable for pre-commit review; building only the first leaves
the DataFrame blind spot open.

**Recommendation:** static first, both if and when the interception layer earns its keep.
Note the static path also does the folding that T1 wants, so effort is not wasted either
way.

---

## T5b — DataFrame writes: known gap

Shipped in 22ed358. One deliberate limitation: an aliased writer defeats the `save` and
`jdbc` half. `w = df.write; w.save("s3://x")` is not detected, because the constant folder
tracks string values and does not model object bindings. `saveAsTable` and `insertInto` are
unaffected — they match on method name alone, so `w.saveAsTable(...)` is caught.

Recorded rather than guessed at. Fixing it means tracking that a name was bound to a
`.write` expression, which is a different kind of analysis from the one the folder does.

## T6 — Should Python-level calls be screened here?

**Status: open. Leaning separate tool.**

Currently everything outside a foldable `spark.sql()` literal is invisible:
`os.system`, `shutil.rmtree`, `dbutils.fs.rm` all return ALLOW with zero findings.

Argument for scoping it out: in the target deployment — ephemeral Kubernetes pods — the
blast radius of `rm -rf /` is a pod that is already disposable. The expensive failures in
that environment are wrong writes to a warehouse that belongs to someone else, which is
the *Spark* problem, not the Python problem.

Argument for keeping it in: a general Python screener is useful for local code review.

**Decided 2026-10-02** (`sparkscreen-znf`): out of scope for this tool, and the
never-raised `Reason.PYTHON_DANGEROUS_CALL` was deleted so the enum stops implying
otherwise.

**Recommendation:** keep the Spark tool Spark-shaped, and build Python screening as a
separate tool that consumes the same `Effect`/`Confidence` vocabulary ([T4](#t4--pluggable-operation-cataloques)).
That is the pluggable-catalogue idea applied to ourselves — a good forcing function for
whether the abstraction is real.

**Would change our mind** if users turn out to run this against code where local
filesystem access genuinely matters.

## T7 — should we execute the code instead of analysing it?

**Status: open. Leaning "no for screening, yes as a differential oracle".** Raised
2026-10-03.

The proposal, worth stating precisely because it is more than "use a sandbox": install a
shim `pyspark` module whose `sql()`, `table()`, and `write` methods do nothing but record
their arguments, then `exec()` the snippet and read off the SQL strings and operations
it *would* have issued. Python's own interpreter does the constant propagation,
control flow, and closure resolution that we currently approximate.

**The argument for it is strong on the merits.** Today, of the shapes a screener meets:

| shape | verdict today |
|---|---|
| `spark.sql("DROP TABLE prod.t")` | DENY — recovered |
| `t="prod.t"` + f-string | DENY — recovered |
| `def d(t): spark.sql(f"DROP TABLE {t}")` then `d("prod.t")` | UNKNOWN |
| `for t in ["a","b"]: spark.sql(f"DROP TABLE {t}")` | UNKNOWN |
| `"".join(... for t in [...])` | UNKNOWN |
| `M["drop"]` | UNKNOWN |
| `input()` | UNKNOWN (correctly — unresolvable in principle) |

Execution collapses rows 3–5 to DENY. That is the entire "interprocedural folding" work
item, and it gets it for free and completely.

**Why not as the primary path.** Three reasons, in order of weight.

1. **It executes the code it is being asked to judge.** For a screener whose input is
   agent-written PySpark, that inverts the trust model. Anything that does not go through
   our shim — a bare `open()`, a subprocess, anything using a real `pyspark` already
   imported by the host process — runs for real. A correct sandbox is a *large* project
   (seccomp/gVisor, filesystem and network namespace isolation, resource limits, timeouts)
   and a permanent source of CVEs. The static path has no such surface because it never
   runs anything.
2. **It cannot run without a cluster-shaped environment**, so the "works offline, in
   pre-commit, no JVM" property is lost. That property is most of the value.
3. **It answers "what did this literal snippet do", not "what does this function do".**
   Agents compose across files and call sites we never see. Executing one file does not
   resolve the program.

**Where it *is* right: as a differential oracle.** A third path for the gaps above. In
`tests/`, exec the snippet against a shim and assert the shim saw what the static folder
claimed — a property test that the folder's UNKNOWN is genuinely a limitation rather than
a bug, and that its recovered strings match an independent interpreter's. Same technique
as the existing live-Spark differential, one layer in, and it cannot hurt production
because it never runs there. This is the version worth building, if any.

**Would change our mind** if the static path's UNKNOWN rate proved to be high enough in
real agent output that operators stopped reading UNKNOWN — at which point the cost of a
real sandbox might be worth paying. We have not measured that, and measuring it means
collecting a corpus of real snippets. That corpus does not exist yet and is probably the
highest-value next artifact of any kind.
