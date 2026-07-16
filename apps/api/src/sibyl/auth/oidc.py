"""OpenID Connect helpers for browser authentication."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

import jwt
from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request, status

from sibyl import config as config_module
from sibyl.config import OIDCProviderSettings
from sibyl_core.auth import OrganizationRole

OIDC_PROVIDER_NOT_FOUND = "OIDC provider is not configured"

_JWT_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
)


@dataclass(frozen=True, slots=True)
class EnabledOIDCProvider:
    name: str
    label: str
    login_url: str


@dataclass(frozen=True, slots=True)
class OIDCClaims:
    provider: OIDCProviderSettings
    claims: dict[str, object]
    subject_key: str
    subject: str
    issuer: str
    role: OrganizationRole


def enabled_oidc_providers() -> list[EnabledOIDCProvider]:
    return [
        EnabledOIDCProvider(
            name=provider.name,
            label=_provider_label(provider.name),
            login_url=f"/api/auth/oidc/{provider.name}/login",
        )
        for provider in config_module.settings.oidc.providers
    ]


def get_oidc_provider(name: str) -> OIDCProviderSettings:
    normalized = name.strip().lower()
    for provider in config_module.settings.oidc.providers:
        if provider.name.strip().lower() == normalized:
            return provider
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "oidc_provider_not_found", "message": OIDC_PROVIDER_NOT_FOUND},
    )


def oidc_redirect_uri(provider: OIDCProviderSettings, *, route: str = "callback") -> str:
    base = (
        config_module.settings.oidc.redirect_uri_base.strip()
        or config_module.settings.server_url.rstrip("/")
    )
    return urljoin(
        base.rstrip("/") + "/",
        f"api/auth/oidc/{provider.name}/{route}",
    )


def get_oauth_client(provider: OIDCProviderSettings):
    client_secret = os.environ.get(provider.client_secret_env, "").strip()
    if not client_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "oidc_client_secret_missing",
                "message": f"{provider.client_secret_env} is not configured",
            },
        )

    oauth = OAuth()
    return oauth.register(
        name=provider.name,
        client_id=provider.client_id,
        client_secret=client_secret,
        server_metadata_url=_discovery_url(provider.issuer),
        client_kwargs={"scope": " ".join(provider.scopes)},
    )


async def oidc_authorize_redirect(
    request: Request,
    *,
    provider: OIDCProviderSettings,
    redirect_uri: str,
    prompt: str | None = None,
):
    client = get_oauth_client(provider)
    kwargs: dict[str, str] = {}
    if prompt:
        kwargs["prompt"] = prompt
    return await client.authorize_redirect(request, redirect_uri, **kwargs)


async def oidc_callback_claims(
    request: Request,
    *,
    provider: OIDCProviderSettings,
) -> OIDCClaims:
    client = get_oauth_client(provider)
    token = await client.authorize_access_token(request)
    claims = _token_claims(token)
    id_token = token.get("id_token")
    if isinstance(id_token, str) and id_token:
        metadata = await client.load_server_metadata()
        jwks_uri = str(metadata.get("jwks_uri") or "")
        claims = verify_id_token(id_token, provider=provider, jwks_uri=jwks_uri)
    return normalize_oidc_claims(provider=provider, claims=claims)


def normalize_oidc_claims(
    *,
    provider: OIDCProviderSettings,
    claims: Mapping[str, object],
) -> OIDCClaims:
    subject = _required_claim(claims, "sub")
    issuer = str(claims.get("iss") or provider.issuer).strip()
    role = extract_sibyl_role(claims, provider=provider)
    subject_key = stable_subject_key(provider=provider, claims=claims)
    return OIDCClaims(
        provider=provider,
        claims={str(key): _json_claim(value) for key, value in claims.items()},
        subject_key=subject_key,
        subject=subject,
        issuer=issuer,
        role=role,
    )


def verify_id_token(
    id_token: str,
    *,
    provider: OIDCProviderSettings,
    jwks_uri: str,
) -> dict[str, object]:
    if not jwks_uri:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "oidc_jwks_missing", "message": "OIDC JWKS URI is missing"},
        )
    try:
        signing_key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
        payload = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=list(_JWT_ALGORITHMS),
            audience=provider.client_id,
            issuer=provider.issuer,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "oidc_id_token_invalid", "message": str(exc)},
        ) from exc
    return {str(key): _json_claim(value) for key, value in payload.items()}


def extract_claim_path(claims: Mapping[str, object], path: str) -> object | None:
    current: object = claims
    for part in path.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def extract_sibyl_role(
    claims: Mapping[str, object],
    *,
    provider: OIDCProviderSettings,
) -> OrganizationRole:
    path = provider.role_claim_override or config_module.settings.oidc.role_claim
    value = extract_claim_path(claims, path)
    role = _role_from_claim(value)
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "code": "oidc_missing_role",
                "message": "OIDC identity is missing a Sibyl role claim",
            },
        )
    return role


def stable_subject_key(
    *,
    provider: OIDCProviderSettings,
    claims: Mapping[str, object],
) -> str:
    issuer = str(claims.get("iss") or provider.issuer).strip()
    tenant_id = str(claims.get("tid") or "").strip()
    object_id = str(claims.get("oid") or "").strip()
    if _is_entra_issuer(issuer) or (tenant_id and object_id):
        if not tenant_id or not object_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "oidc_subject_incomplete",
                    "message": "Entra ID tokens must include tid and oid claims",
                },
            )
        return f"entra:{tenant_id}:{object_id}"
    subject = _required_claim(claims, "sub")
    return f"oidc:{issuer}:{subject}"


def _token_claims(token: Mapping[str, Any]) -> Mapping[str, object]:
    userinfo = token.get("userinfo")
    if isinstance(userinfo, Mapping):
        return userinfo
    id_token = token.get("id_token")
    if isinstance(id_token, str) and id_token:
        return jwt.decode(id_token, options={"verify_signature": False})
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "oidc_claims_missing", "message": "OIDC token did not include claims"},
    )


def _role_from_claim(value: object) -> OrganizationRole | None:
    values: list[object]
    if isinstance(value, list | tuple | set):
        values = list(value)
    else:
        values = [value]
    role_map = {
        "Sibyl.Owner": OrganizationRole.OWNER,
        "Sibyl.Admin": OrganizationRole.ADMIN,
        "Sibyl.Member": OrganizationRole.MEMBER,
        "owner": OrganizationRole.OWNER,
        "admin": OrganizationRole.ADMIN,
        "member": OrganizationRole.MEMBER,
    }
    roles = {role_map[str(item)] for item in values if str(item) in role_map}
    for role in (OrganizationRole.OWNER, OrganizationRole.ADMIN, OrganizationRole.MEMBER):
        if role in roles:
            return role
    return None


def _required_claim(claims: Mapping[str, object], key: str) -> str:
    value = str(claims.get(key) or "").strip()
    if value:
        return value
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": "oidc_claim_missing", "message": f"OIDC claim {key!r} is required"},
    )


def _discovery_url(issuer: str) -> str:
    return urljoin(issuer.rstrip("/") + "/", ".well-known/openid-configuration")


def _is_entra_issuer(issuer: str) -> bool:
    host = urlsplit(issuer).netloc.lower()
    return host == "login.microsoftonline.com" or host.endswith(".login.microsoftonline.com")


def _provider_label(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").strip().title() or "OIDC"


def _json_claim(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_claim(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _json_claim(item) for key, item in value.items()}
    return str(value)
