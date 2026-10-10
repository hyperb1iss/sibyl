"""One MCP tool call authenticates its credential once and resolves it once.

The SDK's bearer backend authenticates every HTTP request through the OAuth
provider. The tool context used to start again from the raw token (a second
API-key KDF, scope reads and last_used write), and the project grants read
resolved the principal a third time. The provider now hands its result to
the tool context on the access token, and the context hands its resolved
authority to the grants read.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import httpx2
import pytest
from pydantic import SecretStr

from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.auth.mcp_oauth import SibylAccessToken, SibylMcpOAuthProvider
from sibyl.config import settings
from sibyl.main import mcp_http_app
from sibyl.mcp_tools import context as mcp_context
from sibyl.persistence.surreal.auth_runtime import projects as project_runtime
from sibyl.server import create_mcp_server
from sibyl_core.auth import OrganizationRole
from tests.harness.auth import stub_auth_context

PROTOCOL_VERSION = "2025-06-18"
ACCEPT = "application/json, text/event-stream"
RAW_KEY = "sk_live_once"


def _api_key() -> ApiKeyAuth:
    return ApiKeyAuth(api_key_id=uuid4(), user_id=uuid4(), organization_id=uuid4(), scopes=["mcp"])


def _carrying_token(auth: ApiKeyAuth) -> SibylAccessToken:
    return SibylAccessToken(
        token=RAW_KEY,
        client_id=f"api_key:{auth.api_key_id}",
        scopes=["mcp"],
        api_key_auth=auth,
    )


def _resolver_for(auth: ApiKeyAuth, role: OrganizationRole = OrganizationRole.MEMBER):
    return AsyncMock(
        return_value=stub_auth_context(
            user_id=auth.user_id, organization_id=auth.organization_id, org_role=role
        )
    )


# ---------------------------------------------------------------------------
# Tool context reuses what the provider resolved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_context_reuses_the_credential_the_provider_resolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _api_key()
    authenticate = AsyncMock(return_value=auth)
    resolve = _resolver_for(auth)
    monkeypatch.setattr(mcp_context, "get_access_token", lambda: _carrying_token(auth))
    monkeypatch.setattr(mcp_context, "authenticate_api_key", authenticate)
    monkeypatch.setattr(mcp_context, "resolve_auth_context", resolve)

    ctx = await mcp_context.get_context()

    assert ctx is not None
    assert ctx.is_api_key is True
    assert ctx.user_id == str(auth.user_id)
    assert ctx.authority is resolve.return_value
    authenticate.assert_not_awaited()
    resolve.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_context_still_authenticates_a_bare_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _api_key()
    authenticate = AsyncMock(return_value=auth)
    monkeypatch.setattr(mcp_context, "get_access_token", lambda: SimpleNamespace(token=RAW_KEY))
    monkeypatch.setattr(mcp_context, "authenticate_api_key", authenticate)
    monkeypatch.setattr(mcp_context, "resolve_auth_context", _resolver_for(auth))

    ctx = await mcp_context.get_context()

    assert ctx is not None
    assert ctx.is_api_key is True
    authenticate.assert_awaited_once_with(RAW_KEY)


@pytest.mark.asyncio
async def test_get_context_reuses_verified_jwt_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, org_id = uuid4(), uuid4()
    claims = {"sub": str(user_id), "org": str(org_id), "scopes": ["mcp"], "typ": "access"}
    token = SibylAccessToken(
        token="jwt.token", client_id=f"user:{user_id}", scopes=["mcp"], jwt_claims=claims
    )
    monkeypatch.setattr(mcp_context, "get_access_token", lambda: token)
    monkeypatch.setattr(
        mcp_context,
        "resolve_auth_context",
        AsyncMock(return_value=stub_auth_context(user_id=user_id, organization_id=org_id)),
    )
    verify = Mock(side_effect=AssertionError("the JWT was verified a second time"))

    with patch("sibyl.auth.jwt.verify_access_token", verify):
        ctx = await mcp_context.get_context()

    assert ctx is not None
    assert ctx.is_api_key is False
    assert ctx.org_id == str(org_id)
    assert ctx.user_id == str(user_id)
    verify.assert_not_called()


# ---------------------------------------------------------------------------
# Project grants reuse the resolved authority
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_project_grants_hand_the_request_authority_to_the_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = AsyncMock(return_value=(frozenset({"project-a"}), frozenset()))
    monkeypatch.setattr(mcp_context, "resolve_project_graph_grants", resolver)
    authority = stub_auth_context()
    ctx = mcp_context.McpContext(
        org_id=authority.organization_id or "",
        user_id=authority.user_id,
        scopes=["mcp"],
        authority=authority,
    )

    assert await mcp_context.get_accessible_projects(ctx) == {"project-a"}
    resolver.assert_awaited_once()
    assert resolver.await_args.kwargs["authority"] is authority


class _StaticScope:
    def __init__(self, client: object) -> None:
        self._client = client

    async def __aenter__(self) -> object:
        return self._client

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _ProjectRows:
    def __init__(self) -> None:
        self.calls = 0

    async def execute_query(self, _query: str, **_params: object) -> object:
        self.calls += 1
        return [{"uuid": "p-a", "graph_project_id": "project-a", "visibility": "org"}]


@pytest.mark.asyncio
async def test_resolve_project_graph_grants_skips_the_resolver_for_a_matching_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = stub_auth_context(org_role=OrganizationRole.ADMIN)
    resolve = AsyncMock(return_value=authority)
    rows = _ProjectRows()
    monkeypatch.setattr(project_runtime, "_resolve_auth_context_from_claims", resolve)
    monkeypatch.setattr(project_runtime, "_auth_client_scope", lambda: _StaticScope(rows))

    readable, writable = await project_runtime.resolve_project_graph_grants(
        user_id=authority.user_id or "",
        org_id=authority.organization_id or "",
        authority=authority,
    )

    assert readable == {"project-a"}
    assert writable == {"project-a"}
    resolve.assert_not_awaited()
    assert rows.calls == 1


@pytest.mark.asyncio
async def test_resolve_project_graph_grants_ignores_another_principals_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = stub_auth_context(user_id=uuid4(), org_role=OrganizationRole.ADMIN)
    foreign = stub_auth_context(user_id=uuid4(), org_role=OrganizationRole.OWNER)
    resolve = AsyncMock(return_value=requested)
    monkeypatch.setattr(project_runtime, "_resolve_auth_context_from_claims", resolve)
    monkeypatch.setattr(project_runtime, "_auth_client_scope", lambda: _StaticScope(_ProjectRows()))

    readable, _ = await project_runtime.resolve_project_graph_grants(
        user_id=requested.user_id or "",
        org_id=requested.organization_id or "",
        authority=foreign,
    )

    assert readable == {"project-a"}
    resolve.assert_awaited_once()


# ---------------------------------------------------------------------------
# The whole tool call over streamable HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def first_party_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "mcp_auth_mode", "on")
    monkeypatch.setattr(settings, "server_url", "http://127.0.0.1:3334")
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("mcp-auth-once-test-secret-at-least-32"))
    monkeypatch.setattr("sibyl_core.auth.jwt._settings_provider", lambda: settings)


async def _post(client: httpx2.AsyncClient, payload: dict[str, object]) -> httpx2.Response:
    headers = {
        "Authorization": f"Bearer {RAW_KEY}",
        "Accept": ACCEPT,
        "mcp-protocol-version": PROTOCOL_VERSION,
    }
    return await client.post("/mcp", headers=headers, json=payload)


def _jsonrpc_result(response: httpx2.Response) -> dict[str, object]:
    """Read one JSON-RPC response from a JSON or SSE body."""
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(f"no SSE data in {response.text!r}")
    return response.json()


@pytest.mark.asyncio
@pytest.mark.usefixtures("first_party_auth")
async def test_a_tool_call_authenticates_the_api_key_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _api_key()
    provider_calls: list[str] = []

    async def provider_authenticate(_provider: object, raw_key: str) -> ApiKeyAuth | None:
        provider_calls.append(raw_key)
        return auth if raw_key == RAW_KEY else None

    monkeypatch.setattr(SibylMcpOAuthProvider, "_authenticate_api_key", provider_authenticate)
    context_authenticate = AsyncMock(return_value=auth)
    monkeypatch.setattr(mcp_context, "authenticate_api_key", context_authenticate)
    resolve_principal = _resolver_for(auth, OrganizationRole.ADMIN)
    monkeypatch.setattr(mcp_context, "resolve_auth_context", resolve_principal)
    resolve_from_claims = AsyncMock(return_value=resolve_principal.return_value)
    monkeypatch.setattr(project_runtime, "_resolve_auth_context_from_claims", resolve_from_claims)
    monkeypatch.setattr(
        project_runtime,
        "_load_project_access_records",
        AsyncMock(return_value=([{"uuid": "p-a", "graph_project_id": "project-a"}], {})),
    )
    search = AsyncMock(return_value={"filters": {}, "results": []})
    monkeypatch.setattr("sibyl_core.tools.core.search", search)

    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "search", "arguments": {"query": "probe", "project": "project-a"}},
    }
    mcp = create_mcp_server()
    app = mcp_http_app(mcp, "127.0.0.1", 3334)
    async with (
        mcp.session_manager.run(),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url=settings.server_url
        ) as client,
    ):
        response = await _post(client, call)

    assert response.status_code == 200
    result = _jsonrpc_result(response)
    assert "error" not in result, result
    assert not result["result"].get("isError"), result
    search.assert_awaited_once()

    # The HTTP request that carried the tool call authenticated the key once,
    # in the SDK's bearer backend, and nothing downstream did it again.
    assert len(provider_calls) == 1
    context_authenticate.assert_not_awaited()
    resolve_principal.assert_awaited_once()
    resolve_from_claims.assert_not_awaited()
