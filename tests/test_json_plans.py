"""The JSON projection path: screening a captured plan with no pyspark at all.

This module runs in the fast venv, which deliberately has no pyspark -- so the
import itself proves `sparkscreen.plans` no longer requires the wheel, and every
test proves the walker reaches the same verdicts through `JsonPlan` that the
differential suite reaches through real protos on the engine wheels.

The dicts here are exactly what `connect.capture_plans` ships: `json_format`
projections with `preserving_proto_field_name` -- snake_case field names, enum
*names*, oneof present as its single set field.
"""

import pytest

from sparkscreen.model import Effect, Reason, Verdict
from sparkscreen.plans import JsonPlan, screen_plan
from sparkscreen.policy import default_policy


def write_op(*, table=None, path=None, mode=None):
    d = {}
    if table is not None:
        d["table"] = {"table_name": table}
    if path is not None:
        d["path"] = path
    if mode is not None:
        d["mode"] = mode
    return {"command": {"write_operation": d}}


def v2_write(table, mode):
    return {"command": {"write_operation_v2": {"table_name": table, "mode": mode}}}


def sql_command(sql):
    return {"command": {"sql_command": {"sql": sql}}}


def test_the_module_imports_without_pyspark():
    import sys

    assert "pyspark" not in sys.modules
    from sparkscreen import plans

    assert plans._HAVE_PROTO is False


def test_descriptor_checks_refuse_legibly_without_pyspark():
    from sparkscreen import plans

    with pytest.raises(ImportError, match="needs pyspark"):
        plans.command_kinds()
    with pytest.raises(ImportError, match="needs pyspark"):
        plans.kind_drift()


def test_overwrite_save_as_table_is_deny():
    report = screen_plan(JsonPlan(write_op(table="prod.users", mode="SAVE_MODE_OVERWRITE")))
    assert report.verdict is Verdict.DENY
    f = report.findings[0]
    assert f.reason is Reason.DESTRUCTIVE_STATEMENT
    assert f.statement == "connect.write_operation"
    assert f.targets == ("prod.users",)
    assert Effect.DESTROY_DATA in f.effect and Effect.WRITE_DATA in f.effect


def test_append_save_as_table_writes_without_destroying():
    report = screen_plan(JsonPlan(write_op(table="prod.users", mode="SAVE_MODE_APPEND")))
    assert report.verdict is Verdict.REVIEW
    f = report.findings[0]
    assert f.reason is Reason.OUTSIDE_ALLOWLIST  # default policy has no writable list
    assert Effect.DESTROY_DATA not in f.effect


def test_unspecified_mode_omitted_from_json_is_the_known_default():
    # json_format omits enum defaults: mode UNSPECIFIED is absent, and absence
    # must read as the known error-if-exists default, not as an unreadable mode.
    report = screen_plan(JsonPlan(write_op(table="prod.users")))
    assert report.verdict is Verdict.REVIEW
    assert Effect.DESTROY_DATA not in report.findings[0].effect


def test_overwrite_path_write_reaches_external_and_destroys():
    report = screen_plan(JsonPlan(write_op(path="/tmp/warehouse/out",
                                           mode="SAVE_MODE_OVERWRITE")))
    assert report.verdict is Verdict.DENY
    assert Effect.REACHES_EXTERNAL in report.findings[0].effect


def test_v2_replace_is_deny_and_v2_create_is_not():
    replace = screen_plan(JsonPlan(v2_write("prod.t", "MODE_REPLACE")))
    assert replace.verdict is Verdict.DENY
    create = screen_plan(JsonPlan(v2_write("prod.t", "MODE_CREATE")))
    assert create.verdict is Verdict.REVIEW
    assert Effect.DESTROY_DATA not in create.findings[0].effect


def test_sql_command_delegates_to_the_antlr_pipeline_with_no_pyspark():
    report = screen_plan(JsonPlan(sql_command("DROP TABLE prod.t")),
                         engine_version="4.1.3")
    assert report.verdict is Verdict.DENY
    assert report.findings[0].statement == "DropTable"
    assert report.findings[0].targets == ("prod.t",)


def test_sql_command_without_engine_version_and_without_pyspark_is_unknown():
    report = screen_plan(JsonPlan(sql_command("DROP TABLE prod.t")))
    assert report.verdict is Verdict.UNKNOWN
    assert report.findings[0].reason is Reason.UNSUPPORTED_SPARK_VERSION


def test_sql_root_relation_is_screened_too():
    plan = JsonPlan({"root": {"sql": {"query": "DROP TABLE prod.t"}}})
    report = screen_plan(plan, engine_version="4.1.3")
    assert report.verdict is Verdict.DENY
    assert report.findings[0].statement == "DropTable"


def test_empty_command_fails_closed():
    report = screen_plan(JsonPlan({"command": {}}))
    assert report.verdict is Verdict.UNKNOWN
    assert report.findings[0].reason is Reason.UNSUPPORTED_SPARK_VERSION


def test_unmapped_kind_fails_closed():
    report = screen_plan(JsonPlan({"command": {"brand_new_future_kind": {}}}))
    assert report.verdict is Verdict.UNKNOWN
    assert report.findings[0].reason is Reason.UNSUPPORTED_SPARK_VERSION


def test_read_only_plan_is_allowed():
    plan = JsonPlan({"root": {"project": {"input": {"filter": {"input": {"range": {
        "start": 0, "end": 1, "step": 1,
    }}}}}}})
    report = screen_plan(plan)
    assert report.verdict is Verdict.ALLOW
    assert report.findings == []


def test_root_sql_and_command_both_screened_in_one_report():
    plan = JsonPlan({
        "root": {"sql": {"query": "select 1"}},
        "command": {"write_operation": {"table": {"table_name": "prod.users"},
                                        "mode": "SAVE_MODE_OVERWRITE"}},
    })
    report = screen_plan(plan)
    assert report.verdict is Verdict.DENY
    assert len(report.findings) == 2


def test_temp_view_command_screens_like_its_static_analogue():
    report = screen_plan(JsonPlan({"command": {"create_dataframe_view": {"name": "v"}}}))
    assert report.verdict is Verdict.REVIEW
    assert report.findings[0].statement == "connect.create_dataframe_view"