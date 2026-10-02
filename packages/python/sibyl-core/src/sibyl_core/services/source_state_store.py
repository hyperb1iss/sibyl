"""Read source rows and their durable generations in one store transaction."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NotRequired, TypedDict, cast

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
    return source_snapshot_from_records(
        source, rows[0].get("source_row"), rows[0].get("source_state")
    )


def source_snapshot_from_records(
    source: SourceIdentity, row: object, state: object
) -> GraphSourceSnapshot | RawSourceSnapshot | None:
    """Decode a source and ledger captured together by a scoped snapshot owner."""
    if source.kind not in (SourceKind.GRAPH_ENTITY, SourceKind.RAW_CAPTURE):
        return None
    if not isinstance(row, dict) or not isinstance(state, dict):
        return None
    if (
        row.get("uuid") != source.id
        or row.get("group_id" if source.kind is SourceKind.GRAPH_ENTITY else "organization_id")
        != source.organization_id
        or state.get("organization_id") != source.organization_id
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


# The engine computes these hashes before any SDK/model normalization.
def _native_source_selection(kind: SourceKind) -> str:
    table, organization = (
        ("entity", "group_id")
        if kind is SourceKind.GRAPH_ENTITY
        else ("raw_captures", "organization_id")
    )
    association = (
        "(SELECT * OMIT validation_write_witness FROM $associations[0])[0]"
        if kind is SourceKind.GRAPH_ENTITY
        else "$associations[0]"
    )
    association_scope = "" if kind is SourceKind.GRAPH_ENTITY else "organization_id=$org AND "
    return f"""
        LET $sources=(SELECT * FROM {table} WHERE (uuid=$uuid AND {organization}=$org)
            OR id=type::record('{table}',$uuid));
        LET $state_key=type::record(string::concat('source_states:',
            crypto::sha256(type::string([$org,$kind,$uuid]))));
        LET $states=(SELECT * FROM source_states WHERE (organization_id=$org
            AND source_kind=$kind AND source_id=$uuid) OR id=$state_key);
        LET $associations=(SELECT * FROM memory_derivations
            WHERE {association_scope}target_kind=$kind AND target_id=$uuid);
        IF array::len($sources)!=1 OR array::len($states)!=1
            OR array::len($associations)>1 {{
            THROW 'publication_source_cardinality';
        }};
        IF array::len($associations)=1 AND $associations[0].organization_id!=$org {{
            THROW 'publication_source_association';
        }};
        LET $source=$sources[0];
        LET $state=$states[0];
        IF $source.uuid!=$uuid OR $source.{organization}!=$org
            OR $state.id!=$state_key OR $state.organization_id!=$org
            OR $state.source_kind!=$kind OR $state.source_id!=$uuid
            OR $state.deleted!=false OR $state.revision!=$source.revision
            OR !type::is_int($state.revision) OR !type::is_int($state.generation)
            OR $state.generation<1 OR !type::is_string($state.incarnation)
            OR string::len($state.incarnation)=0 {{
            THROW 'publication_source_state';
        }};
        LET $descriptor={{
            row_id:type::string($source.id),
            row_sha256:crypto::sha256(type::string($source)),
            state_id:type::string($state.id),
            state_sha256:crypto::sha256(type::string(
                (SELECT * OMIT validation_write_witness FROM $state)[0])),
            association_id:IF array::len($associations)=1 {{
                type::string($associations[0].id)
            }} ELSE {{ NONE }},
            association_sha256:IF array::len($associations)=1 {{
                crypto::sha256(type::string({association}))
            }} ELSE {{ NONE }}
        }};
    """


class NativeSourceDescriptor(TypedDict):
    row_id: str
    row_sha256: str
    state_id: str
    state_sha256: str
    association_id: NotRequired[str | None]
    association_sha256: NotRequired[str | None]


@dataclass(frozen=True, slots=True)
class NativeSourceCut:
    """Engine-hashed physical evidence, distinct from a semantic snapshot."""

    source: SourceIdentity
    descriptor: NativeSourceDescriptor
    snapshot: GraphSourceSnapshot | RawSourceSnapshot
    association: dict[str, object] | None


async def load_native_source_cut(
    source: SourceIdentity, *, execute_query: SurrealExecute
) -> NativeSourceCut:
    """Require exact physical cardinality and retained State without healing."""
    rows = normalize_records(
        await execute_query(
            "RETURN {"
            + _native_source_selection(source.kind)
            + "RETURN {source_row:$source,source_state:$state,"
            "association:$associations[0],descriptor:$descriptor}; };",
            org=source.organization_id,
            uuid=source.id,
            kind=source.kind.value,
        )
    )
    if len(rows) != 1 or not isinstance(rows[0].get("descriptor"), dict):
        raise SourceUnavailableError()
    snapshot = source_snapshot_from_records(
        source, rows[0].get("source_row"), rows[0].get("source_state")
    )
    if snapshot is None:
        raise SourceUnavailableError()
    association = rows[0].get("association")
    if association is not None and not isinstance(association, dict):
        raise SourceUnavailableError()
    descriptor = rows[0]["descriptor"]
    if not isinstance(descriptor, dict) or set(descriptor) - {
        "row_id",
        "row_sha256",
        "state_id",
        "state_sha256",
        "association_id",
        "association_sha256",
    }:
        raise SourceUnavailableError()
    for name in ("row_id", "state_id", "row_sha256", "state_sha256"):
        if not isinstance(descriptor.get(name), str) or not descriptor[name]:
            raise SourceUnavailableError()
    for name in ("association_id", "association_sha256"):
        if descriptor.get(name) is not None and not isinstance(descriptor[name], str):
            raise SourceUnavailableError()
    for name in ("row_sha256", "state_sha256", "association_sha256"):
        if (
            descriptor.get(name) is not None
            and re.fullmatch(r"[0-9a-f]{64}", descriptor[name]) is None
        ):
            raise SourceUnavailableError()
    return NativeSourceCut(
        source, cast("NativeSourceDescriptor", descriptor), snapshot, association
    )


async def check_native_source_cuts(
    cuts: list[NativeSourceCut], *, execute_query: SurrealExecute, witness: bool = False
) -> None:
    """Check all cuts before writing, selecting native mutation IDs server-side."""
    if not cuts:
        return
    kind = cuts[0].source.kind
    org = cuts[0].source.organization_id
    if any(cut.source.kind is not kind or cut.source.organization_id != org for cut in cuts):
        raise SourceUnavailableError()
    mutation = ""
    if witness:
        from sibyl_core.backends.surreal.schema_source_witness import SOURCE_STATE_WRITE_WITNESS

        mutation = "LET $source_states_to_fence=[$state];" + SOURCE_STATE_WRITE_WITNESS
        if kind is SourceKind.GRAPH_ENTITY:
            mutation += """
                IF array::len($associations)=1 {
                    UPDATE $associations[0].id SET validation_write_witness=type::string(rand::uuid());
                };
            """
    await execute_query(
        "FOR $expected IN $native_cuts { LET $uuid=$expected.uuid;"
        + _native_source_selection(kind)
        + "IF $descriptor!=$expected.descriptor { THROW 'publication_native_cut_changed'; };"
        + mutation
        + "};",
        org=org,
        kind=kind.value,
        native_cuts=[{"uuid": cut.source.id, "descriptor": cut.descriptor} for cut in cuts],
    )
