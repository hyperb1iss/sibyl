"""Source-local archive snapshots and checked restoration through existing stores."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sibyl_core.backends.surreal.records import normalize_records
from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.embeddings.provenance import mark_unverified_vector
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.source_integrity import (
    ArchiveDatetime,
    build_integrity_archive,
    native_archive_parameters,
)


def source_table(kind: SourceKind) -> tuple[str, str]:
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


type DatetimePath = tuple[Any, str | int, str]


def datetime_paths(root: Any) -> list[DatetimePath]:
    """Every datetime under ``root``: its parent, its key, and its SurrealQL field path."""
    dates: list[DatetimePath] = []

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

    collect(root, "")
    return dates


def apply_datetime_texts(dates: list[DatetimePath], texts: object) -> None:
    """Replace each truncated SDK datetime with the server's own text for it."""
    if not isinstance(texts, list) or len(texts) != len(dates):
        raise ValueError("source archive datetime capture returned an invalid shape")
    for (parent, key, _), text in zip(dates, texts, strict=True):
        parent[key] = ArchiveDatetime.parse(text)


async def read_source_archive_snapshot(
    execute_query: SurrealExecute,
    *,
    kind: SourceKind,
    organizations: list[str] | None,
    include_graph_auxiliary: bool = False,
) -> dict[str, Any]:
    """Read live sources, absent-source history, and target associations together."""
    table, org_field = source_table(kind)
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
    dates = datetime_paths(snapshot)
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
        apply_datetime_texts(dates, captured[0].get("datetimes") if len(captured) == 1 else None)
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


def _source_visibility(row: dict[str, Any], kind: SourceKind) -> tuple[bool, tuple[object, ...]]:
    """Read lifecycle and audience through the same adapters used by source reads."""
    from sibyl_core.memory_pipeline.lifecycle import (
        graph_metadata_recallable,
        raw_memory_lifecycle_recallable,
    )
    from sibyl_core.services.content_models import raw_memory_from_record
    from sibyl_core.services.graph_records import _entity_from_row

    if kind is SourceKind.RAW_CAPTURE:
        memory = raw_memory_from_record(row)
        return (
            memory.deleted_at is None and raw_memory_lifecycle_recallable(memory),
            (
                memory.principal_id,
                memory.memory_scope.value,
                memory.scope_key,
                memory.project_id,
                memory.agent_id,
            ),
        )
    entity = _entity_from_row(row)
    return graph_metadata_recallable(entity.metadata), tuple(
        entity.metadata.get(key) or None
        for key in ("principal_id", "memory_scope", "scope_key", "project_id", "agent_id")
    )


def _mark_unverified_entity_vector(record: dict[str, Any]) -> None:
    """An archived vector that does not name its model is not trusted as current.

    Archives written since vectors were stamped carry the stamp in the row's
    attributes and keep it, so the embedding sweep compares it against the
    configured provider like any other row. Older archives carry none.
    """
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        attributes = {}
        record["attributes"] = attributes
    mark_unverified_vector(attributes, has_vector=bool(record.get("name_embedding")))


# A restore carries at most this many source units, companions, or deleted rows
# in one request and one transaction, whatever the size of the organization.
ARCHIVE_RESTORE_BATCH_SIZE = 200

_DESTINATION_CHANGED = "archive destination changed before restore"


def restore_batches[T](items: Sequence[T]) -> list[list[T]]:
    """Cut restore work into the bounded batches one transaction carries."""
    size = ARCHIVE_RESTORE_BATCH_SIZE
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


@dataclass
class _UnitDecision:
    """What restoring one source unit does to the destination."""

    delete: str | None = None
    write: dict[str, Any] | None = None
    ledger: dict[str, Any] | None = None
    association: dict[str, Any] | None = None
    retire_association: bool = False
    conflict: str | None = None


def _decide_unit(
    *,
    kind: SourceKind,
    record_key: str | None,
    incoming_state: dict[str, Any],
    row: dict[str, Any] | None,
    association: dict[str, Any] | None,
    previous: dict[str, Any] | None,
    previous_row: dict[str, Any] | None,
    prior_association: dict[str, Any] | None,
    skip_existing: bool,
) -> _UnitDecision:
    """Decide one unit against its destination state, never lowering trust history."""
    from copy import deepcopy

    fresh = previous is None and previous_row is None and prior_association is None
    same_state = previous is not None and all(
        previous.get(name) == incoming_state.get(name)
        for name in ("incarnation", "generation", "deleted")
    )
    forward = previous is not None and (
        previous["incarnation"] == incoming_state["incarnation"]
        and previous["generation"] < incoming_state["generation"]
        and not previous["deleted"]
    )
    trusted = fresh or ((same_state or forward) and (row is None or previous_row is not None))
    if previous is not None and previous["deleted"] and row is not None:
        return _UnitDecision(conflict="retained_tombstone")
    if row is not None and previous_row is not None and not trusted:
        recallable, audience = _source_visibility(previous_row, kind)
        _, incoming_audience = _source_visibility(row, kind)
        if not recallable or audience != incoming_audience:
            return _UnitDecision(
                conflict="retained_source_revocation"
                if not recallable
                else "retained_source_authority"
            )
    decision = _UnitDecision()
    if row is None:
        if fresh or forward:
            if previous_row is not None:
                decision.delete = previous_row["archive_record_key"]
            decision.ledger = incoming_state
        elif not same_state:
            return _UnitDecision(conflict="retained_high_water")
    elif previous_row is not None and (skip_existing or same_state):
        if not same_state:
            return _UnitDecision(conflict="existing_source_preserved")
    else:
        record = deepcopy(row)
        record.pop("archive_record_key", None)
        if previous_row is not None:
            record_key = previous_row["archive_record_key"]
            record["revision"] = max(record["revision"], previous_row["revision"] + 1)
        if (
            association is not None
            or prior_association is not None
            or (previous_row or {}).get("derivation_required") is True
        ):
            record["derivation_required"] = True
        if kind is SourceKind.GRAPH_ENTITY:
            _mark_unverified_entity_vector(record)
        decision.write = {"key": record_key, "record": record}
        if trusted:
            decision.ledger = {**incoming_state, "revision": record["revision"]}
        else:
            decision.retire_association = prior_association is not None
            decision.conflict = "incoming_lineage_not_adopted"
    if association is not None and trusted:
        if prior_association is not None and prior_association["active"] is False:
            return decision
        decision.association = association
    return decision


def _archive_record_key(row: dict[str, Any] | None, table: str) -> str | None:
    if row is None:
        return None
    record_key = row.get("archive_record_key")
    if not isinstance(record_key, str) or not record_key.startswith(table + ":"):
        raise ValueError("archive source record key does not match its table")
    return record_key


def _source_write_statements(org_field: str) -> str:
    # Each loop binds a row's fields to plain variables before filtering on
    # them: SurrealDB 3.2 plans `field = $row.field` as a whole-table scan and
    # `field = $value` as an index lookup.
    return f"""
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
            LET $organization = $association.organization_id;
            LET $target = $association.target_id;
            LET $existing = (SELECT id FROM memory_derivations
                WHERE organization_id=$organization AND target_kind=$kind
                    AND target_id=$target LIMIT 1)[0];
            IF $existing = NONE {{ CREATE memory_derivations CONTENT $association; }}
            ELSE {{ UPDATE $existing.id CONTENT $association; }};
        }};
    """


def _conflict(key: tuple[str, str], reason: str) -> dict[str, str]:
    return {"organization_id": key[0], "source_id": key[1], "reason": reason}


_RECORDS_PROJECTION = "$records.map(|$record| (SELECT * FROM $record)[0])"


@dataclass(frozen=True)
class CleanupPage:
    """Rows a clean restore deletes, with the digest of their planned contents."""

    records: list[Any]
    digest: str
    # The same rows as record-key strings, for the uniqueness preflight.
    keys: list[str] = field(default_factory=list)


async def plan_cleanup(
    execute_query: SurrealExecute,
    table: str,
    *,
    organization_field: str,
    scope: str,
    keep: Callable[[tuple[object, object]], bool],
    **params: Any,
) -> list[CleanupPage]:
    """Find the rows in scope that ``keep`` rejects, one bounded page at a time.

    Pages walk the table in record order and resume after the last id, so a
    page costs the same at any depth. ``WITH NOINDEX`` keeps the plan a scan
    that stops at the page limit rather than an index plan that sorts the
    whole table for every page.
    """
    cursor: object = None
    pages: list[CleanupPage] = []
    while True:
        bound = {**params, "restore_page_size": ARCHIVE_RESTORE_BATCH_SIZE}
        seek = ""
        if cursor is not None:
            seek = " AND id > $restore_cursor"
            bound["restore_cursor"] = cursor
        result = await execute_query(
            f"SELECT id, type::string(id) AS key, {organization_field} AS organization, uuid "
            f"FROM {table} "
            f"WITH NOINDEX WHERE {scope}{seek} ORDER BY id LIMIT $restore_page_size;",
            **bound,
        )
        rows = [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []
        if not rows:
            return pages
        cursor = rows[-1]["id"]
        doomed_rows = [row for row in rows if not keep((row.get("organization"), row.get("uuid")))]
        doomed = [row["id"] for row in doomed_rows]
        if doomed:
            digest = await execute_query(
                f"RETURN crypto::sha256(type::string({_RECORDS_PROJECTION}));", records=doomed
            )
            if not isinstance(digest, str):
                raise ValueError("archive cleanup read returned an invalid shape")
            pages.append(
                CleanupPage(
                    records=doomed, digest=digest, keys=[str(row["key"]) for row in doomed_rows]
                )
            )
        if len(rows) < ARCHIVE_RESTORE_BATCH_SIZE:
            return pages


async def apply_cleanup(
    execute_query: SurrealExecute,
    pages: Sequence[CleanupPage],
    *,
    refusal: str = _DESTINATION_CHANGED,
) -> None:
    """Delete each planned page, refusing one whose rows moved since it was read."""
    for page in pages:
        await execute_query(
            f"""BEGIN TRANSACTION;
            IF crypto::sha256(type::string({_RECORDS_PROJECTION})) != $expected {{
                THROW '{refusal}';
            }};
            FOR $record IN $records {{ DELETE $record; }};
            COMMIT TRANSACTION;""",
            records=page.records,
            expected=page.digest,
        )


def interrupted_restore(error: BaseException) -> str:
    """Describe a restore that failed after its first committed write.

    A lost connection or a concurrent change is worth retrying: the next run
    replans against the destination as it stands. Any other refusal comes
    back the same way until its cause is resolved, and the message says so.
    """
    from sibyl_core.backends.surreal.connection import _is_transient_connection_error

    if (
        isinstance(error, ConnectionError | TimeoutError)
        or _is_transient_connection_error(error)
        or "destination changed before restore" in str(error)
    ):
        return (
            "restore stopped partway; the whole units already written are kept, and "
            f"running the same import again finishes it: {error}"
        )
    return (
        "restore refused partway; the whole units already written are kept, and the "
        f"import is refused the same way until the reported conflict is resolved: {error}"
    )


@dataclass(frozen=True)
class UniqueWrite:
    """A row a restore writes, and how it relates to the destination row it replaces.

    ``target`` is the record key the write lands on. ``identity`` names the
    fields the write replaces a destination row by: a delete-and-create by
    uuid, an upsert by uuid, an update found by a natural key. A destination
    row at ``target`` or sharing ``identity`` is the row being replaced, not
    a collision.
    """

    table: str
    record: Mapping[str, Any]
    target: str | None = None
    identity: tuple[str, ...] = ()


_INDEX_FIELDS = re.compile(r"\b(?:FIELDS|COLUMNS)\s+(.+?)\s+UNIQUE\b")


async def unique_indexes(execute_query: SurrealExecute, table: str) -> list[tuple[str, list[str]]]:
    """Every UNIQUE index the destination enforces on ``table``, read from the live schema."""
    info = await execute_query(f"INFO FOR TABLE {table};")
    indexes = info.get("indexes") if isinstance(info, dict) else None
    if not isinstance(indexes, dict):
        return []
    found = []
    for name, definition in sorted(indexes.items()):
        match = _INDEX_FIELDS.search(str(definition))
        if match:
            found.append((str(name), [part.strip() for part in match.group(1).split(",")]))
    return found


def _field_value(record: Mapping[str, Any], path: str) -> Any:
    value: Any = record
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


async def check_unique_writes(
    execute_query: SurrealExecute, writes: Sequence[UniqueWrite], *, vacated: set[str]
) -> None:
    """Refuse a restore whose writes would collide on any UNIQUE index of the destination.

    The indexes come from the destination itself, so a restore checks exactly
    what the database will enforce, including indexes added after this code.
    Each key is a point lookup a bounded batch at a time; a destination row
    the restore deletes first or replaces under the same identity is not a
    collision.
    """
    by_table: dict[str, list[UniqueWrite]] = {}
    for write in writes:
        by_table.setdefault(write.table, []).append(write)
    for table, table_writes in by_table.items():
        for name, fields in await unique_indexes(execute_query, table):
            keyed = []
            for write in table_writes:
                values = [_field_value(write.record, column) for column in fields]
                # A missing value is never indexed as a duplicate key.
                if all(value is not None for value in values):
                    keyed.append((write, values))
            identity_fields = sorted({column for write, _ in keyed for column in write.identity})
            projection = ", ".join(["type::string(id) AS key", *identity_fields])
            # Each probe carries its own values: the embedded engine does not
            # bind query parameters inside a closure.
            where = " AND ".join(f"{field} = $probe.v{index}" for index, field in enumerate(fields))
            for batch in restore_batches(keyed):
                found = await execute_query(
                    f"RETURN $probes.map(|$probe| (SELECT {projection} FROM {table} "
                    f"WHERE {where} LIMIT 4));",
                    probes=[
                        native_archive_parameters(
                            {f"v{index}": value for index, value in enumerate(values)}
                        )
                        for _, values in batch
                    ],
                )
                if not isinstance(found, list) or len(found) != len(batch):
                    raise ValueError("archive uniqueness read returned an invalid shape")
                for (write, values), rows in zip(batch, found, strict=True):
                    for row in rows if isinstance(rows, list) else []:
                        if not isinstance(row, dict):
                            continue
                        if row.get("key") in vacated or row.get("key") == write.target:
                            continue
                        if write.identity and all(
                            row.get(column) == _field_value(write.record, column)
                            for column in write.identity
                        ):
                            continue
                        key = ", ".join(f"{f}={v!r}" for f, v in zip(fields, values, strict=True))
                        raise ValueError(
                            f"archive {table} row collides with destination row {row.get('key')} "
                            f"on unique index {name} ({key})"
                        )


def _unit_state_projection(table: str, org_field: str) -> str:
    """Everything a source unit's restore decision reads, found by its identity.

    Each unit carries its own kind: the embedded engine does not bind query
    parameters inside a closure.
    """
    return f"""$units.map(|$unit| {{
        row: (SELECT *, type::string(id) AS archive_record_key OMIT id FROM {table}
            WHERE {org_field} = $unit.organization AND uuid = $unit.source LIMIT 1)[0],
        state: (SELECT * OMIT id FROM source_states
            WHERE organization_id = $unit.organization AND source_kind = $unit.kind
                AND source_id = $unit.source LIMIT 1)[0],
        association: (SELECT * OMIT id FROM memory_derivations
            WHERE organization_id = $unit.organization AND target_kind = $unit.kind
                AND target_id = $unit.source LIMIT 1)[0]
    }})"""


@dataclass
class _UnitBatch:
    """One bounded transaction of source units and the state they were planned on."""

    units: list[dict[str, str]]
    digest: str
    deletes: list[str] = field(default_factory=list)
    writes: list[dict[str, Any]] = field(default_factory=list)
    ledger_writes: list[dict[str, Any]] = field(default_factory=list)
    association_writes: list[dict[str, Any]] = field(default_factory=list)
    retired_associations: list[dict[str, str]] = field(default_factory=list)

    def changes_anything(self) -> bool:
        return bool(
            self.deletes
            or self.writes
            or self.ledger_writes
            or self.association_writes
            or self.retired_associations
        )


@dataclass
class SourceRestorePlan:
    """Every write a restore makes, decided before the first one is sent."""

    kind: SourceKind
    cleanup: list[CleanupPage]
    batches: list[_UnitBatch]
    restored: list[str]
    conflicts: list[dict[str, str]]
    # Archive identities that have a source row once the restore lands, and
    # the ones that do not (deleted or refused here, whatever the destination
    # holds for them now).
    present: set[tuple[str, str]]
    absent: set[tuple[str, str]] = field(default_factory=set)
    # Every row this restore writes, for the uniqueness preflight, and the
    # record keys of the destination rows it deletes before writing.
    unique_writes: list[UniqueWrite] = field(default_factory=list)
    vacated: set[str] = field(default_factory=set)


async def plan_source_restore(
    execute_query: SurrealExecute,
    payload: object,
    *,
    kind: SourceKind,
    organizations: list[str],
    clean: bool = False,
    skip_existing: bool = True,
    global_scope: bool = False,
    clean_graph_auxiliary: bool = False,
) -> SourceRestorePlan:
    """Read the destination a bounded batch at a time and decide every write.

    Nothing is written. An archive the destination refuses, such as one whose
    row lands on a physical key another source holds, fails here.
    """
    from sibyl_core.migrate.source_integrity import validate_integrity_archive

    if clean_graph_auxiliary and (not clean or kind is not SourceKind.GRAPH_ENTITY):
        raise ValueError("graph companion cleanup requires a scoped clean graph restore")
    if global_scope and kind is not SourceKind.RAW_CAPTURE:
        raise ValueError("global restore scope is only supported for the shared raw source store")
    rows, states, associations = validate_integrity_archive(
        payload, kind=kind, organizations=organizations
    )
    table, org_field = source_table(kind)
    projection = _unit_state_projection(table, org_field)
    incoming_rows = {(row[org_field], row["uuid"]): row for row in rows}
    incoming_states = {(state["organization_id"], state["source_id"]): state for state in states}
    incoming_associations = {
        (row["organization_id"], row["target_id"]): row for row in associations
    }
    plan = SourceRestorePlan(
        kind=kind, cleanup=[], batches=[], restored=[], conflicts=[], present=set()
    )
    # Identities whose destination row this restore deletes before it writes.
    retired: set[tuple[str, str]] = set()

    def vacated(holder: tuple[object, object]) -> bool:
        """Whether this restore deletes the source holding a physical key first."""
        return holder in retired or (
            clean and (global_scope or holder[0] in organizations) and holder not in incoming_states
        )

    # Units without an incoming row only retire destination rows. Planning and
    # writing them first frees any physical key a later unit's row reuses.
    keys = sorted(incoming_states, key=lambda key: (key in incoming_rows, key))
    for batch_keys in restore_batches(keys):
        record_keys = {
            key: record_key
            for key in batch_keys
            if (record_key := _archive_record_key(incoming_rows.get(key), table)) is not None
        }
        units = [
            {"organization": key[0], "source": key[1], "kind": kind.value} for key in batch_keys
        ]
        read = normalize_records(
            await execute_query(
                f"""RETURN {{
                    LET $fenced = {projection};
                    RETURN {{
                        units: $fenced,
                        digest: crypto::sha256(type::string($fenced)),
                        occupants: $keys.map(|$key|
                            (SELECT {org_field} AS organization, uuid FROM type::record($key))[0]),
                        state_keys: $units.map(|$unit| type::string(type::record(string::concat(
                            'source_states:', crypto::sha256(type::string(
                                [$unit.organization, $unit.kind, $unit.source]))))))
                    }};
                }};""",
                units=units,
                keys=list(record_keys.values()),
            )
        )
        found = read[0].get("units") if len(read) == 1 else None
        occupants = read[0].get("occupants") if len(read) == 1 else None
        digest = read[0].get("digest") if len(read) == 1 else None
        state_keys = read[0].get("state_keys") if len(read) == 1 else None
        if (
            not isinstance(found, list)
            or len(found) != len(batch_keys)
            or not isinstance(state_keys, list)
            or len(state_keys) != len(batch_keys)
            or not isinstance(occupants, list)
            or len(occupants) != len(record_keys)
            or not isinstance(digest, str)
        ):
            raise ValueError("source archive restore read returned an invalid shape")
        occupant_at = dict(zip(record_keys.values(), occupants, strict=True))
        batch = _UnitBatch(units=units, digest=digest)
        for key, destination, state_key in zip(batch_keys, found, state_keys, strict=True):
            current = destination if isinstance(destination, dict) else {}
            previous_row = current.get("row")
            decision = _decide_unit(
                kind=kind,
                record_key=record_keys.get(key),
                incoming_state=incoming_states[key],
                row=incoming_rows.get(key),
                association=incoming_associations.get(key),
                previous=current.get("state"),
                previous_row=previous_row,
                prior_association=current.get("association"),
                skip_existing=skip_existing,
            )
            if decision.write is not None:
                occupant = occupant_at.get(decision.write["key"]) if previous_row is None else None
                if occupant is not None and not vacated(
                    (occupant.get("organization"), occupant.get("uuid"))
                ):
                    raise ValueError("archive physical record identity conflicts with destination")
                batch.writes.append(decision.write)
                plan.restored.append(key[1])
            if decision.conflict is not None:
                plan.conflicts.append(_conflict(key, decision.conflict))
            if decision.delete is not None:
                batch.deletes.append(decision.delete)
                retired.add(key)
            if decision.ledger is not None:
                batch.ledger_writes.append(decision.ledger)
            if decision.retire_association:
                batch.retired_associations.append({"organization": key[0], "source": key[1]})
            if decision.association is not None:
                batch.association_writes.append(decision.association)
            if decision.write is not None or (previous_row is not None and decision.delete is None):
                plan.present.add(key)
            else:
                plan.absent.add(key)
            if decision.write is not None:
                plan.unique_writes.append(
                    UniqueWrite(table, decision.write["record"], target=decision.write["key"])
                )
            if decision.write is not None or decision.ledger is not None:
                # Writing a source row upserts its ledger state through an event.
                plan.unique_writes.append(
                    UniqueWrite(
                        "source_states",
                        {"organization_id": key[0], "source_kind": kind.value, "source_id": key[1]},
                        target=str(state_key),
                    )
                )
            if decision.association is not None:
                plan.unique_writes.append(
                    UniqueWrite(
                        "memory_derivations",
                        decision.association,
                        identity=("organization_id", "target_kind", "target_id"),
                    )
                )
            if decision.delete is not None:
                plan.vacated.add(decision.delete)
        if batch.changes_anything():
            plan.batches.append(batch)

    if clean_graph_auxiliary:
        for companion in ("relates_to", "mentions", "episode"):
            plan.cleanup += await plan_cleanup(
                execute_query,
                companion,
                organization_field="group_id",
                scope="group_id IN $organizations",
                keep=lambda _identity: False,
                organizations=organizations,
            )
    if clean:
        plan.cleanup += await plan_cleanup(
            execute_query,
            table,
            organization_field=org_field,
            scope="true" if global_scope else f"{org_field} IN $organizations",
            keep=incoming_states.__contains__,
            **({} if global_scope else {"organizations": organizations}),
        )
    for page in plan.cleanup:
        plan.vacated.update(page.keys)
    return plan


async def apply_source_restore(execute_query: SurrealExecute, plan: SourceRestorePlan) -> None:
    """Send a planned restore, one bounded transaction at a time.

    Clean deletes go first, so a unit whose row reuses a vacated physical key
    finds it free. Each transaction refuses to commit when what it was planned
    against has moved. A unit's row, ledger state and derivation association
    always commit together, so a failure partway leaves whole units behind,
    and running the same restore again finishes it: units already restored
    read as unchanged and are skipped.
    """
    await apply_cleanup(execute_query, plan.cleanup)
    table, org_field = source_table(plan.kind)
    write = f"""BEGIN TRANSACTION;
        LET $fenced = {_unit_state_projection(table, org_field)};
        IF crypto::sha256(type::string($fenced)) != $expected {{
            THROW '{_DESTINATION_CHANGED}';
        }};
        {_source_write_statements(org_field)}
        FOR $retired IN $retired_associations {{
            LET $organization = $retired.organization;
            LET $target = $retired.source;
            UPDATE memory_derivations SET active = false
                WHERE organization_id=$organization AND target_kind=$kind
                    AND target_id=$target;
        }};
        COMMIT TRANSACTION;
    """
    for batch in plan.batches:
        await execute_query(
            write,
            units=batch.units,
            kind=plan.kind.value,
            expected=batch.digest,
            deletes=batch.deletes,
            writes=native_archive_parameters(batch.writes),
            ledger_writes=batch.ledger_writes,
            association_writes=batch.association_writes,
            retired_associations=batch.retired_associations,
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
) -> dict[str, Any]:
    """Restore complete source units without lowering destination trust history.

    Every write is planned before the first one is sent, and each goes out in
    a bounded, fenced transaction; see ``apply_source_restore``.
    """
    plan = await plan_source_restore(
        execute_query,
        payload,
        kind=kind,
        organizations=organizations,
        clean=clean,
        skip_existing=skip_existing,
        global_scope=global_scope,
        clean_graph_auxiliary=clean_graph_auxiliary,
    )
    await check_unique_writes(execute_query, plan.unique_writes, vacated=plan.vacated)
    await apply_source_restore(execute_query, plan)
    return {"restored_source_ids": plan.restored, "conflicts": plan.conflicts}
