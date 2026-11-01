"""Description stays index text on the real retrieval path.

The unit tests stub the search, so they prove the projection but not that the
row was found through its description in the first place. These write a real
row into an embedded SurrealDB graph and read it back through
`compile_context`, the entry an agent reaches, so the full-text lane matches on
the stored description and the pack serializer decides what the reader sees.
"""

from __future__ import annotations

import json

import pytest

from sibyl_core.tools.context import context_pack_to_dict, render_context_pack
from tests.test_supersession_live_graph import _entity, _pack, _Runtime
from tests.test_supersession_live_graph import content_store as content_store
from tests.test_supersession_live_graph import graph as graph

GOAL_CARRY = "Goal: rotate the zephyrine credentials before the quarterly audit"
SPAN_BODY = "Rotate keys through the vault CLI, then restart the gateway."


@pytest.mark.asyncio
async def test_a_description_term_retrieves_the_row_without_reaching_the_reader(
    graph: _Runtime,
) -> None:
    row = _entity(graph, "goal-carry-row", "Credential rotation", SPAN_BODY)
    await graph.entity_manager.create_direct(row.model_copy(update={"description": GOAL_CARRY}))

    pack = await _pack(graph, "zephyrine")

    payload = context_pack_to_dict(pack)
    items = {item["id"]: item for section in payload["sections"] for item in section["items"]}
    assert "goal-carry-row" in items, "the description term alone has to retrieve the row"
    assert items["goal-carry-row"]["content"] == SPAN_BODY
    assert "zephyrine" not in json.dumps(items, default=str)
    assert "quarterly audit" not in json.dumps(payload, default=str)
    markdown = render_context_pack(pack).markdown
    assert SPAN_BODY in markdown
    assert "quarterly audit" not in markdown
