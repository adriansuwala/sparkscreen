"""Runtime gate: screen plans where Spark Connect clients build them.

`plans.py` screens a `Plan` you already hold. This module gets you the plan: it
wraps a live `SparkConnectClient` so that every `ExecutePlan` request is screened
in-process, *before the network byte*, and a denied command never leaves the Python
process. Verified shape (T8): with a spy stub in place of the gRPC stub, a real
write built the real request and was caught with no server listening.

Enforcement model -- a gate, not a tap:

*   The wrapper replaces the client's gRPC stub with a screening proxy. Only the
    plan-bearing RPC (`ExecutePlan`) is intercepted; session-level RPCs pass through
    untouched.
*   A DENY or UNKNOWN verdict raises `ScreenedCommandError` before the RPC is sent.
    The caller sees an exception, not a verdict object -- the same shape as any
    other refused operation.
*   This is a guardrail against mistakes, not a boundary against a determined
    adversary: code that constructs its own unwrapped session is outside it. The
    static scan remains the boundary-relevant tier (T8 deployment tiers).

Import contract: importing needs pyspark installed (the proto modules). Screening
a plan needs no JVM. Holding a *session* needs whatever the session needs.
"""

from __future__ import annotations

from .model import Report, Verdict
from .plans import screen_plan
from .policy import Policy

try:
    from pyspark.sql.connect.proto import base_pb2
except ImportError as _e:  # pragma: no cover - depends on environment
    raise ImportError(
        "sparkscreen.connect needs pyspark's Connect proto modules "
        f"(underlying error: {_e})"
    ) from None


class ScreenedCommandError(PermissionError):
    """A screened command was refused before it was sent.

    Carries the full `Report`; `str()` summarises it the way the CLI does.
    """

    def __init__(self, report: Report) -> None:
        lines = [f"{f.statement or 'plan'}: {f.message}" for f in report.findings]
        super().__init__(
            "sparkscreen refused this Spark operation "
            f"(verdict {report.verdict.value}): " + "; ".join(lines)
        )
        self.report = report


class ScreeningStub:
    """A gRPC-stub stand-in that screens every `ExecutePlan` it sees.

    Any attribute other than `ExecutePlan` is forwarded to `inner`. `ExecutePlan`
    calls `screen_plan` on the request's plan and either calls through or raises.
    """

    #: RPCs that carry a `Plan`. Everything else is session bookkeeping.
    PLAN_BEARING = ("ExecutePlan",)

    def __init__(self, inner, policy: Policy | None, on_report=None) -> None:
        self._inner = inner
        self._policy = policy
        self._on_report = on_report
        self.screened = 0
        self.blocked = 0

    def __getattr__(self, name):
        inner_attr = getattr(self._inner, name)
        if name not in self.PLAN_BEARING:
            return inner_attr

        def rpc(request, *args, **kwargs):
            # Accept either an ExecutePlanRequest (the live shape) or a bare Plan
            # (tests, and any caller that already holds the plan). The request
            # always carries .plan; a bare Plan does not.
            plan = request.plan if hasattr(request, "plan") else request
            report = screen_plan(plan, self._policy)
            self.screened += 1
            if self._on_report is not None:
                self._on_report(report)
            if not report.ok:
                self.blocked += 1
                raise ScreenedCommandError(report)
            return inner_attr(request, *args, **kwargs)

        return rpc


def install_gate(client, policy: Policy | None = None, on_report=None) -> ScreeningStub:
    """Screen every plan a Spark Connect client sends, in place.

    `client` is a `pyspark.sql.connect.client.core.SparkConnectClient` (reachable as
    `spark.client` on a Connect session). After this call, every `ExecutePlan` the
    session issues is screened first; `report.ok` is False -> `ScreenedCommandError`
    and nothing is sent. The previous stub is returned so the gate can be removed
    (tests, interactive unwinding); `restore_gate(client, previous)` undoes it.

    The attribute name is resolved per wheel (`_stub` on 3.5.1 and 4.1.x) and
    asserted rather than guessed: if a future wheel renames it, this raises instead
    of silently wrapping nothing.
    """
    for attr in ("_stub", "_internal_stub"):
        if hasattr(client, attr):
            previous = getattr(client, attr)
            setattr(client, attr, ScreeningStub(previous, policy, on_report))
            return previous
    raise RuntimeError(
        "no gRPC stub attribute found on this SparkConnectClient "
        f"(looked for _stub, _internal_stub on {type(client).__name__}); "
        "this wheel is newer than the ones sparkscreen.connect knows"
    )


def restore_gate(client, previous) -> None:
    """Put back the stub `install_gate` returned."""
    for attr in ("_stub", "_internal_stub"):
        if hasattr(client, attr):
            setattr(client, attr, previous)
            return
    raise RuntimeError("no stub attribute to restore")


def gated_session_builder(policy: Policy | None = None, on_report=None):
    """Return a `SparkSession.Builder` whose sessions are born screened.

    The kernelspec/startup-file hook: every session this process creates via the
    returned builder gets `install_gate` applied at creation, so agent code that
    uses the provided builder cannot forget the wrapper. Sessions created some
    other way are NOT wrapped -- the bypass is documented, not hidden.
    """
    from pyspark.sql import SparkSession

    builder = SparkSession.builder

    class GatedBuilder:
        def __getattr__(self, name):
            attr = getattr(builder, name)

            def call(*a, **kw):
                result = attr(*a, **kw)
                return _wrap_if_session(result, policy, on_report)

            return call

        def getOrCreate(self):
            session = builder.getOrCreate()
            _wrap_if_session(session, policy, on_report)
            return session

    def _wrap_if_session(obj, pol, cb):
        client = getattr(obj, "client", None)
        if client is not None and hasattr(client, "_stub"):
            if not isinstance(getattr(client, "_stub", None), ScreeningStub):
                install_gate(client, pol, cb)
        return obj

    return GatedBuilder()
