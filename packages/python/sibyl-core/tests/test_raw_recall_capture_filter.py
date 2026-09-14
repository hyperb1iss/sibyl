"""Fixed capture membership is applied before retrieval candidate limits."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.services import content_client, content_raw_recall
from sibyl_core.services.content_models import RawMemoryRecallResult
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import (
    recall_raw_memory,
    recall_raw_memory_with_sources,
    remember_raw_memory,
)
from tests.test_reflection_identity import content_store as content_store


@pytest.mark.parametrize("recall", [recall_raw_memory, recall_raw_memory_with_sources])
@pytest.mark.parametrize("capture_ids", [[], ["", "   "]])
async def test_empty_capture_membership_does_not_dispatch(monkeypatch, recall, capture_ids):
    forbidden = AsyncMock(side_effect=AssertionError("empty membership must not dispatch"))
    monkeypatch.setattr(content_raw_recall, "raw_memory_query_embedding", forbidden)
    monkeypatch.setattr(content_client, "surreal_content_client", forbidden)
    result = await recall(
        organization_id="org", principal_id="owner", query="telescope", capture_ids=capture_ids
    )
    assert not (result.memories if isinstance(result, RawMemoryRecallResult) else result)
    forbidden.assert_not_called()


@pytest.mark.parametrize("recall", [recall_raw_memory, recall_raw_memory_with_sources])
@pytest.mark.parametrize("lexical_fallback", [False, True])
async def test_capture_membership_preserves_scope_and_lifecycle(
    content_store, monkeypatch, recall, lexical_fallback
):
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

    async def remember(source, *, organization=org, principal="owner"):
        return await remember_raw_memory(
            organization_id=organization,
            principal_id=principal,
            source_id=source,
            raw_content=f"Telescope observation {source}",
            embedding_provider=None,
        )

    retained = await remember("original-source")
    await remember("later-source")
    foreign_org = await remember("foreign-org", organization=str(uuid4()))
    foreign_principal = await remember("foreign-principal", principal="other")

    async def selected(**filters):
        result = await recall(
            organization_id=org, principal_id="owner", query="telescope", **filters
        )
        return list(result.memories) if isinstance(result, RawMemoryRecallResult) else result

    assert len(await selected()) == 2
    capture_ids = [f" {retained.id} ", retained.id, foreign_org.id, foreign_principal.id]
    assert [m.id for m in await selected(capture_ids=capture_ids)] == [retained.id]
    assert await selected(capture_ids=[retained.source_id]) == []
    assert await selected(capture_ids=capture_ids, source_ids=["later-source"]) == []
    assert [
        m.id for m in await selected(capture_ids=capture_ids, source_ids=["original-source"])
    ] == [retained.id]
    await apply_memory_correction(
        organization_id=org, principal_id="owner", source_id=retained.id, action="hide"
    )
    assert await selected(capture_ids=capture_ids) == []


@pytest.mark.parametrize("lexical_fallback", [False, True])
async def test_capture_membership_precedes_text_candidate_limit(
    content_store, monkeypatch, lexical_fallback
):
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
    memories = [
        await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id=f"observation-{index}",
            raw_content="Telescope observation",
            embedding_provider=None,
        )
        for index in range(9)
    ]
    result = await recall_raw_memory(
        organization_id=org,
        principal_id="owner",
        query="telescope",
        capture_ids=[memories[0].id],
        limit=1,
    )
    assert [memory.id for memory in result] == [memories[0].id]
