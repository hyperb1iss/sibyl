from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from sibyl import config as config_module
from sibyl.auth import jit, oidc
from sibyl.config import OIDCProviderSettings, OIDCSettings
from sibyl_core.auth import OrganizationRole


def _provider() -> OIDCProviderSettings:
    return OIDCProviderSettings(
        name="okta",
        issuer="https://example.okta.com/oauth2/default",
        client_id="sibyl-client",
        client_secret_env="SIBYL_OIDC_OKTA_CLIENT_SECRET",
    )


def test_jit_rejects_missing_role_claim() -> None:
    with pytest.raises(HTTPException) as exc_info:
        oidc.normalize_oidc_claims(
            provider=_provider(),
            claims={
                "iss": "https://example.okta.com/oauth2/default",
                "sub": "subject",
                "email": "nova@example.com",
            },
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "oidc_missing_role"


@pytest.mark.asyncio
async def test_jit_provisions_member_role_without_email_matching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    identity = oidc.OIDCClaims(
        provider=provider,
        claims={
            "iss": provider.issuer,
            "sub": "subject",
            "email": "shared@example.com",
            "roles": ["Sibyl.Member"],
        },
        subject_key="oidc:https://example.okta.com/oauth2/default:subject",
        subject="subject",
        issuer=provider.issuer,
        role=OrganizationRole.MEMBER,
    )
    login = AsyncMock(return_value=SimpleNamespace(access_token="access-token"))
    monkeypatch.setattr(jit, "login_oidc_identity", login)

    await jit.provision_oidc_user(identity=identity, request=SimpleNamespace())

    login.assert_awaited_once()
    call = login.await_args.kwargs
    assert call["subject_key"] == "oidc:https://example.okta.com/oauth2/default:subject"
    assert call["email"] == "shared@example.com"
    assert call["role"] is OrganizationRole.MEMBER


def test_jit_rejects_user_after_sibyl_role_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    monkeypatch.setattr(config_module.settings, "oidc", OIDCSettings(role_claim="groups"))

    with pytest.raises(HTTPException) as exc_info:
        oidc.normalize_oidc_claims(
            provider=provider,
            claims={
                "iss": provider.issuer,
                "sub": "subject",
                "groups": ["Former.Employee"],
            },
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "oidc_missing_role"
