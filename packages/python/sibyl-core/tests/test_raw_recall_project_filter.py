"""Project selection narrows every raw lane before its candidate limit."""

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl_core.services import content_raw_recall
from sibyl_core.services.surreal_content import recall_raw_memory, remember_raw_memory
from tests.test_reflection_identity import content_store as content_store


@pytest.mark.parametrize("lexical_fallback", [False, True])
async def test_project_selection_preserves_private_owner_and_precedes_text_limits(
    content_store, monkeypatch, lexical_fallback
):
    monkeypatch.setattr(
        content_raw_recall, "raw_memory_query_embedding", AsyncMock(return_value=None)
    )
    if lexical_fallback:
        monkeypatch.setattr(
            content_raw_recall,
            "_recall_raw_memory_fulltext",
            AsyncMock(side_effect=RuntimeError("lexical fallback")),
        )
    org = str(uuid4())

    async def remember(project, principal="owner"):
        return await remember_raw_memory(
            organization_id=org,
            principal_id=principal,
            metadata={"project_id": project},
            source_id=str(uuid4()),
            raw_content="Telescope observation",
            embedding_provider=None,
        )

    retained = await remember("a")
    other_selected = await remember("b")
    await remember("a", principal="foreign")
    for _ in range(9):
        await remember("c")

    async def selected(**kwargs):
        return await recall_raw_memory(
            organization_id=org, principal_id="owner", query="telescope", **kwargs
        )

    assert [m.id for m in await selected(project_ids=["a"], limit=1)] == [retained.id]
    assert {m.id for m in await selected(project_ids=["a", "b"])} == {
        retained.id,
        other_selected.id,
    }
    assert len(await selected(limit=50)) == 11
    assert await selected(project_ids=[]) == []
    assert await selected(project_id="a", project_ids=["b"]) == []
