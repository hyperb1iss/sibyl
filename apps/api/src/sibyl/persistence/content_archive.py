"""Content archive export/import helpers."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from uuid import UUID

import structlog

from sibyl import config as config_module
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.records import (
    normalize_records as _normalize_records,
    query_error as _query_error,
)
from sibyl_core.backends.surreal.schema_embedding_states import REOPEN_EMBEDDING_STATES
from sibyl_core.backends.surreal.schema_invariants import (
    drop_undeclared_fields as _drop_undeclared_fields,
    fetch_declared_fields,
)
from sibyl_core.embeddings.provenance import mark_unverified_vector
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate import validation_receipt_archive
from sibyl_core.migrate.archive_lineage import LineageSection
from sibyl_core.migrate.archive_stream import (
    ArchiveSpool,
    PageReader,
    SourceCapture,
    SpooledCollection,
    Streamed,
    StreamedObject,
    capture_snapshot_rows,
    read_source_capture,
    spool_path,
    write_json_file,
)

log = structlog.get_logger()

CONTENT_ARCHIVE_VERSION = "2.3"
_INTEGRITY_ARCHIVE_VERSIONS = {"2.0", "2.1", "2.2", CONTENT_ARCHIVE_VERSION}
_VALIDATION_ARCHIVE_TABLES = frozenset(
    {"memory_validation_executions", "memory_validation_attempts"}
)
_DREAM_ARCHIVE_TABLES = frozenset({"dream_source_checkpoints", "dream_source_cursors"})


@dataclass(frozen=True)
class ContentArchiveTableSpec:
    name: str
    source_identity_field: str
    target_identity_field: str
    select_sql: str
    delete_by_identity_sql: str
    delete_all_sql: str
    create_sql: str


_CONTENT_ARCHIVE_TABLE_SPECS = (
    ContentArchiveTableSpec(
        name="crawl_sources",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM crawl_sources ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM crawl_sources WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM crawl_sources;",
        create_sql="CREATE crawl_sources CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="crawled_documents",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM crawled_documents ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM crawled_documents WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM crawled_documents;",
        create_sql="CREATE crawled_documents CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="document_chunks",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM document_chunks ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM document_chunks WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM document_chunks;",
        create_sql="CREATE document_chunks CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="entity",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM entity ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM entity WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM entity;",
        create_sql="CREATE entity CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="raw_captures",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM raw_captures ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM raw_captures WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM raw_captures;",
        create_sql="CREATE raw_captures CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="eval_attempts",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM eval_attempts ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM eval_attempts WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM eval_attempts;",
        create_sql="CREATE eval_attempts CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="eval_consolidations",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM eval_consolidations ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM eval_consolidations WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM eval_consolidations;",
        create_sql="CREATE eval_consolidations CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="api_idempotency_records",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM api_idempotency_records ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM api_idempotency_records WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM api_idempotency_records;",
        create_sql="CREATE api_idempotency_records CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="source_imports",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM source_imports ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM source_imports WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM source_imports;",
        create_sql="CREATE source_imports CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="content_changefeed_cursors",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM content_changefeed_cursors ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM content_changefeed_cursors WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM content_changefeed_cursors;",
        create_sql="CREATE content_changefeed_cursors CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="derived_from",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM derived_from ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM derived_from WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM derived_from;",
        create_sql="CREATE derived_from CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="chunk_of",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM chunk_of ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM chunk_of WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM chunk_of;",
        create_sql="CREATE chunk_of CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="supersedes",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM supersedes ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM supersedes WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM supersedes;",
        create_sql="CREATE supersedes CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="extracted_into",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM extracted_into ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM extracted_into WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM extracted_into;",
        create_sql="CREATE extracted_into CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="system_settings",
        source_identity_field="key",
        target_identity_field="key",
        select_sql="SELECT * FROM system_settings ORDER BY key ASC;",
        delete_by_identity_sql="DELETE FROM system_settings WHERE key = $identity;",
        delete_all_sql="DELETE FROM system_settings;",
        create_sql="CREATE system_settings CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="telemetry_rollups",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM telemetry_rollups ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM telemetry_rollups WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM telemetry_rollups;",
        create_sql="CREATE telemetry_rollups CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="backup_settings",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM backup_settings ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM backup_settings WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM backup_settings;",
        create_sql="CREATE backup_settings CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="backups",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM backups ORDER BY id ASC;",
        delete_by_identity_sql="DELETE FROM backups WHERE uuid = $identity;",
        delete_all_sql="DELETE FROM backups;",
        create_sql="CREATE backups CONTENT $record;",
    ),
)

_CONTENT_ARCHIVE_TABLE_SPECS += (
    ContentArchiveTableSpec(
        name="dream_source_checkpoints",
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql="SELECT * FROM dream_source_checkpoints ORDER BY id ASC;",
        delete_by_identity_sql="DELETE dream_source_checkpoints WHERE uuid = $identity;",
        delete_all_sql="DELETE dream_source_checkpoints;",
        create_sql="CREATE dream_source_checkpoints CONTENT $record;",
    ),
    ContentArchiveTableSpec(
        name="dream_source_cursors",
        source_identity_field="organization_id",
        target_identity_field="organization_id",
        select_sql="SELECT * FROM dream_source_cursors ORDER BY id ASC;",
        delete_by_identity_sql="DELETE dream_source_cursors WHERE organization_id = $identity;",
        delete_all_sql="DELETE dream_source_cursors;",
        create_sql="CREATE dream_source_cursors CONTENT $record;",
    ),
)

_CONTENT_ARCHIVE_TABLE_SPECS += tuple(
    ContentArchiveTableSpec(
        name=name,
        source_identity_field="id",
        target_identity_field="uuid",
        select_sql=f"SELECT * FROM {name} ORDER BY id ASC;",  # noqa: S608
        delete_by_identity_sql=f"DELETE {name} WHERE uuid = $identity;",
        delete_all_sql=f"DELETE {name};",
        create_sql=f"CREATE {name} CONTENT $record;",
    )
    for name in ("memory_validation_executions", "memory_validation_attempts")
)

CONTENT_ARCHIVE_TABLES = tuple(spec.name for spec in _CONTENT_ARCHIVE_TABLE_SPECS)
_CONTENT_ARCHIVE_TABLES_BY_NAME = {spec.name: spec for spec in _CONTENT_ARCHIVE_TABLE_SPECS}
_BACKUP_ARCHIVE_TABLES = frozenset({"backup_settings", "backups"})
_GLOBAL_CONTENT_ARCHIVE_TABLES = frozenset({"system_settings", "telemetry_rollups"})
_RETAINED_OPERATION_TABLES = _VALIDATION_ARCHIVE_TABLES | frozenset(
    {"eval_attempts", "eval_consolidations", "api_idempotency_records"}
)
_CONTENT_RELATION_ARCHIVE_TABLES = frozenset(
    {"derived_from", "chunk_of", "supersedes", "extracted_into"}
)


@dataclass(frozen=True)
class ContentArchiveRestoreResult:
    """Summary of one content archive restore."""

    success: bool
    tables_restored: int
    rows_restored: int
    errors: list[str] = field(default_factory=list)
    dropped_fields: dict[str, list[str]] = field(default_factory=dict)
    integrity_conflicts: list[dict[str, str]] = field(default_factory=list)
    quarantined: list[dict[str, str]] = field(default_factory=list)
    embedding_repair: dict[str, dict[str, int | str]] = field(default_factory=dict)


async def _repair_restored_capture_embeddings(
    client: SurrealContentClient,
    organizations: Sequence[str],
) -> dict[str, dict[str, int | str]]:
    """Give restored raw captures their vector before the scheduler's next pass.

    Archived rows carry no embedding, so without this a restore leaves raw
    recall lexical-only until the lifecycle job happens to run. The repair
    runs on the restore's own client so it reaches the rows that just landed.
    A repair failure is recorded per organization and never fails the restore.
    """
    from sibyl_core.services.content_raw_embedding_repair import repair_raw_capture_embeddings

    # The chunk plane's verdict weighs raw capture stamps photographed when the
    # content schema upgraded, so restamping restored captures cannot sway it.
    receipts: dict[str, dict[str, int | str]] = {}
    for organization_id in organizations:
        try:
            result = await repair_raw_capture_embeddings(organization_id, client=client)
        except Exception as exc:
            log.warning(
                "content_archive_restore_embedding_repair_failed",
                organization_id=organization_id,
                error_type=type(exc).__name__,
            )
            receipts[organization_id] = {"error": type(exc).__name__}
        else:
            receipts[organization_id] = dict(asdict(result))
    return receipts


async def _restored_embedding_follow_up(
    client: SurrealContentClient,
    tables: Mapping[str, object],
    scope: Sequence[str] | None,
    *,
    repair_captures: bool,
) -> dict[str, dict[str, int | str]]:
    await _reopen_restored_planes(client, tables, scope)
    if not repair_captures:
        return {}
    return await _repair_restored_capture_embeddings(
        client, _restored_capture_organizations(tables, scope)
    )


async def _reopen_restored_planes(
    client: SurrealContentClient,
    tables: Mapping[str, object],
    scope: Sequence[str] | None,
) -> None:
    """Send the embedding passes back over the chunks and captures a restore just wrote.

    A plane that already finished a pass, chunk sweep and raw capture repair
    alike, would otherwise skip its walk until the verify interval lapses.
    A failure here only delays that, so it never fails the restore.
    """
    organizations = list(
        dict.fromkeys(
            [
                *_restored_row_organizations(tables, "document_chunks", scope),
                *_restored_capture_organizations(tables, scope),
            ]
        )
    )
    if not organizations:
        return
    try:
        await client.execute_query(REOPEN_EMBEDDING_STATES, organizations=organizations)
    except Exception as exc:
        log.warning(
            "content_archive_restore_embedding_reopen_failed",
            error_type=type(exc).__name__,
        )


def _restored_row_organizations(
    tables: Mapping[str, object], table: str, scope: Sequence[str] | None
) -> list[str]:
    rows = tables.get(table)
    seen: dict[str, None] = {}
    for row in rows if isinstance(rows, list) else []:
        organization_id = row.get("organization_id") if isinstance(row, dict) else None
        if isinstance(organization_id, str) and organization_id.strip():
            seen.setdefault(organization_id.strip(), None)
    if scope is not None:
        allowed = {str(item) for item in scope}
        return [organization_id for organization_id in seen if organization_id in allowed]
    return list(seen)


def _restored_capture_organizations(
    tables: Mapping[str, object], scope: Sequence[str] | None
) -> list[str]:
    return _restored_row_organizations(tables, "raw_captures", scope)


def build_surreal_content_client() -> SurrealContentClient:
    """Build a Surreal content client from application settings."""

    return SurrealContentClient(
        url=config_module.settings.resolved_surreal_url,
        username=config_module.settings.surreal_username,
        password=config_module.settings.surreal_password.get_secret_value(),
        token=config_module.settings.surreal_token.get_secret_value(),
        pool_size=config_module.settings.surreal_client_pool_size("content"),
    )


def _serialize_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return _serialize_value(value.value)
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_serialize_value(item) for item in value]
    return value


def _deserialize_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _deserialize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deserialize_value(item) for item in value]
    if isinstance(value, str):
        if "T" not in value and not value.endswith("Z"):
            return value
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return value
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return value


def _deserialize_vector(value: object) -> list[float] | object:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text.startswith("[") or not text.endswith("]"):
        return value
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return value
    if not isinstance(parsed, list):
        return value
    out: list[float] = []
    for item in parsed:
        if isinstance(item, bool):
            return value
        if isinstance(item, int | float):
            out.append(float(item))
            continue
        return value
    return out


def _content_row_sort_key(spec: ContentArchiveTableSpec, row: dict[str, object]) -> str:
    value = row.get(spec.target_identity_field) or row.get(spec.source_identity_field)
    if value is not None:
        return str(value)
    return json.dumps(row, sort_keys=True, default=str)


def _sort_content_rows(
    spec: ContentArchiveTableSpec,
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    return sorted(rows, key=lambda row: _content_row_sort_key(spec, row))


def _coerce_archive_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    return None


def _resolve_archive_database_dump_value(row: dict[str, object]) -> bool | None:
    return _coerce_archive_bool(row.get("include_database_dump"))


def _normalize_backup_archive_flags(row: dict[str, object]) -> dict[str, object]:
    normalized = dict(row)
    normalized.pop("include_postgres", None)
    normalized["include_database_dump"] = False
    normalized["include_graph"] = True
    return normalized


def _normalize_content_archive_export_row(
    spec: ContentArchiveTableSpec,
    row: dict[str, object],
) -> dict[str, object]:
    normalized = dict(row)
    if spec.name not in _BACKUP_ARCHIVE_TABLES:
        return normalized

    return _normalize_backup_archive_flags(normalized)


def _normalize_content_archive_restore_row(
    spec: ContentArchiveTableSpec,
    row: dict[str, object],
) -> dict[str, object]:
    normalized = dict(row)
    if spec.name not in _BACKUP_ARCHIVE_TABLES:
        return normalized

    return _normalize_backup_archive_flags(normalized)


def _content_archive_projection(spec: ContentArchiveTableSpec) -> str:
    projection = "*"
    if spec.name in _RETAINED_OPERATION_TABLES:
        projection += ", type::string(created_at) AS created_at"
        if spec.name == "eval_attempts":
            projection += (
                ", IF admitted_at != NONE { type::string(admitted_at) } "
                "ELSE { NONE } AS admitted_at"
            )
    return projection


def _content_archive_export_query(
    spec: ContentArchiveTableSpec,
    organization_id: str | None,
) -> tuple[str, dict[str, object]] | None:
    query = f"SELECT {_content_archive_projection(spec)} FROM {spec.name}"  # noqa: S608
    if organization_id is None:
        return query + ";", {}
    if spec.name in _GLOBAL_CONTENT_ARCHIVE_TABLES:
        return None
    return (
        query + " WHERE organization_id = $organization_id;",
        {"organization_id": organization_id},
    )


_AUXILIARY_CHANGED = "archive auxiliary destination changed before restore"


async def _plan_auxiliary_cleanup(client, organization_id: str | None) -> list:
    """Page the auxiliary rows a clean restore wipes; relation tables go first."""
    from sibyl_core.services.source_archive_store import plan_cleanup

    wipe_specs = sorted(
        [
            spec
            for spec in _CONTENT_ARCHIVE_TABLE_SPECS
            if spec.name != "raw_captures"
            and spec.name not in _RETAINED_OPERATION_TABLES
            and spec.name not in _DREAM_ARCHIVE_TABLES
            and (organization_id is None or spec.name not in _GLOBAL_CONTENT_ARCHIVE_TABLES)
        ],
        key=lambda spec: spec.name not in _CONTENT_RELATION_ARCHIVE_TABLES,
    )
    pages = []
    for spec in wipe_specs:
        pages += await plan_cleanup(
            client.execute_query,
            spec.name,
            organization_field="organization_id",
            scope="true" if organization_id is None else "organization_id = $organization_id",
            keep=lambda _identity: False,
            **({} if organization_id is None else {"organization_id": organization_id}),
        )
    return pages


@dataclass(frozen=True)
class _AuxiliaryRow:
    """One checked auxiliary write and the identity it lands on."""

    table: str
    identity_field: str
    identity: str
    statement: str
    parameters: dict[str, object]


async def _check_auxiliary_ownership(client, rows: list[_AuxiliaryRow], organization_id: str):
    """Refuse, before any write, an archive row whose identity another organization holds."""
    from sibyl_core.services.source_archive_store import restore_batches

    by_table: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        by_table.setdefault((row.table, row.identity_field), []).append(row.identity)
    for (table, identity_field), identities in by_table.items():
        for batch in restore_batches(identities):
            # Each probe carries its own value: the embedded engine does not
            # bind query parameters inside a closure.
            owners = await client.execute_query(
                "RETURN array::flatten($probes.map(|$probe| (SELECT VALUE organization_id "  # noqa: S608
                f"FROM {table} WHERE {identity_field} = $probe.identity)));",
                probes=[{"identity": identity} for identity in batch],
            )
            if not isinstance(owners, list):
                raise TypeError("archive auxiliary ownership read returned an invalid shape")
            if any(owner != organization_id for owner in owners):
                raise ValueError("archive identity belongs to another organization")


async def _export_surreal_content_archive_payload(
    organization_id: str | UUID | None = None,
) -> dict[str, object]:
    tables: dict[str, list[dict[str, object]]] = {}
    row_counts: dict[str, int] = {}
    client = build_surreal_content_client()

    try:
        org_id = str(organization_id) if organization_id is not None else None
        for spec in _CONTENT_ARCHIVE_TABLE_SPECS:
            query = _content_archive_export_query(spec, org_id)
            if query is None:
                tables[spec.name] = []
                row_counts[spec.name] = 0
                continue
            rows = await _read_content_table(client, spec, query)
            tables[spec.name] = rows
            row_counts[spec.name] = len(rows)
        from sibyl_core.memory_pipeline.observations import SourceKind
        from sibyl_core.migrate.source_integrity import validate_integrity_archive
        from sibyl_core.services.source_archive_store import export_source_integrity

        integrity = await export_source_integrity(
            client.execute_query,
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org_id] if org_id is not None else None,
        )
        raw_rows, _, _ = validate_integrity_archive(
            integrity, kind=SourceKind.RAW_CAPTURE, organizations=integrity["organizations"]
        )
        tables["raw_captures"] = [
            {
                key: _serialize_value(value)
                for key, value in row.items()
                if key != "archive_record_key"
            }
            for row in raw_rows
        ]
        row_counts["raw_captures"] = len(raw_rows)
        receipt_section = await _capture_validation_receipts(
            client, org_id, tables["memory_validation_executions"]
        )
    finally:
        await client.close()

    return {
        "version": CONTENT_ARCHIVE_VERSION,
        "source_integrity": integrity,
        "validation_receipts": receipt_section,
        "created_at": datetime.now(UTC).isoformat(),
        "organization_id": str(organization_id) if organization_id is not None else None,
        "tables": tables,
        "row_counts": row_counts,
        "total_rows": sum(row_counts.values()),
    }


async def _read_content_table(
    client: SurrealContentClient,
    spec: ContentArchiveTableSpec,
    query: tuple[str, dict[str, object]],
) -> list[dict[str, object]]:
    statement, params = query
    result = await client.execute_query(statement, **params)
    error = _query_error(result)
    if error is not None:
        raise RuntimeError(error)
    return _sort_content_rows(spec, _content_export_rows(spec, result))


def _content_export_rows(spec: ContentArchiveTableSpec, result: object) -> list[dict[str, object]]:
    return [
        _normalize_content_archive_export_row(
            spec,
            {str(key): _serialize_value(value) for key, value in row.items()},
        )
        for row in _normalize_records(result)
    ]


async def _capture_validation_receipts(
    client: SurrealContentClient,
    organization_id: str | None,
    receipt_rows: list[dict[str, object]],
) -> dict[str, object]:
    """Capture the receipts these executions name, then prove the executions held still."""
    receipt_section = await asyncio.to_thread(validation_receipt_archive.capture, receipt_rows)
    spec = _CONTENT_ARCHIVE_TABLES_BY_NAME["memory_validation_executions"]
    receipt_query = _content_archive_export_query(spec, organization_id)
    assert receipt_query is not None
    statement, params = receipt_query
    current = await client.execute_query(statement, **params)
    current_rows = _sort_content_rows(
        spec,
        [
            {str(key): _serialize_value(value) for key, value in row.items()}
            for row in _normalize_records(current)
        ],
    )
    if _query_error(current) is not None or current_rows != receipt_rows:
        raise ValueError("Validation executions changed during receipt archive capture")
    return receipt_section


async def export_content_archive_payload(
    organization_id: str | UUID | None = None,
) -> dict[str, object]:
    """Export Surreal content/operations tables into a JSON-safe payload."""

    return await _export_surreal_content_archive_payload(organization_id)


class ContentArchiveCapture:
    """One organization's content backup, captured to a spool and written as content.json.

    The document is the payload ``export_content_archive_payload`` returns for
    the organization, after the joint lineage seal the API backup applies.
    """

    def __init__(self, organization_id: str, workdir: Path) -> None:
        self.organization_id = organization_id
        self._spool = ArchiveSpool(spool_path(workdir, "content"))
        self.source = SourceCapture(SourceKind.RAW_CAPTURE, organization_id, self._spool)
        self._tables: dict[str, SpooledCollection] = {}
        self._executions: list[dict[str, object]] = []
        self._receipts: dict[str, object] = {}
        self.created_at = ""
        # The URL scheme when content was read in one statement per table, not paged.
        self.unpaged_scheme: str | None = None

    @property
    def lineage(self) -> LineageSection:
        return self.source.lineage

    @property
    def quarantined(self) -> list[dict[str, str]]:
        return []

    def finish(self, executions: list[dict[str, object]], receipts: dict[str, object]) -> None:
        self._executions = executions
        self._receipts = receipts
        self.created_at = datetime.now(UTC).isoformat()

    def add_table_rows(self, spec: ContentArchiveTableSpec, rows: list[dict[str, object]]) -> None:
        collection = self._tables.setdefault(spec.name, SpooledCollection(self._spool, (False,)))
        for row in rows:
            collection.add((_content_row_sort_key(spec, row),), row)

    def _table(self, spec: ContentArchiveTableSpec) -> tuple[Streamed | list, int]:
        if spec.name == "raw_captures":
            rows = (
                {
                    key: _serialize_value(value)
                    for key, value in row.items()
                    if key != "archive_record_key"
                }
                for row in self.source.decoded_rows(set())
            )
            return Streamed(rows), self.source.row_count
        if spec.name == "memory_validation_executions":
            return self._executions, len(self._executions)
        collection = self._tables.get(spec.name)
        if collection is None:
            return [], 0
        return Streamed(collection.ordered()), len(collection)

    def document(self, report: list[dict[str, str]]) -> StreamedObject:
        tables = {spec.name: self._table(spec) for spec in _CONTENT_ARCHIVE_TABLE_SPECS}
        row_counts = {name: count for name, (_, count) in tables.items()}
        return StreamedObject(
            [
                ("version", CONTENT_ARCHIVE_VERSION),
                ("source_integrity", self.source.section(set(self.lineage.flagged))),
                ("validation_receipts", self._receipts),
                ("created_at", self.created_at),
                ("organization_id", self.organization_id),
                ("tables", StreamedObject([(name, rows) for name, (rows, _) in tables.items()])),
                ("row_counts", row_counts),
                ("total_rows", sum(row_counts.values())),
                (
                    "lineage_validation",
                    [row for row in report if row["source_kind"] == self.source.kind.value],
                ),
            ]
        )

    def write(self, path: Path, report: list[dict[str, str]]) -> int:
        return write_json_file(path, self.document(report))

    def close(self) -> None:
        self._spool.close()


async def capture_content_archive(*, organization_id: str, workdir: Path) -> ContentArchiveCapture:
    """Capture an organization's content tables and raw capture lineage through disk.

    Tables and the raw capture ledger are read in pages inside one read
    transaction, so neither SurrealDB nor this process holds a whole table.
    Validation executions keep their own read, receipt capture and re-read,
    which proves no execution moved while its receipt was copied.
    """
    from sibyl_core.services.source_archive_store import read_source_archive_snapshot

    capture = ContentArchiveCapture(organization_id, workdir)
    client = build_surreal_content_client()
    try:
        async with client.read_snapshot() as execute:
            reader = PageReader(execute) if execute is not None else None
            if reader is None:
                capture.unpaged_scheme = client.url_scheme
            for spec in _CONTENT_ARCHIVE_TABLE_SPECS:
                query = _content_archive_export_query(spec, organization_id)
                if query is None or spec.name in {"raw_captures", "memory_validation_executions"}:
                    continue
                if reader is None:
                    capture.add_table_rows(spec, await _read_content_table(client, spec, query))
                    continue
                async for page in reader.pages(
                    spec.name,
                    fields=_content_archive_projection(spec),
                    where="organization_id = $organization_id",
                    params={"organization_id": organization_id},
                ):
                    capture.add_table_rows(spec, _content_export_rows(spec, page))
            if reader is None:
                capture_snapshot_rows(
                    capture.source,
                    await read_source_archive_snapshot(
                        client.execute_query,
                        kind=SourceKind.RAW_CAPTURE,
                        organizations=[organization_id],
                    ),
                )
            else:
                await read_source_capture(capture.source, reader)
        capture.source.finish()
        spec = _CONTENT_ARCHIVE_TABLES_BY_NAME["memory_validation_executions"]
        query = _content_archive_export_query(spec, organization_id)
        assert query is not None
        executions = await _read_content_table(client, spec, query)
        receipts = await _capture_validation_receipts(client, organization_id, executions)
        capture.finish(executions, receipts)
    except BaseException:
        capture.close()
        raise
    finally:
        await client.close()
    return capture


def _prepare_content_source_integrity(payload, tables, organization_id):
    """Validate source scope and translate legacy content before cleanup starts."""
    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.migrate.source_integrity import validate_integrity_archive

    version = payload.get("version", "1.0")
    required = set(CONTENT_ARCHIVE_TABLES)
    if version == "2.0":
        required -= _DREAM_ARCHIVE_TABLES
    if version in {"2.0", "2.1"}:
        required -= _VALIDATION_ARCHIVE_TABLES
    if version in _INTEGRITY_ARCHIVE_VERSIONS and required - set(tables):
        raise ValueError("current content archive is missing required tables")
    if version not in {"2.1", "2.2", CONTENT_ARCHIVE_VERSION} and any(
        tables.get(name) for name in _DREAM_ARCHIVE_TABLES
    ):
        raise ValueError("dream checkpoints require content archive 2.1")
    if version not in {"2.2", CONTENT_ARCHIVE_VERSION} and any(
        tables.get(name) for name in _VALIDATION_ARCHIVE_TABLES
    ):
        raise ValueError("validation history requires content archive 2.2")
    for table, rows in tables.items():
        if table not in CONTENT_ARCHIVE_TABLES or not isinstance(rows, list):
            raise ValueError("content archive has an unknown or malformed table")
        if organization_id is not None and table in _GLOBAL_CONTENT_ARCHIVE_TABLES and rows:
            raise ValueError("organization-scoped archive cannot restore global tables")
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("content archive row must be an object")
            if (
                organization_id is not None
                and table not in _GLOBAL_CONTENT_ARCHIVE_TABLES
                and str(row.get("organization_id")) != organization_id
            ):
                raise ValueError("content archive row is outside restore organization")
    if version not in {"1.0", *_INTEGRITY_ARCHIVE_VERSIONS}:
        raise ValueError("unsupported content archive version")
    integrity = payload.get("source_integrity")
    if version in _INTEGRITY_ARCHIVE_VERSIONS:
        scope = (
            [organization_id]
            if organization_id is not None
            else (integrity.get("organizations") if isinstance(integrity, dict) else None)
        )
        if not isinstance(scope, list):
            raise ValueError("content archive integrity scope is missing")
        validate_integrity_archive(integrity, kind=SourceKind.RAW_CAPTURE, organizations=scope)
    elif integrity is not None:
        raise ValueError("legacy content archive cannot carry unrecognized integrity")

    unavailable_ids: set[str] = set()
    if version not in _INTEGRITY_ARCHIVE_VERSIONS:
        from sibyl_core.migrate.legacy_source_archive import build_legacy_source_archive

        legacy_rows = tables.get("raw_captures", [])
        if not isinstance(legacy_rows, list) or any(
            not isinstance(row, dict) for row in legacy_rows
        ):
            raise ValueError("legacy raw capture collection is malformed")
        records = []
        for row in legacy_rows:
            # Legacy rows may omit fields whose defaults only run on CREATE.
            # Complete those fields before a checked restore updates an existing row.
            now = datetime.now(UTC)
            record = {
                "source_id": "",
                "principal_id": "",
                "memory_scope": "private",
                "review_state": "pending",
                "title": "",
                "raw_content": "",
                "entity_type": "",
                "tags": [],
                "metadata": {},
                "provenance": {},
                "captured_at": now,
                "created_at": now,
                "retrieval_count": 0,
                "citation_count": 0,
                "misled_count": 0,
                **{str(key): value for key, value in row.items() if key != "id"},
            }
            for field in ("created_at", "captured_at"):
                if record[field] is None:
                    record[field] = now
            record["uuid"] = str(row.get("uuid") or row.get("id") or "").strip()
            for field in (
                "created_at",
                "updated_at",
                "captured_at",
                "deleted_at",
                "purge_after",
                "last_recalled_at",
                "last_used_at",
            ):
                if field in record:
                    record[field] = _deserialize_value(record[field])
            records.append(record)
        scope = (
            [organization_id]
            if organization_id is not None
            else sorted({str(row.get("organization_id") or "") for row in records})
        )
        integrity, unavailable_ids = build_legacy_source_archive(
            records,
            kind=SourceKind.RAW_CAPTURE,
            organizations=scope,
        )

    return integrity, scope, unavailable_ids


async def _filter_legacy_source_fields(client, integrity, scope):
    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.migrate.source_integrity import (
        build_integrity_archive,
        validate_integrity_archive,
    )

    rows, states, associations = validate_integrity_archive(
        integrity, kind=SourceKind.RAW_CAPTURE, organizations=scope
    )
    declared = await fetch_declared_fields(client.execute_query, "raw_captures")
    dropped_fields: set[str] = set()
    filtered = []
    for row in rows:
        record_key = row.pop("archive_record_key")
        record, dropped = _drop_undeclared_fields(row, declared)
        record["archive_record_key"] = record_key
        dropped_fields.update(dropped)
        filtered.append(record)
    return build_integrity_archive(
        kind=SourceKind.RAW_CAPTURE,
        organizations=scope,
        source_rows=filtered,
        source_states=states,
        derivations=associations,
    ), dropped_fields


def _auxiliary_restore_statement(
    spec: ContentArchiveTableSpec,
    organization_id: str | None,
    record: dict[str, object],
    *,
    legacy: bool = False,
) -> str:
    """Preserve operation history and reject foreign identities in the same transaction."""
    existing = (
        f"LET $existing = SELECT * OMIT id FROM {spec.name} "  # noqa: S608
        f"WHERE {spec.target_identity_field} = $identity;\n"
    )
    ownership_guard = ""
    if organization_id is not None:
        ownership_guard = (
            "IF array::len($existing[WHERE organization_id = $organization_id]) "
            "!= array::len($existing) { "
            "THROW 'archive identity belongs to another organization'; };\n"
        )
    if spec.name in _DREAM_ARCHIVE_TABLES:
        from sibyl_core.services.dream_checkpoints import archive_dream_restore_statement

        return existing + ownership_guard + archive_dream_restore_statement(spec.name, record)
    conversion = ""
    if spec.name in _RETAINED_OPERATION_TABLES:
        fields = ["created_at"] + (
            ["admitted_at"] if spec.name == "eval_attempts" and "admitted_at" in record else []
        )
        converted = ", ".join(
            f"IF $record.{field} != NONE AND $record.{field} != NULL "
            f"{{ type::datetime($record.{field}) }} "
            f"ELSE {{ NONE }} AS {field}"
            for field in fields
        )
        conversion = f"LET $record = (SELECT *, {converted} FROM ONLY $record);\n"  # noqa: S608
        comparison = "$existing != [$record]"
        omitted = []
        if spec.name == "memory_validation_executions":
            # Dependency guards advance the destination's conflict witness.
            omitted.append("promotion_write_witness")
        if legacy and "created_at" not in record:
            omitted.append("created_at")
        if omitted:
            fields_to_omit = ", ".join(omitted)
            comparison = (
                f"(SELECT * OMIT {fields_to_omit} FROM $existing) "  # noqa: S608
                f"!= (SELECT * OMIT {fields_to_omit} FROM [$record])"
            )
        write = (
            f"IF array::len($existing) = 0 {{ {spec.create_sql} }} ELSE {{ "
            f"IF {comparison} {{ "
            "THROW 'archive conflicts with retained operation history'; }; };\n"
        )
    else:
        write = f"{spec.delete_by_identity_sql}\n{spec.create_sql}\n"
    if spec.name in _VALIDATION_ARCHIVE_TABLES:
        from sibyl_core.services.validation_execution import validation_archive_guard

        conversion += validation_archive_guard(spec.name, record)
    return f"{conversion}{existing}{ownership_guard}{write}"


def _ordered_validation_history(rows):
    from sibyl_core.services.validation_dependencies import normalize_legacy_dependencies
    from sibyl_core.services.validation_execution import validation_archive_guard

    for row in rows:
        validation_archive_guard("memory_validation_executions", row)
    return sorted(
        normalize_legacy_dependencies(rows),
        key=lambda row: (len(row.get("dependency_ids", [])), row["uuid"]),
    )


async def _prepare_auxiliary_content_restore(client, tables, organization_id, *, legacy=False):
    """Validate and compile non-source writes, one checked statement per row."""
    restores: list[_AuxiliaryRow] = []
    tables_restored = rows_restored = 0
    errors: list[str] = []
    dropped_fields: dict[str, set[str]] = {}
    for spec in _CONTENT_ARCHIVE_TABLE_SPECS:
        if spec.name == "raw_captures":
            continue
        rows = tables.get(spec.name)
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            errors.append(f"{spec.name} payload must be a list")
            continue
        if not rows:
            continue

        if spec.name == "memory_validation_executions":
            try:
                rows = _ordered_validation_history(rows)
            except ValueError as exc:
                errors.append(f"{spec.name} invalid archive row ({type(exc).__name__})")
                continue
        declared = await fetch_declared_fields(client.execute_query, spec.name)
        tables_restored += 1
        for row in rows:
            if not isinstance(row, dict):
                errors.append(f"{spec.name} row payload must be an object")
                continue
            normalized_row = _normalize_content_archive_restore_row(
                spec,
                {str(key): value for key, value in row.items()},
            )
            identity = str(
                normalized_row.get(spec.source_identity_field)
                or normalized_row.get(spec.target_identity_field)
                or ""
            ).strip()
            if not identity:
                errors.append(f"{spec.name} row is missing {spec.source_identity_field}")
                continue

            record = {
                key: (
                    value if spec.name in _RETAINED_OPERATION_TABLES else _deserialize_value(value)
                )
                for key, value in normalized_row.items()
                if not (
                    spec.source_identity_field != spec.target_identity_field
                    and key == spec.source_identity_field
                )
            }
            if legacy and spec.name == "eval_consolidations":
                record.setdefault("admission_bindings", [])
            elif legacy and spec.name == "api_idempotency_records":
                record.setdefault("response_body", {})
            if spec.name == "document_chunks":
                record["embedding"] = _deserialize_vector(record.get("embedding"))
                # Archives from before chunk stamps carry vectors of unknown origin.
                mark_unverified_vector(record, has_vector=bool(record.get("embedding")))
            record[spec.target_identity_field] = identity
            record, dropped = _drop_undeclared_fields(record, declared)
            if dropped:
                if spec.name in _DREAM_ARCHIVE_TABLES | _VALIDATION_ARCHIVE_TABLES:
                    errors.append(f"{spec.name} has undeclared checkpoint fields")
                    continue
                dropped_fields.setdefault(spec.name, set()).update(dropped)

            if spec.name == "eval_attempts" and record.get("admitted_at") is None:
                record.pop("admitted_at", None)
            index = rows_restored
            try:
                statement = _auxiliary_restore_statement(
                    spec, organization_id, record, legacy=legacy
                )
            except ValueError as exc:
                if len(errors) < 50:
                    errors.append(f"{spec.name} invalid archive row ({type(exc).__name__})")
                continue
            restores.append(
                _AuxiliaryRow(
                    table=spec.name,
                    identity_field=spec.target_identity_field,
                    identity=identity,
                    statement=(
                        f"FOR $archive_once IN [true] {{ LET $record = $archive_record_{index}; "
                        f"LET $identity = $archive_identity_{index}; {statement} }};"
                    ),
                    parameters={
                        f"archive_record_{index}": record,
                        f"archive_identity_{index}": identity,
                    },
                )
            )
            rows_restored += 1

    return tables_restored, rows_restored, errors, dropped_fields, restores


async def _restore_content_sources_and_rows(
    client,
    integrity,
    auxiliary_restores: list[_AuxiliaryRow],
    pending_receipts,
    *,
    scope,
    organization_id: str | None,
    clean: bool,
    global_scope: bool,
) -> tuple[list[str], list[dict[str, str]]]:
    """Plan the content restore, then write it in bounded transactions.

    Returns the restored source ids and the integrity conflicts.
    """
    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.services.source_archive_store import (
        apply_cleanup,
        apply_source_restore,
        interrupted_restore,
        plan_source_restore,
        restore_batches,
    )

    # Decide every write, and raise every refusal the destination's state alone
    # can cause, before receipts publish or any row changes.
    plan = await plan_source_restore(
        client.execute_query,
        integrity,
        kind=SourceKind.RAW_CAPTURE,
        organizations=scope,
        clean=clean,
        skip_existing=False,
        global_scope=global_scope,
    )
    if organization_id is not None:
        await _check_auxiliary_ownership(client, auxiliary_restores, organization_id)
    cleanup = await _plan_auxiliary_cleanup(client, organization_id) if clean else []
    await asyncio.to_thread(validation_receipt_archive.publish, pending_receipts)
    # The auxiliary wipe goes first: deleting a raw capture also deletes the
    # relation edges that end at it, which would move a planned wipe page.
    # Then raw captures with their own clean deletes, then auxiliary rows in
    # archive order, each statement checking its row inside its batch.
    try:
        await apply_cleanup(client.execute_query, cleanup, refusal=_AUXILIARY_CHANGED)
        await apply_source_restore(client.execute_query, plan)
        for batch in restore_batches(auxiliary_restores):
            parameters: dict[str, object] = {"organization_id": organization_id}
            for row in batch:
                parameters.update(row.parameters)
            statements = "\n".join(row.statement for row in batch)
            await client.execute_query(
                f"BEGIN TRANSACTION;\n{statements}\nCOMMIT TRANSACTION;", **parameters
            )
    except Exception as exc:
        raise RuntimeError(interrupted_restore(exc)) from exc
    return plan.restored, plan.conflicts


async def restore_content_archive_payload(
    payload: dict[str, object],
    *,
    clean: bool = False,
    global_scope: bool = False,
) -> ContentArchiveRestoreResult:
    """Restore a content payload into the Surreal content namespace."""

    raw_tables = payload.get("tables")
    if not isinstance(raw_tables, dict):
        raise TypeError("content archive payload is missing a tables object")
    tables = {str(key): value for key, value in raw_tables.items()}
    payload_org_id = payload.get("organization_id")
    organization_id = str(payload_org_id).strip() if payload_org_id is not None else None
    if organization_id == "":
        organization_id = None
    if organization_id is None and not global_scope:
        raise ValueError("global content restore requires explicit operator scope")
    if organization_id is not None and global_scope:
        raise ValueError("global content scope cannot be applied to an organization-scoped archive")

    from sibyl_core.migrate.archive_lineage import seal_archive_lineage

    _, sealed, _ = seal_archive_lineage(None, payload)
    assert sealed is not None
    payload = sealed
    integrity, scope, unavailable_ids = _prepare_content_source_integrity(
        payload, tables, organization_id
    )

    client = build_surreal_content_client()
    tables_restored = rows_restored = 0
    errors: list[str] = []
    dropped_fields: dict[str, set[str]] = {}
    integrity_conflicts: list[dict[str, str]] = []
    quarantined: list[dict[str, str]] = []

    try:
        await bootstrap_content_schema(client, reset=False)
        if payload.get("version", "1.0") not in _INTEGRITY_ARCHIVE_VERSIONS:
            integrity, legacy_dropped = await _filter_legacy_source_fields(client, integrity, scope)
            if legacy_dropped:
                dropped_fields["raw_captures"] = legacy_dropped
        (
            auxiliary_tables,
            auxiliary_rows,
            errors,
            auxiliary_dropped,
            auxiliary_restores,
        ) = await _prepare_auxiliary_content_restore(
            client, tables, organization_id, legacy=payload.get("version", "1.0") == "1.0"
        )
        if errors:
            return ContentArchiveRestoreResult(
                success=False, tables_restored=0, rows_restored=0, errors=errors[:50]
            )
        pending_receipts = await asyncio.to_thread(
            validation_receipt_archive.prepare_payload, payload
        )
        try:
            restored_ids, integrity_conflicts = await _restore_content_sources_and_rows(
                client,
                integrity,
                auxiliary_restores,
                pending_receipts,
                scope=scope,
                organization_id=organization_id,
                clean=clean,
                global_scope=global_scope,
            )
        except Exception as exc:
            return ContentArchiveRestoreResult(
                success=False, tables_restored=0, rows_restored=0, errors=[str(exc)]
            )
        rows_restored += len(restored_ids)
        tables_restored += 1
        quarantined = [
            {
                "source_id": identity,
                "reason": "unverifiable_legacy_lineage",
                "repair": "reauthor_under_new_capture_identity",
            }
            for identity in restored_ids
            if identity in unavailable_ids
        ]
        quarantined.extend(
            dict(row)
            for row in payload.get("lineage_validation", [])
            if isinstance(row, dict) and row.get("status") == "quarantined"
        )
        dropped_fields.update(auxiliary_dropped)
        tables_restored += auxiliary_tables
        rows_restored += auxiliary_rows

        if dropped_fields:
            log.warning(
                "content_archive_restore_dropped_undeclared_fields",
                dropped_fields={
                    table: sorted(names) for table, names in sorted(dropped_fields.items())
                },
            )
        return ContentArchiveRestoreResult(
            success=not errors,
            tables_restored=tables_restored,
            rows_restored=rows_restored,
            errors=errors[:50],
            integrity_conflicts=integrity_conflicts,
            quarantined=quarantined,
            dropped_fields={
                table: sorted(names) for table, names in sorted(dropped_fields.items())
            },
            embedding_repair=(
                await _restored_embedding_follow_up(
                    client, tables, scope, repair_captures=bool(restored_ids)
                )
                if not errors
                else {}
            ),
        )
    finally:
        await client.close()


__all__ = [
    "CONTENT_ARCHIVE_TABLES",
    "CONTENT_ARCHIVE_VERSION",
    "ContentArchiveCapture",
    "ContentArchiveRestoreResult",
    "build_surreal_content_client",
    "capture_content_archive",
    "export_content_archive_payload",
    "restore_content_archive_payload",
]
