"""Server-only immutable metadata for checked personal archive imports."""

ARCHIVE_IMPORT_TABLES = ("archive_import_runs", "archive_import_artifacts")

_RUN_FIELDS = {
    "uuid": "string",
    "organization_id": "string",
    "actor_id": "string",
    "intake_identity": "string",
    "request_sha256": "string",
    "contract_version": "int ASSERT $value = 1",
    "archive_sha256": "string",
    "artifact_id": "string",
    "artifact_sha256": "string",
    "origin_json": "string",
    "mappings_json": "string",
    "mappings_sha256": "string",
    "conflict_policy": "string ASSERT $value = 'additive'",
    "credential_kind": "string ASSERT $value IN ['session', 'api_key']",
    "original_api_key_id": "option<string>",
    "original_ceiling_json": "string",
    "checked_plan_json": "string",
    "checked_plan_sha256": "string",
    "preview_counts_json": "string",
    "created_at": "datetime DEFAULT time::now()",
    "status": "string DEFAULT 'checked' ASSERT $value = 'checked'",
    "revision": "int DEFAULT 0 ASSERT $value >= 0",
    "updated_at": "datetime DEFAULT time::now()",
}
_ARTIFACT_FIELDS = {
    "uuid": "string",
    "organization_id": "string",
    "actor_id": "string",
    "run_id": "string",
    "archive_sha256": "string",
    "artifact_sha256": "string",
    "contract_version": "int ASSERT $value = 1",
    "member_inventory_json": "string",
    "staged_payload_json": "string",
    "measured_sizes_json": "string",
    "created_at": "datetime DEFAULT time::now()",
}


def _metadata_definitions(table: str, fields: dict[str, str]) -> str:
    definitions = [
        f"DEFINE TABLE IF NOT EXISTS {table} SCHEMAFULL;",
        f"ALTER TABLE IF EXISTS {table} SCHEMAFULL;",
        f"ALTER TABLE IF EXISTS {table} PERMISSIONS NONE;",
        *(
            f"DEFINE FIELD IF NOT EXISTS {name} ON {table} TYPE {field_type};"
            for name, field_type in fields.items()
        ),
        f"DEFINE INDEX IF NOT EXISTS {table}_uuid ON {table} FIELDS uuid UNIQUE;",
    ]
    return "\n".join(definitions)


_RUN_IMMUTABLE = tuple(
    name for name in _RUN_FIELDS if name not in {"status", "revision", "updated_at"}
)
_RUN_CHANGED = " OR ".join(f"$before.{name} != $after.{name}" for name in _RUN_IMMUTABLE)

ARCHIVE_IMPORT_DEFINITIONS = (
    _metadata_definitions("archive_import_runs", _RUN_FIELDS)
    + "\n"
    + _metadata_definitions("archive_import_artifacts", _ARTIFACT_FIELDS)
    + f"""
DEFINE INDEX IF NOT EXISTS archive_import_runs_intake ON archive_import_runs
    FIELDS organization_id, actor_id, intake_identity UNIQUE;
DEFINE INDEX IF NOT EXISTS archive_import_runs_owner ON archive_import_runs
    FIELDS organization_id, actor_id, uuid;
DEFINE INDEX IF NOT EXISTS archive_import_artifacts_run ON archive_import_artifacts
    FIELDS organization_id, actor_id, run_id UNIQUE;
DEFINE EVENT IF NOT EXISTS archive_import_artifact_immutable ON archive_import_artifacts
    WHEN $event = 'UPDATE' THEN {{ THROW 'Archive artifact is immutable'; }};
DEFINE EVENT IF NOT EXISTS archive_import_run_bindings_immutable ON archive_import_runs
    WHEN $event = 'UPDATE' AND ({_RUN_CHANGED})
    THEN {{ THROW 'Checked archive bindings are immutable'; }};
"""
)
