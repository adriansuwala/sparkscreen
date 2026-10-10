# Screening a JupyterHub kernel in place (T8 tiers 2–3)

The scenario: an agent writes PySpark cells; the cells execute in a Jupyter kernel
on a remote server; you want the same sparkscreen verdicts on what the kernel is
about to send to Spark, not only on the file before it was sent.

All of this is user-level. No admin access to the JupyterHub instance, no packages
added to the kernel image, no network component.

## What ships where

| artifact | lives in | what it does |
|---|---|---|
| `sparkscreen` wheel | your persistent home volume (unpacked, not installed) | the screening engine; pure Python, no JVM |
| `00-sparkscreen-gate.py` | `~/.ipython/profile_default/startup/` | wraps `SparkSession.Builder.getOrCreate` at kernel start |
| `kernel.json` (example) | `~/.local/share/jupyter/kernels/<name>/` | same outcome via the kernel launcher, when startup files are not enough |

## The startup hook (primary)

ipykernel executes every `.py`/`.ipy` file in the default profile's `startup/`
directory when the kernel starts. The stock kernelspec launches
`python -m ipykernel_launcher`, which loads `~/.ipython/profile_default` from your
home directory *regardless of what the image contains* — that is the whole trick.

Install:

```bash
# 1. the wheel, unpacked into the persistent home (no pip install needed)
unzip sparkscreen-0.9.0-py3-none-any.whl -d ~/sparkscreen-lib

# 2. the hook
cp examples/connect-gate/00-sparkscreen-gate.py \
   ~/.ipython/profile_default/startup/

# 3. point the hook at the unpacked wheel (one line at its top, or via env var)
export SPARKSCREEN_LIB=~/sparkscreen-lib   # e.g. in ~/.bash_profile
```

What it does, in order: put the unpacked wheel on `sys.path`, import
`sparkscreen.connect`, and patch `SparkSession.Builder.getOrCreate` so every
session created afterwards gets `install_gate` applied — a screening stub in place
of the client's gRPC stub. A Connect session then has every plan screened
in-process before the network byte; a classic session gets the EXPLAIN gate.

## The shadowing kernelspec (fallback)

If the image's IPython profile is unusual, or you want the gate visible in the
kernel list, install a kernelspec into the *user* location under a name that
shadows the image's:

```bash
mkdir -p ~/.local/share/jupyter/kernels/python3
cp examples/connect-gate/kernel.json ~/.local/share/jupyter/kernels/python3/
```

The user location takes priority over system locations, and because the name is
the same, it passes even where an admin has pinned
`KernelSpecManager.allowed_kernelspecs` by name. `argv` must point at the image's
own interpreter; adjust the example if the image's python is not `python3` on
PATH.

## What gets screened, per session type

- **Connect session** (`spark.remote` set, Spark Connect): every `ExecutePlan`
  request the client sends is screened against the plan effect table
  (`sparkscreen.plans`); DataFrame writes, MERGEs, function/DataSource
  registration are judged natively, and any SQL inside the plan goes down the same
  ANTLR pipeline as the static scan. DENY/UNKNOWN raises before the request is
  sent; the agent sees a `ScreenedCommandError` like any other cell exception.
- **Classic session** (`spark://` master): there is no client-side plan; the hook
  falls back to wrapping `spark.sql` with an EXPLAIN-then-decide gate (tier 3).
  This needs a JVM in the kernel environment, which a classic session provides
  anyway.

## The honest limits

- The gate is a guardrail, not a security boundary: the hook lives in the agent's
  own writable home, so agent code that deliberately patches it, or builds a
  session without `Builder.getOrCreate`, runs unwrapped. Use the static scan at
  the MCP/harness boundary for the adversarial case — it runs before the code
  leaves your machine and cannot be skipped from inside the kernel.
- The kernel's pyspark version is the pin for the plan kind table
  (`sparkscreen.plans.kind_drift()` reports a wheel newer than the table; new
  kinds screen as UNKNOWN, never as ALLOW).
- Screening a plan needs the proto modules but not a JVM; the EXPLAIN fallback
  for classic sessions needs a JVM.

## Caveats worth knowing

- **A dead server hangs the first RPC — pyspark behavior, not the gate.** Before
  the first `ExecutePlan` the client fires a `Config` RPC, and plan building
  itself calls `get_configs()`; gRPC retries toward an unreachable endpoint for
  about a minute. With a live server this never bites. Easy to misread as the
  gate hanging — the gate only intercepts `ExecutePlan` and raises before the
  wire.
- pyspark 4.x also has a native session-hook API (`client._session_hooks`,
  `Hook.on_execute_plan`) that could serve as the interception point; the stub
  gate is used because it additionally covers 3.5.1, which has no hook API.
- A blocked write raises `ScreenedCommandError` (a `PermissionError`) from inside
  the client's retry-wrapped call and is **not retried** — pinned by
  `tests/differential/test_connect_gate_live.py` on a real client, and proven
  end-to-end against a real 4.1.3 Connect server (blocked overwrite; the
  server-side table was untouched afterward).

## The two-tool design: capture the plan, screen it anywhere

For the harness topology where sparkscreen lives on the *local* machine and the
kernel is remote, the kernel side exposes one call that returns the typed plan a
snippet would send — without sending it, and without a server behind it:

```python
# Jupyter-side tool body (the kernel has pyspark and the session's state):
from sparkscreen.connect import capture_plans
import json

result = capture_plans(SNIPPET)   # runs SNIPPET in the caller's globals
print(json.dumps(result))         # cell output = the tool's response
```

```python
# Local side (sparkscreen installed, pyspark NOT required):
from sparkscreen.plans import JsonPlan, screen_plan

report = screen_plan(JsonPlan(cap["plan"]), engine_version=cap["pyspark_version"])
```

What the capture guarantees, all differential-tested per wheel:

- The snippet runs to its **first plan send** and stops there; the plan it built is
  returned as a self-describing JSON projection plus the kernel's pyspark version
  (that version is the grammar pin for the SQL inside the plan — the local machine
  may have no pyspark at all).
- Plan *building* works offline: the capture stub answers the client's `Config`
  prefetch locally (4.x fetches two compression keys before its first plan), so no
  server is needed for the capture itself.
- A snippet that never sends a plan returns an empty captures list; its own
  exceptions propagate, and the original stub is always restored.

This is the advisory half of the design — the enforcing variant ("hold the request,
replay it only after the verdict allows") is T9 in `docs/threads.md`, deliberately
not built yet.

The full arc has an integration test that runs the real wire per pinned wheel:
a `jupyter_server` subprocess with a pyspark kernel, the harness side creating the
session over REST and executing the bootstrap/capture cells, and a second
interpreter **without pyspark** screening what came back
(`tests/differential/test_capture_two_tool.py`). What it does not simulate is
JupyterHub's auth/session layer on top of the same API, or a live cluster — the
kernel never needs one for the capture itself.
