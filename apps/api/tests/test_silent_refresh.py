from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from sibyl import config as config_module
from sibyl.api.routes import auth as auth_routes
from sibyl.auth import oidc
from sibyl.config import OIDCProviderSettings, OIDCSettings
from sibyl_core.auth import OrganizationRole


class FakeRequest:
    def __init__(
        self,
        *,
        query_params: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> None:
        self.query_params = query_params or {}
        self.cookies = cookies or {}
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")


def _provider() -> OIDCProviderSettings:
    return OIDCProviderSettings(
        name="entra",
        issuer="https://login.microsoftonline.com/tenant/v2.0",
        client_id="sibyl-client",
        client_secret_env="SIBYL_OIDC_ENTRA_CLIENT_SECRET",
    )


def _install_provider(monkeypatch: pytest.MonkeyPatch) -> OIDCProviderSettings:
    provider = _provider()
    monkeypatch.setattr(
        config_module.settings,
        "oidc",
        OIDCSettings(providers=[provider], session_minutes=15, silent_refresh_enabled=True),
    )
    return provider


def _set_cookie_headers(response) -> list[str]:
    return [
        value.decode()
        for name, value in response.raw_headers
        if name.lower() == b"set-cookie"
    ]


@pytest.mark.asyncio
async def test_silent_refresh_soft_error_bounces_to_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch)

    response = await auth_routes.oidc_silent_refresh(
        request=FakeRequest(query_params={"error": "login_required", "next": "/studio"}),
        provider_name="entra",
    )

    assert response.status_code == 302
    assert response.headers["location"].endswith("/login?error=login_required&next=%2Fstudio")


@pytest.mark.asyncio
async def test_silent_refresh_starts_prompt_none_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _install_provider(monkeypatch)
    redirect = AsyncMock(return_value=SimpleNamespace(status_code=302))
    request = FakeRequest()
    monkeypatch.setattr(auth_routes, "oidc_authorize_redirect", redirect)

    response = await auth_routes.oidc_silent_refresh(
        request=request,
        provider_name="entra",
    )

    assert response.status_code == 302
    redirect.assert_awaited_once_with(
        request,
        provider=provider,
        redirect_uri=auth_routes.oidc_redirect_uri(provider, route="refresh"),
        prompt="none",
    )


@pytest.mark.asyncio
async def test_silent_refresh_success_sets_access_cookie_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _install_provider(monkeypatch)
    identity = oidc.OIDCClaims(
        provider=provider,
        claims={"sub": "subject", "roles": ["Sibyl.Member"]},
        subject_key="entra:tenant:object",
        subject="subject",
        issuer=provider.issuer,
        role=OrganizationRole.MEMBER,
    )
    issued = SimpleNamespace(
        user=SimpleNamespace(id=uuid4(), email="nova@example.com", name="Nova"),
        organization=SimpleNamespace(id=uuid4(), slug="sibyl", name="Sibyl"),
        session_id=uuid4(),
        access_token="fresh-access-token",
        access_expires=datetime.now(UTC) + timedelta(minutes=15),
    )
    monkeypatch.setattr(auth_routes, "oidc_callback_claims", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth_routes, "provision_oidc_user", AsyncMock(return_value=issued))

    response = await auth_routes.oidc_silent_refresh(
        request=FakeRequest(query_params={"code": "auth-code"}),
        provider_name="entra",
    )

    headers = _set_cookie_headers(response)
    assert response.status_code == 302
    assert any(
        header.startswith(f"{auth_routes.ACCESS_TOKEN_COOKIE}=fresh-access-token")
        for header in headers
    )
    assert not any(header.startswith(f"{auth_routes.REFRESH_TOKEN_COOKIE}=") for header in headers)


@pytest.mark.asyncio
async def test_silent_refresh_role_removed_denies_without_minting_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_provider(monkeypatch)
    provision = AsyncMock()
    monkeypatch.setattr(
        auth_routes,
        "oidc_callback_claims",
        AsyncMock(
            side_effect=HTTPException(
                status_code=403,
                detail={"code": "oidc_missing_role"},
            )
        ),
    )
    monkeypatch.setattr(auth_routes, "provision_oidc_user", provision)

    with pytest.raises(HTTPException) as exc_info:
        await auth_routes.oidc_silent_refresh(
            request=FakeRequest(query_params={"code": "auth-code"}),
            provider_name="entra",
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "oidc_missing_role"
    provision.assert_not_awaited()
