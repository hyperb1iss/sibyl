"""Usage feedback events and memory stamp maintenance."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, cast

from sibyl_core.backends.surreal.in_clumps import clumped
from sibyl_core.backends.surreal.records import coerce_datetime, normalize_records


class MemoryUsageSignal(StrEnum):
    EXPOSURE = "exposure"
    CITATION = "citation"
    MISLED = "misled"


class MemoryUsageItemKind(StrEnum):
    GRAPH_ENTITY = "graph_entity"
    RAW_CAPTURE = "raw_capture"


class UsageContentClient(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


class UsageGraphClient(Protocol):
    async def execute_query(self, query: str, **params: object) -> object: ...


@dataclass(frozen=True, slots=True)
class MemoryUsageEvent:
    organization_id: str
    session_key: str
    message_key: str
    source_surface: str
    item_kind: MemoryUsageItemKind | str
    item_id: str
    signal_type: MemoryUsageSignal | str
    principal_id: str | None = None
    project_id: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    event_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MemoryUsageTarget:
    """One stamped row: the organization it lives in, its kind, and its id."""

    organization_id: str
    item_kind: MemoryUsageItemKind
    item_id: str


@dataclass(frozen=True, slots=True)
class MemoryUsageStamp:
    item_kind: MemoryUsageItemKind
    item_id: str
    retrieval_count: int
    citation_count: int
    last_recalled_at: datetime | None
    last_used_at: datetime | None
    misled_count: int = 0


@dataclass(frozen=True, slots=True)
class MemoryUsageWriteResult:
    events_processed: int
    stamps: tuple[MemoryUsageStamp, ...]


_EVENT_INSERT_QUERY = """
INSERT INTO memory_usage_events $rows ON DUPLICATE KEY UPDATE
    uuid = uuid,
    organization_id = organization_id,
    session_key = session_key,
    message_key = message_key,
    source_surface = source_surface,
    item_kind = item_kind,
    item_id = item_id,
    signal_type = signal_type,
    principal_id = principal_id,
    project_id = project_id,
    metadata = metadata,
    event_at = event_at,
    created_at = created_at;
"""

# One statement reads the aggregates for every target: per target, one GROUP
# BY over its prefix of idx_memory_usage_events_item (organization, kind,
# item, signal, event_at) counts each signal and finds its latest event, so
# the cost is the matched events, not the table. Each target's fields are
# client params (`_N` becomes the target's index): a closure argument is
# unknown when 4.0 plans the statement, and every probe scanned the events
# table there. One grouped read per target also costs 4.0 a fraction of the
# five count and latest probes it answers. `time::max`, not `math::max`,
# which reads datetimes as numbers.
_USAGE_AGGREGATE = """{
    item_kind: $kind_N,
    item_id: $item_N,
    signals: (SELECT signal_type, count() AS total, time::max(event_at) AS latest
        FROM memory_usage_events
        WHERE organization_id = $org_N AND item_kind = $kind_N AND item_id = $item_N
        GROUP BY signal_type)
}"""
_TARGET_SLOT = re.compile(r"\$(org|kind|item)_N\b")


def _usage_aggregates_query(targets: Sequence[MemoryUsageTarget]) -> tuple[str, dict[str, str]]:
    objects = []
    params: dict[str, str] = {}
    for index, target in enumerate(targets):
        objects.append(_TARGET_SLOT.sub(rf"$\1_{index}", _USAGE_AGGREGATE))
        params[f"org_{index}"] = target.organization_id
        params[f"kind_{index}"] = target.item_kind.value
        params[f"item_{index}"] = target.item_id
    return "RETURN [" + ", ".join(objects) + "];", params


# A stamp is telemetry, not an edit, so it leaves revision alone. The counts
# and timestamps it writes are derived from the append-only events table and
# recomputed monotonically (max) on every stamp, so a writer that carries a
# stale copy back onto the row is corrected by the next stamp rather than
# fenced out by this one. Bumping revision here made every recall invalidate
# the expected_revision of whoever was editing the same popular row, and
# turned a read into a conflicting write on the hottest rows in the store.
# The snapshot fold protects itself by never overwriting a key that exists at
# write time, which covers a stamp landing in its window.
#
# Each stamp statement is one RETURN block rather than a BEGIN/COMMIT
# transaction: a statement runs in its own transaction, and the response then
# carries exactly one result on every engine. The server answers BEGIN/COMMIT
# with one entry per statement while the embedded engine collapses it, so a
# RETURN inside a transaction read the rows on one engine and NONE on the other.
#
# Rows are found by uuid in index-served clumps of client params, and one
# UPDATE applies every stamp, each row reading its own from `$stamps` (keyed
# by uuid). A uuid bound by a FOR variable or a closure argument would scan
# the table on 4.0 for every stamp. Stamps for one item are merged first
# (_merged_stamps), which leaves the row as applying them in turn would.
_STAMP_SET = """
    last_recalled_at = IF last_recalled_at != NONE
        AND ($stamps[uuid].last_recalled_at = NONE
            OR last_recalled_at > $stamps[uuid].last_recalled_at)
        THEN last_recalled_at ELSE $stamps[uuid].last_recalled_at END,
    last_used_at = IF last_used_at != NONE
        AND ($stamps[uuid].last_used_at = NONE OR last_used_at > $stamps[uuid].last_used_at)
        THEN last_used_at ELSE $stamps[uuid].last_used_at END,
    retrieval_count = math::max([retrieval_count ?? 0, $stamps[uuid].retrieval_count]),
    citation_count = math::max([citation_count ?? 0, $stamps[uuid].citation_count]),
    misled_count = math::max([misled_count ?? 0, $stamps[uuid].misled_count]),
    __BAG__.last_recalled_at = IF last_recalled_at != NONE
        AND ($stamps[uuid].last_recalled_at = NONE
            OR last_recalled_at > $stamps[uuid].last_recalled_at)
        THEN last_recalled_at ELSE $stamps[uuid].last_recalled_at END,
    __BAG__.last_used_at = IF last_used_at != NONE
        AND ($stamps[uuid].last_used_at = NONE OR last_used_at > $stamps[uuid].last_used_at)
        THEN last_used_at ELSE $stamps[uuid].last_used_at END,
    __BAG__.retrieval_count = math::max([retrieval_count ?? 0, $stamps[uuid].retrieval_count]),
    __BAG__.citation_count = math::max([citation_count ?? 0, $stamps[uuid].citation_count]),
    __BAG__.misled_count = math::max([misled_count ?? 0, $stamps[uuid].misled_count])
"""

_STAMP_QUERY = """
RETURN {
LET $stamped = __TARGETS__;
UPDATE $stamped SET __SET__
WHERE __ORGANIZATION__ = $organization_id
RETURN NONE;
RETURN __ROWS__;
};
"""


def _stamp_query(
    table: str, organization_field: str, bag: str, item_ids: Sequence[str]
) -> tuple[str, dict[str, list]]:
    targets, clumps = clumped(
        f"SELECT VALUE id FROM {table} WHERE uuid IN $stamp_ids", "stamp_ids", item_ids
    )
    rows, _ = clumped(
        f"SELECT uuid, {organization_field}, retrieval_count, citation_count, misled_count,"
        f" last_recalled_at, last_used_at FROM {table} WHERE uuid IN $stamp_ids",
        "stamp_ids",
        item_ids,
    )
    query = (
        _STAMP_QUERY.replace("__TARGETS__", targets)
        .replace("__SET__", _STAMP_SET.replace("__BAG__", bag))
        .replace("__ORGANIZATION__", organization_field)
        .replace("__ROWS__", rows)
    )
    return query, clumps


def _raw_capture_stamp_query(item_ids: Sequence[str]) -> tuple[str, dict[str, list]]:
    return _stamp_query("raw_captures", "organization_id", "metadata", item_ids)


def _graph_entity_stamp_query(item_ids: Sequence[str]) -> tuple[str, dict[str, list]]:
    return _stamp_query("entity", "group_id", "attributes", item_ids)


async def record_memory_usage_events(
    content_client: UsageContentClient,
    events: Sequence[MemoryUsageEvent],
) -> tuple[Mapping[str, object], ...]:
    """Append the deduplicated usage events in one statement and return them."""
    rows = _dedupe_event_records(_event_record(event) for event in events)
    if rows:
        await content_client.execute_query(_EVENT_INSERT_QUERY, rows=list(rows))
    return rows


def usage_targets_for_events(
    rows: Iterable[Mapping[str, object]],
) -> tuple[MemoryUsageTarget, ...]:
    """The distinct rows a batch of events refers to, in first-seen order."""
    return tuple(MemoryUsageTarget(*parts) for parts in _unique_targets(rows))


async def stamp_memory_usage(
    content_client: UsageContentClient,
    targets: Sequence[MemoryUsageTarget],
    *,
    graph_client: UsageGraphClient | None = None,
) -> tuple[MemoryUsageStamp, ...]:
    """Recompute and write the usage stamps for a batch of rows.

    One aggregate read covers every target, then one write per organization
    and store: raw captures through the content client, graph entities
    through the graph client. A graph target without a graph client keeps
    its aggregate as the stamp, the same as before.
    """
    unique = tuple(dict.fromkeys(targets))
    if not unique:
        return ()
    aggregates = await _usage_aggregates(content_client, unique)

    stamps: list[MemoryUsageStamp] = []
    by_organization: dict[str, list[MemoryUsageTarget]] = {}
    for target in unique:
        by_organization.setdefault(target.organization_id, []).append(target)
    for organization_id, organization_targets in by_organization.items():
        raw_stamps = [
            aggregates[(target.item_kind, target.item_id)]
            for target in organization_targets
            if target.item_kind is MemoryUsageItemKind.RAW_CAPTURE
        ]
        graph_stamps = [
            aggregates[(target.item_kind, target.item_id)]
            for target in organization_targets
            if target.item_kind is MemoryUsageItemKind.GRAPH_ENTITY
        ]
        if raw_stamps:
            stamps.extend(
                await _stamp_raw_captures(
                    content_client,
                    organization_id=organization_id,
                    stamps=raw_stamps,
                )
            )
        if graph_stamps and graph_client is not None:
            stamps.extend(
                await _stamp_graph_entities(
                    graph_client,
                    organization_id=organization_id,
                    stamps=graph_stamps,
                )
            )
        elif graph_stamps:
            stamps.extend(graph_stamps)
    return tuple(stamps)


async def record_memory_usage(
    content_client: UsageContentClient,
    events: Sequence[MemoryUsageEvent],
    *,
    graph_client: UsageGraphClient | None = None,
) -> MemoryUsageWriteResult:
    rows = await record_memory_usage_events(content_client, events)
    if not rows:
        return MemoryUsageWriteResult(events_processed=0, stamps=())
    stamps = await stamp_memory_usage(
        content_client,
        usage_targets_for_events(rows),
        graph_client=graph_client,
    )
    return MemoryUsageWriteResult(events_processed=len(rows), stamps=stamps)


def _event_record(event: MemoryUsageEvent) -> dict[str, object]:
    organization_id = _required_text(event.organization_id, "organization_id")
    session_key = _required_text(event.session_key, "session_key")
    message_key = _required_text(event.message_key, "message_key")
    source_surface = _required_text(event.source_surface, "source_surface")
    item_kind = MemoryUsageItemKind(str(event.item_kind))
    item_id = _required_text(event.item_id, "item_id")
    signal_type = MemoryUsageSignal(str(event.signal_type))
    event_at = event.event_at or datetime.now(UTC)
    uuid = _usage_event_uuid(
        organization_id=organization_id,
        session_key=session_key,
        message_key=message_key,
        source_surface=source_surface,
        item_kind=item_kind.value,
        item_id=item_id,
        signal_type=signal_type.value,
    )
    return {
        "uuid": uuid,
        "organization_id": organization_id,
        "session_key": session_key,
        "message_key": message_key,
        "source_surface": source_surface,
        "item_kind": item_kind.value,
        "item_id": item_id,
        "signal_type": signal_type.value,
        "principal_id": _optional_text(event.principal_id),
        "project_id": _optional_text(event.project_id),
        "metadata": {str(key): value for key, value in dict(event.metadata).items()},
        "event_at": event_at,
        "created_at": event_at,
    }


async def _usage_aggregates(
    content_client: UsageContentClient,
    targets: Sequence[MemoryUsageTarget],
) -> dict[tuple[MemoryUsageItemKind, str], MemoryUsageStamp]:
    query, params = _usage_aggregates_query(targets)
    rows = normalize_records(await content_client.execute_query(query, **params))
    aggregates: dict[tuple[MemoryUsageItemKind, str], MemoryUsageStamp] = {}
    for row in rows:
        item_kind = MemoryUsageItemKind(str(row.get("item_kind") or ""))
        item_id = str(row.get("item_id") or "")
        signals = {
            str(group.get("signal_type")): group
            for group in cast("list[Mapping[str, object]]", row.get("signals") or [])
        }
        exposure = signals.get(MemoryUsageSignal.EXPOSURE.value, {})
        citation = signals.get(MemoryUsageSignal.CITATION.value, {})
        misled = signals.get(MemoryUsageSignal.MISLED.value, {})
        aggregates[(item_kind, item_id)] = _stamp_from_row(
            item_kind,
            item_id,
            {
                "retrieval_count": exposure.get("total"),
                "citation_count": citation.get("total"),
                "misled_count": misled.get("total"),
                "last_recalled_at": exposure.get("latest"),
                "last_used_at": citation.get("latest"),
            },
        )
    for target in targets:
        aggregates.setdefault(
            (target.item_kind, target.item_id),
            _stamp_from_row(target.item_kind, target.item_id, {}),
        )
    return aggregates


async def _stamp_raw_captures(
    content_client: UsageContentClient,
    *,
    organization_id: str,
    stamps: Sequence[MemoryUsageStamp],
) -> list[MemoryUsageStamp]:
    merged = _merged_stamps(stamps)
    query, clumps = _raw_capture_stamp_query(list(merged))
    rows = normalize_records(
        await content_client.execute_query(
            query, organization_id=organization_id, stamps=merged, **clumps
        )
    )
    return _stamps_from_rows(
        MemoryUsageItemKind.RAW_CAPTURE,
        stamps,
        [row for row in rows if row.get("organization_id") == organization_id],
    )


async def _stamp_graph_entities(
    graph_client: UsageGraphClient,
    *,
    organization_id: str,
    stamps: Sequence[MemoryUsageStamp],
) -> list[MemoryUsageStamp]:
    merged = _merged_stamps(stamps)
    query, clumps = _graph_entity_stamp_query(list(merged))
    rows = normalize_records(
        await graph_client.execute_query(
            query,
            organization_id=organization_id,
            stamps=merged,
            **clumps,
            # Bookkeeping only: the graph caches stay warm (BOOKKEEPING_QUERY_LABELS).
            _query_label="usage.graph_stamp",
        )
    )
    return _stamps_from_rows(
        MemoryUsageItemKind.GRAPH_ENTITY,
        stamps,
        [row for row in rows if row.get("group_id") == organization_id],
    )


async def _stamp_graph_entity(
    graph_client: UsageGraphClient,
    *,
    organization_id: str,
    stamp: MemoryUsageStamp,
) -> MemoryUsageStamp:
    """Stamp one graph entity with a stamp computed elsewhere."""
    stamped = await _stamp_graph_entities(
        graph_client,
        organization_id=organization_id,
        stamps=[stamp],
    )
    return stamped[0]


def _merged_stamps(stamps: Sequence[MemoryUsageStamp]) -> dict[str, dict[str, object]]:
    """One stamp per item, in first-seen order: its latest timestamps and largest counters.

    Applying a stamp keeps each stored timestamp unless the stamp's is later
    (a missing one keeps it) and each counter at the larger value, so
    applying several in turn leaves the row as applying this one does.
    """
    merged: dict[str, dict[str, object]] = {}
    for stamp in stamps:
        prior = merged.get(stamp.item_id)
        if prior is None:
            merged[stamp.item_id] = {
                "retrieval_count": stamp.retrieval_count,
                "citation_count": stamp.citation_count,
                "misled_count": stamp.misled_count,
                "last_recalled_at": stamp.last_recalled_at,
                "last_used_at": stamp.last_used_at,
            }
            continue
        for counter in ("retrieval_count", "citation_count", "misled_count"):
            prior[counter] = max(cast("int", prior[counter]), getattr(stamp, counter))
        for moment in ("last_recalled_at", "last_used_at"):
            prior[moment] = _later(cast("datetime | None", prior[moment]), getattr(stamp, moment))
    return merged


def _later(current: datetime | None, candidate: datetime | None) -> datetime | None:
    if current is None or candidate is None:
        return candidate if current is None else current
    return candidate if _instant(candidate) >= _instant(current) else current


def _instant(moment: datetime) -> datetime:
    # Compared in UTC: two values sharing a zone compare by wall time in Python,
    # which orders a repeated hour (a DST fold) by its clock reading instead of
    # its instant. A naive value is UTC, as coerce_datetime and the SDK read it.
    if moment.tzinfo is not None:
        return moment.astimezone(UTC)
    return moment.replace(tzinfo=UTC)


def _dedupe_event_records(rows: Iterable[Mapping[str, object]]) -> tuple[Mapping[str, object], ...]:
    rows_by_uuid: dict[str, Mapping[str, object]] = {}
    for row in rows:
        rows_by_uuid.setdefault(str(row["uuid"]), row)
    return tuple(rows_by_uuid.values())


def _stamps_from_rows(
    item_kind: MemoryUsageItemKind,
    stamps: Sequence[MemoryUsageStamp],
    rows: Sequence[Mapping[str, object]],
) -> list[MemoryUsageStamp]:
    """The row each stamp landed on, or a zero stamp when the row is gone."""
    rows_by_uuid = {str(row.get("uuid") or ""): row for row in rows}
    return [
        _stamp_from_row(item_kind, stamp.item_id, rows_by_uuid.get(stamp.item_id, {}))
        for stamp in stamps
    ]


def _stamp_from_row(
    item_kind: MemoryUsageItemKind,
    item_id: str,
    row: Mapping[str, object],
) -> MemoryUsageStamp:
    return MemoryUsageStamp(
        item_kind=item_kind,
        item_id=item_id,
        retrieval_count=_int_count(row.get("retrieval_count")),
        citation_count=_int_count(row.get("citation_count")),
        last_recalled_at=coerce_datetime(row.get("last_recalled_at")),
        last_used_at=coerce_datetime(row.get("last_used_at")),
        misled_count=_int_count(row.get("misled_count")),
    )


def _unique_targets(
    rows: Iterable[Mapping[str, object]],
) -> tuple[tuple[str, MemoryUsageItemKind, str], ...]:
    seen: dict[tuple[str, MemoryUsageItemKind, str], None] = {}
    for row in rows:
        organization_id = str(row["organization_id"])
        item_kind = MemoryUsageItemKind(str(row["item_kind"]))
        item_id = str(row["item_id"])
        seen.setdefault((organization_id, item_kind, item_id), None)
    return tuple(seen.keys())


def _usage_event_uuid(
    *,
    organization_id: str,
    session_key: str,
    message_key: str,
    source_surface: str,
    item_kind: str,
    item_id: str,
    signal_type: str,
) -> str:
    payload = "\0".join(
        (
            organization_id,
            session_key,
            message_key,
            source_surface,
            item_kind,
            item_id,
            signal_type,
        )
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _int_count(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    return int(text) if text else 0


__all__ = [
    "MemoryUsageEvent",
    "MemoryUsageItemKind",
    "MemoryUsageSignal",
    "MemoryUsageStamp",
    "MemoryUsageTarget",
    "MemoryUsageWriteResult",
    "record_memory_usage",
    "record_memory_usage_events",
    "stamp_memory_usage",
    "usage_targets_for_events",
]
