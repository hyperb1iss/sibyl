"""Entity list windows push archived and private-scope exclusions into SurrealQL."""

from __future__ import annotations

from typing import Any

import pytest

from sibyl_core.models.entities import EntityType
from sibyl_core.services.graph_entities import EntityManager

GROUP_ID = "org-list"


class _RecordingClient:
    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def execute_query(self, query: str, **params: Any) -> list[dict[str, Any]]:
        self.queries.append((query, params))
        return []


def _manager() -> tuple[EntityManager, _RecordingClient]:
    client = _RecordingClient()
    return EntityManager(client, group_id=GROUP_ID), client  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_list_all_window_is_one_statement_with_the_readers_private_rule() -> None:
    manager, client = _manager()

    rows = await manager.list_all(
        limit=21,
        offset=40,
        include_archived=False,
        include_content=False,
        exact_window=True,
        private_memory_owner="user-1",
    )

    assert rows == []
    assert len(client.queries) == 1
    query, params = client.queries[0]
    assert "LIMIT $limit START $offset" in query
    assert params["limit"] == 21
    assert params["offset"] == 40
    assert params["private_memory_owner"] == "user-1"
    assert "string::lowercase(status ?? attributes.status ?? '') != 'archived'" in query
    assert "(attributes.memory_scope ?? memory_scope) = 'private'" in query
    assert "attributes.principal_id != $private_memory_owner" in query
    assert "OMIT content" in query


@pytest.mark.asyncio
async def test_list_by_type_window_can_drop_every_private_row() -> None:
    manager, client = _manager()

    await manager.list_by_type(
        EntityType.NOTE,
        limit=21,
        offset=0,
        include_archived=False,
        exact_window=True,
        exclude_private_memory=True,
    )

    assert len(client.queries) == 1
    query, params = client.queries[0]
    assert "(attributes.memory_scope ?? memory_scope) != 'private'" in query
    assert "private_memory_owner" not in params
    assert "(status IS NONE OR status = '' OR status != 'archived')" in query
    assert params["entity_type"] == "note"


@pytest.mark.asyncio
async def test_list_all_without_scope_kwargs_keeps_the_plain_statement() -> None:
    manager, client = _manager()

    await manager.list_all(limit=5, offset=0, include_archived=True, exact_window=True)

    query, params = client.queries[0]
    assert "memory_scope" not in query
    assert "archived" not in query
    assert params == {"group_id": GROUP_ID, "limit": 5, "offset": 0}
