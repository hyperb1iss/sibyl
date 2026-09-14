"""Production-shaped compatibility checks for the MCP SDK v2 boundary."""

from __future__ import annotations

import httpx2
import pytest
from mcp import Client, ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken

from sibyl.config import settings
from sibyl.main import _mcp_transport_security, create_combined_app
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
async def test_streamable_http_app_serves_modern_client_at_configured_proxy_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_auth_mode", "off")
    monkeypatch.setattr(settings, "server_url", "https://sibyl.example.com")
    monkeypatch.setattr(settings, "frontend_url", "https://memory.example.com/")
    mcp = create_mcp_server()
    app = mcp.streamable_http_app(
        host="127.0.0.1",
        stateless_http=False,
        transport_security=_mcp_transport_security("127.0.0.1", 3334),
    )
    http_client = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="https://sibyl.example.com",
    )
    monkeypatch.setattr(
        "mcp.client.streamable_http.create_mcp_http_client",
        lambda: http_client,
    )

    async with mcp.session_manager.run():
        async with Client("https://sibyl.example.com/mcp", mode="auto") as client:
            tools = await client.list_tools()

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="https://attacker.example.com",
        ) as rejected_client:
            rejected = await rejected_client.post("/mcp", json={})

        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="https://sibyl.example.com",
        ) as origin_client:
            rejected_origin = await origin_client.post(
                "/mcp",
                json={},
                headers={"Origin": "https://attacker.example.com"},
            )
            allowed_origin = await origin_client.post(
                "/mcp",
                json={},
                headers={"Origin": "https://memory.example.com"},
            )

    assert {tool.name for tool in tools.tools} >= {"search", "add", "manage"}
    assert rejected.status_code == 421
    assert rejected_origin.status_code == 403
    assert allowed_origin.status_code != 403


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


@pytest.mark.asyncio
async def test_streamable_http_auth_rejects_missing_invalid_and_unscoped_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def load_access_token(_provider: object, token: str) -> AccessToken | None:
        if token == "unscoped-token":
            return AccessToken(token=token, client_id="client-test", scopes=["api:read"])
        return None

    monkeypatch.setattr(settings, "mcp_auth_mode", "on")
    monkeypatch.setattr(
        "sibyl.auth.mcp_oauth.SibylMcpOAuthProvider.load_access_token",
        load_access_token,
    )
    mcp = create_mcp_server()
    app = mcp.streamable_http_app(
        host="127.0.0.1",
        stateless_http=False,
        transport_security=_mcp_transport_security("127.0.0.1", 3334),
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "transport-test", "version": "1"},
        },
    }

    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url="http://127.0.0.1:3334",
        ) as http_client,
    ):
        missing = await http_client.post("/mcp", json=request)
        invalid = await http_client.post(
            "/mcp",
            json=request,
            headers={"Authorization": "Bearer invalid-token"},
        )
        unscoped = await http_client.post(
            "/mcp",
            json=request,
            headers={"Authorization": "Bearer unscoped-token"},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert unscoped.status_code == 403


@pytest.mark.asyncio
async def test_combined_app_preserves_session_capacity_and_releases_closed_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "mcp_auth_mode", "off")
    monkeypatch.setattr(settings, "server_url", "http://127.0.0.1:3334")
    mcp = create_mcp_server()
    monkeypatch.setattr("sibyl.server.create_mcp_server", lambda: mcp)
    app = create_combined_app(host="127.0.0.1", port=3334)
    manager = mcp.session_manager

    assert manager.session_idle_timeout is None
    assert manager.max_sessions is None
    assert manager.stateless is False

    async with (
        manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=settings.server_url,
            headers={"Accept": "application/json, text/event-stream"},
        ) as client,
    ):
        refused = await client.post("/mcp", content="{")
        assert refused.status_code == 400
        assert not manager._server_instances

        initialized = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "session-lifecycle-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        session_id = initialized.headers["mcp-session-id"]
        transport = manager._server_instances[session_id]
        assert transport.idle_scope is None
        session_headers = {
            "mcp-session-id": session_id,
            "mcp-protocol-version": "2025-06-18",
        }
        notified = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=session_headers,
        )
        assert notified.status_code == 202
        ping = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        resumed = await client.post("/mcp", json=ping, headers=session_headers)
        assert resumed.status_code == 200

        deleted = await client.delete("/mcp", headers=session_headers)
        assert deleted.status_code == 200
        assert not manager._server_instances
        assert not manager._session_owners
        assert transport.is_terminated
        stale = await client.post("/mcp", json=ping, headers=session_headers)
        assert stale.status_code == 404
