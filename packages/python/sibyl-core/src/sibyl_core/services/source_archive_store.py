"""Source-local archive snapshots and checked restoration through existing stores."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.source_integrity import (
    ArchiveDatetime,
    build_integrity_archive,
    native_archive_parameters,
)


def _source_table(kind: SourceKind) -> tuple[str, str]:
    if kind is SourceKind.GRAPH_ENTITY:
        return "entity", "group_id"
    return "raw_captures", "organization_id"


def _graph_auxiliary_snapshot_sql() -> str:
    return """LET $graph_auxiliary = {
        episode: (SELECT *, type::string(id) AS archive_record_key OMIT id
            FROM episode WHERE group_id IN $organizations ORDER BY uuid DESC, archive_record_key),
        relates_to: (SELECT *, type::string(id) AS archive_record_key,
            type::string(in) AS source_record_key, type::string(out) AS target_record_key OMIT id, in, out
            FROM relates_to WHERE group_id IN $organizations ORDER BY created_at DESC, uuid DESC, archive_record_key),
        mentions: (SELECT *, type::string(id) AS archive_record_key,
            type::string(in) AS source_record_key, type::string(out) AS target_record_key OMIT id, in, out
            FROM mentions WHERE group_id IN $organizations ORDER BY uuid DESC, archive_record_key)
    };"""


async def read_source_archive_snapshot(
    execute_query: SurrealExecute,
    *,
    kind: SourceKind,
    organizations: list[str] | None,
    include_graph_auxiliary: bool = False,
) -> dict[str, Any]:
    """Read live sources, absent-source history, and target associations together."""
    table, org_field = _source_table(kind)
    row_scope = f"{org_field} IN $organizations" if organizations is not None else "true"
    ledger_scope = "organization_id IN $organizations" if organizations is not None else "true"
    if include_graph_auxiliary and (kind is not SourceKind.GRAPH_ENTITY or organizations is None):
        raise ValueError("graph companion inventory requires explicit organization scope")
    companion_sql = _graph_auxiliary_snapshot_sql() if include_graph_auxiliary else ""
    companion_field = ", graph_auxiliary: $graph_auxiliary" if include_graph_auxiliary else ""
    snapshot_sql = f"""LET $rows = SELECT *, type::string(id) AS archive_record_key OMIT id
        FROM {table} WHERE {row_scope} ORDER BY {org_field}, uuid;
        LET $states = SELECT * OMIT id FROM source_states
            WHERE {ledger_scope} AND source_kind=$kind ORDER BY organization_id, source_id;
        LET $derivations = SELECT * OMIT id FROM memory_derivations
            WHERE {ledger_scope} AND target_kind=$kind ORDER BY organization_id, target_id;
        {companion_sql}
        LET $snapshot = {{ source_rows: $rows, source_states: $states,
            derivations: $derivations {companion_field} }};"""
    result = normalize_records(
        await execute_query(
            f"RETURN {{ {snapshot_sql} RETURN {{snapshot: $snapshot, fingerprint: crypto::sha256(type::string($snapshot))}}; }};",
            organizations=organizations,
            kind=kind.value,
        )
    )
    if len(result) != 1:
        raise ValueError("source archive snapshot returned an invalid shape")
    snapshot = result[0].get("snapshot")
    fingerprint = result[0].get("fingerprint")
    if (
        not isinstance(snapshot, dict)
        or not isinstance(fingerprint, str)
        or any(
            not isinstance(snapshot.get(key), list)
            for key in ("source_rows", "source_states", "derivations")
        )
    ):
        raise ValueError("source archive snapshot returned an invalid shape")

    # The SDK truncates native nanoseconds. Capture typed datetime text only
    # after proving the second source-local read matches the original snapshot.
    dates: list[tuple[Any, str | int, str]] = []

    def collect(value: Any, path: str) -> None:
        if isinstance(value, dict | list):
            items = value.items() if isinstance(value, dict) else enumerate(value)
            for key, item in items:
                if isinstance(key, int):
                    child = f"{path}[{key}]"
                else:
                    escaped = key.replace("\\", "\\\\").replace("`", "\\`")
                    child = f"{path}.`{escaped}`" if path else f"`{escaped}`"
                if isinstance(item, datetime):
                    dates.append((value, key, child))
                else:
                    collect(item, child)

    collect(snapshot, "")
    if dates:
        captured = normalize_records(
            await execute_query(
                f"""RETURN {{ {snapshot_sql}
                IF crypto::sha256(type::string($snapshot)) != $fingerprint {{
                    THROW 'archive source changed during datetime capture';
                }};
                RETURN SELECT array::map($paths, |$path| type::string(type::field($path))) AS datetimes
                    FROM ONLY $snapshot;
            }};""",
                organizations=organizations,
                kind=kind.value,
                fingerprint=fingerprint,
                paths=[path for _, _, path in dates],
            )
        )
        texts = captured[0].get("datetimes") if len(captured) == 1 else None
        if not isinstance(texts, list) or len(texts) != len(dates):
            raise ValueError("source archive datetime capture returned an invalid shape")
        for (parent, key, _), text in zip(dates, texts, strict=True):
            parent[key] = ArchiveDatetime.parse(text)
    snapshot["fingerprint"] = fingerprint
    return snapshot


async def export_source_integrity(
    execute_query: SurrealExecute, *, kind: SourceKind, organizations: list[str] | None
) -> dict[str, Any]:
    snapshot = await read_source_archive_snapshot(
        execute_query, kind=kind, organizations=organizations
    )
    if organizations is None:
        organizations = sorted({state["organization_id"] for state in snapshot["source_states"]})
    return build_integrity_archive(
        kind=kind,
        organizations=organizations,
        source_rows=snapshot["source_rows"],
        source_states=snapshot["source_states"],
        derivations=snapshot["derivations"],
    )


async def restore_source_integrity(
    execute_query: SurrealExecute,
    payload: object,
    *,
    kind: SourceKind,
    organizations: list[str],
    clean: bool = False,
    skip_existing: bool = True,
    global_scope: bool = False,
    clean_graph_auxiliary: bool = False,
    auxiliary_preconditions: str = "",
    auxiliary_statements: str = "",
    auxiliary_parameters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Restore complete source units without lowering destination trust history."""
    from copy import deepcopy

    from sibyl_core.migrate.source_integrity import validate_integrity_archive

    if clean_graph_auxiliary and (not clean or kind is not SourceKind.GRAPH_ENTITY):
        raise ValueError("graph companion cleanup requires a scoped clean graph restore")
    if global_scope and kind is not SourceKind.RAW_CAPTURE:
        raise ValueError("global restore scope is only supported for the shared raw source store")
    rows, states, associations = validate_integrity_archive(
        payload, kind=kind, organizations=organizations
    )
    before = await read_source_archive_snapshot(
        execute_query,
        kind=kind,
        organizations=None if global_scope else organizations,
        include_graph_auxiliary=clean_graph_auxiliary,
    )
    table, org_field = _source_table(kind)
    row_scope = "true" if global_scope else f"{org_field} IN $organizations"
    ledger_scope = "true" if global_scope else "organization_id IN $organizations"

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
    companion_sql = _graph_auxiliary_snapshot_sql() if clean_graph_auxiliary else ""
    companion_field = ", graph_auxiliary: $graph_auxiliary" if clean_graph_auxiliary else ""
    companion_cleanup = (
        """FOR $row IN array::concat($graph_auxiliary.relates_to,
        $graph_auxiliary.mentions, $graph_auxiliary.episode) { DELETE type::record($row.archive_record_key); };"""
        if clean_graph_auxiliary
        else ""
    )
    query = f"""BEGIN TRANSACTION;
        LET $rows = SELECT *, type::string(id) AS archive_record_key OMIT id
            FROM {table} WHERE {row_scope} ORDER BY {org_field}, uuid;
        LET $states = SELECT * OMIT id FROM source_states
            WHERE {ledger_scope} AND source_kind=$kind
            ORDER BY organization_id, source_id;
        LET $associations = SELECT * OMIT id FROM memory_derivations
            WHERE {ledger_scope} AND target_kind=$kind
            ORDER BY organization_id, target_id;
        {companion_sql}
        IF crypto::sha256(type::string({{ source_rows: $rows, source_states: $states, derivations: $associations {companion_field} }})) != $expected {{
            THROW 'archive destination changed before restore';
        }};
        {auxiliary_preconditions}
        {companion_cleanup}
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
        {auxiliary_statements}
        COMMIT TRANSACTION;
    """
    extra = auxiliary_parameters or {}
    if set(extra) & {
        "organizations",
        "kind",
        "expected",
        "deletes",
        "writes",
        "ledger_writes",
        "association_writes",
    }:
        raise ValueError("archive auxiliary parameters conflict with source bindings")
    await execute_query(
        query,
        organizations=organizations,
        kind=kind.value,
        expected=before["fingerprint"],
        deletes=deletes,
        writes=native_archive_parameters(writes),
        ledger_writes=ledger_writes,
        association_writes=association_writes,
        **extra,
    )
    return {"restored_source_ids": restored, "conflicts": conflicts}
