"""The MCP tool schema names the predicate vocabulary agents are meant to use.

A model picks parameter values out of the schema it is handed, so a vocabulary
that only exists in a docstring is a vocabulary no agent can reliably reach.
Both write tools that reach `add()` have to carry it.
"""

from __future__ import annotations

import pytest

from sibyl.server import create_mcp_server
from sibyl_core.models.relations import DECLARABLE_RELATIONSHIP_PREDICATES

DECLARING_TOOLS = ("add", "remember")


def _related_to_schema(tool_name: str) -> dict[str, object]:
    mcp = create_mcp_server()
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}
    assert tool_name in tools, sorted(tools)
    properties = tools[tool_name].parameters["properties"]
    assert "related_to" in properties, sorted(properties)
    return properties["related_to"]


@pytest.mark.parametrize("tool_name", DECLARING_TOOLS)
def test_related_to_schema_names_every_predicate(tool_name: str) -> None:
    description = str(_related_to_schema(tool_name).get("description", ""))
    assert description, f"{tool_name}.related_to carries no description"
    for predicate in DECLARABLE_RELATIONSHIP_PREDICATES:
        assert f"{predicate}:" in description, (predicate, description)


@pytest.mark.parametrize("tool_name", DECLARING_TOOLS)
def test_related_to_schema_states_the_direction(tool_name: str) -> None:
    description = str(_related_to_schema(tool_name).get("description", ""))
    assert "subject" in description.lower()


@pytest.mark.parametrize("tool_name", DECLARING_TOOLS)
def test_related_to_still_accepts_a_bare_id_list(tool_name: str) -> None:
    """The declaration channel stays optional and stays a list of strings."""
    rendered = repr(_related_to_schema(tool_name))
    assert "array" in rendered
    assert "string" in rendered
