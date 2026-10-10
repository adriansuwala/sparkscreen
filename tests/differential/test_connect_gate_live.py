"""Differential: the gate on a REAL SparkConnectClient, before any network byte.

`tests/test_connect_plans.py` proves the semantics on hand-built protos and a fake
client. This file proves the wiring claim T8 makes: on a real pyspark
`SparkConnectClient` built with `.remote(...)` -- no server running anywhere -- the
gate intercepts the real `ExecutePlan` request a real DataFrame write produces, and
a DENY raises before the stub is reached. This is the in-process interception
property from T8, as an assertion instead of a probe.

Needs pyspark's Connect modules (which need pandas/pyarrow/zstandard on 4.x wheels)
but NOT a JVM and NOT a server: the request never leaves the process. Skipped
without pyspark so the fast suite stays free of all of it.
"""
from __future__ import annotations

import pytest

pytest.importorskip("pyspark", reason="needs pyspark's Connect client")
pytest.importorskip("pandas", reason="pyspark Connect imports require it")
pytest.importorskip("pyarrow", reason="pyspark Connect imports require it")

# 3.5.1's Connect dependency check raises a DeprecationWarning from distutils on
# Python 3.11+; the wheel works, the warning is fatal under pytest's filter.
import warnings as _warnings  # noqa: E402

_warnings.filterwarnings(
    "ignore",
    message=".*distutils.*",
    category=DeprecationWarning,
    module=r"pyspark\..*",
)

from pyspark.sql import SparkSession  # noqa: E402
from pyspark.sql.connect.proto import commands_pb2 as cb  # noqa: E402

from sparkscreen import Verdict  # noqa: E402
from sparkscreen.connect import (  # noqa: E402
    ScreenedCommandError,
    install_gate,
    restore_gate,
)


class ExplodingStub:
    """Anything that reaches the real stub fails the test: no network, ever."""

    def __getattr__(self, name):
        def rpc(*a, **kw):
            raise AssertionError(f"request reached the network stub: {name}")

        return rpc


@pytest.fixture(scope="module")
def remote_spark():
    """A real Connect session whose server does not exist.

    The Connect `SparkSession` is constructed directly rather than through
    `builder.getOrCreate()`: on 3.5.1 that path dials the (nonexistent) server
    during session creation and hangs, which is not a property under test. Direct
    construction builds the client without dialing on every pinned wheel
    (verified 3.5.1 and 4.1.3), so the requests below are assembled in-process and
    never leave -- which is the point.
    """
    import warnings

    with warnings.catch_warnings():
        # 3.5.1's dependency check raises a distutils DeprecationWarning with
        # stacklevel pointing here; suppress exactly that one, nothing else.
        warnings.filterwarnings("ignore", message="The distutils package is deprecated.*")
        from pyspark.sql.connect.session import SparkSession as ConnectSession

    yield ConnectSession("sc://localhost:1")


@pytest.fixture()
def gated(remote_spark):
    """The gate installed on a real client whose "network" is an exploding stub."""
    remote_spark.client._stub = ExplodingStub()
    previous = install_gate(remote_spark.client)
    yield remote_spark
    restore_gate(remote_spark.client, previous)
    remote_spark.client._stub = ExplodingStub()


def test_real_client_builds_and_the_gate_blocks_an_overwrite(gated):
    """A real write through a real client, refused before the network byte."""
    with pytest.raises(ScreenedCommandError) as excinfo:
        gated.range(1).write.mode("overwrite").saveAsTable("prod.users")
    assert excinfo.value.report.verdict is Verdict.DENY


def test_real_client_gate_waves_a_clean_query_through_to_the_stub(gated):
    """A clean query is screened, passes, and reaches the (exploding) stub.

    The AssertionError from ExplodingStub is the success signal here: it proves the
    gate let a screened-clean request through to the transport layer -- the gate is
    a pass-through for allowed traffic, not a black hole.
    """
    with pytest.raises(AssertionError, match="ExecutePlan"):
        gated.sql("select 1").collect()


def test_real_client_request_shape_matches_the_hand_built_ones(remote_spark):
    """Ground truth: the proto the real client builds has the fields we screen.

    This is the bridge between the hand-built plans in test_connect_plans.py and
    reality: saveAsTable(mode=overwrite) really lands as write_operation with
    SAVE_MODE_OVERWRITE and the table in the `table` field, on this wheel.
    """
    captured: list = []

    class CaptureStub:
        def __getattr__(self, name):
            def rpc(req, *a, **kw):
                captured.append((name, req))
                raise RuntimeError("captured")

            return rpc

    remote_spark.client._stub = CaptureStub()
    try:
        remote_spark.range(1).write.mode("overwrite").saveAsTable("prod.users")
    except RuntimeError:
        pass
    remote_spark.client._stub = ExplodingStub()

    execs = [r for n, r in captured if n == "ExecutePlan"]
    assert execs, "the write did not produce an ExecutePlan request"
    cmd = execs[-1].plan.command
    assert cmd.WhichOneof("command_type") == "write_operation"
    wo = cmd.write_operation
    assert wo.HasField("table") and wo.table.table_name == "prod.users"
    assert wo.mode == cb.WriteOperation.SAVE_MODE_OVERWRITE
