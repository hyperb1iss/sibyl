"""JWT helpers for Sibyl."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt

from sibyl import config as config_module


class JwtError(ValueError):
    """JWT validation or creation error."""


def _require_secret() -> str:
    secret = config_module.settings.jwt_secret.get_secret_value()
    if not secret:
        raise JwtError("JWT secret is not configured (set SIBYL_JWT_SECRET)")
    return secret


def create_access_token(
    *,
    user_id: UUID,
    organization_id: UUID | None = None,
    expires_in: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Create a signed access token.

    Token schema:
    - sub: user_id
    - org: organization_id (optional)
    - typ: "access"
    - iat/exp: unix timestamps
    """
    secret = _require_secret()
    now = datetime.now(UTC)
    ttl = expires_in or timedelta(hours=config_module.settings.jwt_expiry_hours)

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    if organization_id is not None:
        payload["org"] = str(organization_id)
    if extra_claims:
        payload.update(extra_claims)

    try:
        return jwt.encode(payload, secret, algorithm=config_module.settings.jwt_algorithm)
    except Exception as e:
        raise JwtError(f"Failed to sign JWT: {e}") from e


def verify_access_token(token: str) -> dict[str, Any]:
    """Verify token signature + expiry and return claims."""
    secret = _require_secret()
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[config_module.settings.jwt_algorithm],
            options={"require": ["sub", "iat", "exp"]},
        )
    except jwt.PyJWTError as e:
        raise JwtError(str(e)) from e

    if claims.get("typ") != "access":
        raise JwtError("Invalid token type")

    return claims
