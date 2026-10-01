"""Upgrade retained ledgers before any repair can create a new incarnation."""

from functools import partial

from sibyl_core.backends.surreal.schema_invariants import fetch_declared_fields
from sibyl_core.backends.surreal.schema_ownership import SchemaOwnership
from sibyl_core.backends.surreal.schema_source_states import (
    SOURCE_STATE_DEFINITIONS,
    source_state_event,
)
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.memory_pipeline.observations import SourceKind


async def prepare_source_integrity_upgrade(
    execute_query: SurrealExecute, *, ownership: SchemaOwnership | None = None
) -> None:
    """Fence legacy identity assignment before historical source backfills.

    The field declaration is the durable completion marker. Later bootstraps
    never assign legacy identities to missing or corrupt incarnation values.
    """
    mutate = ownership.mutate if ownership is not None else execute_query
    await mutate(SOURCE_STATE_DEFINITIONS)
    fields = await fetch_declared_fields(execute_query, "source_states")
    if "incarnation" in fields:
        return
    body = """
        DEFINE FIELD incarnation ON source_states TYPE option<string>
            DEFAULT type::string(rand::uuid());
        UPDATE source_states SET incarnation = string::concat('legacy-v1:',
            crypto::sha256(organization_id), ':', source_kind, ':', crypto::sha256(source_id))
            WHERE incarnation = NONE RETURN NONE;
    """
    if ownership is not None:
        await ownership.mutate(body)
    else:
        await execute_query("BEGIN TRANSACTION; " + body + " COMMIT TRANSACTION;")


def source_derivation_event(kind: SourceKind) -> str:
    """Fence association publication on each retained target source ledger."""
    table = "entity" if kind is SourceKind.GRAPH_ENTITY else "raw_captures"
    organization_field = "group_id" if kind is SourceKind.GRAPH_ENTITY else "organization_id"
    return f"""
DEFINE EVENT OVERWRITE require_target_derivation ON memory_derivations WHEN true THEN {{
    LET $targets = array::distinct(array::concat(
        IF $event IN ['UPDATE', 'DELETE'] THEN [{{
            organization_id: $before.organization_id,
            target_kind: $before.target_kind, target_id: $before.target_id
        }}] ELSE [] END,
        IF $event IN ['CREATE', 'UPDATE'] THEN [{{
            organization_id: $after.organization_id,
            target_kind: $after.target_kind, target_id: $after.target_id
        }}] ELSE [] END
    ));
    FOR $target IN $targets {{
        IF $target.target_kind != '{kind.value}' {{ THROW 'derivation target kind mismatch'; }};
        LET $state = (SELECT * FROM source_states
            WHERE organization_id = $target.organization_id
                AND source_kind = $target.target_kind AND source_id = $target.target_id)[0];
        IF $state = NONE {{
            -- Removing an orphan adds no authority; a new binding needs retained history.
            LET $publishing = $event != 'DELETE'
                AND $target.organization_id = $after.organization_id
                AND $target.target_kind = $after.target_kind
                AND $target.target_id = $after.target_id;
            LET $physical = (SELECT VALUE id FROM {table}
                WHERE {organization_field} = $target.organization_id
                    AND uuid = $target.target_id)[0];
            IF $publishing OR $physical != NONE {{
                THROW 'derivation target source state is missing';
            }};
        }} ELSE {{
            UPDATE $state.id SET validation_write_witness = type::string(rand::uuid());
        }};
    }};
    IF $event IN ['CREATE', 'UPDATE'] {{
        UPDATE {table} SET derivation_required = true
            WHERE uuid = $after.target_id AND {organization_field} = $after.organization_id
                AND derivation_required != true;
    }};
}};
"""


async def migrate_source_integrity(
    execute_query: SurrealExecute,
    *,
    kind: SourceKind,
    ownership: SchemaOwnership | None = None,
) -> None:
    table = "entity" if kind is SourceKind.GRAPH_ENTITY else "raw_captures"
    organization_field = "group_id" if kind is SourceKind.GRAPH_ENTITY else "organization_id"
    event = source_state_event(
        kind,
        retire_derivations=True,
        publication_bookkeeping=kind is SourceKind.RAW_CAPTURE,
        integrity=True,
    ).replace("DEFINE EVENT IF NOT EXISTS", "DEFINE EVENT OVERWRITE")
    body = f"""
        DEFINE FIELD IF NOT EXISTS incarnation ON source_states TYPE option<string>;
        DEFINE FIELD IF NOT EXISTS derivation_required ON {table} TYPE option<bool> DEFAULT false;
        {event}
        DEFINE EVENT OVERWRITE require_target_derivation ON memory_derivations
            WHEN $event IN ['CREATE', 'UPDATE'] THEN {{
                IF $after.target_kind != '{kind.value}' {{ THROW 'derivation target kind mismatch'; }};
                UPDATE {table} SET derivation_required = true
                    WHERE uuid = $after.target_id AND {organization_field} = $after.organization_id
                        AND derivation_required != true;
            }};
        RETURN {{
            LET $associations = SELECT organization_id, target_id FROM memory_derivations
                WHERE target_kind = '{kind.value}';
            FOR $association IN $associations {{
                UPDATE {table} SET derivation_required = true
                    WHERE uuid = $association.target_id
                        AND {organization_field} = $association.organization_id
                        AND derivation_required != true;
            }};
            RETURN NONE;
        }};
    """
    if ownership is not None:
        await ownership.mutate(body)
    else:
        await execute_query("BEGIN TRANSACTION; " + body + " COMMIT TRANSACTION;")


migrate_graph_source_integrity = partial(migrate_source_integrity, kind=SourceKind.GRAPH_ENTITY)
