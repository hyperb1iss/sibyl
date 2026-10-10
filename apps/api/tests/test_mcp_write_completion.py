"""Write tools finish even when the caller drops the call.

Over the stateless transport a tool call lives only as long as its HTTP
response stream, so a client that disconnects mid-call cancels the tool. A
write that spans several stores must not stop between them.
"""

from __future__ import annotations

import asyncio

import anyio
import httpx2
import pytest
from structlog.testing import capture_logs

from sibyl.config import settings
from sibyl.main import mcp_http_app
from sibyl.mcp_tools import synthesis as synthesis_tools
from sibyl.mcp_tools.completion import uninterruptible
from sibyl.server import create_mcp_server
from tests.test_mcp_entry_point_scope_coverage import WRITE_TOOLS

# synthesis_draft writes only when asked to remember, so it shields per call.
ALWAYS_WRITING = WRITE_TOOLS - {"synthesis_draft"}
ACCEPT = "application/json, text/event-stream"
HEADERS = {"Accept": ACCEPT, "mcp-protocol-version": "2025-06-18"}


async def _drop_call(client: httpx2.AsyncClient, name: str, arguments: dict[str, object]) -> None:
    """Give up on a tool call mid-flight, as a client that disconnects does."""
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.post("/mcp", headers=HEADERS, json=call), 0.1)


@pytest.mark.asyncio
async def test_every_write_tool_is_uninterruptible_and_reads_are_not() -> None:
    mcp = create_mcp_server()
    names = {tool.name for tool in await mcp.list_tools()}

    wrapped = {
        name for name in names if hasattr(mcp._tool_manager.get_tool(name).fn, "__wrapped__")
    }

    assert names >= WRITE_TOOLS
    # Reads stay cancellable so an abandoned search stops spending work.
    assert wrapped == ALWAYS_WRITING


@pytest.mark.asyncio
async def test_a_dropped_call_still_lands_its_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_auth_mode", "off")
    monkeypatch.setattr(settings, "server_url", "http://127.0.0.1:3334")
    outcomes: dict[str, str] = {}
    mcp = create_mcp_server()

    async def slow(name: str) -> str:
        try:
            await anyio.sleep(0.5)
        except BaseException:
            outcomes[name] = "cancelled"
            raise
        outcomes[name] = "finished"
        return name

    @mcp.tool()
    @uninterruptible
    async def probe_write() -> str:
        return await slow("write")

    @mcp.tool()
    async def probe_read() -> str:
        return await slow("read")

    app = mcp_http_app(mcp, "127.0.0.1", 3334)
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=settings.server_url
        ) as client,
    ):
        for name in ("probe_write", "probe_read"):
            await _drop_call(client, name, {})
        await anyio.sleep(1.0)

    assert outcomes == {"write": "finished", "read": "cancelled"}


@pytest.mark.asyncio
async def test_task_cancel_waits_for_the_write_then_cancels_the_caller() -> None:
    landed = asyncio.Event()

    @uninterruptible
    async def write() -> str:
        await asyncio.sleep(0.2)
        landed.set()
        return "written"

    caller = asyncio.ensure_future(write())
    await asyncio.sleep(0.05)
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert landed.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(("remember", "outcome"), [(True, "finished"), (False, "cancelled")])
async def test_a_dropped_draft_lands_only_when_it_remembers(
    monkeypatch: pytest.MonkeyPatch, remember: bool, outcome: str
) -> None:
    monkeypatch.setattr(settings, "mcp_auth_mode", "off")
    monkeypatch.setattr(settings, "server_url", "http://127.0.0.1:3334")
    outcomes: list[str] = []

    async def draft(**_kwargs: object) -> dict[str, object]:
        try:
            await anyio.sleep(0.5)
        except BaseException:
            outcomes.append("cancelled")
            raise
        outcomes.append("finished")
        return {}

    monkeypatch.setattr(synthesis_tools, "_synthesis_mcp_draft", draft)
    mcp = create_mcp_server()
    app = mcp_http_app(mcp, "127.0.0.1", 3334)
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=settings.server_url
        ) as client,
    ):
        await _drop_call(client, "synthesis_draft", {"goal": "probe", "remember": remember})
        await anyio.sleep(1.0)

    assert outcomes == [outcome]


@pytest.mark.asyncio
async def test_a_write_that_fails_after_the_caller_left_is_logged() -> None:
    @uninterruptible
    async def write() -> None:
        await asyncio.sleep(0.1)
        raise RuntimeError("store unavailable")

    caller = asyncio.ensure_future(write())
    await asyncio.sleep(0.02)
    caller.cancel()
    with capture_logs() as logs, pytest.raises(asyncio.CancelledError) as cancelled:
        await caller

    assert isinstance(cancelled.value.__cause__, RuntimeError)
    assert [(entry["event"], entry["log_level"], entry["error"]) for entry in logs] == [
        ("mcp_write_failed_after_caller_left", "error", "store unavailable")
    ]
