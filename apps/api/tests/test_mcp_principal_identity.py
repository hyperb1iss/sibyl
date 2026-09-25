"""Pin the principal identity MCP bearer tokens resolve to.

MCP SDK 2 binds each streamable-HTTP session to the principal that opened it,
compared as ``principal_components(token)``: the token's ``client_id``, issuer
and subject. Sibyl's provider sets no issuer or subject, so ``client_id`` is
the only component that tells one principal from another. If it stopped
naming the user or API key, any authenticated caller could drive any other
caller's MCP session.
"""

from __future__ import annotations

from collections.abc import Callable
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
# Session ownership over the real streamable-HTTP boundary
# ---------------------------------------------------------------------------


async def _post(
    client: httpx2.AsyncClient, token: str, payload: dict[str, object], session_id: str | None
) -> httpx2.Response:
    headers = {"Authorization": f"Bearer {token}", "Accept": ACCEPT}
    if session_id is not None:
        headers["mcp-session-id"] = session_id
        headers["mcp-protocol-version"] = PROTOCOL_VERSION
    return await client.post("/mcp", headers=headers, json=payload)


async def _reuse_session(owner_token: str, other_token: str) -> tuple[int, int]:
    """Open a session as the owner, then list tools as the owner and as the other caller."""
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
    app = mcp.streamable_http_app(host="127.0.0.1", stateless_http=False)
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=settings.server_url
        ) as client,
    ):
        opened = await _post(client, owner_token, initialize, None)
        assert opened.status_code == 200
        session_id = opened.headers["mcp-session-id"]
        owner = await _post(client, owner_token, list_tools, session_id)
        other = await _post(client, other_token, list_tools, session_id)
    return owner.status_code, other.status_code


def _two_users(_monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    return _session_token(uuid4()), _session_token(uuid4())


def _two_keys_of_one_user(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    owner = uuid4()
    _serve_api_keys(
        monkeypatch, {"sk_live_owner": _api_key(owner), "sk_live_sibling": _api_key(owner)}
    )
    return "sk_live_owner", "sk_live_sibling"


def _session_then_own_key(monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    owner = uuid4()
    _serve_api_keys(monkeypatch, {"sk_live_own": _api_key(owner)})
    return _session_token(owner), "sk_live_own"


@pytest.mark.asyncio
@pytest.mark.usefixtures("active_sessions")
@pytest.mark.parametrize(
    "credentials",
    [
        pytest.param(_two_users, id="another-user"),
        pytest.param(_two_keys_of_one_user, id="another-key-of-the-same-user"),
        pytest.param(_session_then_own_key, id="the-same-user-through-an-api-key"),
    ],
)
async def test_a_session_answers_only_the_credential_that_opened_it(
    monkeypatch: pytest.MonkeyPatch,
    credentials: Callable[[pytest.MonkeyPatch], tuple[str, str]],
) -> None:
    owner_token, other_token = credentials(monkeypatch)

    owner_status, other_status = await _reuse_session(owner_token, other_token)

    assert owner_status == 200
    # The SDK answers a foreign credential exactly as it answers an unknown session.
    assert other_status == 404


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
