"""Bulk projection writes select the connected database's function spelling."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl_core.services.graph_entity_store import _execute_replace_entities_bulk_query


@pytest.mark.parametrize(
    ("url", "expected", "absent"),
    [
        ("memory://", "type::is::object", "type::is_object"),
        ("ws://127.0.0.1:33333/rpc", "type::is_object", "type::is::object"),
    ],
)
async def test_bulk_projection_renders_object_checks_for_backend(url, expected, absent):
    client = SimpleNamespace(_url=url, execute_query=AsyncMock(return_value=[]))
    await _execute_replace_entities_bulk_query(client, [])
    query = client.execute_query.await_args.args[0]
    assert expected in query
    assert absent not in query
    assert client.execute_query.await_args.kwargs == {"rows": []}
