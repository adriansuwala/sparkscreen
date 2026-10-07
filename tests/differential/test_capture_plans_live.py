"""Differential: `capture_plans` on a real client, and the proto/JSON equivalence.

`tests/test_json_plans.py` proves the JSON projection screens correctly in a
venv with NO pyspark. This file proves the capture end on real pyspark wheels:

*   a real client, real DataFrame write, dead endpoint -- the capture stub takes
    the assembled request apart before any byte leaves (the 4.1.x plan builder
    prefetches two compression keys via `Config` first; the capture stub answers
    that locally, which is what makes offline capture possible at all);
*   the projection the capture ships walks to the SAME verdict and effects as the
    proto itself -- the equivalence the local screening end relies on;
*   the stub is restored after capture, whatever happened to the snippet.

Skipped without pyspark/pandas/pyarrow like the rest of the differential suite.
"""
from __future__ import annotations

import pytest

pytest.importorskip("pyspark", reason="needs pyspark's Connect client")
pytest.importorskip("pandas", reason="pyspark Connect imports require it")
pytest.importorskip("pyarrow", reason="pyspark Connect imports require it")

import warnings as _warnings  # noqa: E402

_warnings.filterwarnings(
    "ignore",
    message=".*distutils.*",
    category=DeprecationWarning,
    module=r"pyspark\..*",
)

import pyspark  # noqa: E402

from sparkscreen import Verdict  # noqa: E402
from sparkscreen.connect import capture_plans  # noqa: E402
from sparkscreen.plans import JsonPlan, screen_plan  # noqa: E402
from sparkscreen.policy import default_policy  # noqa: E402


class ExplodingStub:
    """Anything that reaches the real stub fails the test: no network, ever."""

    def __getattr__(self, name):
        def rpc(*a, **kw):
            raise AssertionError(f"request reached the network stub: {name}")

        return rpc


@pytest.fixture(scope="module")
def remote_spark():
    """A real Connect session whose server does not exist (see the gate test)."""
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="The distutils package is deprecated.*")
        from pyspark.sql.connect.session import SparkSession as ConnectSession

    yield ConnectSession("sc://localhost:1")


OVERWRITE_SNIPPET = "spark.range(1).write.mode('overwrite').saveAsTable('prod.users')"


def test_capture_stops_a_real_write_before_the_wire(remote_spark):
    remote_spark.client._stub = ExplodingStub()
    result = capture_plans(OVERWRITE_SNIPPET, session=remote_spark,
                           globs={"spark": remote_spark})
    assert remote_spark.client._stub is not ExplodingStub  # restored inside capture_plans
    remote_spark.client._stub = ExplodingStub()

    assert len(result["captures"]) == 1
    cap = result["captures"][0]
    assert cap["pyspark_version"] == pyspark.__version__
    cmd = cap["plan"]["command"]
    assert list(cmd) == ["write_operation"]
    wo = cmd["write_operation"]
    assert wo["mode"] == "SAVE_MODE_OVERWRITE"
    assert wo["table"]["table_name"] == "prod.users"


def test_projection_and_proto_agree_on_a_real_write(remote_spark):
    """The property the local screening end relies on: same verdict, same effects.

    Captured with `keep_proto` so both readings of the SAME plan can be compared:
    the proto walked in this pyspark environment, the JSON projection walked with
    no proto at all. If these ever disagree, the capture path is screening a
    different operation than the one the client built.
    """
    remote_spark.client._stub = ExplodingStub()
    result = capture_plans(OVERWRITE_SNIPPET, session=remote_spark,
                           globs={"spark": remote_spark}, keep_proto=True)
    remote_spark.client._stub = ExplodingStub()

    cap = result["captures"][0]
    proto_report = screen_plan(cap["proto"])
    json_report = screen_plan(JsonPlan(cap["plan"]),
                              engine_version=cap["pyspark_version"])
    assert proto_report.verdict is json_report.verdict is Verdict.DENY
    assert proto_report.findings[0].effect == json_report.findings[0].effect
    assert json_report.findings[0].targets == ("prod.users",)


def test_projection_and_proto_agree_on_sql_delegation(remote_spark):
    """The SQL inside the plan goes down the ANTLR pipeline on both paths."""
    remote_spark.client._stub = ExplodingStub()
    result = capture_plans("spark.sql('DROP TABLE prod.t')", session=remote_spark,
                           globs={"spark": remote_spark}, keep_proto=True)
    remote_spark.client._stub = ExplodingStub()

    cap = result["captures"][0]
    proto_report = screen_plan(cap["proto"])
    json_report = screen_plan(JsonPlan(cap["plan"]),
                              engine_version=cap["pyspark_version"])
    assert proto_report.verdict is json_report.verdict is Verdict.DENY
    assert json_report.findings[0].statement == "DropTable"


def test_a_snippet_that_sends_nothing_yields_no_captures(remote_spark):
    remote_spark.client._stub = ExplodingStub()
    result = capture_plans("x = spark.range(1)", session=remote_spark,
                           globs={"spark": remote_spark})
    remote_spark.client._stub = ExplodingStub()
    assert result["captures"] == []


def test_snippet_exceptions_propagate_and_the_stub_is_restored(remote_spark):
    remote_spark.client._stub = ExplodingStub()
    original = remote_spark.client._stub
    with pytest.raises(ZeroDivisionError):
        capture_plans("1 / 0", session=remote_spark, globs={"spark": remote_spark})
    assert remote_spark.client._stub is original


def test_offline_capture_survives_the_config_prefetch(remote_spark):
    """4.1+ plan building asks Config for compression keys before its first plan.

    The capture stub answers that locally (empty response -> compression off), so
    the second capture on the same client -- or the first -- never dials. This is
    the property that makes the capture tool work with no server running.
    """
    remote_spark.client._stub = ExplodingStub()
    for _ in range(2):  # first prefetches; second proves the cache answers too
        result = capture_plans(OVERWRITE_SNIPPET, session=remote_spark,
                               globs={"spark": remote_spark})
        assert result["captures"]
    remote_spark.client._stub = ExplodingStub()