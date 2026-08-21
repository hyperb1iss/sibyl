"""Every registered MCP entry point enforces scopes, observed by invoking it.

This walks whatever the MCPServer managers hold (tools, resources, resource
templates, prompts), not a hand-written list, so an entry point that ships
ungated is exercised here the moment it is registered. Each one is invoked
under three credentials:

- unauthorized: no `mcp` scope at all, so every entry point must refuse
- read-only: `mcp` plus `api:read`, so write tools must refuse and reads proceed
- write: `mcp` plus `api:write`, so write tools must get past the gate

Refusal is not enough on its own. Every call runs with the store boundaries
replaced by recorders, and a refused call has to leave every recorder empty:
"nothing was written before the refusal" is then observed rather than argued
from the shape of the source.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.mcp_tools.context import McpContext
from sibyl.server import create_mcp_server

# Tools that mutate state. Registering a tool without listing it here fails the
# inventory test, which is what forces a new tool to be classified and gated.
WRITE_TOOLS = frozenset({"add", "remember", "reflect", "manage", "synthesis_draft"})

READ_TOOLS = frozenset(
    {
        "search",
        "context",
        "explore",
        "expand_neighbors",
        "fetch_slice",
        "synthesis_plan",
        "synthesis_verify",
        "logs",
    }
)

RESOURCES = frozenset({"sibyl://health", "sibyl://stats"})

# Every call the MCP surface makes into the store. A refused call must leave
# all of them untouched, and an allowed write must reach at least one, which is
# what keeps this list honest: a write path that stops being covered here fails
# the write-authority test rather than silently weakening the refusal claim.
STORE_BOUNDARIES: tuple[tuple[str, str], ...] = (
    ("sibyl_core.tools.core", "add"),
    ("sibyl_core.tools.core", "reflect_memory"),
    ("sibyl_core.tools.core", "synthesis_draft"),
    ("sibyl_core.services.surreal_content", "remember_raw_memory"),
    ("sibyl_core.tools.manage", "manage"),
    ("sibyl_core.services.memory", "apply_memory_correction"),
    ("sibyl.mcp_tools.management", "log_memory_audit_event"),
    ("sibyl.services.work_item_workflow", "transition_work_item"),
)

# Reads the gated code performs on its way to the store boundaries above.
# Stubbed so the probes run without a live graph; none of them are mutations.
READ_SCAFFOLDING: tuple[tuple[str, str, Any], ...] = (
    ("sibyl.mcp_tools.context", "get_accessible_projects", {"project-a"}),
    ("sibyl.mcp_tools.observability", "has_owner_membership", True),
    ("sibyl.mcp_tools.context", "resolve_accessible_project_graph_ids", {"project-a"}),
)

TOOL_ARGUMENTS: dict[str, dict[str, Any]] = {
    "search": {"query": "probe"},
    "context": {"goal": "probe"},
    "explore": {},
    "expand_neighbors": {"entity_ids": ["entity_1"]},
    "fetch_slice": {"entity_id": "entity_1"},
    "synthesis_plan": {"goal": "probe"},
    "synthesis_verify": {"goal": "probe"},
    # synthesis_draft only writes when asked to remember its output, so the
    # probe has to take that path for the classification to mean anything.
    "synthesis_draft": {"goal": "probe", "remember": True},
    "add": {"title": "probe", "content": "probe", "project": "project-a"},
    "remember": {"title": "probe", "content": "probe", "project": "project-a"},
    "reflect": {"content": "probe", "project": "project-a"},
    "manage": {"action": "crawl", "data": {"url": "https://example.com"}},
    "logs": {},
}

SCOPE_REFUSAL = "missing the scope required for this MCP"


def _context(scopes: list[str]) -> McpContext:
    return McpContext(
        org_id=str(uuid4()),
        user_id=str(uuid4()),
        scopes=scopes,
        org_role="member",
        is_api_key=True,
    )


UNAUTHORIZED = ["billing:admin"]
READ_ONLY = ["mcp", "api:read"]
WRITE = ["mcp", "api:write"]


def _fallback_arguments(fn: Callable[..., object]) -> dict[str, Any]:
    """Invent arguments for an entry point that has no pinned probe."""
    arguments: dict[str, Any] = {}
    for parameter in inspect.signature(fn).parameters.values():
        if parameter.default is not inspect.Parameter.empty:
            continue
        annotation = str(parameter.annotation)
        if "list" in annotation:
            arguments[parameter.name] = ["probe"]
        elif "int" in annotation or "float" in annotation:
            arguments[parameter.name] = 1
        elif "bool" in annotation:
            arguments[parameter.name] = False
        elif "dict" in annotation:
            arguments[parameter.name] = {}
        else:
            arguments[parameter.name] = "probe"
    return arguments


@contextmanager
def _recorded_store() -> Iterator[list[str]]:
    """Replace every store boundary with a recorder for the duration."""
    touched: list[str] = []
    with ExitStack() as stack:
        for module_name, attribute in STORE_BOUNDARIES:
            target = f"{module_name}.{attribute}"

            def record(*_args: object, _target: str = target, **_kwargs: object) -> AsyncMock:
                touched.append(_target)
                return AsyncMock()

            stack.enter_context(patch(target, AsyncMock(side_effect=record)))
        for module_name, attribute, value in READ_SCAFFOLDING:
            module = importlib.import_module(module_name)
            if not hasattr(module, attribute):
                continue
            stack.enter_context(patch(f"{module_name}.{attribute}", AsyncMock(return_value=value)))
        yield touched


def _entry_points() -> dict[str, dict[str, Callable[..., object]]]:
    mcp = create_mcp_server()
    return {
        "tools": {tool.name: tool.fn for tool in mcp._tool_manager.list_tools()},
        "resources": {
            str(resource.uri): resource.fn  # type: ignore[attr-defined]
            for resource in mcp._resource_manager.list_resources()
        },
        "templates": {
            template.uri_template: template.fn
            for template in mcp._resource_manager.list_templates()
        },
        "prompts": {prompt.name: prompt.fn for prompt in mcp._prompt_manager.list_prompts()},
    }


ENTRY_POINTS = _entry_points()
ALL_TOOLS = WRITE_TOOLS | READ_TOOLS


def _every_entry_point() -> list[tuple[str, str, Callable[..., object]]]:
    """Flatten the managers, so templates and prompts are covered when added."""
    return [
        (kind, name, fn)
        for kind, registered in ENTRY_POINTS.items()
        for name, fn in registered.items()
    ]


async def _invoke(fn: Callable[..., object], scopes: list[str]) -> BaseException | None:
    """Call an entry point under ``scopes`` and hand back what it raised."""
    arguments = TOOL_ARGUMENTS.get(getattr(fn, "__name__", ""))
    if arguments is None:
        arguments = _fallback_arguments(fn)
    with patch("sibyl.mcp_tools.context.get_context", AsyncMock(return_value=_context(scopes))):
        try:
            result = fn(**arguments)
            if inspect.isawaitable(result):
                await result
        # The exception is the observation, so every failure shape is caught.
        except BaseException as error:
            return error
    return None


def _is_refusal(error: BaseException | None) -> bool:
    return error is not None and SCOPE_REFUSAL in str(error)


def test_registered_entry_points_match_the_pinned_classification() -> None:
    assert set(ENTRY_POINTS["tools"]) == ALL_TOOLS, (
        "MCP tool set changed. Classify each new tool as read or write here."
    )
    assert set(ENTRY_POINTS["resources"]) == RESOURCES, "MCP resource set changed."
    assert set(TOOL_ARGUMENTS) >= ALL_TOOLS, (
        "Every tool needs a pinned probe so it is invoked on its real path."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "name", "fn"),
    [pytest.param(*entry, id=f"{entry[0]}:{entry[1]}") for entry in _every_entry_point()],
)
async def test_unauthorized_credentials_are_refused_without_touching_the_store(
    kind: str, name: str, fn: Callable[..., object]
) -> None:
    with _recorded_store() as touched:
        error = await _invoke(fn, UNAUTHORIZED)

    assert _is_refusal(error), (
        f"MCP {kind[:-1]} {name!r} accepted a credential with no mcp scope "
        f"(raised {error!r}). Every entry point has to gate."
    )
    assert touched == [], f"MCP {kind[:-1]} {name!r} reached the store before refusing: {touched}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
async def test_write_tools_refuse_a_read_only_key_without_touching_the_store(name: str) -> None:
    with _recorded_store() as touched:
        error = await _invoke(ENTRY_POINTS["tools"][name], READ_ONLY)

    assert _is_refusal(error), f"MCP tool {name!r} accepted a read-only key (raised {error!r})."
    assert touched == [], f"MCP tool {name!r} reached the store before refusing: {touched}"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
async def test_write_tools_reach_the_store_with_write_authority(name: str) -> None:
    with _recorded_store() as touched:
        error = await _invoke(ENTRY_POINTS["tools"][name], WRITE)

    assert not _is_refusal(error), f"MCP tool {name!r} refused a key holding api:write"
    assert touched, (
        f"MCP tool {name!r} never reached a recorded store boundary, so its "
        "refusal test proves nothing. Add the boundary it writes through to "
        "STORE_BOUNDARIES."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(READ_TOOLS))
async def test_read_tools_accept_a_read_only_key(name: str) -> None:
    with _recorded_store():
        error = await _invoke(ENTRY_POINTS["tools"][name], READ_ONLY)

    assert not _is_refusal(error), f"MCP tool {name!r} refused a read-only key"


@pytest.mark.asyncio
@pytest.mark.parametrize("uri", sorted(RESOURCES))
async def test_resources_accept_a_read_only_key(uri: str) -> None:
    with _recorded_store():
        error = await _invoke(ENTRY_POINTS["resources"][uri], READ_ONLY)

    assert not _is_refusal(error), f"MCP resource {uri!r} refused a read-only key"
