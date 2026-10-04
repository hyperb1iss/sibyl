"""Native owner and project guards for authenticated synchronous writes."""

from __future__ import annotations

from sibyl_core.backends.surreal.schema import render_surreal_compatible_sql
from sibyl_core.models.entities import Entity
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_common import execute_graph_transaction
from sibyl_core.services.graph_entity_store import (
    _ENTITY_BULK_UPSERT_QUERY,
    _enforce_entity_content_limit,
    _entity_record,
    _parsed_metadata_snapshot,
)
from sibyl_core.services.graph_records import _snapshot_without_owned_keys, entity_from_surreal_row


def row_author(row: Entity) -> str | None:
    metadata = row.metadata or {}
    author = metadata.get("principal_id")
    if author in (None, "") and metadata.get("memory_scope") == "private":
        author = metadata.get("scope_key")
    if author in (None, ""):
        author = getattr(row, "created_by", None)
    return str(author) if author not in (None, "") else None


def row_project(row: Entity) -> str | None:
    metadata = row.metadata or {}
    project = getattr(row, "project_id", None) or metadata.get("project_id")
    if not project and metadata.get("memory_scope") == "project":
        project = metadata.get("scope_key")
    return str(project) if project else None


async def replace_authorized_entity(
    client: SurrealGraphClient,
    entity: Entity,
    *,
    group_id: str,
    principal_id: str,
    project_id: str | None,
) -> Entity:
    """Commit only while the exact row authorized by this request still exists."""
    if not principal_id:
        raise ValueError("Protected entity writes require an authenticated principal")
    _enforce_entity_content_limit([entity])
    snapshots = await execute_graph_transaction(
        client,
        """
        LET $existing = SELECT * FROM entity WHERE uuid = $uuid;
        RETURN {rows: $existing, sha256: crypto::sha256(type::string($existing))};
        """,
        uuid=entity.id,
    )
    if len(snapshots) != 1:
        raise RuntimeError("Protected entity write returned an invalid snapshot")
    previous = snapshots[0].get("rows")
    if not isinstance(previous, list):
        raise RuntimeError("Protected entity write returned an invalid snapshot")
    fingerprint = snapshots[0].get("sha256")
    if not isinstance(fingerprint, str) or len(previous) > 1:
        raise ValueError("Entity identity already exists with ambiguous ownership")
    record = _entity_record(entity, group_id=group_id)
    if previous:
        row = previous[0]
        if not isinstance(row, dict) or row.get("group_id") != group_id:
            raise ValueError("Entity identity already exists in another organization")
        stored = entity_from_surreal_row(row)
        owner, project = row_author(stored), row_project(stored)
        if (owner and owner != principal_id) or (project and project != project_id):
            raise ValueError("Entity identity already exists under another owner or project")
        # Fold a legacy snapshot into the replacement, never mutate it before
        # the authority fence. The canonical upsert preserves flattened keys.
        attributes = row.get("attributes") or {}
        if not isinstance(attributes, dict):
            raise ValueError("Entity identity already exists with invalid metadata")
        snapshot = _parsed_metadata_snapshot(attributes.get("metadata"))
        if snapshot is not None:
            preserved = {
                key: value
                for key, value in _snapshot_without_owned_keys(snapshot).items()
                if key not in attributes
            }
            incoming_attributes = record["attributes"]
            if not isinstance(incoming_attributes, dict):
                raise RuntimeError("Protected entity write generated invalid metadata")
            record["attributes"] = {**preserved, **incoming_attributes, "metadata": None}
    upsert = (
        render_surreal_compatible_sql(_ENTITY_BULK_UPSERT_QUERY, url=client._url)
        .strip()
        .rstrip(";")
    )
    results = await execute_graph_transaction(
        client,
        """
        BEGIN TRANSACTION;
        LET $result = {
        LET $existing = SELECT * FROM entity WHERE uuid = $uuid;
        IF crypto::sha256(type::string($existing)) != $fingerprint {
            RETURN {error: 'changed write authority'};
        };
        LET $written = ("""
        + upsert
        + """);
        RETURN {row: $written[0]};
        };
        RETURN $result;
        COMMIT TRANSACTION;
        """,
        uuid=entity.id,
        fingerprint=fingerprint,
        rows=[record],
    )
    if len(results) == 1 and results[0].get("error") == "changed write authority":
        raise ValueError("Entity identity already exists with changed write authority")
    if len(results) != 1:
        raise RuntimeError("Protected entity write returned no transaction receipt")
    written_row = results[0].get("row")
    if not isinstance(written_row, dict):
        raise RuntimeError("Protected entity write returned no transaction receipt")
    result = entity_from_surreal_row(written_row)
    if result.id != entity.id or type(result.revision) is not int or result.revision < 1:
        raise RuntimeError("Protected entity write returned an invalid transaction receipt")
    return result
