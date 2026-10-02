"""Statement label -> what the statement does.

This is the *effect* axis, and it is orthogonal to the verdict axis in `policy.py`.
A `DROP TABLE prod.users` is `Effect.DESTROY_DATA` here under every policy ever
configured; whether it comes back DENY, UNKNOWN, or (in some hypothetical policy)
ALLOW is a separate question with a separate answer. Keeping the two apart is the
whole point of this module: the old design folded "what Spark would do" into the
rule's verdict, which made the answer depend on the operator's configuration and
left no way to ask "is this irreversible?" without first asking "is this allowed?".

## Totality is a safety property, not a tidiness property

`LABEL_EFFECTS` must cover **every label the grammar can emit for a statement**. A
missing entry is not a cosmetic gap:

    "we analysed it and it is a DROP but have no effect flags" is a fail-open bug.

If a `DROP TABLE` arrives with an empty effect set, every downstream consumer that
asks "does anything here destroy data?" -- a dashboard, a diff gate, a future
`deny regardless of namespace` rule -- reads it as "no". `effects_for_label` therefore
*raises* on an unknown label instead of returning an empty frozenset, and
`lookup_effects` returns a distinguishable `UNMAPPED` sentinel rather than an empty
set. Returning `frozenset()` for "we have never heard of this statement" and
`frozenset()` for "this statement genuinely does nothing" makes the two
indistinguishable, and the second is a claim while the first is an absence.

Two independent tests hold this in place:

* `tests/test_effects.py::test_every_grammar_label_has_an_effect` walks the *generated
  parser classes* for both pinned grammars and asserts coverage. It does not use a
  SQL corpus, because a corpus only proves the labels you thought to write SQL for.
* `test_corpus_statements_all_have_effects` runs a real corpus through the real
  parsers and asserts every emitted label is mapped, so a grammar that starts emitting
  something new is caught from both directions.
* `effect_label_drift()` reports labels that appear in one vocabulary but not another,
  in the spirit of `policy.policy_label_drift`.

## How the label set was derived

Not by reading the grammar and guessing. `analysis/label_universe.py` holds the
derivation, and it produced **114 labels** reachable as a top-level statement under
`effective_label()` across the two pinned grammars (113 for 4.0, 82 for 3.5.1, unioned
because sparkscreen can be asked to screen with either). Those 114 all have entries
here. Eight more are mapped as well -- `SetTableCollation`, `InsertIntoPartition`,
`ShowCollation`, `ShowDatabases`, `Execute`, `Fetch`, `Open`, `Close` -- which no pinned
grammar can currently emit but which appear in `policy.py`'s label lists. They are
reported by `effect_label_drift()` as `grammar_uncovered` / `policy_only` rather than
dropped, because pruning them is a `policy.py` change and this module must not make
that call. 114 + 8 = **122 entries**.

## Conventions

`Effect.DESTROY_DATA` means *irreversible loss*. That is stricter than "the data
changed", and it is the flag that justifies denying regardless of namespace: nothing
about a namespace allowlist makes an unrecoverable delete safe. `WRITE_SCHEMA` alone
does not qualify -- it is the "fine in staging, review in prod" case.
"""

from __future__ import annotations

from typing import Iterable

from ..model import Effect

#: Returned by `lookup_effects` for a label the table does not cover.
#:
#: A sentinel rather than `None` and rather than `frozenset()`: `None` reads as
#: "no effects", which is the same lie `frozenset()` tells. `UNMAPPED` is an object
#: whose only correct use is to be compared against.
UNMAPPED: frozenset[Effect] = frozenset()


class UnmappedLabelError(KeyError):
    """Raised when a statement label has no effect classification.

    A `KeyError` because that is what it is -- a lookup miss -- while carrying a
    message naming the label, because a bare `KeyError('DropTable')` in a report is
    not a useful bug report.
    """

    def __init__(self, label: str):
        self.label = label
        super().__init__(
            f"no effect classification for statement label {label!r}; "
            f"LABEL_EFFECTS in sparkscreen/analysis/effects.py is incomplete -- add an "
            f"entry rather than letting this statement report no effects"
        )


# ---------------------------------------------------------------------------
# shorthand constructors -- these read better in the table than repeated
# `frozenset({...})` and make a wrong flag obvious at the call site.
# ---------------------------------------------------------------------------

def _e(*effects: Effect) -> frozenset[Effect]:
    return frozenset(effects)


#: An effect set for a statement that changes nothing durable and observable.
#:
#: Deliberately empty rather than a "harmless" flag: there is no such flag, because
#: "harmless" is a *policy* judgement and this table makes no policy judgements. The
#: honest description of `USE prod` is "it changes the session's current namespace",
#: which is neither schema, data, config, nor code, and which does not outlive the
#: session. See `LABEL_EFFECTS` entries below for the individual reasoning.
_NO_EFFECT: frozenset[Effect] = frozenset()


#: label -> the set of effects that statement has.
#:
#: TOTAL over every label reachable from either pinned grammar (see the module
#: docstring for how that set was derived). Adding a Spark release that introduces a
#: statement must come with an entry here, or `test_every_grammar_label_has_an_effect`
#: fails -- by design.
#:
#: The comments are the deliverable as much as the values. Each non-obvious case
#: records *why*, because "why is DROP COLUMN both WRITE_SCHEMA and DESTROY_DATA" is
#: a question that will otherwise be re-litigated every time someone reads the table.
LABEL_EFFECTS: dict[str, frozenset[Effect]] = {
    # -- reads ---------------------------------------------------------------
    # A plain SELECT. Nothing else in the grammar can produce this label:
    # `effective_label()` collapses the entire query hierarchy (query / queryTerm /
    # queryPrimary / querySpecification and their labeled alternatives) down to
    # QUERY_LABEL, which is "StatementDefault". So a label here means "this statement
    # read some data and did nothing else", which is exactly READ_DATA and nothing
    # more. A SELECT with a side effect inside it cannot exist in SQL.
    "StatementDefault": _e(Effect.READ_DATA),

    # Everything under "reads" below is metadata introspection, not data access:
    # SHOW/DESCRIBE read the catalog, not table rows. They get no READ_DATA because
    # reading the catalog is not reading anyone's data, and lumping them in would
    # make a `SHOW TABLES` look like a data exfiltration signal. They do read *from
    # the catalog*, which is inside the cluster, so no REACHES_EXTERNAL either.
    "ShowCatalogs": _NO_EFFECT,
    "ShowCollation": _NO_EFFECT,        # policy-only label; grammar says ShowCollations
    "ShowCollations": _NO_EFFECT,
    "ShowColumns": _NO_EFFECT,
    "ShowCreateTable": _NO_EFFECT,
    "ShowCurrentNamespace": _NO_EFFECT,
    "ShowDatabases": _NO_EFFECT,       # policy-only; 4.0 emits ShowNamespaces
    "ShowFunctions": _NO_EFFECT,
    "ShowNamespaces": _NO_EFFECT,
    "ShowPartitions": _NO_EFFECT,
    "ShowProcedures": _NO_EFFECT,
    "ShowTableExtended": _NO_EFFECT,
    "ShowTables": _NO_EFFECT,
    "ShowTblProperties": _NO_EFFECT,
    "ShowViews": _NO_EFFECT,
    "DescribeFunction": _NO_EFFECT,
    "DescribeNamespace": _NO_EFFECT,
    "DescribeProcedure": _NO_EFFECT,
    "DescribeQuery": _NO_EFFECT,
    "DescribeRelation": _NO_EFFECT,

    # -- writes to rows -------------------------------------------------------
    # INSERT adds rows to a table that keeps its rows. Not DESTROY_DATA: running it
    # twice, or rolling back, does not lose anything that was there before. This is
    # exactly the case that separates WRITE_DATA from DESTROY_DATA.
    "InsertIntoTable": _e(Effect.WRITE_DATA),
    # policy-only label: the pinned grammars fold PARTITION into the table reference
    # and emit InsertIntoTable. Kept mapped so a grammar change that does split them
    # does not turn into an unmapped label.
    "InsertIntoPartition": _e(Effect.WRITE_DATA),
    # INSERT ... REPLACE (4.0) replaces the table's contents wholesale. It is an
    # INSERT in spelling and an overwrite in effect: the previous rows are gone with
    # no way to recover them, so it carries DESTROY_DATA as well as WRITE_DATA.
    # Without that second flag this table would report a data-destroying statement as
    # a plain row write, which is precisely the fail-open the sentinel exists to stop.
    "InsertIntoReplaceBooleanCond": _e(Effect.WRITE_DATA, Effect.DESTROY_DATA),
    "InsertIntoReplaceUsing": _e(Effect.WRITE_DATA, Effect.DESTROY_DATA),
    "InsertIntoReplaceWhere": _e(Effect.WRITE_DATA, Effect.DESTROY_DATA),
    # INSERT OVERWRITE replaces the target's contents. Same reasoning as REPLACE
    # above; the policy already calls it "destructive_statement" and the effect axis
    # agrees, independently.
    "InsertOverwriteTable": _e(Effect.WRITE_DATA, Effect.DESTROY_DATA),
    "InsertOverwriteHiveDir": _e(Effect.WRITE_DATA, Effect.DESTROY_DATA,
                                  Effect.REACHES_EXTERNAL),
    "InsertOverwriteDir": _e(Effect.WRITE_DATA, Effect.DESTROY_DATA,
                              Effect.REACHES_EXTERNAL),
    # UPDATE, DELETE and MERGE all carry DESTROY_DATA.
    #
    # The earlier reasoning here was "both are bounded by their WHERE clause, and what
    # they destroy is decided by the predicate rather than the label". That is true, and
    # it is the wrong question for this axis. Decided 2026-10-02 (sparkscreen-120).
    #
    # The screener is not asked whether a particular DELETE is harmful -- a
    # `DELETE FROM t WHERE id=3` removes a record, and the fact that the record is one
    # row rather than a million is not the same as it being safe. The tool's job is to
    # decide whether a human needs to look, and if the screener concludes "harmless"
    # because the statement looked narrow, then a user's record can be removed on the
    # strength of that analysis being wrong.
    #
    # So DESTROY_DATA here is a deliberate slowdown flag, not a damage estimate. It says
    # "rows are removed and a person should carry that out deliberately". Policy may
    # still auto-approve it in a sandbox namespace; the point is that the default answer
    # is not ALLOW, and no amount of narrowness in the predicate should produce one.
    # The cost is that routine cleanup DELETEs need an explicit policy allowance. That
    # is the intended price.
    "UpdateTable": _e(Effect.WRITE_DATA, Effect.DESTROY_DATA),
    "DeleteFromTable": _e(Effect.WRITE_DATA, Effect.DESTROY_DATA),
    # MERGE can insert, update *and* delete in one statement (WHEN NOT MATCHED THEN
    # INSERT / WHEN MATCHED THEN DELETE). Same reasoning as DELETE, and the strongest
    # case of the three: a single MERGE can delete rows in one branch and insert them in
    # another, so its blast radius is not visible from the label at all.
    "MergeIntoTable": _e(Effect.WRITE_DATA, Effect.DESTROY_DATA),

    # -- irreversible loss ---------------------------------------------------
    # DROP TABLE removes the table and everything in it. No WHERE-clause caveat, no
    # namespace that makes it safe: this is the canonical DESTROY_DATA.
    "DropTable": _e(Effect.DESTROY_DATA),
    # DROP VIEW loses the view definition, not table rows. Still DESTROY_DATA,
    # because the definition is the durable object and a view over a sensitive table
    # may be the only record of a masking/permission rule. Losing it is irreversible
    # from Spark's point of view.
    "DropView": _e(Effect.DESTROY_DATA),
    # DROP NAMESPACE removes every table in it.
    "DropNamespace": _e(Effect.DESTROY_DATA),
    # DROP INDEX / DROP CONSTRAINT lose an object, not rows.
    "DropIndex": _e(Effect.WRITE_SCHEMA, Effect.DESTROY_DATA),
    "DropTableConstraint": _e(Effect.WRITE_SCHEMA, Effect.DESTROY_DATA),
    # DROP FUNCTION loses executable code definitions.
    "DropFunction": _e(Effect.WRITE_SCHEMA, Effect.DESTROY_DATA),
    # DROP PARTITION deletes the partition's rows outright. This is the second place
    # where WRITE_SCHEMA alone would be actively wrong: `ALTER TABLE ... DROP
    # PARTITION` is how a single partition of a trillion-row table is destroyed, and
    # calling that a "schema change" would rank it alongside CREATE INDEX.
    "DropTablePartitions": _e(Effect.WRITE_SCHEMA, Effect.DESTROY_DATA),
    # ALTER TABLE ... DROP COLUMN. The case the whole Flag design exists for: it is
    # *simultaneously* a schema change and an irreversible data loss. The column's
    # values are gone, and Spark has no undo. A single-valued axis would have to
    # choose, and either choice misclassifies the statement for some consumer.
    #
    # Note the contrast with `AlterTableAlterColumn` (ALTER COLUMN ... COMMENT/TYPE),
    # which is WRITE_SCHEMA alone: the columns' *data* survives, only the metadata
    # changes. Same `ALTER TABLE`, opposite effect profile, and the difference is
    # exactly what this axis exists to make visible.
    "DropTableColumns": _e(Effect.WRITE_SCHEMA, Effect.DESTROY_DATA),
    # TRUNCATE TABLE: DESTROY_DATA but NOT WRITE_SCHEMA. This asymmetry is the point
    # of the example in the Effect docstring. TRUNCATE removes every row and leaves
    # the schema *exactly* as declared -- column names, types and constraints are all
    # still there. Calling it a schema change would put "add a nullable column"
    # (harmless) in the same class as "delete everything", which is the
    # fine-in-staging/review-in-prod tier. Calling it a mere row write would be worse.
    # So: rows are irrecoverably gone, structure is untouched. One flag, deliberately.
    "TruncateTable": _e(Effect.DESTROY_DATA),
    # Hive REPLACE COLUMNS: the replacement list is authoritative, so any column not
    # named is dropped. Same dual effect as DROP COLUMN, reached by different SQL.
    "HiveReplaceColumns": _e(Effect.WRITE_SCHEMA, Effect.DESTROY_DATA),
    # Hive CHANGE COLUMN renames and retypes in one step. The old values survive
    # (subject to the cast), so unlike HiveReplaceColumns this does not destroy data
    # -- but the rename is a schema change and the cast can silently truncate. This
    # is the debatable one: a narrowing cast (`bigint` -> `int`) destroys values, and
    # this table cannot see the types involved. Categorised as WRITE_SCHEMA because
    # the *statement* is a retyping, not a deletion; flagged to the maintainer as a
    # candidate for DESTROY_DATA once type information is available.
    "HiveChangeColumn": _e(Effect.WRITE_SCHEMA),
    # REPLACE TABLE AS SELECT: the schema *and* every row of the existing table go.
    # The most complete destroyer in the grammar, and a good illustration of why
    # flags compose -- no single word for "destroys rows and structure".
    "ReplaceTable": _e(Effect.WRITE_SCHEMA, Effect.WRITE_DATA, Effect.DESTROY_DATA),
    # RECOVER PARTITIONS rebuilds the metastore from the filesystem. It reads outside
    # the cluster (the storage backend) and writes catalog metadata.
    "RecoverPartitions": _e(Effect.WRITE_SCHEMA, Effect.REACHES_EXTERNAL),
    # RepairTable is the Hive spelling of the same idea.
    "RepairTable": _e(Effect.WRITE_SCHEMA, Effect.REACHES_EXTERNAL),

    # -- schema changes that preserve data -----------------------------------
    # Everything in this block changes structure while leaving existing rows intact,
    # which is what "fine in staging, review in prod" means. None of them justify
    # denying regardless of namespace.
    "AddTableColumns": _e(Effect.WRITE_SCHEMA),
    "AddTableConstraint": _e(Effect.WRITE_SCHEMA),
    "AddTablePartition": _e(Effect.WRITE_SCHEMA),
    "AlterTableAlterColumn": _e(Effect.WRITE_SCHEMA),
    "AlterTableCollation": _e(Effect.WRITE_SCHEMA),
    "SetTableCollation": _e(Effect.WRITE_SCHEMA),   # policy-only; grammar says AlterTableCollation
    "AlterViewQuery": _e(Effect.WRITE_SCHEMA),
    "AlterViewSchemaBinding": _e(Effect.WRITE_SCHEMA),
    "AlterClusterBy": _e(Effect.WRITE_SCHEMA),
    "SetTableLocation": _e(Effect.WRITE_SCHEMA, Effect.REACHES_EXTERNAL),
    "SetTableSerDe": _e(Effect.WRITE_SCHEMA),
    "SetTableProperties": _e(Effect.WRITE_SCHEMA),
    "UnsetTableProperties": _e(Effect.WRITE_SCHEMA),
    "RenameTable": _e(Effect.WRITE_SCHEMA),
    "RenameTableColumn": _e(Effect.WRITE_SCHEMA),
    "RenameTablePartition": _e(Effect.WRITE_SCHEMA),
    "SetNamespaceLocation": _e(Effect.WRITE_SCHEMA, Effect.REACHES_EXTERNAL),
    "SetNamespaceProperties": _e(Effect.WRITE_SCHEMA),
    "UnsetNamespaceProperties": _e(Effect.WRITE_SCHEMA),
    "SetNamespaceCollation": _e(Effect.WRITE_SCHEMA),
    "CreateIndex": _e(Effect.WRITE_SCHEMA),
    "CreateTable": _e(Effect.WRITE_SCHEMA),
    "CreateTableLike": _e(Effect.WRITE_SCHEMA),
    "CreateView": _e(Effect.WRITE_SCHEMA),
    # CREATE TEMPORARY VIEW creates a view scoped to the session. Still WRITE_SCHEMA:
    # the catalog entry is real metadata, it just does not outlive the session. It is
    # not DESTROY_DATA because losing it loses nothing.
    "CreateTempViewUsing": _e(Effect.WRITE_SCHEMA),
    # 4.0-only metric view; a view with aggregation semantics. Same category.
    "CreateMetricView": _e(Effect.WRITE_SCHEMA),
    "CreateNamespace": _e(Effect.WRITE_SCHEMA),
    # CREATE FUNCTION ... USING JAR / USING CLASS. Two effects, and both matter:
    # it registers a schema entry (WRITE_SCHEMA) *and* loads a Java class the engine
    # will execute (LOAD_CODE). The LOAD_CODE flag is what justifies denying this
    # regardless of namespace -- the JAR path is not namespace-scoped, so an
    # allowlist cannot constrain it, and the code runs with the driver's privileges.
    "CreateFunction": _e(Effect.WRITE_SCHEMA, Effect.LOAD_CODE),
    "CreateUserDefinedFunction": _e(Effect.WRITE_SCHEMA, Effect.LOAD_CODE),
    # 4.0's CREATE USER DEFINED FUNCTION ... USING CLASS, same reasoning.

    # -- code loading --------------------------------------------------------
    # ADD JAR / ADD FILE puts a resource on the driver's classpath. LOAD_CODE is the
    # whole point: whatever is in that JAR will be loaded by the JVM, and the JVM runs
    # with the submitter's filesystem and network access. REACHES_EXTERNAL because
    # the resource is fetched from outside the cluster. Not DESTROY_DATA -- nothing is
    # lost -- but under the "deny regardless of namespace" rule this is the second
    # flag that qualifies, and it is not namespace-scoped at all.
    "ManageResource": _e(Effect.LOAD_CODE, Effect.REACHES_EXTERNAL),

    # -- filesystem and external reads ---------------------------------------
    # LOAD DATA reads files and writes rows, so two effects minimum. Whether it is
    # READ_LOCAL_FS depends on the LOCAL keyword, which is *not* visible in the
    # statement label: both `LOAD DATA INPATH` and `LOAD DATA LOCAL INPATH` are
    # LoadData. Because this table keys on labels alone it cannot distinguish them,
    # and it must not under-report -- an unflagged `LOAD DATA LOCAL INPATH
    # '/etc/passwd'` is the exact attack this project exists to catch. So the
    # conservative reading is taken: LOAD DATA is flagged as potentially reading the
    # driver's local filesystem.
    #
    # This is an over-approximation and is documented as one. The alternative --
    # keying the table on (label, local) -- would be exact, and is the right
    # refinement if the effect axis ever grows beyond the label. Flagged for the
    # maintainer.
    "LoadData": _e(Effect.WRITE_DATA, Effect.READ_LOCAL_FS, Effect.REACHES_EXTERNAL),

    # -- config and session state --------------------------------------------
    # SET spark.* and friends. CHANGE_CONFIG, not LOAD_CODE: setting
    # `spark.sql.hive.metastore.jars=/tmp/x.jar` *can* lead to code execution, but
    # the statement itself writes configuration. Conflating the two would make every
    # `SET spark.sql.shuffle.partitions=200` -- the single most common statement in
    # agent-written PySpark -- indistinguishable from `ADD JAR`. A caller who wants
    # the transitive answer needs literal inspection, which is a separate concern.
    "SetConfiguration": _e(Effect.CHANGE_CONFIG),
    "SetQuotedConfiguration": _e(Effect.CHANGE_CONFIG),
    "ResetConfiguration": _e(Effect.CHANGE_CONFIG),
    "ResetQuotedConfiguration": _e(Effect.CHANGE_CONFIG),
    # SET spark.sql variable assignment (`SET @v = ...`). CHANGE_CONFIG: session
    # state that another statement may later read into a query.
    "SetVariable": _e(Effect.CHANGE_CONFIG),
    "SetVariableInsideSqlScript": _e(Effect.CHANGE_CONFIG),
    "SetTimeZone": _e(Effect.CHANGE_CONFIG),
    # SET PATH belongs to CHANGE_CONFIG, unlike SET CATALOG below: it changes how
    # unqualified names resolve for every subsequent statement, which is engine
    # configuration rather than a pointer to an existing object.
    "SetPath": _e(Effect.CHANGE_CONFIG),
    # SET catalog is the catalog-scoped twin of USE / USE NAMESPACE: all three move a
    # session pointer and nothing else. It is deliberately *not* CHANGE_CONFIG despite
    # starting with "SET" -- the flag means "changes how the engine computes results",
    # and the `ChangeConfig` vocabulary is `SET spark.*`, CACHE, SET ROLE and MSCK
    # REPAIR. A session's current catalog is not in that set, and `Use` below is empty
    # for exactly the same reason.
    "SetCatalog": _NO_EFFECT,
    # SET ROLE changes the identity subsequent statements run as. CHANGE_CONFIG is
    # correct but arguably undersells it -- it is privilege escalation by another
    # name. Noted for the maintainer; it is a schema-adjacent identity change, not
    # data or code, so it fits the flag as defined.
    "FailSetRole": _e(Effect.CHANGE_CONFIG),
    # CACHE / UNCACHE / CLEAR CACHE manage cached data, not durable data. They change
    # runtime state and consume cluster memory, so CHANGE_CONFIG.
    "CacheTable": _e(Effect.CHANGE_CONFIG),
    "UncacheTable": _e(Effect.CHANGE_CONFIG),
    "ClearCache": _e(Effect.CHANGE_CONFIG),
    # REFRESH invalidates a cached entry -- same category.
    "RefreshTable": _e(Effect.CHANGE_CONFIG),
    "RefreshFunction": _e(Effect.CHANGE_CONFIG),
    "RefreshResource": _e(Effect.CHANGE_CONFIG),
    # ANALYZE writes statistics into the catalog. It mutates metadata, not data, and
    # not the schema either -- so CHANGE_CONFIG, on the grounds that the observable
    # effect is on runtime/planning state. Debatable (a third option, "WRITE_CATALOG",
    # would fit better); flagged for the maintainer.
    "Analyze": _e(Effect.CHANGE_CONFIG),
    "AnalyzeTables": _e(Effect.CHANGE_CONFIG),
    # 4.0 pipeline DDL. Newly reachable in the 4.0 grammar and not present in 3.5.1;
    # mapped as schema writes, which is what creating a pipeline object does.
    "CreatePipelineDataset": _e(Effect.WRITE_SCHEMA),
    "CreatePipelineInsertIntoFlow": _e(Effect.WRITE_SCHEMA),
    "CreateFlowAutoCdc": _e(Effect.WRITE_SCHEMA),

    # -- procedure calls -----------------------------------------------------
    # CALL runs a stored procedure whose body is not in the SQL text. Its effects are
    # therefore genuinely unbounded, and REACHES_EXTERNAL is the honest flag: the
    # effect lives outside what we parsed. This is also why the policy keeps CALL at
    # UNKNOWN -- and the two facts are related but not the same. The verdict says "we
    # cannot clear this"; the effect says "whatever this does, it happens somewhere we
    # cannot see".
    "Call": _e(Effect.REACHES_EXTERNAL),
    # policy-only labels for the same construct under older grammar spellings.
    "Execute": _e(Effect.REACHES_EXTERNAL),
    # EXECUTE IMMEDIATE runs SQL held in a string literal. The payload is extracted
    # and analysed separately by `screen._check_execute_immediate`, which produces its
    # own findings with their own effects -- so this finding's own effect is only the
    # indirection. REACHES_EXTERNAL is a stretch for "runs SQL we have not seen yet";
    # it is here so the label is not left unmapped, and because the payload's real
    # effects land on the nested findings. See the drift note in `screen.py`.
    "VisitExecuteImmediate": _e(Effect.REACHES_EXTERNAL),

    # -- cursors (4.0) -------------------------------------------------------
    # Cursor lifecycle. Opening one runs a query; the query itself is analysed as its
    # own statement, so these are session-object management.
    "DeclareCursorStatement": _e(Effect.CHANGE_CONFIG),
    "OpenCursorStatement": _e(Effect.CHANGE_CONFIG),
    "FetchCursorStatement": _e(Effect.CHANGE_CONFIG),
    "CloseCursorStatement": _e(Effect.CHANGE_CONFIG),
    # policy-only spellings of the same four.
    "Open": _e(Effect.CHANGE_CONFIG),
    "Fetch": _e(Effect.CHANGE_CONFIG),
    "Close": _e(Effect.CHANGE_CONFIG),
    # Session variables. CREATE/DROP VARIABLE manage a session-scoped value.
    "CreateVariable": _e(Effect.CHANGE_CONFIG),
    "DropVariable": _e(Effect.CHANGE_CONFIG),

    # -- comments ------------------------------------------------------------
    # COMMENT ON writes metadata. It is not a schema change (the schema is unchanged)
    # and not data. CHANGE_CONFIG is the closest existing flag: it changes state the
    # catalog reports. This is the weakest-fitting entry in the table and is called
    # out as such -- a COMMENT can carry data (a PII value pasted into a comment), but
    # that is a content question, not a statement-kind question.
    "CommentTable": _e(Effect.CHANGE_CONFIG),
    "CommentNamespace": _e(Effect.CHANGE_CONFIG),
    "CommentColumn": _e(Effect.CHANGE_CONFIG),

    # -- session context -----------------------------------------------------
    # USE / USE NAMESPACE change which catalog is current for the rest of the
    # session. No schema, no data, no config, no code, nothing outside the cluster.
    # This is the honest reason these map to the empty set rather than to a
    # "harmless" flag: there is no such flag, and inventing one would put a policy
    # judgement inside a table that is supposed to make none.
    "Use": _NO_EFFECT,
    "UseNamespace": _NO_EFFECT,
    # EXPLAIN plans a query without running it. Not READ_DATA: the rows are read
    # from the metastore, not the table.
    "Explain": _NO_EFFECT,

    # -- script control flow (4.0 BEGIN...END) ------------------------------
    # CASE / IF / WHILE inside a SQL script. These are control flow over statements
    # that are analysed individually, so the enclosing block has no effect of its own
    # -- which is exactly why they are safe to leave empty. Note that this holds only
    # because `top_level_statement_contexts` surfaces the enclosed statements as
    # separate findings; if that ever regressed, an empty set here would silently
    # cover a nested DROP.
    "SearchedCaseStatement": _NO_EFFECT,
    "SimpleCaseStatement": _NO_EFFECT,

    # -- wrappers ------------------------------------------------------------
    # `effective_label()` descends *through* these, so policy rules should never see
    # them. They are in this table anyway, and that is not defensive padding:
    # `grammar.parser._wrap` attaches the *raw* top-level label to `ParsedStatement`,
    # and `screen()` passes that raw label straight into the resource-limit
    # `Finding`, so `statement="DmlStatement"` is reachable in real output today.
    #
    # A wrapper's effect is whatever its contents do, and its contents are not known
    # from the label -- so the fail-closed reading is the union of the whole DML
    # family: writes data, writes schema, and may destroy both. Reporting an INSERT
    # wrapper as DESTROY_DATA is an over-approximation on the safe side, and it costs
    # nothing because nothing downstream consumes a wrapper label except these
    # resource-limit findings, which are UNKNOWN regardless.
    #
    # Mapping these to an empty set instead would be the fail-open mistake this table
    # exists to prevent: a statement we only partly understood would report "no known
    # effect" and read as harmless.
    "DmlStatement": _e(Effect.WRITE_DATA, Effect.WRITE_SCHEMA, Effect.DESTROY_DATA),
    "SingleInsertQuery": _e(Effect.WRITE_DATA, Effect.WRITE_SCHEMA,
                            Effect.DESTROY_DATA),
    "MultiInsertQuery": _e(Effect.WRITE_DATA, Effect.WRITE_SCHEMA,
                           Effect.DESTROY_DATA),

    # -- catch-alls ----------------------------------------------------------
    # FailNativeCommand is the grammar's `.*?` alternative: it matches anything at
    # all, and is reached when nothing else does. Its effect is unknowable from the
    # label, and this is the one entry where an empty set would be a lie rather than
    # a statement of fact -- so REACHES_EXTERNAL stands in for "somewhere we cannot
    # see". It is the weakest flag available, deliberately, and it exists so this
    # label is not silently unmapped. The real protection is the verdict axis: an
    # unmatched statement is UNKNOWN/UNSUPPORTED_STATEMENT, never ALLOW.
    "FailNativeCommand": _e(Effect.REACHES_EXTERNAL),
}


def effects_for_label(label: str) -> frozenset[Effect]:
    """The effects of `label`. Raises `UnmappedLabelError` if it has no entry.

    Raising is the point. The alternative -- returning an empty frozenset -- makes
    "we have never seen this statement" look identical to "this statement does
    nothing", and the consumer of that value is a security gate. A miss must be loud.
    """
    try:
        return LABEL_EFFECTS[label]
    except KeyError:
        raise UnmappedLabelError(label) from None


def lookup_effects(label: str) -> frozenset[Effect] | None:
    """Non-raising variant: the effects, or `None` if the label is unmapped.

    `None` and not `frozenset()` -- see the module docstring. Callers that cannot
    tolerate an exception must check for `None` rather than test truthiness, because
    `frozenset()` is falsy too and that is the bug this signature exists to prevent.
    """
    return LABEL_EFFECTS.get(label)


def mapped_labels() -> frozenset[str]:
    return frozenset(LABEL_EFFECTS)


#: Effects that mean "this cannot be undone and no namespace makes it acceptable".
#:
#: The rule this axis exists to enable. DESTROY_DATA because the data is gone;
#: LOAD_CODE because the code runs with the driver's privileges and is not
#: namespace-scoped at all, so an allowlist cannot constrain it. WRITE_SCHEMA without
#: DESTROY_DATA is explicitly *not* in here -- that is the "fine in staging, review
#: in prod" tier, and lumping it in would make CREATE INDEX as forbidden as DROP.
DENY_REGARDLESS_OF_NAMESPACE: frozenset[Effect] = frozenset({
    Effect.DESTROY_DATA,
    Effect.LOAD_CODE,
})


def denies_regardless_of_namespace(effects: Iterable[Effect]) -> bool:
    """True when `effects` justify denying no matter which namespaces are allowed."""
    return bool(DENY_REGARDLESS_OF_NAMESPACE & frozenset(effects))


def effect_label_drift() -> dict[str, list[str]]:
    """Labels that appear in one vocabulary and not another.

    The same duplication-bug guard as `policy.policy_label_drift`, for the same
    reason: `LABEL_EFFECTS`, `DESTRUCTIVE_LABELS`, `READ_ONLY_LABELS` and the rules'
    label tuples are maintained separately, and a label that is missing from one is a
    statement whose effect this package cannot state.

    The categories, and what each means:

    * `grammar_unmapped` -- a pinned grammar can emit this label and `LABEL_EFFECTS`
      has no entry for it. ALWAYS a bug, and a fail-open one: the statement would be
      analysed and reported with no effect flags. `tests/test_effects.py` asserts this
      list is empty against the generated parsers directly.
    * `grammar_uncovered` -- the inverse: an entry exists for a label no pinned
      grammar can emit. Usually harmless (a label removed upstream, or a synonym), but
      it means the table is describing something unreachable and should be confirmed
      rather than assumed correct.
    * `policy_only` -- named in `DESTRUCTIVE_LABELS` / `READ_ONLY_LABELS` / a rule's
      `labels` but emitted by neither grammar. Dead policy vocabulary today. Reported
      rather than removed: deleting them is a `policy.py` edit, and this module does
      not get to make that call. See the report.
    * `effect_unmapped` -- named in policy but absent from `LABEL_EFFECTS`. Always a
      bug, and it is the one that would bite at runtime.
    """
    from ..policy import DESTRUCTIVE_LABELS, READ_ONLY_LABELS, default_policy

    policy_labels: set[str] = set(DESTRUCTIVE_LABELS) | set(READ_ONLY_LABELS)
    for rule in default_policy().rules:
        policy_labels |= set(rule.labels)
    mapped = set(LABEL_EFFECTS)
    try:
        from .label_universe import grammar_labels
        grammar: set[str] = set(grammar_labels())
    except Exception:
        # The universe walk imports the generated parsers. If that is unavailable
        # (a packaging problem), report the categories that do not need it rather than
        # pretending the grammar-side checks passed.
        grammar = set()
    return {
        "grammar_unmapped": sorted(grammar - mapped),
        "grammar_uncovered": sorted(mapped - grammar),
        "policy_only": sorted(policy_labels - grammar),
        "effect_unmapped": sorted(policy_labels - mapped),
    }