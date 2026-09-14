"""Exercise first-party credentials through the real MCP bearer boundary."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import SecretStr

from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.auth.jwt import create_access_token, verify_access_token
from sibyl.auth.mcp_oauth import SibylAuthorizationCode, SibylMcpOAuthProvider
from sibyl.config import settings
from sibyl.server import create_mcp_server


@pytest.fixture(autouse=True)
def first_party_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_auth_mode", "on")
    monkeypatch.setattr(settings, "server_url", "http://127.0.0.1:3334")
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("mcp-credential-test-secret-at-least-32"))
    monkeypatch.setattr("sibyl_core.auth.jwt._settings_provider", lambda: settings)


async def _initialize(token: str) -> int:
    mcp = create_mcp_server()
    app = mcp.streamable_http_app(host="127.0.0.1", stateless_http=False)
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=settings.server_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            },
        ) as client,
    ):
        response = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "credential-test", "version": "1"},
                },
            },
        )
    return response.status_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_claims", "active_session", "signing_secret", "expected_status"),
    [
        pytest.param({}, True, None, 200, id="local-session-without-audience"),
        pytest.param({"aud": "https://other.example/mcp"}, True, None, 401, id="foreign-audience"),
        pytest.param({"aud": ["https://other.example/mcp"]}, True, None, 401, id="audience-list"),
        pytest.param(
            {"aud": "http://127.0.0.1:3334/mcp"}, True, None, 401, id="unsupported-audience"
        ),
        pytest.param({}, False, None, 401, id="revoked-session"),
        pytest.param({}, True, "foreign-signing-secret-at-least-32", 401, id="foreign-signature"),
        pytest.param({"typ": "refresh"}, True, None, 401, id="refresh-token"),
        pytest.param({"exp": 1}, True, None, 401, id="expired-token"),
        pytest.param({"scope": "api:read"}, True, None, 403, id="missing-mcp-scope"),
    ],
)
async def test_signed_session_credential_policy(
    monkeypatch: pytest.MonkeyPatch,
    extra_claims: dict[str, object],
    active_session: bool,
    signing_secret: str | None,
    expected_status: int,
) -> None:
    session = AsyncMock(return_value=active_session)
    monkeypatch.setattr("sibyl.auth.mcp_oauth.validate_access_session", session)
    with monkeypatch.context() as signing:
        if signing_secret:
            signing.setattr(settings, "jwt_secret", SecretStr(signing_secret))
        token = create_access_token(
            user_id=uuid4(),
            session_id=uuid4(),
            extra_claims={"scope": "mcp", **extra_claims},
        )

    assert await _initialize(token) == expected_status
    if (
        signing_secret
        or extra_claims.get("aud")
        or extra_claims.get("typ")
        or "exp" in extra_claims
    ):
        session.assert_not_awaited()
    else:
        session.assert_awaited_once_with(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [["mcp"], [], ["mcp", "api:read"]])
async def test_first_party_api_keys_need_no_resource_metadata(
    monkeypatch: pytest.MonkeyPatch, scopes: list[str]
) -> None:
    auth = ApiKeyAuth(api_key_id=uuid4(), user_id=uuid4(), organization_id=uuid4(), scopes=scopes)
    authenticate = AsyncMock(return_value=auth)
    monkeypatch.setattr(SibylMcpOAuthProvider, "_authenticate_api_key", authenticate)

    assert await _initialize("sk_test_local_credential") == 200
    authenticate.assert_awaited_once_with("sk_test_local_credential")


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_resource", [None, "http://127.0.0.1:3334/mcp"])
async def test_oauth_issuance_and_refresh_keep_first_party_session_credentials(
    monkeypatch: pytest.MonkeyPatch, requested_resource: str | None
) -> None:
    provider = SibylMcpOAuthProvider()
    user_id, organization_id = uuid4(), uuid4()
    persist = AsyncMock()
    monkeypatch.setattr(provider, "_create_session_record", persist)
    monkeypatch.setattr(
        "sibyl.auth.mcp_oauth.validate_access_session", AsyncMock(return_value=True)
    )
    client = OAuthClientInformationFull(client_id="credential-test")
    code = SibylAuthorizationCode(
        code="authorized-code",
        scopes=["mcp"],
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).timestamp(),
        client_id="credential-test",
        code_challenge="verified-challenge",
        redirect_uri="http://127.0.0.1:9911/callback",
        redirect_uri_provided_explicitly=True,
        resource=requested_resource,
        user_id=str(user_id),
        organization_id=str(organization_id),
    )

    issued = await provider.exchange_authorization_code(client, code)
    claims = verify_access_token(issued.access_token)
    assert "aud" not in claims
    assert claims["scope"] == "mcp"
    assert await _initialize(issued.access_token) == 200
    assert persist.await_args is not None
    persisted = persist.await_args.kwargs
    session_record = SimpleNamespace(
        id=persisted["session_id"], user_id=user_id, organization_id=organization_id
    )
    monkeypatch.setattr(
        provider, "_load_refresh_session_record", AsyncMock(return_value=session_record)
    )
    rotate = AsyncMock(return_value=session_record)
    monkeypatch.setattr(provider, "_rotate_refresh_session_record", rotate)

    assert issued.refresh_token is not None
    refresh = await provider.load_refresh_token(client, issued.refresh_token)
    assert refresh is not None
    refreshed = await provider.exchange_refresh_token(client, refresh, ["mcp"])

    refreshed_claims = verify_access_token(refreshed.access_token)
    assert "aud" not in refreshed_claims
    assert refreshed_claims["sid"] == claims["sid"]
    assert await _initialize(refreshed.access_token) == 200
    rotate.assert_awaited_once()
