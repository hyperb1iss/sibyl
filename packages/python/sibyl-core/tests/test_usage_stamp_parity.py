"""Set-based recall stamps leave every row as the per-stamp loop did.

The loop statements below are the shape the set-based stamps replaced (a FOR
over the stamps, then a closure read per stamp). They are the oracle: the
same stamps, applied to twin stores by each form, must leave the same stored
fields and return the same stamps, including several stamps for one item,
missing timestamps, more items than one index-served clump holds, a row in
another organization and an id with no row.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from sibyl_core.backends.surreal.content_client import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.backends.surreal.records import coerce_datetime, normalize_records
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.graph import EntityManager, SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.usage import (
    MemoryUsageItemKind,
    MemoryUsageStamp,
    MemoryUsageTarget,
    _merged_stamps,
    _stamp_from_row,
    _stamp_graph_entities,
    _stamp_raw_captures,
    _stamps_from_rows,
    _usage_aggregates,
)

_LOOP_SET = """
        last_recalled_at = IF last_recalled_at != NONE
            AND ($stamp.last_recalled_at = NONE
                OR last_recalled_at > $stamp.last_recalled_at)
            THEN last_recalled_at ELSE $stamp.last_recalled_at END,
        last_used_at = IF last_used_at != NONE
            AND ($stamp.last_used_at = NONE OR last_used_at > $stamp.last_used_at)
            THEN last_used_at ELSE $stamp.last_used_at END,
        retrieval_count = math::max([retrieval_count ?? 0, $stamp.retrieval_count]),
        citation_count = math::max([citation_count ?? 0, $stamp.citation_count]),
        misled_count = math::max([misled_count ?? 0, $stamp.misled_count]),
        BAG.last_recalled_at = IF last_recalled_at != NONE
            AND ($stamp.last_recalled_at = NONE
                OR last_recalled_at > $stamp.last_recalled_at)
            THEN last_recalled_at ELSE $stamp.last_recalled_at END,
        BAG.last_used_at = IF last_used_at != NONE
            AND ($stamp.last_used_at = NONE OR last_used_at > $stamp.last_used_at)
            THEN last_used_at ELSE $stamp.last_used_at END,
        BAG.retrieval_count = math::max([retrieval_count ?? 0, $stamp.retrieval_count]),
        BAG.citation_count = math::max([citation_count ?? 0, $stamp.citation_count]),
        BAG.misled_count = math::max([misled_count ?? 0, $stamp.misled_count])
"""
_LOOP_RAW = (
    """
RETURN {
FOR $stamp IN $stamps {
    UPDATE (SELECT VALUE id FROM raw_captures WHERE uuid = $stamp.item_id) SET"""
    + _LOOP_SET.replace("BAG", "metadata")
    + """
    WHERE organization_id = $organization_id
    RETURN NONE;
};
RETURN $stamps.map(|$s| (SELECT uuid, organization_id, retrieval_count, citation_count,
    misled_count, last_recalled_at, last_used_at
    FROM raw_captures WHERE uuid = $s.item_id LIMIT 1)[0]);
};
"""
)
_LOOP_GRAPH = (
    """
RETURN {
FOR $stamp IN $stamps {
    UPDATE entity SET"""
    + _LOOP_SET.replace("BAG", "attributes")
    + """
    WHERE group_id = $organization_id
        AND uuid = $stamp.item_id
    RETURN NONE;
};
RETURN $stamps.map(|$s| (SELECT uuid, group_id, retrieval_count, citation_count,
    misled_count, last_recalled_at, last_used_at
    FROM entity WHERE uuid = $s.item_id LIMIT 1)[0]);
};
"""
)

# The closure read the per-target aggregates replaced.
_CLOSURE_AGGREGATES = """
RETURN $targets.map(|$t| {
    item_kind: $t.item_kind,
    item_id: $t.item_id,
    retrieval_count: (SELECT count() AS total FROM memory_usage_events
        WHERE organization_id = $t.organization_id AND item_kind = $t.item_kind
            AND item_id = $t.item_id AND signal_type = "exposure"
        GROUP ALL)[0].total ?? 0,
    citation_count: (SELECT count() AS total FROM memory_usage_events
        WHERE organization_id = $t.organization_id AND item_kind = $t.item_kind
            AND item_id = $t.item_id AND signal_type = "citation"
        GROUP ALL)[0].total ?? 0,
    misled_count: (SELECT count() AS total FROM memory_usage_events
        WHERE organization_id = $t.organization_id AND item_kind = $t.item_kind
            AND item_id = $t.item_id AND signal_type = "misled"
        GROUP ALL)[0].total ?? 0,
    last_recalled_at: (SELECT VALUE event_at FROM memory_usage_events
        WHERE organization_id = $t.organization_id AND item_kind = $t.item_kind
            AND item_id = $t.item_id AND signal_type = "exposure"
        ORDER BY event_at DESC LIMIT 1)[0],
    last_used_at: (SELECT VALUE event_at FROM memory_usage_events
        WHERE organization_id = $t.organization_id AND item_kind = $t.item_kind
            AND item_id = $t.item_id AND signal_type = "citation"
        ORDER BY event_at DESC LIMIT 1)[0]
});
"""

_BASE = datetime(2026, 9, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None)
_ITEMS = [f"item-{index:03d}" for index in range(40)]
_FOREIGN = "item-foreign"
_SEEDED = [*_ITEMS, _FOREIGN]
_STAMP_FIELDS = (
    "retrieval_count",
    "citation_count",
    "misled_count",
    "last_recalled_at",
    "last_used_at",
)


def _minutes(value: int | None) -> datetime | None:
    return None if value is None else _BASE + timedelta(minutes=value)


def _stored(index: int) -> dict[str, object]:
    """What a row holds before stamping; every fourth row has never been stamped."""
    if index % 4 == 0 and index < len(_ITEMS):
        return {}
    return {
        "last_recalled_at": _minutes(index * 3),
        "last_used_at": _minutes(None if index % 3 == 0 else 100 - index),
        "retrieval_count": index % 7,
        "citation_count": 5 if index % 5 == 0 else 0,
        "misled_count": 1 if index % 6 == 0 else 0,
    }


def _stamps() -> list[MemoryUsageStamp]:
    """Several stamps per item for a third of the items, out of order, some without times."""
    stamps: list[MemoryUsageStamp] = []
    for index, item_id in enumerate([*_ITEMS, _FOREIGN, "item-missing"]):
        repeats = 3 if index % 3 == 0 else 1
        for repeat in range(repeats):
            stamps.append(
                MemoryUsageStamp(
                    item_kind=MemoryUsageItemKind.GRAPH_ENTITY,
                    item_id=item_id,
                    retrieval_count=(index + repeat * 5) % 9,
                    citation_count=repeat,
                    misled_count=(index + repeat) % 2,
                    last_recalled_at=_minutes(
                        None if (index + repeat) % 4 == 1 else index * 2 + (repeat * 37) % 11
                    ),
                    last_used_at=_minutes(None if repeat == 1 else 90 - index + repeat),
                )
            )
    return stamps


def _loop_parameters(stamps: Sequence[MemoryUsageStamp]) -> list[dict[str, object]]:
    return [
        {"item_id": stamp.item_id, **{field: getattr(stamp, field) for field in _STAMP_FIELDS}}
        for stamp in stamps
    ]


def _comparable(rows: list[dict[str, object]], bag: str) -> list[dict[str, object]]:
    def normal(value: object) -> object:
        moment = coerce_datetime(value) if not isinstance(value, int | float) else None
        return moment if moment is not None else value

    compared = []
    for row in sorted(rows, key=lambda row: str(row["uuid"])):
        nested = row.get(bag) or {}
        assert isinstance(nested, dict)
        compared.append(
            {
                "uuid": row["uuid"],
                **{field: normal(row.get(field)) for field in _STAMP_FIELDS},
                **{f"{bag}.{field}": normal(nested.get(field)) for field in _STAMP_FIELDS},
            }
        )
    return compared


def _live_url() -> str | None:
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        return None
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    return url if url and not is_embedded_surreal_url(url) else None


def _credentials() -> dict[str, str]:
    return {
        "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
        "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
    }


@pytest.fixture(params=["embedded", "live"])
async def graph_twins(request) -> AsyncIterator[tuple[SurrealGraphClient, SurrealGraphClient]]:
    """Two organizations seeded alike, each in its own namespace."""
    live = request.param == "live"
    url = _live_url() if live else "memory://"
    if url is None:
        pytest.skip("live SurrealDB tests require SIBYL_LIVE_SURREAL_TESTS=1 and a server URL")
    clients = []
    for arm in ("loop", "set"):
        group_id = f"stamp-{arm}-{uuid4().hex[:10]}"
        if live:
            client = SurrealGraphClient(
                group_id=group_id,
                url=url,
                namespace_prefix="verify_",
                database="graph",
                pool_size=2,
                **_credentials(),
            )
        else:
            client = SurrealGraphClient(group_id=group_id, url=url)
        await prepare_graph_schema(client)
        clients.append(client)
    try:
        for client in clients:
            manager = EntityManager(client, group_id=client.group_id)
            for item_id in _ITEMS:
                await manager.create_direct(
                    Entity(id=item_id, entity_type=EntityType.NOTE, name=item_id),
                    generate_embedding=False,
                )
            # The stamped id names a row of another group in the same namespace.
            await EntityManager(client, group_id="other-group").create_direct(
                Entity(id=_FOREIGN, entity_type=EntityType.NOTE, name=_FOREIGN),
                generate_embedding=False,
            )
            for index, item_id in enumerate(_SEEDED):
                stored = _stored(index)
                if stored:
                    await client.execute_query(
                        "UPDATE entity SET "
                        + ", ".join(
                            f"{prefix}{field} = $stored.{field}"
                            for prefix in ("", "attributes.")
                            for field in _STAMP_FIELDS
                        )
                        + " WHERE uuid = $uuid RETURN NONE;",
                        stored=stored,
                        uuid=item_id,
                    )
        yield clients[0], clients[1]
    finally:
        for client in clients:
            if live:
                await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
            await client.close()


async def test_graph_stamps_match_the_per_stamp_loop(
    graph_twins: tuple[SurrealGraphClient, SurrealGraphClient],
) -> None:
    loop_client, set_client = graph_twins
    stamps = _stamps()

    loop_rows = normalize_records(
        await loop_client.execute_query(
            _LOOP_GRAPH,
            organization_id=loop_client.group_id,
            stamps=_loop_parameters(stamps),
        )
    )
    expected = _stamps_from_rows(
        MemoryUsageItemKind.GRAPH_ENTITY,
        stamps,
        [row for row in loop_rows if row.get("group_id") == loop_client.group_id],
    )
    returned = await _stamp_graph_entities(
        set_client, organization_id=set_client.group_id, stamps=stamps
    )

    assert returned == expected
    read = "SELECT * FROM entity WHERE uuid IN $uuids;"
    loop_stored = _comparable(
        normalize_records(await loop_client.execute_query(read, uuids=_SEEDED)), "attributes"
    )
    set_stored = _comparable(
        normalize_records(await set_client.execute_query(read, uuids=_SEEDED)), "attributes"
    )
    assert set_stored == loop_stored
    _assert_the_oracle_merged(stamps, loop_stored)


def _raw_row(item_id: str, organization_id: str, stored: dict[str, object]) -> dict[str, object]:
    return {
        "uuid": item_id,
        "organization_id": organization_id,
        "source_id": f"source-{item_id}",
        "principal_id": "user-stamp",
        "title": item_id,
        "raw_content": item_id,
        "tags": [],
        "metadata": dict(stored),
        "provenance": {},
        "captured_at": _BASE,
        "created_at": _BASE,
        **stored,
    }


@pytest.fixture(params=["embedded", "live"])
async def content_twins(
    request,
) -> AsyncIterator[tuple[SurrealContentClient, SurrealContentClient]]:
    live = request.param == "live"
    url = _live_url() if live else "memory://"
    if url is None:
        pytest.skip("live SurrealDB tests require SIBYL_LIVE_SURREAL_TESTS=1 and a server URL")
    clients = []
    for arm in ("loop", "set"):
        if live:
            client = SurrealContentClient(
                url=url, namespace=f"verify_stamp_{arm}_{uuid4().hex}", **_credentials()
            )
        else:
            client = SurrealContentClient(url=url)
        await bootstrap_content_schema(client, reset=not live)
        clients.append(client)
    try:
        for client in clients:
            rows = [
                _raw_row(item_id, "org-stamp", _stored(index))
                for index, item_id in enumerate(_ITEMS)
            ]
            rows.append(_raw_row(_FOREIGN, "other-org", _stored(len(_ITEMS))))
            await client.execute_query("INSERT INTO raw_captures $rows RETURN NONE;", rows=rows)
        yield clients[0], clients[1]
    finally:
        for client in clients:
            if live:
                await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
            await client.close()


async def test_raw_capture_stamps_match_the_per_stamp_loop(
    content_twins: tuple[SurrealContentClient, SurrealContentClient],
) -> None:
    loop_client, set_client = content_twins
    stamps = [replace(stamp, item_kind=MemoryUsageItemKind.RAW_CAPTURE) for stamp in _stamps()]

    loop_rows = normalize_records(
        await loop_client.execute_query(
            _LOOP_RAW, organization_id="org-stamp", stamps=_loop_parameters(stamps)
        )
    )
    expected = _stamps_from_rows(
        MemoryUsageItemKind.RAW_CAPTURE,
        stamps,
        [row for row in loop_rows if row.get("organization_id") == "org-stamp"],
    )
    returned = await _stamp_raw_captures(set_client, organization_id="org-stamp", stamps=stamps)

    assert returned == expected
    read = "SELECT * FROM raw_captures WHERE uuid IN $uuids;"
    loop_stored = _comparable(
        normalize_records(await loop_client.execute_query(read, uuids=_SEEDED)), "metadata"
    )
    set_stored = _comparable(
        normalize_records(await set_client.execute_query(read, uuids=_SEEDED)), "metadata"
    )
    assert set_stored == loop_stored
    _assert_the_oracle_merged(stamps, loop_stored)


def _assert_the_oracle_merged(
    stamps: Sequence[MemoryUsageStamp], stored: list[dict[str, object]]
) -> None:
    """The fixture really exercises the merge and the guards."""
    assert len(stamps) > len({stamp.item_id for stamp in stamps}) > 32
    rows = {str(row["uuid"]): row for row in stored}
    # item-000 starts unstamped and gets 0, 5, then 1: the loop keeps the
    # largest, where applying only the last stamp would leave 1.
    assert rows["item-000"]["retrieval_count"] == 5
    # The row in another organization keeps what it held.
    assert rows[_FOREIGN]["retrieval_count"] == _stored(len(_ITEMS))["retrieval_count"]


async def test_usage_aggregates_match_the_closure_read(
    content_twins: tuple[SurrealContentClient, SurrealContentClient],
) -> None:
    client, _ = content_twins
    signals = ("exposure", "citation", "misled")
    events = [
        {
            "uuid": f"event-{organization}-{kind}-{index}-{repeat}",
            "organization_id": organization,
            "session_key": "session",
            "message_key": f"message-{repeat}",
            "source_surface": "search",
            "item_kind": kind,
            "item_id": f"item-{index:03d}",
            "signal_type": signals[(index + repeat) % 3],
            "metadata": {},
            "event_at": _minutes(index * 5 + repeat * 3),
            "created_at": _minutes(index * 5 + repeat * 3),
        }
        for organization in ("org-stamp", "other-org")
        for kind in ("raw_capture", "graph_entity")
        for index in range(40)
        for repeat in range(index % 5)
    ]
    await client.execute_query("INSERT INTO memory_usage_events $rows RETURN NONE;", rows=events)
    targets = [
        MemoryUsageTarget(organization, MemoryUsageItemKind(kind), f"item-{index:03d}")
        for index in (*range(0, 40, 3), 99)
        for organization, kind in (("org-stamp", "raw_capture"), ("other-org", "graph_entity"))
    ]

    before = normalize_records(
        await client.execute_query(
            _CLOSURE_AGGREGATES,
            targets=[
                {
                    "organization_id": target.organization_id,
                    "item_kind": target.item_kind.value,
                    "item_id": target.item_id,
                }
                for target in targets
            ],
        )
    )
    expected = {
        (MemoryUsageItemKind(str(row["item_kind"])), str(row["item_id"])): _stamp_from_row(
            MemoryUsageItemKind(str(row["item_kind"])), str(row["item_id"]), row
        )
        for row in before
    }
    after = await _usage_aggregates(client, targets)

    assert after == expected
    assert len(after) == len({(t.item_kind, t.item_id) for t in targets})
    assert any(stamp.retrieval_count for stamp in after.values())
    assert any(stamp.misled_count for stamp in after.values())
    assert any(stamp.last_used_at is not None for stamp in after.values())


# 2026-11-01 01:45 then 01:15 in Los Angeles: the repeated hour of the DST fall
# back. The first is 08:45Z (fold=0), the second 09:15Z (fold=1), so the later
# instant has the earlier wall clock.
_LOS_ANGELES = ZoneInfo("America/Los_Angeles")
_BEFORE_FALL_BACK = datetime(2026, 11, 1, 1, 45, tzinfo=_LOS_ANGELES, fold=0)
_AFTER_FALL_BACK = datetime(2026, 11, 1, 1, 15, tzinfo=_LOS_ANGELES, fold=1)


def test_stamp_merge_keeps_the_later_instant_across_a_dst_fold() -> None:
    assert _BEFORE_FALL_BACK.astimezone(UTC) < _AFTER_FALL_BACK.astimezone(UTC)
    stamps = [
        MemoryUsageStamp(
            MemoryUsageItemKind.GRAPH_ENTITY, "item", 1, 0, moment, moment, misled_count=0
        )
        for moment in (_BEFORE_FALL_BACK, _AFTER_FALL_BACK, _BEFORE_FALL_BACK)
    ]
    merged = _merged_stamps(stamps)["item"]
    assert merged["last_recalled_at"] is _AFTER_FALL_BACK
    assert merged["last_used_at"] is _AFTER_FALL_BACK
    # A naive value is UTC: 09:00 naive sits between the two instants.
    naive = datetime(2026, 11, 1, 9, 0)
    for order in ((naive, _AFTER_FALL_BACK), (_AFTER_FALL_BACK, naive)):
        stamps = [
            MemoryUsageStamp(MemoryUsageItemKind.GRAPH_ENTITY, "item", 1, 0, moment, None)
            for moment in order
        ]
        assert _merged_stamps(stamps)["item"]["last_recalled_at"] is _AFTER_FALL_BACK


async def test_graph_stamps_across_a_dst_fold_match_the_per_stamp_loop(
    graph_twins: tuple[SurrealGraphClient, SurrealGraphClient],
) -> None:
    loop_client, set_client = graph_twins
    stamps = [
        MemoryUsageStamp(
            MemoryUsageItemKind.GRAPH_ENTITY, "item-003", 1, 0, moment, moment, misled_count=0
        )
        for moment in (_BEFORE_FALL_BACK, _AFTER_FALL_BACK, _BEFORE_FALL_BACK)
    ]
    await loop_client.execute_query(
        _LOOP_GRAPH, organization_id=loop_client.group_id, stamps=_loop_parameters(stamps)
    )
    await _stamp_graph_entities(set_client, organization_id=set_client.group_id, stamps=stamps)

    read = "SELECT * FROM entity WHERE uuid = 'item-003';"
    loop_stored = _comparable(
        normalize_records(await loop_client.execute_query(read)), "attributes"
    )
    set_stored = _comparable(normalize_records(await set_client.execute_query(read)), "attributes")
    assert set_stored == loop_stored
    after = _AFTER_FALL_BACK.astimezone(UTC).replace(tzinfo=None)
    assert loop_stored[0]["last_recalled_at"] == after
    assert loop_stored[0]["attributes.last_used_at"] == after
