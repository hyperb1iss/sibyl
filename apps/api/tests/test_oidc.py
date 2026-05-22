from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

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
        OIDCSettings(providers=[provider], session_minutes=15),
    )
    return provider


def _set_cookie_headers(response) -> list[str]:
    return [
        value.decode()
        for name, value in response.raw_headers
        if name.lower() == b"set-cookie"
    ]


def test_oidc_provider_list_exposes_configured_enterprise_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _install_provider(monkeypatch)

    providers = oidc.enabled_oidc_providers()

    assert providers[0].name == provider.name
    assert providers[0].label == "Entra"
    assert providers[0].login_url == "/api/auth/oidc/entra/login"


def test_oidc_role_claim_supports_dotted_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module.settings, "oidc", OIDCSettings(role_claim="resource.roles"))

    role = oidc.extract_sibyl_role(
        {"resource": {"roles": ["Sibyl.Member"]}},
        provider=_provider(),
    )

    assert role is OrganizationRole.MEMBER


def test_oidc_stable_subject_uses_entra_tenant_object_id() -> None:
    subject_key = oidc.stable_subject_key(
        provider=_provider(),
        claims={
            "iss": "https://login.microsoftonline.com/tenant/v2.0",
            "sub": "pairwise-subject",
            "tid": "tenant-id",
            "oid": "object-id",
            "email": "not-used@example.com",
        },
    )

    assert subject_key == "entra:tenant-id:object-id"


def test_oidc_id_token_verification_uses_provider_jwks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _provider()
    seen: dict[str, object] = {}

    class FakeJWKClient:
        def __init__(self, jwks_uri: str) -> None:
            seen["jwks_uri"] = jwks_uri

        def get_signing_key_from_jwt(self, token: str):
            seen["token"] = token
            return SimpleNamespace(key="public-key")

    def fake_decode(*args, **kwargs):
        seen["decode"] = (args, kwargs)
        return {
            "iss": provider.issuer,
            "sub": "subject",
            "roles": ["Sibyl.Admin"],
        }

    monkeypatch.setattr(oidc.jwt, "PyJWKClient", FakeJWKClient)
    monkeypatch.setattr(oidc.jwt, "decode", fake_decode)

    claims = oidc.verify_id_token(
        "id-token",
        provider=provider,
        jwks_uri="https://issuer.example/jwks",
    )

    assert claims["sub"] == "subject"
    assert seen["jwks_uri"] == "https://issuer.example/jwks"
    args, kwargs = seen["decode"]
    assert args[0] == "id-token"
    assert args[1] == "public-key"
    assert kwargs["audience"] == provider.client_id
    assert kwargs["issuer"] == provider.issuer


@pytest.mark.asyncio
async def test_oidc_callback_sets_access_cookie_without_refresh_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _install_provider(monkeypatch)
    identity = oidc.OIDCClaims(
        provider=provider,
        claims={"sub": "subject", "roles": ["Sibyl.Member"]},
        subject_key="oidc:issuer:subject",
        subject="subject",
        issuer=provider.issuer,
        role=OrganizationRole.MEMBER,
    )
    issued = SimpleNamespace(
        user=SimpleNamespace(id=uuid4(), email="nova@example.com", name="Nova"),
        organization=SimpleNamespace(id=uuid4(), slug="sibyl", name="Sibyl"),
        session_id=uuid4(),
        access_token="oidc-access-token",
        access_expires=datetime.now(UTC) + timedelta(minutes=15),
    )

    monkeypatch.setattr(auth_routes, "oidc_callback_claims", AsyncMock(return_value=identity))
    monkeypatch.setattr(auth_routes, "provision_oidc_user", AsyncMock(return_value=issued))

    response = await auth_routes.oidc_callback(
        request=FakeRequest(query_params={"code": "auth-code"}),
        provider_name="entra",
    )

    headers = _set_cookie_headers(response)
    assert response.status_code == 302
    assert any(
        header.startswith(f"{auth_routes.ACCESS_TOKEN_COOKIE}=oidc-access-token")
        for header in headers
    )
    assert not any(header.startswith(f"{auth_routes.REFRESH_TOKEN_COOKIE}=") for header in headers)
