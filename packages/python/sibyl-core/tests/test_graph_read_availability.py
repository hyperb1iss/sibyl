"""Public readers use current stored entities and their protected ancestry."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services import graph_read_availability as availability


def entity(identifier, **kwargs):
    return Entity(
        id=identifier,
        name=identifier,
        entity_type=EntityType.PATTERN,
        organization_id=kwargs.pop("organization_id", "org"),
        **kwargs,
    )


async def test_graph_availability_refreshes_actual_rows_and_denies_unavailable(monkeypatch):
    rows = [
        entity("ordinary"),
        entity("protected"),
        entity("retired", metadata={"excluded_from_recall": True}),
        entity("foreign", organization_id="other"),
    ]
    manager = SimpleNamespace(get_many=AsyncMock(return_value=rows))
    guard = AsyncMock(return_value={"protected"})
    monkeypatch.setattr(availability, "unavailable_publication_ids", guard)
    result = await availability.available_graph_entities(
        "org",
        ["ordinary", "protected", "retired", "foreign", "missing"],
        runtime=SimpleNamespace(entity_manager=manager, client="owned"),
    )
    assert result == {"ordinary": rows[0]}
    guard.assert_awaited_once_with(
        "org",
        {"ordinary": {}, "protected": {}},
        graph_entities={"ordinary": rows[0], "protected": rows[1]},
        graph_client="owned",
    )
    rows[0].name = "current source name"
    again = await availability.available_graph_entities(
        "org", ["ordinary"], runtime=SimpleNamespace(entity_manager=manager, client="owned")
    )
    assert again["ordinary"].name == "current source name"


async def test_graph_availability_batches_deduplicates_and_uses_checked_owner(monkeypatch):
    ids = [str(index) for index in range(1025)]
    manager = SimpleNamespace(
        get_many=AsyncMock(side_effect=lambda batch: [entity(i) for i in batch])
    )
    guard = AsyncMock(return_value=set())
    monkeypatch.setattr(availability, "unavailable_publication_ids", guard)
    result = await availability.available_graph_entities(
        "org", ids + ids, runtime=SimpleNamespace(entity_manager=manager, client="owned")
    )
    assert list(result) == ids
    assert [len(call.args[0]) for call in manager.get_many.await_args_list] == [512, 512, 1]
    assert guard.await_count == 1


async def test_graph_availability_empty_does_not_allocate_runtime(monkeypatch):
    factory = AsyncMock()
    monkeypatch.setattr(availability, "get_surreal_graph_runtime", factory)
    assert await availability.available_graph_entities("org", []) == {}
    factory.assert_not_awaited()


async def test_graph_availability_propagates_owner_failure(monkeypatch):
    manager = SimpleNamespace(get_many=AsyncMock(return_value=[entity("protected")]))
    monkeypatch.setattr(
        availability,
        "unavailable_publication_ids",
        AsyncMock(side_effect=RuntimeError("unavailable")),
    )
    with pytest.raises(RuntimeError, match="unavailable"):
        await availability.available_graph_entities(
            "org", ["protected"], runtime=SimpleNamespace(entity_manager=manager, client="owned")
        )
