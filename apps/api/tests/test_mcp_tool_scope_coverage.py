"""Every registered MCP tool routes through the scope gate.

A tool added without `_require_mcp_context` reaches the graph with no scope
check at all, and a mutating tool that forgets `write=True` is gated as a read.
Neither shows up in a behavioural test of the tools that already exist, so this
walks the registered tool set structurally and pins the write set explicitly.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

import sibyl.server as server_module
from sibyl.server import create_mcp_server

GATE = "_require_mcp_context"

# Tools that mutate state and must resolve their context with write=True.
# Adding a tool to the server without adding it here fails the coverage test.
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


def _module_functions() -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    """Map every function defined in server.py to its AST node, nesting included."""
    tree = ast.parse(inspect.getsource(server_module))
    functions: dict[str, ast.AsyncFunctionDef | ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            functions[node.name] = node
    return functions


def _gate_calls(
    node: ast.AST,
    functions: dict[str, ast.AsyncFunctionDef | ast.FunctionDef],
    seen: set[str] | None = None,
) -> list[ast.Call]:
    """Collect every ``_require_mcp_context`` call reachable from ``node``."""
    seen = seen if seen is not None else set()
    calls: list[ast.Call] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Name):
            continue
        name = child.func.id
        if name == GATE:
            calls.append(child)
            continue
        if name in seen or name not in functions:
            continue
        seen.add(name)
        calls.extend(_gate_calls(functions[name], functions, seen))
    return calls


def _write_argument(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "write":
            return keyword.value
    return None


def _requests_write(call: ast.Call) -> bool:
    """True when the call asks for write access, literally or via a flag argument."""
    value = _write_argument(call)
    if value is None:
        return False
    if isinstance(value, ast.Constant):
        return value.value is True
    # synthesis_draft forwards its own `remember` flag.
    return True


def _tool_functions() -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    tools = create_mcp_server()._tool_manager.list_tools()
    parsed: dict[str, ast.AsyncFunctionDef | ast.FunctionDef] = {}
    for tool in tools:
        source = textwrap.dedent(inspect.getsource(tool.fn))
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        parsed[tool.name] = node
    return parsed


def test_registered_tools_match_the_pinned_classification() -> None:
    registered = set(_tool_functions())

    assert registered == WRITE_TOOLS | READ_TOOLS, (
        "MCP tool set changed. Classify each new tool as read or write in this "
        "test, and gate write tools with _require_mcp_context(write=True)."
    )


@pytest.mark.parametrize("tool_name", sorted(WRITE_TOOLS | READ_TOOLS))
def test_every_tool_routes_through_the_scope_gate(tool_name: str) -> None:
    functions = _module_functions()
    calls = _gate_calls(_tool_functions()[tool_name], functions)

    assert calls, f"MCP tool {tool_name!r} never resolves context through {GATE}"


@pytest.mark.parametrize("tool_name", sorted(WRITE_TOOLS))
def test_write_tools_request_write_access(tool_name: str) -> None:
    functions = _module_functions()
    calls = _gate_calls(_tool_functions()[tool_name], functions)

    assert any(_requests_write(call) for call in calls), (
        f"MCP tool {tool_name!r} mutates state but resolves context as a read. "
        f"Call {GATE}(write=True) on its write path."
    )


@pytest.mark.parametrize("tool_name", sorted(READ_TOOLS))
def test_read_tools_do_not_request_write_access(tool_name: str) -> None:
    functions = _module_functions()
    calls = _gate_calls(_tool_functions()[tool_name], functions)

    assert not any(_requests_write(call) for call in calls), (
        f"MCP tool {tool_name!r} is classified as a read but asks for write "
        "access. Move it to WRITE_TOOLS if it mutates state."
    )
