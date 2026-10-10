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

from sibyl.config import settings
from sibyl.main import mcp_http_app
from sibyl.mcp_tools.completion import uninterruptible
from sibyl.server import create_mcp_server

WRITE_TOOLS = {"add", "remember", "reflect", "manage"}
ACCEPT = "application/json, text/event-stream"


@pytest.mark.asyncio
async def test_every_write_tool_is_uninterruptible_and_reads_are_not() -> None:
    mcp = create_mcp_server()
    names = {tool.name for tool in await mcp.list_tools()}

    wrapped = {
        name for name in names if hasattr(mcp._tool_manager.get_tool(name).fn, "__wrapped__")
    }

    assert names >= WRITE_TOOLS
    # Reads stay cancellable so an abandoned search stops spending work.
    assert wrapped == WRITE_TOOLS


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
    headers = {"Accept": ACCEPT, "mcp-protocol-version": "2025-06-18"}
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=settings.server_url
        ) as client,
    ):
        for request_id, name in enumerate(("probe_write", "probe_read"), start=1):
            call = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": {}},
            }
            # Give up on the response mid-call, as a client that disconnects does.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(client.post("/mcp", headers=headers, json=call), 0.1)
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
