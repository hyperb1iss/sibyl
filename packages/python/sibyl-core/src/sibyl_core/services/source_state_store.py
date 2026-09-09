"""Read source rows and their durable generations in one store transaction."""

from __future__ import annotations

from dataclasses import dataclass

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.memory_pipeline.observations import (
    SourceIdentity,
    SourceKind,
    SourceObservation,
    evidence_hash,
)
from sibyl_core.services.content_models import RawMemory, raw_memory_from_record
from sibyl_core.services.graph_records import entity_from_surreal_row
from sibyl_core.services.source_observations import (
    GraphSourceSnapshot,
    SourceUnavailableError,
    graph_evidence,
)


@dataclass(frozen=True, slots=True)
class RawSourceSnapshot:
    memory: RawMemory
    observation: SourceObservation


async def load_source_snapshot(
    source: SourceIdentity,
    *,
    organization_id: str,
    execute_query: SurrealExecute,
) -> GraphSourceSnapshot | RawSourceSnapshot | None:
    """Bind data and state locally; callers still enforce current read authority.

    Missing ledgers are never reconstructed from a mutable row revision. This
    also fails closed for data imported without its retained source state.
    """
    if source.organization_id != organization_id:
        raise SourceUnavailableError()
    table, org_field = (
        ("entity", "group_id")
        if source.kind is SourceKind.GRAPH_ENTITY
        else ("raw_captures", "organization_id")
    )
    rows = normalize_records(
        await execute_query(
            f"""RETURN {{
            LET $row = (SELECT * FROM {table}
                WHERE uuid = $uuid AND {org_field} = $org LIMIT 1)[0];
            LET $state = (SELECT * FROM type::record(string::concat('source_states:', crypto::sha256(type::string([$org, $kind, $uuid])))))[0];
            RETURN {{ source_row: $row, source_state: $state }};
        }};""",
            uuid=source.id,
            org=source.organization_id,
            kind=source.kind.value,
        )
    )
    if len(rows) != 1:
        return None
    row, state = rows[0].get("source_row"), rows[0].get("source_state")
    if not isinstance(row, dict) or not isinstance(state, dict):
        return None
    if (
        state.get("organization_id") != source.organization_id
        or state.get("source_kind") != source.kind.value
        or state.get("source_id") != source.id
        or state.get("deleted") is not False
        or type(state.get("revision")) is not int
        or type(row.get("revision")) is not int
        or state["revision"] != row["revision"]
        or type(state.get("generation")) is not int
        or state["generation"] <= 0
        or not isinstance(state.get("incarnation"), str)
        or not state["incarnation"]
    ):
        return None
    if source.kind is SourceKind.GRAPH_ENTITY:
        entity = entity_from_surreal_row(row)
        return GraphSourceSnapshot(
            entity,
            SourceObservation(
                source=source,
                generation=state["generation"],
                incarnation=state["incarnation"],
                revision=state["revision"],
                content_sha256=graph_evidence(entity),
                durable=True,
            ),
        )
    memory = raw_memory_from_record(row)
    return RawSourceSnapshot(
        memory,
        SourceObservation(
            source=source,
            generation=state["generation"],
            incarnation=state["incarnation"],
            revision=state["revision"],
            content_sha256=evidence_hash({"version": 1, "raw_content": memory.raw_content}),
            durable=True,
        ),
    )
