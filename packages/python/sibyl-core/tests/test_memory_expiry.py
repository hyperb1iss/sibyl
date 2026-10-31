"""Time-boxed captures retire through the correction path once their expiry passes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.models.reflection import memory_lifecycle_from_metadata
from sibyl_core.services.graph_read_availability import available_graph_entities
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.memory_expiry import (
    CAPTURE_EXPIRED_AT_METADATA_KEY,
    CAPTURE_EXPIRES_AT_METADATA_KEY,
    capture_expiry_stamp,
    expire_due_captures,
    parse_capture_ttl,
    resolve_capture_expiry,
)
from sibyl_core.services.surreal_content import (
    get_raw_memory,
    recall_raw_memory,
    remember_raw_memory,
)
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
TEAM = "team-oncall"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("30m", timedelta(minutes=30)),
        ("2h", timedelta(hours=2)),
        ("1d", timedelta(days=1)),
        ("1h30m", timedelta(hours=1, minutes=30)),
        (" 2H ", timedelta(hours=2)),
        ("1w", timedelta(weeks=1)),
    ],
)
def test_ttl_parses_compact_durations(text: str, expected: timedelta) -> None:
    assert parse_capture_ttl(text) == expected


@pytest.mark.parametrize("text", ["", "2", "h", "2 hours", "-1h", "1.5h", "2h-", "tomorrow"])
def test_ttl_rejects_anything_else(text: str) -> None:
    with pytest.raises(ValueError, match="not a duration"):
        parse_capture_ttl(text)


def test_absurd_duration_is_a_value_error_not_an_overflow() -> None:
    with pytest.raises(ValueError, match="at most 30 days"):
        parse_capture_ttl("99999999999999d")
    with pytest.raises(ValueError, match="at most 30 days"):
        resolve_capture_expiry(expires_in="99999999999999d", now=NOW)


def test_expiry_resolves_from_duration_or_instant() -> None:
    assert resolve_capture_expiry(now=NOW) is None
    assert resolve_capture_expiry(expires_in="2h", now=NOW) == NOW + timedelta(hours=2)
    instant = datetime(2026, 10, 31, 9, 15, 42, 123456, tzinfo=UTC)
    assert resolve_capture_expiry(expires_at=instant, now=NOW) == instant.replace(microsecond=0)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"expires_in": "30s"}, "at least one minute"),
        ({"expires_in": "31d"}, "at most 30 days"),
        ({"expires_at": NOW - timedelta(hours=1)}, "at least one minute"),
        ({"expires_at": datetime(2026, 11, 1)}, "timezone"),
        ({"expires_in": "1h", "expires_at": NOW + timedelta(hours=1)}, "not both"),
    ],
)
def test_expiry_rejects_nonsense(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        resolve_capture_expiry(now=NOW, **kwargs)


async def test_stamp_is_server_owned(content_store) -> None:
    forged = await remember_raw_memory(
        organization_id="org-expiry",
        principal_id="user_a",
        source_id="forged",
        raw_content="Caller tries to schedule its own expiry.",
        embedding_provider=None,
        metadata={CAPTURE_EXPIRES_AT_METADATA_KEY: "2099-01-01T00:00:00Z"},
    )
    assert CAPTURE_EXPIRES_AT_METADATA_KEY not in forged.metadata

    expires_at = NOW + timedelta(hours=2)
    stamped = await remember_raw_memory(
        organization_id="org-expiry",
        principal_id="user_a",
        source_id="stamped",
        raw_content="Validated expiry wins over a forged one.",
        embedding_provider=None,
        metadata={CAPTURE_EXPIRES_AT_METADATA_KEY: "2099-01-01T00:00:00Z"},
        expires_at=expires_at,
    )
    assert stamped.metadata[CAPTURE_EXPIRES_AT_METADATA_KEY] == "2026-10-30T14:00:00Z"


async def _team_notice(runtime, monkeypatch, *, expires_at: datetime, text: str):
    monkeypatch.setattr(
        "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    memory = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="oncall-agent",
        source_id=f"incident:{text}",
        raw_content=text,
        memory_scope="team",
        scope_key=TEAM,
        embedding_provider=None,
        expires_at=expires_at,
    )
    note = Entity(
        id=f"note-{memory.id}",
        name="Active incident",
        content=text,
        entity_type=EntityType.NOTE,
        metadata={
            "raw_memory_id": memory.id,
            "raw_source_id": memory.source_id,
            "memory_scope": "team",
            "scope_key": TEAM,
            "principal_id": "oncall-agent",
        },
    )
    await runtime.entity_manager.create_direct(note, generate_embedding=False)
    return memory, note


async def _team_recall(runtime, query: str) -> list[str]:
    rows = await recall_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="teammate",
        query=query,
        memory_scope="team",
        scope_key=TEAM,
    )
    return [row.id for row in rows]


async def test_expired_team_notice_leaves_graph_reads_and_recall(
    runtime, content_store, monkeypatch
) -> None:
    group = runtime.client.group_id
    expired, expired_note = await _team_notice(
        runtime, monkeypatch, expires_at=NOW + timedelta(hours=1), text="checkout is down"
    )
    live, live_note = await _team_notice(
        runtime, monkeypatch, expires_at=NOW + timedelta(hours=5), text="checkout deploy freeze"
    )
    both = [expired_note.id, live_note.id]
    assert set(await available_graph_entities(group, both, runtime=runtime)) == set(both)
    assert expired.id in await _team_recall(runtime, "checkout")

    result = await expire_due_captures(now=NOW + timedelta(hours=2))

    assert [item.memory.id for item in result.expired] == [expired.id]
    assert result.expired[0].corrected
    assert set(await available_graph_entities(group, both, runtime=runtime)) == {live_note.id}
    recalled = await _team_recall(runtime, "checkout")
    assert expired.id not in recalled
    assert live.id in recalled

    stored = await get_raw_memory(organization_id=group, memory_id=expired.id)
    assert stored is not None
    lifecycle = memory_lifecycle_from_metadata(
        stored.metadata, source_id=stored.id, review_state=stored.review_state
    )
    assert (str(lifecycle.state), lifecycle.action, lifecycle.reason) == (
        "contested",
        "mark_stale",
        "expired",
    )
    assert stored.metadata.get(CAPTURE_EXPIRES_AT_METADATA_KEY) is None
    assert stored.metadata[CAPTURE_EXPIRED_AT_METADATA_KEY] == "2026-10-30T13:00:00Z"
    history = stored.metadata["correction_history"]
    assert [entry["reason"] for entry in history] == ["expired"]

    # A handled capture has left the due range, so the next tick is a no-op.
    again = await expire_due_captures(now=NOW + timedelta(hours=2))
    assert again.expired == []


async def test_resolved_early_notice_is_not_corrected_twice(
    runtime, content_store, monkeypatch
) -> None:
    group = runtime.client.group_id
    memory, note = await _team_notice(
        runtime, monkeypatch, expires_at=NOW + timedelta(hours=1), text="payments degraded"
    )
    resolved = await apply_memory_correction(
        organization_id=group,
        source_id=memory.id,
        principal_id="teammate",
        action="stale",
        reason="resolved",
        accessible_projects=(),
        accessible_teams=(TEAM,),
    )
    assert resolved.applied
    assert note.id not in await available_graph_entities(group, [note.id], runtime=runtime)

    result = await expire_due_captures(now=NOW + timedelta(hours=2))

    assert result.already_retired == 1
    assert [item.corrected for item in result.expired] == [False]
    stored = await get_raw_memory(organization_id=group, memory_id=memory.id)
    assert stored is not None
    assert [entry["reason"] for entry in stored.metadata["correction_history"]] == ["resolved"]
    assert stored.metadata.get(CAPTURE_EXPIRES_AT_METADATA_KEY) is None


async def test_restore_after_expiry_keeps_the_notice_live(
    runtime, content_store, monkeypatch
) -> None:
    group = runtime.client.group_id
    memory, note = await _team_notice(
        runtime, monkeypatch, expires_at=NOW + timedelta(hours=1), text="search index rebuild"
    )
    await expire_due_captures(now=NOW + timedelta(hours=2))
    restored = await apply_memory_correction(
        organization_id=group,
        source_id=memory.id,
        principal_id="oncall-agent",
        action="restore",
        reason="still happening",
        accessible_projects=(),
        accessible_teams=(TEAM,),
    )
    assert restored.applied

    result = await expire_due_captures(now=NOW + timedelta(hours=3))

    assert result.expired == []
    assert note.id in await available_graph_entities(group, [note.id], runtime=runtime)


async def test_capture_not_yet_due_is_untouched(runtime, content_store, monkeypatch) -> None:
    memory, _note = await _team_notice(
        runtime, monkeypatch, expires_at=NOW + timedelta(hours=1), text="queue backlog"
    )
    plain = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="oncall-agent",
        source_id="plain",
        raw_content="A capture with no expiry at all.",
        embedding_provider=None,
    )

    result = await expire_due_captures(now=NOW + timedelta(minutes=59))

    assert result.expired == []
    for row_id in (memory.id, plain.id):
        stored = await get_raw_memory(organization_id=runtime.client.group_id, memory_id=row_id)
        assert stored is not None
        assert "correction_history" not in stored.metadata


def test_stamp_format_orders_as_time() -> None:
    earlier = capture_expiry_stamp(NOW)
    later = capture_expiry_stamp(NOW + timedelta(seconds=1))
    assert earlier < later
    assert earlier == "2026-10-30T12:00:00Z"


def test_pack_marks_a_live_notice_with_its_expiry() -> None:
    from sibyl_core.models.context import (
        ContextFacet,
        ContextIntent,
        ContextItem,
        ContextPack,
        ContextSection,
    )
    from sibyl_core.tools.context import _lean_item_metadata
    from sibyl_core.tools.context_rendering import render_context_pack

    def item(item_id: str, metadata: dict) -> ContextItem:
        return ContextItem(
            id=item_id,
            type="raw_memory",
            name=f"Notice {item_id}",
            content="Checkout is down; hold every deploy.",
            score=1.0,
            facet=ContextFacet.RECENT_MEMORY,
            reason="matched",
            metadata=_lean_item_metadata(metadata),
        )

    pack = ContextPack(
        goal="checkout",
        intent=ContextIntent.BUILD,
        query="checkout",
        domain=None,
        project=None,
        sections=[
            ContextSection(
                facet=ContextFacet.RECENT_MEMORY,
                title="Recent Memory",
                items=[
                    item("live", {CAPTURE_EXPIRES_AT_METADATA_KEY: "2026-10-30T14:00:00Z"}),
                    item("history", {CAPTURE_EXPIRES_AT_METADATA_KEY: None}),
                ],
            )
        ],
        total_items=2,
    )

    lines = render_context_pack(pack).markdown.splitlines()
    live = next(line for line in lines if "Notice live" in line)
    history = next(line for line in lines if "Notice history" in line)
    assert "_expires=2026-10-30T14:00:00Z_" in live
    assert "expires=" not in history
