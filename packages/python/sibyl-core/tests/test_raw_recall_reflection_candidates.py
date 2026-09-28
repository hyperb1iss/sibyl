"""Raw recall serves promoted dream proposals, never drafts the critic has not accepted."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from sibyl_core.services import content_client, content_raw_recall, validation_promotion
from sibyl_core.services.content_models import (
    RawMemory,
    RawMemoryRecallResult,
    raw_memory_unpublished_reflection_candidate,
)
from sibyl_core.services.surreal_content import (
    MemoryScope,
    recall_raw_memory,
    recall_raw_memory_with_sources,
    remember_raw_memory,
)
from tests.test_reflection_identity import content_store as content_store


def _memory(**overrides: object) -> RawMemory:
    values: dict[str, object] = {
        "id": "candidate",
        "organization_id": "org",
        "source_id": "source",
        "principal_id": "owner",
        "memory_scope": MemoryScope.PRIVATE,
        "scope_key": None,
        "review_state": "pending",
        "entity_type": "procedure",
        "title": "Reject malformed percent escapes",
        "raw_content": "Validate every escape before decoding once.",
        "tags": [],
        "metadata": {},
        "provenance": {},
        "capture_surface": "reflection_candidate",
        "captured_at": datetime(2026, 9, 23, tzinfo=UTC),
        "created_at": datetime(2026, 9, 23, tzinfo=UTC),
        "revision": 1,
    }
    values.update(overrides)
    return RawMemory(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "unpublished"),
    [
        ({}, True),
        ({"review_state": "PENDING"}, True),
        ({"review_state": "promoted"}, False),
        ({"review_state": " Promoted "}, False),
        ({"capture_surface": None, "metadata": {"capture_surface": "reflection_candidate"}}, True),
        ({"capture_surface": "verified_eval"}, False),
        ({"capture_surface": None}, False),
    ],
)
def test_only_an_unpromoted_reflection_candidate_is_unpublished(overrides, unpublished) -> None:
    assert raw_memory_unpublished_reflection_candidate(_memory(**overrides)) is unpublished


async def _set_review_state(memory_id: str, review_state: str) -> None:
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET review_state = $state WHERE uuid = $id;",
            state=review_state,
            id=memory_id,
        )


@pytest.mark.parametrize("recall", [recall_raw_memory, recall_raw_memory_with_sources])
@pytest.mark.parametrize("lexical_fallback", [False, True])
async def test_recall_serves_the_promoted_correction_not_its_draft_or_stalled_child(
    content_store, monkeypatch, recall, lexical_fallback
):
    """The 2026-09-23 scoped cycle's lineage, shaped the same way.

    A draft proposal was corrected, the correction was promoted, and the draft
    was retired as superseded in the supersession ledger while its raw row kept
    review_state pending. A second chain stalled with its correction child
    pending. Before the reader gate, recall returned the draft and the stalled
    child beside the promoted correction, so an agent read two unreviewed
    proposals as memory.
    """
    monkeypatch.setattr(
        content_raw_recall, "raw_memory_query_embedding", AsyncMock(return_value=None)
    )
    if lexical_fallback:
        monkeypatch.setattr(
            content_raw_recall,
            "_recall_raw_memory_fulltext",
            AsyncMock(side_effect=RuntimeError("exercise lexical fallback")),
        )
    org = str(uuid4())

    async def remember(source: str, **kwargs: object) -> RawMemory:
        return await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id=source,
            raw_content=f"Telescope routing lesson from {source}",
            embedding_provider=None,
            **kwargs,  # type: ignore[arg-type]
        )

    episode = await remember("episode")
    draft = await remember("draft", capture_surface="reflection_candidate")
    promoted = await remember(
        "correction",
        capture_surface="reflection_candidate",
        metadata={"automatic_correction": {"parent_id": draft.id, "execution_id": "exec-1"}},
    )
    stalled = await remember(
        "stalled-child",
        capture_surface="reflection_candidate",
        metadata={"automatic_correction": {"parent_id": "archived", "execution_id": "exec-2"}},
    )
    await _set_review_state(promoted.id, "promoted")

    result = await recall(organization_id=org, principal_id="owner", query="telescope")
    memories = result.memories if isinstance(result, RawMemoryRecallResult) else result
    recalled = {memory.id for memory in memories}

    assert episode.id in recalled
    assert promoted.id in recalled
    assert draft.id not in recalled
    assert stalled.id not in recalled


async def test_an_unreadable_stored_result_reads_unavailable_and_says_so(monkeypatch) -> None:
    class _Strict(BaseModel):
        status: int

    try:
        _Strict.model_validate({"status": "ordinary_cohort_proposal"})
    except ValidationError as exc:
        schema_error = exc
    execution = AsyncMock()
    execution.load.return_value = {"state": "returned"}
    execution.result.side_effect = schema_error
    monkeypatch.setattr(validation_promotion, "ValidationExecution", lambda *_args: execution)
    monkeypatch.setattr(validation_promotion, "validated_result", lambda *_args: None)
    monkeypatch.setattr(
        validation_promotion.ValidationBinding,
        "model_validate_json",
        classmethod(lambda _cls, _value: type("Binding", (), {"execution_id": "exec-9"})()),
    )
    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        validation_promotion.log,
        "warning",
        lambda event, **fields: warnings.append((event, fields)),
    )
    memory = replace(_memory(), organization_id="org-9", id="candidate-9")

    current = await validation_promotion.validation_binding_current(
        memory, {"validation_binding_json": "{}"}
    )

    assert current is False
    assert warnings == [
        (
            "validation_result_unreadable",
            {
                "organization_id": "org-9",
                "candidate_id": "candidate-9",
                "execution_id": "exec-9",
                "error_count": 1,
            },
        )
    ]


async def test_an_ordinary_invalidation_stays_quiet(monkeypatch) -> None:
    execution = AsyncMock()
    execution.load.return_value = {"state": "returned"}
    execution.result.side_effect = ValueError("binding changed")
    monkeypatch.setattr(validation_promotion, "ValidationExecution", lambda *_args: execution)
    monkeypatch.setattr(validation_promotion, "validated_result", lambda *_args: None)
    monkeypatch.setattr(
        validation_promotion.ValidationBinding,
        "model_validate_json",
        classmethod(lambda _cls, _value: type("Binding", (), {"execution_id": "exec-9"})()),
    )
    warnings: list[str] = []
    monkeypatch.setattr(
        validation_promotion.log, "warning", lambda event, **_fields: warnings.append(event)
    )

    assert (
        await validation_promotion.validation_binding_current(
            _memory(), {"validation_binding_json": "{}"}
        )
        is False
    )
    assert warnings == []
