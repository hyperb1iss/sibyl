"""Source-local archive snapshots and checked restoration through existing stores."""

from __future__ import annotations

from typing import Any

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.source_integrity import build_integrity_archive


def _source_table(kind: SourceKind) -> tuple[str, str]:
    if kind is SourceKind.GRAPH_ENTITY:
        return "entity", "group_id"
    return "raw_captures", "organization_id"


async def read_source_archive_snapshot(
    execute_query: SurrealExecute, *, kind: SourceKind, organizations: list[str] | None
) -> dict[str, Any]:
    """Read live sources, absent-source history, and target associations together."""
    table, org_field = _source_table(kind)
    row_scope = f"{org_field} IN $organizations" if organizations is not None else "true"
    ledger_scope = "organization_id IN $organizations" if organizations is not None else "true"
    result = normalize_records(
        await execute_query(
            f"""RETURN {{
            LET $rows = SELECT *, type::string(id) AS archive_record_key OMIT id
                FROM {table} WHERE {row_scope}
                ORDER BY {org_field}, uuid;
            LET $states = SELECT * OMIT id FROM source_states
                WHERE {ledger_scope} AND source_kind=$kind
                ORDER BY organization_id, source_id;
            LET $derivations = SELECT * OMIT id FROM memory_derivations
                WHERE {ledger_scope} AND target_kind=$kind
                ORDER BY organization_id, target_id;
            RETURN {{ source_rows: $rows, source_states: $states, derivations: $derivations }};
        }};""",
            organizations=organizations,
            kind=kind.value,
        )
    )
    if len(result) != 1 or any(
        not isinstance(result[0].get(key), list)
        for key in ("source_rows", "source_states", "derivations")
    ):
        raise ValueError("source archive snapshot returned an invalid shape")
    return result[0]


async def export_source_integrity(
    execute_query: SurrealExecute, *, kind: SourceKind, organizations: list[str] | None
) -> dict[str, Any]:
    snapshot = await read_source_archive_snapshot(
        execute_query, kind=kind, organizations=organizations
    )
    if organizations is None:
        organizations = sorted({state["organization_id"] for state in snapshot["source_states"]})
    return build_integrity_archive(kind=kind, organizations=organizations, **snapshot)


async def restore_source_integrity(
    execute_query: SurrealExecute,
    payload: object,
    *,
    kind: SourceKind,
    organizations: list[str],
    clean: bool = False,
    skip_existing: bool = True,
) -> dict[str, Any]:
    """Restore complete source units without lowering destination trust history."""
    from copy import deepcopy

    from sibyl_core.migrate.source_integrity import validate_integrity_archive

    rows, states, associations = validate_integrity_archive(
        payload, kind=kind, organizations=organizations
    )
    before = await read_source_archive_snapshot(
        execute_query, kind=kind, organizations=organizations
    )
    table, org_field = _source_table(kind)

    def row_key(row):
        return row[org_field], row["uuid"]

    def state_key(state):
        return state["organization_id"], state["source_id"]

    def association_key(association):
        return association["organization_id"], association["target_id"]

    old_rows = {row_key(row): row for row in before["source_rows"]}
    old_states = {state_key(state): state for state in before["source_states"]}
    old_associations = {association_key(row): row for row in before["derivations"]}
    incoming_rows = {row_key(row): row for row in rows}
    incoming_states = {state_key(state): state for state in states}
    incoming_associations = {association_key(row): row for row in associations}
    writes: list[dict[str, Any]] = []
    ledger_writes: list[dict[str, Any]] = []
    association_writes: list[dict[str, Any]] = []
    deletes: list[str] = []
    conflicts: list[dict[str, str]] = []
    restored: list[str] = []

    for key, incoming_state in incoming_states.items():
        row = incoming_rows.get(key)
        previous = old_states.get(key)
        previous_row = old_rows.get(key)
        prior_association = old_associations.get(key)
        fresh = previous is None and previous_row is None and prior_association is None
        same_state = previous is not None and all(
            previous.get(field) == incoming_state.get(field)
            for field in ("incarnation", "generation", "deleted")
        )
        forward = previous is not None and (
            previous["incarnation"] == incoming_state["incarnation"]
            and previous["generation"] < incoming_state["generation"]
            and not previous["deleted"]
        )
        trusted = fresh or ((same_state or forward) and (row is None or previous_row is not None))
        if previous is not None and previous["deleted"] and row is not None:
            conflicts.append(
                {"organization_id": key[0], "source_id": key[1], "reason": "retained_tombstone"}
            )
            continue
        if row is None:
            if fresh or forward:
                if previous_row is not None:
                    deletes.append(previous_row["archive_record_key"])
                ledger_writes.append(incoming_state)
            elif not same_state:
                conflicts.append(
                    {
                        "organization_id": key[0],
                        "source_id": key[1],
                        "reason": "retained_high_water",
                    }
                )
                continue
        elif previous_row is not None and (skip_existing or same_state):
            if not same_state:
                conflicts.append(
                    {
                        "organization_id": key[0],
                        "source_id": key[1],
                        "reason": "existing_source_preserved",
                    }
                )
                continue
        else:
            record = deepcopy(row)
            record_key = record.pop("archive_record_key", None)
            if not isinstance(record_key, str) or not record_key.startswith(table + ":"):
                raise ValueError("archive source record key does not match its table")
            if previous_row is not None:
                record_key = previous_row["archive_record_key"]
                record["revision"] = max(record["revision"], previous_row["revision"] + 1)
            if (
                key in incoming_associations
                or prior_association is not None
                or (previous_row or {}).get("derivation_required") is True
            ):
                record["derivation_required"] = True
            writes.append({"key": record_key, "record": record})
            restored.append(key[1])
            if trusted:
                ledger_writes.append({**incoming_state, "revision": record["revision"]})
            else:
                if prior_association is not None:
                    association_writes.append({**prior_association, "active": False})
                conflicts.append(
                    {
                        "organization_id": key[0],
                        "source_id": key[1],
                        "reason": "incoming_lineage_not_adopted",
                    }
                )
        association = incoming_associations.get(key)
        if association is not None and trusted:
            if prior_association is not None and prior_association["active"] is False:
                continue
            association_writes.append(association)

    if clean:
        deletes.extend(
            row["archive_record_key"] for key, row in old_rows.items() if key not in incoming_states
        )
    query = f"""BEGIN TRANSACTION;
        LET $rows = SELECT *, type::string(id) AS archive_record_key OMIT id
            FROM {table} WHERE {org_field} IN $organizations ORDER BY {org_field}, uuid;
        LET $states = SELECT * OMIT id FROM source_states
            WHERE organization_id IN $organizations AND source_kind=$kind
            ORDER BY organization_id, source_id;
        LET $associations = SELECT * OMIT id FROM memory_derivations
            WHERE organization_id IN $organizations AND target_kind=$kind
            ORDER BY organization_id, target_id;
        IF {{ source_rows: $rows, source_states: $states, derivations: $associations }} != $expected {{
            THROW 'archive destination changed before restore';
        }};
        FOR $key IN $deletes {{ DELETE type::record($key); }};
        FOR $entry IN $writes {{
            LET $existing = (SELECT * FROM type::record($entry.key))[0];
            IF $existing != NONE AND ($existing.{org_field} != $entry.record.{org_field}
                OR $existing.uuid != $entry.record.uuid) {{
                THROW 'archive physical record identity conflicts with destination';
            }};
            UPSERT type::record($entry.key) CONTENT $entry.record;
        }};
        FOR $state IN $ledger_writes {{
            LET $key = type::record(string::concat('source_states:',
                crypto::sha256(type::string([$state.organization_id, $kind, $state.source_id]))));
            UPSERT $key CONTENT $state;
        }};
        FOR $association IN $association_writes {{
            LET $existing = (SELECT id FROM memory_derivations
                WHERE organization_id=$association.organization_id AND target_kind=$kind
                    AND target_id=$association.target_id LIMIT 1)[0];
            IF $existing = NONE {{ CREATE memory_derivations CONTENT $association; }}
            ELSE {{ UPDATE $existing.id CONTENT $association; }};
        }};
        COMMIT TRANSACTION;
    """
    await execute_query(
        query,
        organizations=organizations,
        kind=kind.value,
        expected=before,
        deletes=deletes,
        writes=writes,
        ledger_writes=ledger_writes,
        association_writes=association_writes,
    )
    return {"restored_source_ids": restored, "conflicts": conflicts}
