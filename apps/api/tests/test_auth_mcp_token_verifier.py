from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.auth.jwt import create_access_token
from sibyl.auth.mcp_auth import SibylMcpTokenVerifier
from sibyl.config import Settings


@pytest.mark.asyncio
async def test_mcp_token_verifier_accepts_jwt(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-key-for-api-tests")

    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    token = create_access_token(user_id=uuid4(), extra_claims={"scope": "mcp"})
    with patch(
        "sibyl.auth.mcp_auth.validate_access_session",
        AsyncMock(return_value=True),
    ) as validate_session:
        access = await SibylMcpTokenVerifier().verify_token(token)
    assert access is not None
    assert access.client_id.startswith("user:")
    assert "mcp" in access.scopes
    validate_session.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_mcp_token_verifier_rejects_jwt_without_mcp_scope(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-key-for-api-tests")

    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    token = create_access_token(user_id=uuid4())
    with patch(
        "sibyl.auth.mcp_auth.validate_access_session",
        AsyncMock(return_value=True),
    ) as validate_session:
        access = await SibylMcpTokenVerifier().verify_token(token)
    assert access is None
    validate_session.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_mcp_token_verifier_rejects_revoked_jwt(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-key-for-api-tests")

    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    token = create_access_token(user_id=uuid4())
    with patch(
        "sibyl.auth.mcp_auth.validate_access_session",
        AsyncMock(return_value=False),
    ) as validate_session:
        access = await SibylMcpTokenVerifier().verify_token(token)
    assert access is None
    validate_session.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_mcp_token_verifier_rejects_auth_store_timeout(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-key-for-api-tests")

    from sibyl import config as config_module

    config_module.settings = Settings(_env_file=None)  # type: ignore[assignment]

    token = create_access_token(user_id=uuid4())
    with patch(
        "sibyl.auth.mcp_auth.validate_access_session",
        AsyncMock(side_effect=TimeoutError),
    ) as validate_session:
        access = await SibylMcpTokenVerifier().verify_token(token)
    assert access is None
    validate_session.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_mcp_token_verifier_rejects_invalid_jwt(monkeypatch) -> None:
    monkeypatch.setenv("SIBYL_JWT_SECRET", "test-jwt-secret-key-for-api-tests")

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


@pytest.mark.asyncio
async def test_mcp_token_verifier_rejects_api_key_with_empty_scopes() -> None:
    auth = ApiKeyAuth(
        api_key_id=uuid4(),
        user_id=uuid4(),
        organization_id=uuid4(),
        scopes=[],
    )

    with patch(
        "sibyl.auth.mcp_auth.authenticate_api_key",
        AsyncMock(return_value=auth),
    ) as authenticate:
        access = await SibylMcpTokenVerifier().verify_token("sk_live_test")
        assert access is None
    authenticate.assert_awaited_once_with("sk_live_test")
