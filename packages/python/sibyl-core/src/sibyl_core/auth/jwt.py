"""JWT helpers for Sibyl.

Token Types:
- Access Token: Short-lived (default 1 hour), used for API authentication
- Refresh Token: Long-lived (default 30 days), used to obtain new access tokens
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import jwt
from sibyl import config as config_module

if TYPE_CHECKING:
    from uuid import UUID


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
    ttl = expires_in or timedelta(minutes=config_module.settings.access_token_expire_minutes)

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


def create_refresh_token(
    *,
    user_id: UUID,
    organization_id: UUID | None = None,
    session_id: UUID | None = None,
    expires_in: timedelta | None = None,
) -> tuple[str, datetime]:
    """Create a signed refresh token.

    Returns:
        Tuple of (token, expires_at)

    Token schema:
    - sub: user_id
    - org: organization_id (optional)
    - sid: session_id (optional, for token rotation)
    - typ: "refresh"
    - jti: unique token ID (for revocation)
    - iat/exp: unix timestamps
    """
    secret = _require_secret()
    now = datetime.now(UTC)
    ttl = expires_in or timedelta(days=config_module.settings.refresh_token_expire_days)
    expires_at = now + ttl

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "typ": "refresh",
        "jti": secrets.token_urlsafe(16),  # Unique ID for this refresh token
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if organization_id is not None:
        payload["org"] = str(organization_id)
    if session_id is not None:
        payload["sid"] = str(session_id)

    try:
        token = jwt.encode(payload, secret, algorithm=config_module.settings.jwt_algorithm)
        return token, expires_at
    except Exception as e:
        raise JwtError(f"Failed to sign refresh token: {e}") from e


def verify_refresh_token(token: str, *, verify_expiry: bool = True) -> dict[str, Any]:
    """Verify refresh token signature and return claims.

    Args:
        token: The refresh token to verify
        verify_expiry: If False, allow expired tokens (for grace period refresh)
    """
    secret = _require_secret()
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[config_module.settings.jwt_algorithm],
            options={
                "require": ["sub", "iat", "exp", "jti"],
                "verify_exp": verify_expiry,
            },
        )
    except jwt.ExpiredSignatureError as e:
        raise JwtError("Refresh token expired") from e
    except jwt.PyJWTError as e:
        raise JwtError(str(e)) from e

    if claims.get("typ") != "refresh":
        raise JwtError("Invalid token type (expected refresh)")

    return claims


def decode_token_unverified(token: str) -> dict[str, Any]:
    """Decode token without verification (for debugging/logging only)."""
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return {}
