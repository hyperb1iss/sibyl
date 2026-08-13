"""Every registered MCP entry point routes through the scope gate.

Three failures this guards against, none of which a behavioural test of the
entry points that exist today would catch: a tool registered without
`_require_mcp_context`, a mutating tool that resolves its context as a read,
and a gate that is reachable but not dominant (guarded by a branch, or placed
after the mutation it is supposed to protect).

Dominance is expressed as: the first top-level statement of the entry point
that awaits anything must resolve to the gate. Sync code may run before it,
because nothing in this codebase mutates the store without awaiting.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable

import pytest

import sibyl.server as server_module
from sibyl.server import create_mcp_server

STRICT_GATE = "_require_mcp_context"
ANONYMOUS_GATE = "_optional_mcp_org_id"
GATES = frozenset({STRICT_GATE, ANONYMOUS_GATE})

# Tools that mutate state and must resolve their context with write=True.
# Registering a tool without listing it here fails the inventory test.
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

# Resources are reads. sibyl://health is deliberately anonymous-tolerant, since
# health reporting works without an org, so it gates a credential that is
# presented rather than requiring one.
STRICT_RESOURCES = frozenset({"sibyl://stats"})
ANONYMOUS_RESOURCES = frozenset({"sibyl://health"})

# Nothing registers these yet. Pinned empty so the first one added has to be
# classified here rather than shipping ungated.
RESOURCE_TEMPLATES: frozenset[str] = frozenset()
PROMPTS: frozenset[str] = frozenset()


def _parse(fn: Callable[..., object]) -> ast.AsyncFunctionDef | ast.FunctionDef:
    node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    assert isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    return node


def _module_functions() -> dict[str, ast.AsyncFunctionDef | ast.FunctionDef]:
    """Map every function defined in server.py to its AST node, nesting included."""
    tree = ast.parse(inspect.getsource(server_module))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
    }


# A gate inside an `if`, `try`, `with`, or loop runs conditionally, which is
# what "reachable but not dominant" looks like in practice. Only these simple
# statement forms count as an unconditional gate.
UNCONDITIONAL_STATEMENTS = (ast.Assign, ast.AnnAssign, ast.Expr, ast.Return)


def _first_awaited_statement(node: ast.AsyncFunctionDef | ast.FunctionDef) -> ast.stmt | None:
    for statement in node.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            continue
        if any(isinstance(child, ast.Await) for child in ast.walk(statement)):
            return statement
    return None


def _first_awaited_call(statement: ast.stmt) -> ast.Call | None:
    awaits = [child for child in ast.walk(statement) if isinstance(child, ast.Await)]
    if not awaits:
        return None
    first = min(awaits, key=lambda node: (node.lineno, node.col_offset))
    return first.value if isinstance(first.value, ast.Call) else None


def _dominant_gate(
    node: ast.AsyncFunctionDef | ast.FunctionDef,
    functions: dict[str, ast.AsyncFunctionDef | ast.FunctionDef],
    seen: set[str] | None = None,
) -> tuple[str, ast.Call] | None:
    """Resolve the gate the first awaited statement reaches, following delegation.

    Returns the gate name and the call that invokes it, or None when the first
    thing awaited is not a gate and does not lead to one.
    """
    seen = seen if seen is not None else set()
    statement = _first_awaited_statement(node)
    if statement is None or not isinstance(statement, UNCONDITIONAL_STATEMENTS):
        return None
    call = _first_awaited_call(statement)
    if call is None or not isinstance(call.func, ast.Name):
        return None
    name = call.func.id
    if name in GATES:
        return name, call
    if name in seen or name not in functions:
        return None
    seen.add(name)
    return _dominant_gate(functions[name], functions, seen)


def _requests_write(call: ast.Call) -> bool:
    """True when the call asks for write access, literally or via a flag argument."""
    for keyword in call.keywords:
        if keyword.arg == "write":
            value = keyword.value
            if isinstance(value, ast.Constant):
                return value.value is True
            # synthesis_draft forwards its own `remember` flag.
            return True
    return False


def _server_inventory() -> dict[str, dict[str, ast.AsyncFunctionDef | ast.FunctionDef]]:
    mcp = create_mcp_server()
    return {
        "tools": {tool.name: _parse(tool.fn) for tool in mcp._tool_manager.list_tools()},
        "resources": {
            str(resource.uri): _parse(resource.fn)  # type: ignore[attr-defined]
            for resource in mcp._resource_manager.list_resources()
        },
        "templates": {
            template.uri_template: _parse(template.fn)
            for template in mcp._resource_manager.list_templates()
        },
        "prompts": {
            prompt.name: _parse(prompt.fn) for prompt in mcp._prompt_manager.list_prompts()
        },
    }


INVENTORY = _server_inventory()
ALL_TOOLS = WRITE_TOOLS | READ_TOOLS
ALL_RESOURCES = STRICT_RESOURCES | ANONYMOUS_RESOURCES


def test_registered_entry_points_match_the_pinned_classification() -> None:
    assert set(INVENTORY["tools"]) == ALL_TOOLS, (
        "MCP tool set changed. Classify each new tool as read or write here, "
        "and gate write tools with _require_mcp_context(write=True)."
    )
    assert set(INVENTORY["resources"]) == ALL_RESOURCES, (
        "MCP resource set changed. Classify each new resource as strict or "
        "anonymous-tolerant here, and gate it."
    )
    assert set(INVENTORY["templates"]) == RESOURCE_TEMPLATES, (
        "MCP resource templates changed. Classify and gate the new template."
    )
    assert set(INVENTORY["prompts"]) == PROMPTS, (
        "MCP prompts changed. Classify and gate the new prompt."
    )


@pytest.mark.parametrize("name", sorted(ALL_TOOLS))
def test_tools_gate_before_anything_is_awaited(name: str) -> None:
    resolved = _dominant_gate(INVENTORY["tools"][name], _module_functions())

    assert resolved is not None, (
        f"MCP tool {name!r} awaits something before it resolves context through "
        f"{STRICT_GATE}. The gate has to be the first await on every path."
    )
    assert resolved[0] == STRICT_GATE, (
        f"MCP tool {name!r} gates through {resolved[0]}, which tolerates an "
        f"anonymous caller. Tools require {STRICT_GATE}."
    )


@pytest.mark.parametrize("name", sorted(WRITE_TOOLS))
def test_write_tools_request_write_access(name: str) -> None:
    resolved = _dominant_gate(INVENTORY["tools"][name], _module_functions())

    assert resolved is not None, f"MCP tool {name!r} does not reach a scope gate"
    assert _requests_write(resolved[1]), (
        f"MCP tool {name!r} mutates state but resolves context as a read. "
        f"Call {STRICT_GATE}(write=True) on its write path."
    )


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
def test_read_tools_do_not_request_write_access(name: str) -> None:
    resolved = _dominant_gate(INVENTORY["tools"][name], _module_functions())

    assert resolved is not None, f"MCP tool {name!r} does not reach a scope gate"
    assert not _requests_write(resolved[1]), (
        f"MCP tool {name!r} is classified as a read but asks for write access. "
        "Move it to WRITE_TOOLS if it mutates state."
    )


@pytest.mark.parametrize("uri", sorted(ALL_RESOURCES))
def test_resources_gate_before_anything_is_awaited(uri: str) -> None:
    resolved = _dominant_gate(INVENTORY["resources"][uri], _module_functions())
    expected = STRICT_GATE if uri in STRICT_RESOURCES else ANONYMOUS_GATE

    assert resolved is not None, (
        f"MCP resource {uri!r} awaits something before it resolves context through a scope gate."
    )
    assert resolved[0] == expected, (
        f"MCP resource {uri!r} gates through {resolved[0]}, expected {expected}."
    )
    assert not _requests_write(resolved[1]), f"MCP resource {uri!r} asks for write access."
