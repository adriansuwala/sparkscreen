"""IPython startup hook: gate every Spark session this kernel creates.

Runs at ipykernel start (files in ~/.ipython/profile_default/startup/ execute in
sorted order; the 00- prefix keeps it first). After it runs, every SparkSession
created via `SparkSession.builder` in this kernel is born screened:

- a Spark Connect session gets `install_gate`: every `ExecutePlan` request the
  client sends is screened in-process, before the network byte; DENY/UNKNOWN
  raises `ScreenedCommandError` instead of the request being sent.
- a classic session gets the EXPLAIN gate on `spark.sql` (see
  `examples/connect-gate/classic-explain-gate.py`), because classic sessions have
  no client-side plan to screen.

No admin rights are needed: the user profile directory is in the user's home.
The wheel is loaded from `SPARKSCREEN_LIB` (an unpacked wheel directory) so
nothing has to be installed into the kernel image.

Both builder classes are covered on purpose. `pyspark.sql.SparkSession.Builder`
(the facade, whose `getOrCreate` dispatches to Connect when `spark.remote` is
set) and `pyspark.sql.connect.session.SparkSession.Builder` (the Connect-native
class, used by code that imports it directly) are distinct classes on 4.x, and
patching only the facade would miss kernels that use the Connect one.
"""

import os
import sys


def _load_sparkscreen():
    """Put sparkscreen on sys.path from a wheel directory, or import it directly."""
    lib = os.environ.get("SPARKSCREEN_LIB")
    if lib and os.path.isdir(lib) and lib not in sys.path:
        sys.path.insert(0, lib)
    try:
        import sparkscreen  # noqa: F401
    except ImportError:
        return None
    return sparkscreen


def _install_plan_gate(session, policy=None, on_report=None):
    """Wrap one live Connect session's client with the screening gate."""
    from sparkscreen.connect import ScreeningStub, install_gate

    client = getattr(session, "client", None)
    if client is None or not hasattr(client, "_stub"):
        return False  # not a Connect session, or an unexpected client shape
    if isinstance(getattr(client, "_stub", None), ScreeningStub):
        return True  # already gated
    install_gate(client, policy=policy, on_report=on_report)
    return True


def _make_gated_getOrCreate(original):
    """Build a getOrCreate wrapper around one concrete `original`.

    A factory is used so the loop in `_patch_builders` cannot close over the wrong
    `original` -- the classic late-binding trap when patching several classes in
    one loop.
    """

    def gated_getOrCreate(self, *args, **kwargs):
        session = original(self, *args, **kwargs)
        try:
            _install_plan_gate(session, policy=_policy)
        except Exception as exc:  # noqa: BLE001
            # A broken gate must not silently disable screening, and must not be
            # invisible: print, so it lands in the cell output the user sees.
            print(f"[sparkscreen] gate installation failed: {exc!r}", file=sys.stderr)
        return session

    gated_getOrCreate._sparkscreen_gated = True
    return gated_getOrCreate


def _patch_builders():
    """Wrap getOrCreate on every SparkSession builder class this pyspark has."""
    builders = []
    try:
        from pyspark.sql import SparkSession
        builders.append(SparkSession.Builder)
    except Exception:  # noqa: BLE001
        pass
    try:
        from pyspark.sql.connect.session import SparkSession as ConnectSession
        connect_builder = ConnectSession.Builder
        if all(connect_builder is not b for b in builders):
            builders.append(connect_builder)
    except Exception:  # noqa: BLE001
        pass

    patched = 0
    for cls in builders:
        if getattr(cls.getOrCreate, "_sparkscreen_gated", False):
            continue  # hook loaded twice (e.g. %run): do not double-wrap
        cls.getOrCreate = _make_gated_getOrCreate(cls.getOrCreate)
        patched += 1
    return patched


_policy = None
_patched = 0
_sparkscreen = _load_sparkscreen()
if _sparkscreen is not None:
    try:
        _policy = _sparkscreen.default_policy()
    except Exception:  # noqa: BLE001
        _policy = None
    _patched = _patch_builders()
    print(
        f"[sparkscreen] Spark session gate active ({_patched} builder(s) patched; "
        f"policy: {_policy.name if _policy else 'unconfigured'})"
    )
else:
    print(
        "[sparkscreen] not importable; set SPARKSCREEN_LIB to an unpacked "
        "sparkscreen wheel -- sessions will NOT be screened",
        file=sys.stderr,
    )
