"""Just-in-time OIDC user provisioning."""

from __future__ import annotations

from fastapi import Request

from sibyl.auth.oidc import OIDCClaims
from sibyl.persistence.auth_runtime import login_oidc_identity


async def provision_oidc_user(
    *,
    identity: OIDCClaims,
    request: Request,
    action: str = "auth.oidc.login",
):
    claims = identity.claims
    return await login_oidc_identity(
        provider_name=identity.provider.name,
        issuer=identity.issuer,
        client_id=identity.provider.client_id,
        scopes=list(identity.provider.scopes),
        role_claim=identity.provider.role_claim_override,
        organization_slug=identity.provider.organization_slug,
        subject=identity.subject,
        subject_key=identity.subject_key,
        email=_optional_str(claims.get("email")),
        name=_display_name(claims),
        avatar_url=_optional_str(claims.get("picture")),
        role=identity.role,
        claims=claims,
        request=request,
        action=action,
    )


def _display_name(claims: dict[str, object]) -> str:
    for key in ("name", "preferred_username", "email", "sub"):
        value = _optional_str(claims.get(key))
        if value:
            return value
    return "OIDC User"


def _optional_str(value: object | None) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
