"""HTTP auth helpers."""

from __future__ import annotations


def extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if not authorization.lower().startswith("bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


def select_access_token(*, authorization: str | None, cookie_token: str | None) -> str | None:
    """Pick the access token from Authorization header or cookie."""
    bearer = extract_bearer_token(authorization)
    if bearer:
        return bearer
    if cookie_token:
        token = cookie_token.strip()
        return token or None
    return None
