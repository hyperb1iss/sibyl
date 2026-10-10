"""Pin the principal identity MCP bearer tokens resolve to.

MCP SDK 2 identifies the caller behind each request as
``principal_components(token)``: the token's ``client_id``, issuer and
subject. The bearer middleware publishes it as the request's authorization
context, and the 2026-07-28 protocol binds sealed ``requestState`` to it.
Sibyl's provider sets no issuer or subject, so ``client_id`` is the only
component that tells one principal from another and must name the user or
API key.
"""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx2
import pytest
from mcp.server.auth.provider import principal_components
from pydantic import SecretStr

from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.auth.jwt import create_access_token
from sibyl.auth.mcp_oauth import SibylMcpOAuthProvider
from sibyl.config import settings
from sibyl.main import mcp_http_app
from sibyl.server import create_mcp_server

PROTOCOL_VERSION = "2025-06-18"
ACCEPT = "application/json, text/event-stream"


@pytest.fixture(autouse=True)
def first_party_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_auth_mode", "on")
    monkeypatch.setattr(settings, "server_url", "http://127.0.0.1:3334")
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("mcp-principal-test-secret-at-least-32"))
    monkeypatch.setattr("sibyl_core.auth.jwt._settings_provider", lambda: settings)


@pytest.fixture
def active_sessions(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    validate = AsyncMock(return_value=True)
    monkeypatch.setattr("sibyl.auth.mcp_oauth.validate_access_session", validate)
    return validate


def _session_token(user_id: UUID) -> str:
    return create_access_token(user_id=user_id, session_id=uuid4(), extra_claims={"scope": "mcp"})


def _api_key(user_id: UUID | None = None) -> ApiKeyAuth:
    return ApiKeyAuth(
        api_key_id=uuid4(),
        user_id=user_id or uuid4(),
        organization_id=uuid4(),
        scopes=["mcp"],
    )


def _serve_api_keys(monkeypatch: pytest.MonkeyPatch, keys: dict[str, ApiKeyAuth]) -> None:
    async def authenticate(_provider: object, raw_key: str) -> ApiKeyAuth | None:
        return keys.get(raw_key)

    monkeypatch.setattr(SibylMcpOAuthProvider, "_authenticate_api_key", authenticate)


# ---------------------------------------------------------------------------
# Principal shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_token_principal_names_the_user(active_sessions: AsyncMock) -> None:
    user_id = uuid4()
    token = _session_token(user_id)

    access = await SibylMcpOAuthProvider().load_access_token(token)

    assert access is not None
    assert access.client_id == f"user:{user_id}"
    active_sessions.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_api_key_principal_names_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = _api_key()
    _serve_api_keys(monkeypatch, {"sk_live_principal": auth})

    access = await SibylMcpOAuthProvider().load_access_token("sk_live_principal")

    assert access is not None
    assert access.client_id == f"api_key:{auth.api_key_id}"


@pytest.mark.asyncio
async def test_distinct_credentials_resolve_to_distinct_principals(
    monkeypatch: pytest.MonkeyPatch, active_sessions: AsyncMock
) -> None:
    owner = uuid4()
    first_key, second_key = _api_key(owner), _api_key(owner)
    _serve_api_keys(monkeypatch, {"sk_live_first": first_key, "sk_live_second": second_key})
    provider = SibylMcpOAuthProvider()

    tokens = {
        "owner session": _session_token(owner),
        "other user session": _session_token(uuid4()),
        "owner first key": "sk_live_first",
        "owner second key": "sk_live_second",
    }
    principals = {}
    for label, token in tokens.items():
        access = await provider.load_access_token(token)
        assert access is not None, label
        principals[label] = principal_components(access)

    assert len(set(principals.values())) == len(tokens), principals


# ---------------------------------------------------------------------------
# Each request answers to its own credential
# ---------------------------------------------------------------------------


async def _post(
    client: httpx2.AsyncClient, token: str | None, payload: dict[str, object]
) -> httpx2.Response:
    headers = {"Accept": ACCEPT, "mcp-protocol-version": PROTOCOL_VERSION}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await client.post("/mcp", headers=headers, json=payload)


@pytest.mark.asyncio
@pytest.mark.usefixtures("active_sessions")
async def test_no_request_inherits_an_earlier_credential() -> None:
    owner_token, other_token = _session_token(uuid4()), _session_token(uuid4())
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "principal-test", "version": "1"},
        },
    }
    list_tools = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    mcp = create_mcp_server()
    app = mcp_http_app(mcp, "127.0.0.1", 3334)
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=settings.server_url
        ) as client,
    ):
        opened = await _post(client, owner_token, initialize)
        owner = await _post(client, owner_token, list_tools)
        other = await _post(client, other_token, list_tools)
        anonymous = await _post(client, None, list_tools)

    assert opened.status_code == 200
    # No session is minted, so there is nothing for a later caller to ride on.
    assert "mcp-session-id" not in opened.headers
    assert owner.status_code == 200
    assert other.status_code == 200
    assert anonymous.status_code == 401


# ---------------------------------------------------------------------------
# Fail-closed credential checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_token_is_refused_when_the_auth_store_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validate = AsyncMock(side_effect=TimeoutError)
    monkeypatch.setattr("sibyl.auth.mcp_oauth.validate_access_session", validate)
    token = _session_token(uuid4())

    assert await SibylMcpOAuthProvider().load_access_token(token) is None
    validate.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_unknown_api_key_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    authenticate = AsyncMock(return_value=None)
    monkeypatch.setattr(SibylMcpOAuthProvider, "_authenticate_api_key", authenticate)

    assert await SibylMcpOAuthProvider().load_access_token("sk_live_unknown") is None
    authenticate.assert_awaited_once_with("sk_live_unknown")
