"""Read-model identity stays bound to the actual protected graph association."""

import pytest

from sibyl_core.services import graph_read_availability as availability
from sibyl_core.services.graph_derivations import graph_target_digest
from tests.test_graph_passage_derivations import published_passages
from tests.test_synthesis_source_observations import content_store as content_store
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)
from tests.test_synthesis_source_observations import runtime as runtime


@pytest.mark.parametrize("changed", [False, True])
async def test_graph_availability_binds_the_returned_target_to_its_association(
    runtime, content_store, monkeypatch, changed
):
    _source, target, _projection, _passage = await published_passages(
        runtime, content_store, monkeypatch
    )
    guard = availability.unavailable_publication_ids

    async def replace_between_reads(org, rows, **kwargs):
        if changed:
            await runtime.entity_manager.update(target.id, {"name": "New current publication"})
            replacement = await runtime.entity_manager.get(target.id)
            await runtime.client.execute_query(
                "UPDATE memory_derivations SET body_sha256=$digest WHERE target_id=$id;",
                id=target.id,
                digest=graph_target_digest(replacement),
            )
            # Both states have valid ancestry. Only returning the old model is wrong.
            assert not await guard(org, {replacement.id: replacement.metadata})
        return await guard(org, rows, **kwargs)

    monkeypatch.setattr(availability, "unavailable_publication_ids", replace_between_reads)
    current = await availability.available_graph_entities(
        runtime.client.group_id, [target.id], runtime=runtime
    )
    assert set(current) == (set() if changed else {target.id})
    if not changed:
        assert current[target.id].name == target.name


async def test_graph_availability_retired_original_hides_protected_descendants(
    runtime, content_store, monkeypatch
):
    source, target, _projection, passage = await published_passages(
        runtime, content_store, monkeypatch
    )
    assert set(
        await availability.available_graph_entities(
            runtime.client.group_id, [target.id, passage.id], runtime=runtime
        )
    ) == {target.id, passage.id}
    await runtime.entity_manager.update(source.id, {"content": "Original evidence withdrawn"})
    assert not await availability.available_graph_entities(
        runtime.client.group_id, [target.id, passage.id], runtime=runtime
    )


async def test_graph_availability_supplied_runtime_owns_association_snapshot(
    runtime, content_store, monkeypatch
):
    from unittest.mock import AsyncMock

    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services import graph_runtime

    await runtime.entity_manager.create_direct(
        Entity(id="explicit-owner", name="Explicit runtime", entity_type=EntityType.PATTERN)
    )
    fallback = AsyncMock(side_effect=AssertionError("unrelated runtime lookup"))
    monkeypatch.setattr(graph_runtime, "get_surreal_graph_runtime", fallback)
    actual = await availability.available_graph_entities(
        runtime.client.group_id, ["explicit-owner"], runtime=runtime
    )
    assert actual["explicit-owner"].name == "Explicit runtime"
    fallback.assert_not_awaited()
