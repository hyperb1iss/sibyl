from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from sibyl.jobs.reflection import run_reflection_dream_cycle
from sibyl_core.models.reflection import ReflectionPack
from sibyl_core.services.memory import (
    ReflectionPromotionPreview,
    ReflectionPromotionResult,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory

ORG_ID = "00000000-0000-0000-0000-000000000111"
USER_ID = "00000000-0000-0000-0000-000000000222"


def _raw_memory(**overrides: object) -> RawMemory:
    values = {
        "id": "source-1",
        "organization_id": ORG_ID,
        "source_id": "cli:manual",
        "principal_id": USER_ID,
        "memory_scope": MemoryScope.PRIVATE,
        "scope_key": None,
        "project_id": None,
        "review_state": "pending",
        "entity_type": "raw_memory",
        "title": "Session notes",
        "raw_content": "We decided reflection should run automatically.",
        "tags": ["memory"],
        "metadata": {"domain": "sibyl"},
        "provenance": {},
        "capture_surface": "cli",
        "captured_at": datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        "created_at": datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC),
        "score": 0.0,
    }
    values.update(overrides)
    return RawMemory(**values)


def _reflection_pack(*, persisted_count: int = 1) -> ReflectionPack:
    return ReflectionPack(
        source_title="Session notes",
        source_id="source-1",
        intent="maintenance",
        domain="sibyl",
        project=None,
        candidates=[SimpleNamespace()],
        total_candidates=1,
        persisted_count=persisted_count,
    )


def _preview(
    *,
    candidate_id: str = "candidate-1",
    metadata: dict[str, object] | None = None,
) -> ReflectionPromotionPreview:
    return ReflectionPromotionPreview(
        allowed=True,
        candidate_id=candidate_id,
        reason="promotion_preview_allowed",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
        metadata={"confidence": 0.94, **(metadata or {})},
    )


def _promotion(candidate_id: str = "candidate-1") -> ReflectionPromotionResult:
    return ReflectionPromotionResult(
        success=True,
        candidate_id=candidate_id,
        promoted_id="promoted-1",
        reason="accepted_reflection_candidate",
        review_state="promoted",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
    )


@pytest.mark.asyncio
async def test_reflection_dream_cycle_reflects_sources_and_promotes_candidates() -> None:
    source = _raw_memory(id="source-1")
    candidate = _raw_memory(
        id="candidate-1",
        capture_surface="reflection_candidate",
        raw_content="Reflection should run automatically.",
        metadata={"suggested_memory_scope": "private", "confidence": 0.94},
    )

    with (
        patch(
            "sibyl.jobs.reflection.list_reflection_dream_source_memories",
            AsyncMock(return_value=[source]),
        ),
        patch(
            "sibyl.jobs.reflection.reflect_memory",
            AsyncMock(return_value=_reflection_pack()),
        ) as reflect,
        patch("sibyl.jobs.reflection.save_raw_memory", AsyncMock(return_value=source)) as save,
        patch(
            "sibyl.jobs.reflection.list_reflection_candidate_reviews",
            AsyncMock(return_value=[candidate]),
        ),
        patch(
            "sibyl.jobs.reflection.resolve_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.jobs.reflection.preview_reflection_candidate_promotion",
            AsyncMock(return_value=_preview()),
        ),
        patch(
            "sibyl.jobs.reflection.promote_reflection_candidate_review",
            AsyncMock(return_value=_promotion()),
        ) as promote,
        patch("sibyl.jobs.reflection.log_memory_audit_event", AsyncMock()) as audit,
    ):
        receipt = await run_reflection_dream_cycle(
            {},
            ORG_ID,
            source_limit=1,
            candidate_limit=1,
        )

    reflect.assert_awaited_once()
    reflect_kwargs = reflect.await_args.kwargs
    assert reflect_kwargs["existing_source_id"] == "source-1"
    assert reflect_kwargs["persist"] is True
    assert reflect_kwargs["persist_source"] is False
    assert reflect_kwargs["persist_review"] is True
    promote.assert_awaited_once()
    assert save.await_count == 1
    assert audit.await_count == 1
    assert receipt["sources_reflected"] == 1
    assert receipt["promoted"] == 1
    assert receipt["failed"] == 0


@pytest.mark.asyncio
async def test_reflection_dream_cycle_dry_run_writes_no_memory() -> None:
    source = _raw_memory(id="source-1")
    candidate = _raw_memory(
        id="candidate-1",
        capture_surface="reflection_candidate",
        metadata={"suggested_memory_scope": "private", "confidence": 0.94},
    )

    with (
        patch(
            "sibyl.jobs.reflection.list_reflection_dream_source_memories",
            AsyncMock(return_value=[source]),
        ),
        patch(
            "sibyl.jobs.reflection.reflect_memory",
            AsyncMock(return_value=_reflection_pack(persisted_count=0)),
        ) as reflect,
        patch("sibyl.jobs.reflection.save_raw_memory", AsyncMock()) as save,
        patch(
            "sibyl.jobs.reflection.list_reflection_candidate_reviews",
            AsyncMock(return_value=[candidate]),
        ),
        patch(
            "sibyl.jobs.reflection.resolve_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.jobs.reflection.preview_reflection_candidate_promotion",
            AsyncMock(return_value=_preview()),
        ),
        patch("sibyl.jobs.reflection.promote_reflection_candidate_review", AsyncMock()) as promote,
        patch("sibyl.jobs.reflection.log_memory_audit_event", AsyncMock()),
    ):
        receipt = await run_reflection_dream_cycle(
            {},
            ORG_ID,
            dry_run=True,
            source_limit=1,
            candidate_limit=1,
        )

    reflect_kwargs = reflect.await_args.kwargs
    assert reflect_kwargs["persist"] is False
    assert reflect_kwargs["persist_review"] is False
    save.assert_not_awaited()
    promote.assert_not_awaited()
    assert receipt["dry_run"] is True
    assert receipt["promoted"] == 1
    assert receipt["archived"] == 0


@pytest.mark.asyncio
async def test_reflection_dream_cycle_archives_terminal_exception_candidates() -> None:
    candidate = _raw_memory(
        id="candidate-duplicate",
        capture_surface="reflection_candidate",
        metadata={
            "candidate_duplicate_of_source_id": "source-0",
            "suggested_memory_scope": "private",
            "confidence": 0.94,
        },
    )
    archived = _raw_memory(
        id="candidate-duplicate",
        capture_surface="reflection_candidate",
        review_state="archived",
    )

    with (
        patch(
            "sibyl.jobs.reflection.list_reflection_dream_source_memories",
            AsyncMock(return_value=[]),
        ),
        patch("sibyl.jobs.reflection.save_raw_memory", AsyncMock(return_value=archived)) as save,
        patch(
            "sibyl.jobs.reflection.list_reflection_candidate_reviews",
            AsyncMock(return_value=[candidate]),
        ),
        patch(
            "sibyl.jobs.reflection.resolve_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch(
            "sibyl.jobs.reflection.preview_reflection_candidate_promotion",
            AsyncMock(
                return_value=_preview(
                    candidate_id="candidate-duplicate",
                    metadata={"candidate_duplicate_of_source_id": "source-0"},
                )
            ),
        ),
        patch("sibyl.jobs.reflection.promote_reflection_candidate_review", AsyncMock()) as promote,
        patch("sibyl.jobs.reflection.log_memory_audit_event", AsyncMock()),
    ):
        receipt = await run_reflection_dream_cycle(
            {},
            ORG_ID,
            dry_run=False,
            source_limit=0,
            candidate_limit=1,
        )

    promote.assert_not_awaited()
    save.assert_awaited_once()
    saved_memory = save.await_args.args[0]
    assert saved_memory.review_state == "archived"
    assert receipt["archived"] == 1
    assert receipt["exceptioned"] == 1
    assert receipt["candidates"][0]["exception_reasons"] == ["duplicate_candidate"]
