from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.auth.jwt import create_access_token
from sibyl.auth.mcp_auth import SibylMcpTokenVerifier
from sibyl.config import Settings


@pytest.mark.asyncio
async def test_mcp_token_verifier_accepts_jwt(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_JWT_SECRET", "secret")

    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    token = create_access_token(user_id=uuid4())
    access = await SibylMcpTokenVerifier().verify_token(token)
    assert access is not None
    assert access.client_id.startswith("user:")
    assert "mcp" in access.scopes


@pytest.mark.asyncio
async def test_mcp_token_verifier_rejects_invalid_jwt(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_JWT_SECRET", "secret")

    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    access = await SibylMcpTokenVerifier().verify_token("not-a-jwt")
    assert access is None


@pytest.mark.asyncio
async def test_mcp_token_verifier_accepts_api_key(monkeypatch) -> None:
    auth = ApiKeyAuth(
        api_key_id=uuid4(),
        user_id=uuid4(),
        organization_id=uuid4(),
        scopes=["mcp"],
    )

    with patch(
        "sibyl.auth.mcp_auth.authenticate_api_key",
        AsyncMock(return_value=auth),
    ) as authenticate:
        access = await SibylMcpTokenVerifier().verify_token("sk_live_test")
        assert access is not None
        assert access.client_id == f"api_key:{auth.api_key_id}"
    authenticate.assert_awaited_once_with("sk_live_test")


@pytest.mark.asyncio
async def test_mcp_token_verifier_rejects_unknown_api_key(monkeypatch) -> None:
    with patch(
        "sibyl.auth.mcp_auth.authenticate_api_key",
        AsyncMock(return_value=None),
    ) as authenticate:
        access = await SibylMcpTokenVerifier().verify_token("sk_live_test")
        assert access is None
    authenticate.assert_awaited_once_with("sk_live_test")


@pytest.mark.asyncio
async def test_mcp_token_verifier_rejects_api_key_without_mcp_scope() -> None:
    auth = ApiKeyAuth(
        api_key_id=uuid4(),
        user_id=uuid4(),
        organization_id=uuid4(),
        scopes=["api:read"],
    )

    with patch(
        "sibyl.auth.mcp_auth.authenticate_api_key",
        AsyncMock(return_value=auth),
    ) as authenticate:
        access = await SibylMcpTokenVerifier().verify_token("sk_live_test")
        assert access is None
    authenticate.assert_awaited_once_with("sk_live_test")
