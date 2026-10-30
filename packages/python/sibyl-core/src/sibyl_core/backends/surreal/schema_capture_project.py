"""File capture sidecars under the project of the raw memory they fold.

The entity route archived every remembered memory's capture sidecar with no
project: it read the project from request metadata that had already been
stripped of client-owned scope keys. The raw memory row kept its project,
then folded behind the sidecar (metadata.projected_capture_id), so a
project-filtered capture list found neither row.

Only the project moves. The sidecar stays private to its writer, so no reader
gains access to anything, and only a sidecar written by the raw row's
principal in the same organization that still has no project is touched,
which also makes the backfill idempotent.
"""

from __future__ import annotations

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_version import SurrealExecute

# SurrealDB 3.x stops planning an IN list through its index past 32 values.
_SIDECAR_BATCH_SIZE = 32

_FOLDED_RAW_MEMORIES = """
SELECT
    metadata.projected_capture_id AS sidecar_id,
    organization_id,
    principal_id,
    project_id,
    metadata.project_id AS metadata_project_id,
    memory_scope,
    scope_key
FROM raw_captures
WHERE entity_type = 'raw_memory'
    AND metadata.projected_capture_id IS NOT NONE
    AND principal_id != '';
""".strip()

_FILE_SIDECARS = """
UPDATE (SELECT VALUE id FROM raw_captures WHERE uuid IN $sidecar_ids)
SET project_id = $project_id
WHERE organization_id = $organization_id
    AND principal_id = $principal_id
    AND entity_type != 'raw_memory'
    AND project_id IS NONE;
""".strip()


def _optional_str(value: object) -> str | None:
    return str(value) if value else None


def _raw_memory_project(row: dict[str, object]) -> str | None:
    # The CLI files a private memory's project in metadata only; a
    # project-scoped memory names it as its scope key.
    project = _optional_str(row.get("project_id")) or _optional_str(row.get("metadata_project_id"))
    if project is None and row.get("memory_scope") == "project":
        project = _optional_str(row.get("scope_key"))
    return project


async def file_capture_sidecars_under_project(execute_query: SurrealExecute) -> None:
    """Copy each folded raw memory's project onto its project-less sidecar.

    Sidecars sharing an owner and a project are updated together, so the
    statement count follows the number of distinct projects, not captures.
    """
    groups: dict[tuple[str, str, str], list[str]] = {}
    for row in normalize_records(await execute_query(_FOLDED_RAW_MEMORIES)):
        sidecar_id = _optional_str(row.get("sidecar_id"))
        project = _raw_memory_project(row)
        if sidecar_id is None or project is None:
            continue
        key = (str(row["organization_id"]), str(row["principal_id"]), project)
        groups.setdefault(key, []).append(sidecar_id)

    for (organization_id, principal_id, project_id), ids in groups.items():
        for start in range(0, len(ids), _SIDECAR_BATCH_SIZE):
            await execute_query(
                _FILE_SIDECARS,
                sidecar_ids=ids[start : start + _SIDECAR_BATCH_SIZE],
                organization_id=organization_id,
                principal_id=principal_id,
                project_id=project_id,
            )
