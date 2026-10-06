"""Screen Spark Connect plans: the typed grammar for query plans.

T2/T8 (docs/threads.md): a Spark Connect client never sends SQL for DataFrame calls --
it sends a typed plan. This module screens that plan. The vocabulary it maps from is
the Connect protobuf shipped in every pyspark wheel (`pyspark/sql/connect/proto/`),
read through descriptors so nothing but the wheel itself is imported.

The design mirrors `analysis/calls.py` in reverse. There, the static side is
conservative about the *target* and confident about the *effect*. Here the plan is
already resolved -- the client built it from a real DataFrame -- so both are facts:

*   The **kind universe is derived, not enumerated by hand**: `Relation` and `Command`
    carry closed oneofs, and the wheel's actual kind set is read through
    `DESCRIPTOR.oneofs` (see `kind_drift()`). A wheel that gains a kind is reported,
    and an *unset* oneof reports None, never a guess.
*   **Write modes are typed enums**, not folded strings: `SAVE_MODE_OVERWRITE` is an
    enum value, so the overwrite question -- the one the static side approximates with
    `mode_known` -- is structurally settled. The UNSPECIFIED value is visible and is
    treated the way the static side treats a writer it cannot read: fail closed.
*   **`sql_command` delegates.** The SQL text inside a plan is screened by the same
    ANTLR pipeline and `LABEL_EFFECTS` table the static side uses, so `LABEL_EFFECTS`
    stays the single source of truth and this module adds only the operations plans
    carry natively.

Import contract: importing this module requires pyspark's proto modules but NOT a
JVM, a session, or grpc (the transport; only the messages are touched). Everything
that touches a *live* client lives in `connect.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .analysis.effects import denies_regardless_of_namespace
from .model import Effect, Finding, Reason, Report, Severity, Verdict
from .policy import Policy, default_policy

# The proto import is deliberately at module level with a plain ImportError so the
# failure mode is legible: this backend needs a pyspark wheel, not a JVM.
try:
    from pyspark.sql.connect.proto import Command, Plan, Relation  # noqa: F401
    from pyspark.sql.connect.proto import commands_pb2
except ImportError as _e:  # pragma: no cover - depends on environment
    raise ImportError(
        "sparkscreen.plans needs pyspark's Connect proto modules "
        "(pyspark.sql.connect.proto); install pyspark in this environment "
        f"(underlying error: {_e})"
    ) from None


# ---------------------------------------------------------------------------
# the derived kind universe
# ---------------------------------------------------------------------------


def relation_kinds() -> tuple[str, ...]:
    """Every `Relation` kind this wheel can express, derived from the oneof."""
    return tuple(f.name for f in Relation.DESCRIPTOR.oneofs[0].fields)


def command_kinds() -> tuple[str, ...]:
    """Every `Command` kind this wheel can express, derived from the oneof."""
    return tuple(f.name for f in Command.DESCRIPTOR.oneofs[0].fields)


def kind_drift() -> dict[str, list[str]]:
    """Kinds this wheel can express that the effect table has no entry for.

    The plan-level twin of `effect_label_drift` -- but with a different failure
    story. The label universe is closed by the pinned grammars, so a miss there is
    always a bug. Here the universe is whatever the installed wheel ships, so a miss
    is a *pin mismatch* (a newer wheel than the table) and it fails closed in
    `screen_plan` (UNKNOWN), never silently. Drift is still worth reporting so an
    operator can close it by adding entries rather than living in UNKNOWN.
    """
    known = set(_COMMAND_EFFECTS) | set(_FIELD_REFINED_KINDS) | {"sql_command"}
    return {
        "command_unmapped": [k for k in command_kinds() if k not in known],
        "table_unreachable": [k for k in _COMMAND_EFFECTS if k not in command_kinds()],
    }


#: The union of command kinds across the three pinned wheels (3.5.1, 4.1.3, 4.2.0),
#: derived empirically from `command_kinds()` on each (T8 probes). One table covers
#: the union, so a table entry can legitimately name a kind an older wheel cannot
#: express -- `kind_drift()["table_unreachable"]` is expected to be non-empty on
#: 3.5.1 and must be a subset of this union. New kinds from future wheels are caught
#: by the *other* drift direction, which stays strict per wheel.
KNOWN_KINDS_UNION: frozenset[str] = frozenset({
    "register_function", "write_operation", "create_dataframe_view",
    "write_operation_v2", "sql_command", "write_stream_operation_start",
    "streaming_query_command", "get_resources_command",
    "streaming_query_manager_command", "register_table_function", "extension",
    # 4.1+ additions:
    "streaming_query_listener_bus_command", "register_data_source",
    "create_resource_profile_command", "checkpoint_command",
    "remove_cached_remote_relation_command", "merge_into_table_command",
    "ml_command", "execute_external_command", "pipeline_command",
})

# The effect table. Keyed on command kind; kinds whose fields refine the effect are
# handled by functions below, not here. This is the T4-shaped contract: a table of
# (operation, effects), with `LABEL_EFFECTS` still owning SQL.
#
# Semantics are copied from the closest SQL/DataFrame analogue, and the comment on
# each entry says which one, because agreement between the backends is a testable
# property, not a vibe.

_COMMAND_EFFECTS: dict[str, frozenset[Effect]] = {
    # df.writeTo(table) -- the V2 writer. WRITE_DATA base; the typed `Mode` field
    # refines DESTROY_DATA in `_write_operation_v2_effects` below.
    "write_operation_v2": frozenset({Effect.WRITE_DATA}),
    # MERGE can insert, update and delete in one statement; its blast radius is not
    # visible from the kind. Same reasoning as `MergeIntoTable` in LABEL_EFFECTS
    # (WRITE_DATA + DESTROY_DATA unconditionally -- decision recorded in the ledger).
    "merge_into_table_command": frozenset({Effect.WRITE_DATA, Effect.DESTROY_DATA}),
    # register_function / register_table_function install code the driver will run --
    # the plan-level analogue of `CreateFunction` (WRITE_SCHEMA + LOAD_CODE).
    "register_function": frozenset({Effect.WRITE_SCHEMA, Effect.LOAD_CODE}),
    "register_table_function": frozenset({Effect.WRITE_SCHEMA, Effect.LOAD_CODE}),
    # register_data_source (4.1+): installs a Python DataSource the driver will run
    # on every read/write through it -- LOAD_CODE with a wider reach than a UDF,
    # because the code sits in the read/write path of whatever names it.
    "register_data_source": frozenset({Effect.WRITE_SCHEMA, Effect.LOAD_CODE}),
    # CREATE TEMP VIEW from a DataFrame -- `CreateTempViewUsing` in LABEL_EFFECTS.
    "create_dataframe_view": frozenset({Effect.WRITE_SCHEMA}),
    # Streaming start is a write-shaped commitment to a location the agent names;
    # checkpoint management likewise. Conservative by construction.
    "write_stream_operation_start": frozenset({Effect.WRITE_DATA, Effect.REACHES_EXTERNAL}),
    "checkpoint_command": frozenset({Effect.WRITE_DATA}),
    # Session-state changes: the analogue of `CacheTable` / `UncacheTable` /
    # `ClearCache` (CHANGE_CONFIG) in LABEL_EFFECTS.
    "streaming_query_command": frozenset({Effect.CHANGE_CONFIG}),
    "streaming_query_manager_command": frozenset({Effect.CHANGE_CONFIG}),
    "streaming_query_listener_bus_command": frozenset({Effect.CHANGE_CONFIG}),
    "remove_cached_remote_relation_command": frozenset({Effect.CHANGE_CONFIG}),
    "create_resource_profile_command": frozenset({Effect.CHANGE_CONFIG}),
    # Reaches outside the cluster without writing the warehouse.
    "get_resources_command": frozenset({Effect.REACHES_EXTERNAL}),
    # Executes arbitrary external code/binary. The strongest LOAD_CODE in the set:
    # the driver runs something the plan names, outside any namespace allowlist.
    "execute_external_command": frozenset({Effect.LOAD_CODE, Effect.REACHES_EXTERNAL}),
    # ML / pipeline commands run supplied code and may write. Coarse until a real
    # workload says otherwise; the flags are the honest over-approximation.
    "ml_command": frozenset({Effect.WRITE_DATA, Effect.LOAD_CODE}),
    "pipeline_command": frozenset({Effect.WRITE_DATA, Effect.LOAD_CODE}),
    # Server-side extension points: opaque by definition. NOT empty-because-harmless;
    # empty here means "no effect we can name", and the kind still screens as REVIEW
    # (no policy knows it), which is the fail-closed reading.
    "extension": frozenset(),
    # write_operation is refined by its fields -- see `_write_operation_effects`.
    # sql_command delegates -- see `_eval_plan_sql`; no entry, on purpose.
}


#: Kinds whose effects come from their *fields* rather than from the table. Both
#: dispatch (screen_plan) and drift checking must agree on this list, which is why
#: it is one constant and not two opinions.
_FIELD_REFINED_KINDS: tuple[str, ...] = ("write_operation", "write_operation_v2")


def _write_operation_effects(cmd) -> frozenset[Effect]:
    """Effects for one `write_operation`, refined by its typed fields.

    Mirrors `_WriteFinder._effects` from the static side: WRITE_DATA always;
    DESTROY_DATA when the typed mode is OVERWRITE; REACHES_EXTERNAL when the write
    targets a path/source rather than the warehouse. The `save_method` field
    distinguishes saveAsTable from insertInto; both are WRITE_DATA (the static side
    agrees -- INSERT INTO appends, and `InsertIntoTable` in LABEL_EFFECTS carries no
    DESTROY_DATA).
    """
    wo = cmd.write_operation
    effects: set[Effect] = {Effect.WRITE_DATA}
    if not wo.HasField("table"):
        # A path/source write reaches outside the warehouse: the same reasoning that
        # gives static `save`/`jdbc` their REACHES_EXTERNAL.
        effects.add(Effect.REACHES_EXTERNAL)
    if wo.mode == commands_pb2.WriteOperation.SaveMode.SAVE_MODE_OVERWRITE:
        effects.add(Effect.DESTROY_DATA)
    return frozenset(effects)


def _write_operation_v2_effects(cmd) -> frozenset[Effect]:
    """Effects for one `write_operation_v2`, refined by its typed `Mode`."""
    wo = cmd.write_operation_v2
    effects: set[Effect] = {Effect.WRITE_DATA}
    if wo.mode in (
        commands_pb2.WriteOperationV2.MODE_OVERWRITE,
        commands_pb2.WriteOperationV2.MODE_REPLACE,
        commands_pb2.WriteOperationV2.MODE_CREATE_OR_REPLACE,
    ):
        # The modes that can discard existing data. MODE_CREATE cannot -- the SQL
        # analogue is CreateTable, which is WRITE_SCHEMA-only -- and neither does
        # MODE_OVERWRITE_PARTITIONS beyond the named partitions.
        effects.add(Effect.DESTROY_DATA)
    return frozenset(effects)


class UnmappedKindError(KeyError):
    """A plan command kind with no entry in `_COMMAND_EFFECTS`.

    The mirror of `analysis.effects.UnmappedLabelError`: totality is a safety
    property, and a kind that screens to nothing is a fail-open hole. Unlike the
    label table, the universe here is closed *per wheel* -- a newer wheel legitimately
    has kinds this table has never seen, which is a pin mismatch rather than a bug in
    the table. `screen_plan` reports it as UNKNOWN either way.
    """

    def __init__(self, kind: str | None) -> None:
        super().__init__(
            f"no effect classification for plan command kind {kind!r}; "
            "_COMMAND_EFFECTS in sparkscreen/plans.py is incomplete for this wheel -- "
            "add an entry rather than letting this command report no effects"
        )
        self.kind = kind


def command_effects(cmd) -> frozenset[Effect]:
    """The `Effect` set for one `Command` proto. Raises on an unmapped kind.

    Like `effects_for_label`, this never invents an empty set on a miss: a kind we
    cannot classify is not a kind with no effects.
    """
    kind = cmd.WhichOneof("command_type")
    if kind is None:
        raise UnmappedKindError(None)
    if kind == "write_operation":
        return _write_operation_effects(cmd)
    if kind == "write_operation_v2":
        return _write_operation_v2_effects(cmd)
    if kind == "sql_command":
        # No effect stated here: the SQL path states it. Callers either delegate
        # (extract_commands -> screen_plan) or see UnmappedKindError otherwise.
        raise UnmappedKindError("sql_command (delegate: screen the SQL text)")
    if kind not in _COMMAND_EFFECTS:
        raise UnmappedKindError(kind)
    return _COMMAND_EFFECTS[kind]


# ---------------------------------------------------------------------------
# reading the plan's facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanCommand:
    """One command extracted from a `Plan`, ready for policy evaluation.

    `kind` is the oneof name. `target` is the principal destination when the kind
    carries one (`table_name` / `path` / `name`); `target_known` is False when that
    field is absent or empty -- the plan shape decides, so unlike the static side
    there is no folding to be wrong about. `sql` is set only for `sql_command`s.
    """

    kind: str
    effects: frozenset[Effect]
    target: str | None
    target_known: bool
    sql: str | None = None


def extract_commands(plan: Plan) -> list[PlanCommand]:
    """Walk one `Plan` and return its commands.

    `Plan.command` is the eager half (writes, SQL, registration); `Plan.root` is a
    `Relation` tree for the lazy half. Most relations are reads, but SQL text also
    hides in a root `sql` relation (a `spark.sql()` inside a lazy chain), so the
    root is checked rather than assumed away.
    """
    out: list[PlanCommand] = []
    if plan.HasField("command"):
        cmd = plan.command
        kind = cmd.WhichOneof("command_type")
        if kind == "sql_command":
            sc = cmd.sql_command
            sql = sc.sql or None
            if not sql and "input" in sc.DESCRIPTOR.fields_by_name and sc.HasField("input"):
                # 4.1.x embeds the SQL as a nested relation; 3.5.x has no such field.
                sql = _sql_from_embedded(sc.input)
            out.append(PlanCommand(
                kind="sql_command",
                effects=frozenset(),
                target=None,
                target_known=False,
                sql=sql,
            ))
        else:
            out.append(PlanCommand(
                kind=kind,  # type: ignore[arg-type]
                effects=command_effects(cmd),
                target=_target_of(cmd, kind),          # type: ignore[arg-type]
                target_known=_target_known(cmd, kind),  # type: ignore[arg-type]
            ))
    root_kind = plan.root.WhichOneof("rel_type")
    if root_kind == "sql" and plan.root.sql.query:
        out.append(PlanCommand(
            kind="sql_command(root)",
            effects=frozenset(),
            target=None,
            target_known=False,
            sql=plan.root.sql.query,
        ))
    return out


def _sql_from_embedded(input_relation) -> str | None:
    """The SQL text from a `SqlCommand.input` Relation, per wheel shape.

    Wheels differ: 4.1.x embeds the SQL as a nested `sql` relation inside
    `SqlCommand.input`; 3.5.x keeps it in `sql_command.sql` and has no `input`
    field at all (the caller checks the descriptor before calling). Reading the
    wheel's actual shape is the pin, not a guess.
    """
    if input_relation is None:
        return None
    kind = input_relation.WhichOneof("rel_type")
    if kind == "sql":
        return input_relation.sql.query or None
    return None


def _target_of(cmd, kind: str) -> str | None:
    if kind == "write_operation":
        wo = cmd.write_operation
        if wo.HasField("table"):
            return wo.table.table_name or None
        return wo.path or None
    if kind == "write_operation_v2":
        return cmd.write_operation_v2.table_name or None
    if kind == "merge_into_table_command":
        return cmd.merge_into_table_command.target_table_name or None
    if kind == "create_dataframe_view":
        return cmd.create_dataframe_view.name or None
    return None


def _target_known(cmd, kind: str) -> bool:
    if kind == "write_operation":
        wo = cmd.write_operation
        return bool(wo.HasField("table") and wo.table.table_name) or bool(wo.path)
    if kind == "write_operation_v2":
        return bool(cmd.write_operation_v2.table_name)
    if kind == "merge_into_table_command":
        return bool(cmd.merge_into_table_command.target_table_name)
    if kind == "create_dataframe_view":
        return bool(cmd.create_dataframe_view.name)
    return False


# ---------------------------------------------------------------------------
# screening
# ---------------------------------------------------------------------------


def screen_plan(plan: Plan, policy: Policy | None = None) -> Report:
    """Screen one Spark Connect `Plan` proto against `policy`.

    The plan-level counterpart of `screen()`: one report per plan, one finding per
    command. Verdict semantics match the static path exactly:

    *   `DENY` when the command's effects meet `DENY_REGARDLESS_OF_NAMESPACE`
        (DESTROY_DATA / LOAD_CODE) -- the same predicate the SQL and DataFrame paths
        apply, so "overwrite is not waivable by namespace" is one property, not three.
    *   otherwise `REVIEW`: either an allowlist objection (with `OUTSIDE_ALLOWLIST`,
        including the no-writable-list note that keeps the F8-adjacent bug dead here
        too) or, with no objection, `UNSUPPORTED_STATEMENT` -- the same default
        `Policy.evaluate_statement` applies to a parsed statement the policy was
        not taught.
    *   an unmapped kind is `UNKNOWN` with `UNSUPPORTED_SPARK_VERSION` (analysis
        failure): the wheel gained a kind the table has not caught up with, and a
        screener that cannot classify what it holds must not report a result.
    *   a `sql_command` whose SQL text could not be read on this wheel is `UNKNOWN`
        with `UNRESOLVED_DYNAMIC_SQL` -- the plan-level twin of an unfolded f-string.
    """
    policy = policy or default_policy()
    report = Report(policy=policy.name, grammar="connect-proto")

    commands = extract_commands(plan)
    for ordinal, pc in enumerate(commands):
        if pc.kind == "sql_command" or pc.kind == "sql_command(root)":
            _eval_plan_sql(report, policy, pc, ordinal)
        elif pc.kind not in _COMMAND_EFFECTS and pc.kind not in _FIELD_REFINED_KINDS:
            report.add(Finding(
                verdict=Verdict.UNKNOWN,
                reason=Reason.UNSUPPORTED_SPARK_VERSION,
                message=f"plan command kind {pc.kind!r} has no effect classification "
                        "for this wheel; needs human review",
                severity=Severity.HIGH,
                statement=f"connect.{pc.kind}",
                targets=(pc.target,) if pc.target_known and pc.target else (),
            ))
        else:
            _eval_plan_command(report, policy, pc)
    return report


def _eval_plan_command(report: Report, policy: Policy, pc: PlanCommand) -> None:
    effects = pc.effects
    where = f"connect.{pc.kind}"
    targets = (pc.target,) if pc.target_known and pc.target else ()

    if denies_regardless_of_namespace(effects):
        report.add(Finding(
            verdict=Verdict.DENY,
            reason=Reason.DESTRUCTIVE_STATEMENT,
            message=f"{where} replaces existing data" if Effect.DESTROY_DATA in effects
                    else f"{where} loads code into the session",
            severity=Severity.CRITICAL,
            statement=where,
            targets=targets,
            effect=effects,
        ))
        return

    notes: list[str] = []
    refs = [_nsref(t) for t in targets]
    if not policy.writable_namespaces:
        notes.append(
            "the policy sets no writable_namespaces, so the destination could "
            "not be confirmed as in bounds"
        )
    else:
        for r in refs:
            if not any(r.matches(pat) for pat in policy.writable_namespaces):
                notes.append(
                    f"{r.name} is outside the writable namespaces "
                    f"{list(policy.writable_namespaces)}"
                )
    if policy.readable_namespaces:
        for r in refs:
            if not any(r.matches(pat) for pat in policy.readable_namespaces):
                notes.append(
                    f"{r.name} is outside the readable namespaces "
                    f"{list(policy.readable_namespaces)}"
                )
    if notes:
        report.add(Finding(
            verdict=Verdict.REVIEW,
            reason=Reason.OUTSIDE_ALLOWLIST,
            message=f"{where} writes data outside the permitted namespaces: "
                    + "; ".join(notes),
            severity=Severity.MEDIUM,
            statement=where,
            targets=targets,
            effect=effects,
        ))
    else:
        report.add(Finding(
            verdict=Verdict.REVIEW,
            reason=Reason.UNSUPPORTED_STATEMENT,
            message=f"{where} has no policy rule; needs review",
            severity=Severity.MEDIUM,
            statement=where,
            targets=targets,
            effect=effects,
        ))


def _eval_plan_sql(report: Report, policy: Policy, pc: PlanCommand, ordinal: int) -> None:
    """Delegate a plan's `sql_command` to the ANTLR pipeline.

    One SQL path, deliberately shared with the static side -- but the entry point is
    the parser, not `screen()`: a plan carries raw SQL text, and `screen()` parses
    *Python source* to find it. The grammar is chosen from the running kernel's own
    pyspark version (the pin, exactly as the differential suite pins engines), so a
    kernel on Spark 3.5.1 screens with the 3.5.1 grammar. Findings are re-anchored
    to the command ordinal because plans have no source lines.
    """
    import pyspark

    from .grammar.parser import SqlParser, SqlSyntaxError, get_parser
    from .grammar.spec import spec_for_spark_version

    if not pc.sql:
        report.add(Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNRESOLVED_DYNAMIC_SQL,
            message="plan carries a sql_command whose SQL text could not be read "
                    "on this wheel; needs human review",
            severity=Severity.HIGH,
            statement="connect.sql_command",
            line=ordinal,
        ))
        return

    try:
        spec = spec_for_spark_version(pyspark.__version__)
    except KeyError:
        report.add(Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNSUPPORTED_SPARK_VERSION,
            message=f"kernel runs pyspark {pyspark.__version__}, which has no pinned "
                    "grammar; the SQL inside this plan was not analyzed",
            severity=Severity.HIGH,
            statement="connect.sql_command",
            line=ordinal,
        ))
        return

    parser = get_parser(spec)
    try:
        parsed = parser.parse(pc.sql)
    except SqlSyntaxError as e:
        report.add(Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.UNPARSEABLE_SQL,
            message=f"SQL did not parse, so it was not analyzed: {e}",
            severity=Severity.HIGH,
            line=ordinal,
            sql=pc.sql,
            sql_line=e.line,
            sql_column=e.column,
        ))
        return

    from .screen import _check_execute_immediate, _eval_one

    statements = parsed.statements
    if len(statements) > policy.limits.max_statements:
        report.add(Finding(
            verdict=Verdict.UNKNOWN,
            reason=Reason.RESOURCE_LIMIT,
            message=f"plan carries {len(statements)} statements, over the limit of "
                    f"{policy.limits.max_statements}; not analyzed",
            severity=Severity.MEDIUM,
            line=ordinal,
            sql=pc.sql,
        ))
        return

    for stmt in statements:
        finding = _eval_one(policy, stmt.label, stmt.tree, pc.sql, ordinal, spec.key)
        report.add(finding)

    # EXECUTE IMMEDIATE hides real statements from the top level, on this path too.
    _check_execute_immediate(report, policy, parser, parsed.tree, pc.sql, ordinal)


def _nsref(name: str):
    from .analysis.treewalk import NamespaceRef

    return NamespaceRef(parts=tuple(p for p in name.split(".") if p))
