"""Setup mode must not unlock local surfaces on SSO-only instances (#456).

On a fresh OIDC-only deployment, the window between first boot and the
first owner login was claimable: setup mode bypassed localAuthEnabled=false
for signup and login, and served configuration endpoints unauthenticated.
These tests pin the closed behavior: with providers configured and local
auth disabled, setup mode changes nothing about what is reachable.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from sibyl import config as config_module
from sibyl.api.routes import auth as auth_routes
from sibyl.config import OIDCProviderSettings
from sibyl.persistence.surreal import setup as surreal_setup


def _entra_provider() -> OIDCProviderSettings:
    return OIDCProviderSettings(
        name="entra",
        issuer="https://login.microsoftonline.com/tenant/v2.0",
        client_id="client-id",
        client_secret_env="SIBYL_OIDC_ENTRA_CLIENT_SECRET",
        organization_slug="example-org",
    )


@pytest.fixture
def sso_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module.settings.oidc, "providers", [_entra_provider()])
    monkeypatch.setattr(config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(config_module.settings, "break_glass_enabled", False)


@pytest.fixture
def local_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module.settings.oidc, "providers", [])
    monkeypatch.setattr(config_module.settings, "local_auth_enabled", False)
    monkeypatch.setattr(config_module.settings, "break_glass_enabled", False)


def test_sso_only_instance_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module.settings.oidc, "providers", [_entra_provider()])
    monkeypatch.setattr(config_module.settings, "local_auth_enabled", False)
    assert config_module.settings.sso_only_instance is True

    monkeypatch.setattr(config_module.settings, "local_auth_enabled", True)
    assert config_module.settings.sso_only_instance is False

    monkeypatch.setattr(config_module.settings.oidc, "providers", [])
    monkeypatch.setattr(config_module.settings, "local_auth_enabled", False)
    assert config_module.settings.sso_only_instance is False


@pytest.mark.asyncio
async def test_local_auth_stays_closed_in_setup_mode_when_sso_only(
    monkeypatch: pytest.MonkeyPatch,
    sso_only: None,
) -> None:
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as exc:
        await auth_routes._require_local_auth_allowed(object())

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_local_auth_opens_in_setup_mode_without_sso(
    monkeypatch: pytest.MonkeyPatch,
    local_identity: None,
) -> None:
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=True))

    assert await auth_routes._require_local_auth_allowed(object()) is None


@pytest.mark.asyncio
async def test_signup_stays_closed_in_setup_mode_when_sso_only(
    monkeypatch: pytest.MonkeyPatch,
    sso_only: None,
) -> None:
    monkeypatch.setattr(auth_routes, "is_setup_mode", AsyncMock(return_value=True))
    monkeypatch.setattr(config_module.settings, "public_signups_enabled", False)
    body = auth_routes.LocalSignupRequest(
        email="claim@attacker.test", password="password-123456", name="First Claimer"
    )

    with pytest.raises(HTTPException) as exc:
        await auth_routes._require_signup_allowed(body=body)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_setup_gates_require_token_in_setup_mode_when_sso_only(
    monkeypatch: pytest.MonkeyPatch,
    sso_only: None,
) -> None:
    monkeypatch.setattr(surreal_setup, "is_setup_mode", AsyncMock(return_value=True))

    class _BareRequest:
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}

    with pytest.raises(HTTPException) as exc:
        await surreal_setup.require_setup_mode_or_auth(_BareRequest())
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        await surreal_setup.require_setup_mode_or_admin(_BareRequest())
    assert exc.value.status_code == 401
