"""Production-shaped compatibility checks for the MCP SDK v2 boundary."""

from __future__ import annotations

import httpx2
import pytest
from mcp import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken

from sibyl.config import settings
from sibyl.server import create_mcp_server


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_mcp_server_serves_modern_and_legacy_clients(mode: str) -> None:
    mcp = create_mcp_server()

    async with Client(mcp, mode=mode) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()

    assert {tool.name for tool in tools.tools} >= {"search", "add", "manage"}
    assert {resource.uri for resource in resources.resources} == {
        "sibyl://health",
        "sibyl://stats",
    }


@pytest.mark.asyncio
async def test_streamable_http_app_serves_the_legacy_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_auth_mode", "off")
    mcp = create_mcp_server()
    app = mcp.streamable_http_app(host="testserver", stateless_http=False)
    transport = httpx2.ASGITransport(app=app)

    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http_client,
        streamable_http_client("http://testserver/mcp", http_client=http_client) as streams,
    ):
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            initialize = await session.initialize()
            tools = await session.list_tools()

    assert initialize.server_info.name
    assert {tool.name for tool in tools.tools} >= {"search", "add", "manage"}


@pytest.mark.asyncio
async def test_streamable_http_auth_uses_the_oauth_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked_tokens: list[str] = []

    async def load_access_token(_provider: object, token: str) -> AccessToken:
        checked_tokens.append(token)
        return AccessToken(token=token, client_id="client-test", scopes=["mcp"])

    monkeypatch.setattr(settings, "mcp_auth_mode", "on")
    monkeypatch.setattr(
        "sibyl.auth.mcp_oauth.SibylMcpOAuthProvider.load_access_token",
        load_access_token,
    )
    mcp = create_mcp_server()
    app = mcp.streamable_http_app(host="testserver", stateless_http=False)
    transport = httpx2.ASGITransport(app=app)

    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            headers={"Authorization": "Bearer token-test"},
        ) as http_client,
        streamable_http_client("http://testserver/mcp", http_client=http_client) as streams,
    ):
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()

    assert {tool.name for tool in tools.tools} >= {"search", "add", "manage"}
    assert checked_tokens
    assert set(checked_tokens) == {"token-test"}
