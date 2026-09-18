"""A promoted correction child retires the drafts it replaced."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid5

import pytest

from sibyl_core.services import reflection_supersession
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.reflection_supersession import (
    SUPERSEDED_ARCHIVE_REASON,
    correction_parent_id,
    retire_superseded_reflection_drafts,
)
from sibyl_core.services.surreal_content import MemoryScope
from tests.test_reflection_identity import content_store as content_store

ORG_ID = "00000000-0000-0000-0000-0000000000aa"
USER_ID = "00000000-0000-0000-0000-0000000000bb"


def _child_id(execution_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + execution_id))


def _candidate(memory_id: str, **overrides: object) -> RawMemory:
    values: dict[str, object] = {
        "id": memory_id,
        "organization_id": ORG_ID,
        "source_id": "source-1",
        "principal_id": USER_ID,
        "memory_scope": MemoryScope.PRIVATE,
        "scope_key": None,
        "review_state": "pending",
        "entity_type": "procedure",
        "title": "Repair the escaping probe",
        "raw_content": "Rewrite the probe as an inline script.",
        "tags": ["memory"],
        "metadata": {"confidence": 0.9},
        "provenance": {},
        "capture_surface": "reflection_candidate",
        "captured_at": datetime(2026, 9, 18, tzinfo=UTC),
        "created_at": datetime(2026, 9, 18, tzinfo=UTC),
        "revision": 3,
    }
    values.update(overrides)
    return RawMemory(**values)  # type: ignore[arg-type]


def _corrected(memory_id: str, *, parent_id: str, execution_id: str, **overrides: object):
    metadata = {"automatic_correction": {"parent_id": parent_id, "execution_id": execution_id}}
    metadata.update(overrides.pop("metadata", {}))  # type: ignore[arg-type]
    return _candidate(memory_id, metadata=metadata, **overrides)


class _Store:
    """Stand in for raw_captures and the supersession table, without a database."""

    def __init__(self, memories: list[RawMemory]) -> None:
        self.rows = {memory.id: memory for memory in memories}
        self.records: dict[str, dict[str, object]] = {}
        self.writes: list[str] = []
        self.blocked_ids: set[str] = set()

    async def get(self, *, organization_id: str, memory_id: str) -> RawMemory | None:
        row = self.rows.get(memory_id)
        if row is None or row.organization_id != organization_id:
            return None
        return row

    async def record(self, draft: RawMemory, promoted: RawMemory) -> bool:
        self.writes.append(draft.id)
        if draft.id in self.blocked_ids or draft.id in self.records:
            return False
        self.records[draft.id] = {
            "draft_id": draft.id,
            "superseded_by_candidate_id": promoted.id,
            "promoted_entity_id": (promoted.metadata or {}).get("promoted_entity_id"),
            "archive_reason": reflection_supersession.SUPERSEDED_ARCHIVE_REASON,
        }
        return True


@pytest.fixture
def store(monkeypatch):
    def _install(memories: list[RawMemory]) -> _Store:
        created = _Store(memories)
        monkeypatch.setattr(reflection_supersession, "get_raw_memory", created.get)
        monkeypatch.setattr(reflection_supersession, "_record_supersession", created.record)
        return created

    return _install


def test_correction_parent_requires_the_execution_bound_identity() -> None:
    execution_id = "a" * 64
    child = _corrected(_child_id(execution_id), parent_id="draft-1", execution_id=execution_id)

    assert correction_parent_id(child) == "draft-1"
    assert correction_parent_id(_candidate("draft-1")) is None
    # A marker that does not derive this row's id cannot redirect the walk.
    assert correction_parent_id(replace(child, id=_child_id("b" * 64))) is None


async def test_promoted_child_archives_its_pending_parent(store) -> None:
    execution_id = "a" * 64
    parent = _candidate("11111111-1111-5111-8111-111111111111")
    child = _corrected(
        _child_id(execution_id),
        parent_id=parent.id,
        execution_id=execution_id,
        review_state="promoted",
        metadata={"promoted_entity_id": "procedure_v3_abc"},
    )
    rows = store([parent, child])

    archived = await retire_superseded_reflection_drafts(
        organization_id=ORG_ID, promoted_candidate_id=child.id
    )

    assert archived == [parent.id]
    record = rows.records[parent.id]
    assert record["archive_reason"] == SUPERSEDED_ARCHIVE_REASON
    assert record["superseded_by_candidate_id"] == child.id
    assert record["promoted_entity_id"] == "procedure_v3_abc"
    # The draft row is immutable evidence for the promoted child, so it is left
    # byte-identical: the terminal state lives beside it, never inside it.
    assert rows.rows[parent.id] == parent


async def test_promoted_grandchild_archives_every_pending_ancestor(store) -> None:
    first, second = "a" * 64, "b" * 64
    root = _candidate("22222222-2222-5222-8222-222222222222")
    middle = _corrected(_child_id(first), parent_id=root.id, execution_id=first)
    leaf = _corrected(
        _child_id(second),
        parent_id=middle.id,
        execution_id=second,
        review_state="promoted",
        metadata={"promoted_entity_id": "procedure_v3_def"},
    )
    rows = store([root, middle, leaf])

    archived = await retire_superseded_reflection_drafts(
        organization_id=ORG_ID, promoted_candidate_id=leaf.id
    )

    assert archived == [middle.id, root.id]
    assert set(rows.records) == {middle.id, root.id}
    assert all(rows.rows[memory_id].review_state == "pending" for memory_id in archived)


async def test_pending_frontier_never_retires_its_ancestors(store) -> None:
    execution_id = "a" * 64
    parent = _candidate("33333333-3333-5333-8333-333333333333")
    child = _corrected(_child_id(execution_id), parent_id=parent.id, execution_id=execution_id)
    rows = store([parent, child])

    assert (
        await retire_superseded_reflection_drafts(
            organization_id=ORG_ID, promoted_candidate_id=child.id
        )
        == []
    )
    assert rows.records == {}
    assert rows.rows[parent.id].review_state == "pending"


async def test_repeat_passes_leave_a_retired_draft_untouched(store) -> None:
    execution_id = "a" * 64
    parent = _candidate("44444444-4444-5444-8444-444444444444")
    child = _corrected(
        _child_id(execution_id),
        parent_id=parent.id,
        execution_id=execution_id,
        review_state="promoted",
    )
    rows = store([parent, child])

    first = await retire_superseded_reflection_drafts(
        organization_id=ORG_ID, promoted_candidate_id=child.id
    )
    second = await retire_superseded_reflection_drafts(
        organization_id=ORG_ID, promoted_candidate_id=child.id
    )

    assert first == [parent.id]
    assert second == []
    assert set(rows.records) == {parent.id}


async def test_an_archived_ancestor_does_not_stop_the_walk(store) -> None:
    first, second = "a" * 64, "b" * 64
    root = _candidate("55555555-5555-5555-8555-555555555555")
    middle = _corrected(
        _child_id(first), parent_id=root.id, execution_id=first, review_state="archived"
    )
    leaf = _corrected(
        _child_id(second),
        parent_id=middle.id,
        execution_id=second,
        review_state="promoted",
    )
    rows = store([root, middle, leaf])

    archived = await retire_superseded_reflection_drafts(
        organization_id=ORG_ID, promoted_candidate_id=leaf.id
    )

    assert archived == [root.id]
    assert middle.id not in rows.records


async def test_a_contended_draft_is_left_for_the_next_pass(store) -> None:
    execution_id = "a" * 64
    parent = _candidate("66666666-6666-5666-8666-666666666666")
    child = _corrected(
        _child_id(execution_id),
        parent_id=parent.id,
        execution_id=execution_id,
        review_state="promoted",
    )
    rows = store([parent, child])
    rows.blocked_ids = {parent.id}

    archived = await retire_superseded_reflection_drafts(
        organization_id=ORG_ID, promoted_candidate_id=child.id
    )

    assert archived == []
    assert rows.records == {}


async def test_supersession_records_survive_a_real_content_store(content_store: None) -> None:
    """Exercise the table, its unique guard and the reader against the engine."""
    from sibyl_core.services.content_raw_persistence import save_raw_memory
    from sibyl_core.services.content_raw_recall import list_reflection_candidate_reviews
    from sibyl_core.services.reflection_supersession import (
        _record_supersession,
        superseded_draft_ids,
    )

    execution_id = "a" * 64
    draft = _candidate("99999999-9999-5999-8999-999999999999", revision=1)
    open_draft = _candidate("aaaaaaaa-aaaa-5aaa-8aaa-aaaaaaaaaaaa", revision=1)
    child = _corrected(
        _child_id(execution_id),
        parent_id=draft.id,
        execution_id=execution_id,
        review_state="promoted",
        revision=1,
        metadata={"promoted_entity_id": "procedure_v3_live"},
    )
    for memory in (draft, open_draft, child):
        await save_raw_memory(memory, embedding_provider=None)

    assert await retire_superseded_reflection_drafts(
        organization_id=ORG_ID, promoted_candidate_id=child.id
    ) == [draft.id]
    assert await superseded_draft_ids(ORG_ID) == [draft.id]
    # The write is unconditional, so this really does collide with the unique
    # index and exercise the duplicate branch rather than an early read.
    assert await _record_supersession(draft, child) is False
    assert await superseded_draft_ids(ORG_ID) == [draft.id]
    # A second retirement pass reaches the same branch through the walk.
    assert (
        await retire_superseded_reflection_drafts(
            organization_id=ORG_ID, promoted_candidate_id=child.id
        )
        == []
    )

    listed = await list_reflection_candidate_reviews(
        organization_id=ORG_ID, review_state="pending", limit=10
    )

    assert [memory.id for memory in listed] == [open_draft.id]
    # The exclusion happens inside the query, so a retired draft never consumes
    # a page slot and a single-row page still reaches real work.
    first_page = await list_reflection_candidate_reviews(
        organization_id=ORG_ID, review_state="pending", limit=1
    )
    assert [memory.id for memory in first_page] == [open_draft.id]


async def test_a_non_duplicate_write_failure_is_not_swallowed(content_store: None) -> None:
    """Only the unique-index collision is benign; anything else must surface."""
    from sibyl_core.services import content_client
    from sibyl_core.services.reflection_supersession import _record_supersession

    execution_id = "b" * 64
    draft = _candidate("bbbbbbbb-bbbb-5bbb-8bbb-bbbbbbbbbbbb", revision=1)
    child = _corrected(
        _child_id(execution_id),
        parent_id=draft.id,
        execution_id=execution_id,
        review_state="promoted",
        revision=1,
    )

    async def broken(*_args, **_kwargs):
        raise RuntimeError("content store unreachable")

    original = content_client.select_many
    content_client.select_many = broken  # type: ignore[assignment]
    try:
        with pytest.raises(RuntimeError, match="unreachable"):
            await _record_supersession(draft, child)
    finally:
        content_client.select_many = original  # type: ignore[assignment]


async def test_promotion_never_fails_on_a_retirement_error(monkeypatch) -> None:
    """Retirement is bookkeeping after a committed write, so it cannot 500."""
    from sibyl_core.services import memory_reflection, reflection_supersession
    from sibyl_core.services.memory_contract import ReflectionPromotionResult

    async def explode(**_kwargs):
        raise RuntimeError("supersession write failed")

    monkeypatch.setattr(reflection_supersession, "retire_superseded_reflection_drafts", explode)
    promoted = ReflectionPromotionResult(
        success=True,
        candidate_id="child-1",
        promoted_id="procedure_v3_abc",
        reason="promoted",
        review_state="promoted",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
    )

    assert await memory_reflection._retire_superseded_drafts(promoted, ORG_ID) is promoted


async def test_only_a_successful_promotion_retires_drafts(monkeypatch) -> None:
    """A denied or replayed-pending promotion must not retire anything."""
    from sibyl_core.services import memory_reflection, reflection_supersession
    from sibyl_core.services.memory_contract import ReflectionPromotionResult

    calls: list[str] = []

    async def record(*, organization_id, promoted_candidate_id):
        calls.append(promoted_candidate_id)
        return []

    monkeypatch.setattr(reflection_supersession, "retire_superseded_reflection_drafts", record)

    def _result(**overrides) -> ReflectionPromotionResult:
        values = {
            "success": True,
            "candidate_id": "child-1",
            "promoted_id": "procedure_v3_abc",
            "reason": "promoted",
            "review_state": "promoted",
            "memory_scope": MemoryScope.PRIVATE,
            "scope_key": None,
            "raw_source_ids": ["source-1"],
        }
        values.update(overrides)
        return ReflectionPromotionResult(**values)  # type: ignore[arg-type]

    await memory_reflection._retire_superseded_drafts(_result(), ORG_ID)
    await memory_reflection._retire_superseded_drafts(_result(success=False), ORG_ID)
    await memory_reflection._retire_superseded_drafts(_result(review_state="pending"), ORG_ID)

    assert calls == ["child-1"]


async def test_the_promotion_entry_points_are_wired_to_retirement(monkeypatch) -> None:
    """Pin the two splices in promote_reflection_candidate_review, not just the helper."""
    from types import SimpleNamespace

    from sibyl_core.services import memory_reflection
    from sibyl_core.services.memory_contract import ReflectionPromotionResult

    promoted = ReflectionPromotionResult(
        success=True,
        candidate_id="child-1",
        promoted_id="procedure_v3_abc",
        reason="promoted",
        review_state="promoted",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
    )
    plan = SimpleNamespace(
        candidate_memory=SimpleNamespace(id="child-1", revision=1),
        raw_source_ids=["source-1"],
        target_scope=MemoryScope.PRIVATE,
        target_scope_key=None,
    )

    # The replay path returns a finished result straight from the planner; the
    # write path goes through _apply_promotion_plan. Both splices must retire.
    for planned in (promoted, plan):
        seen: list[str] = []

        async def retire(result, organization_id, _seen=seen):
            _seen.append(result.candidate_id)
            return result

        monkeypatch.setattr(memory_reflection, "_retire_superseded_drafts", retire)
        monkeypatch.setattr(
            memory_reflection,
            "_resolve_reflection_promotion_plan",
            AsyncMock(return_value=planned),
        )
        monkeypatch.setattr(
            memory_reflection, "_enqueue_promoted_embedding", AsyncMock(return_value=promoted)
        )
        monkeypatch.setattr(
            memory_reflection, "_apply_promotion_plan", AsyncMock(return_value=promoted)
        )

        result = await memory_reflection.promote_reflection_candidate_review(
            candidate_id="child-1",
            organization_id=ORG_ID,
            principal_id=USER_ID,
            promote_to_scope="private",
        )

        assert result is promoted
        assert seen == ["child-1"]
