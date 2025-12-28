"""FastMCP token verification for Sibyl.

FastMCP can be configured as an OAuth Resource Server. We don't run a full OAuth
authorization server yet, but we still want MCP endpoints to require a valid
Bearer token.

Accepted tokens:
- JWT access tokens issued by Sibyl (/api/auth/*)
- API keys starting with "sk_" (hashed + stored in Postgres)
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from mcp.server.auth.provider import AccessToken

from sibyl.auth.api_keys import ApiKeyManager
from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.db.connection import get_session


def _parse_scopes(claims: dict[str, Any]) -> list[str]:
    scopes = claims.get("scopes")
    if isinstance(scopes, list) and all(isinstance(item, str) for item in scopes):
        return scopes
    scope = claims.get("scope")
    if isinstance(scope, str) and scope.strip():
        return scope.split()
    return ["mcp"]


class SibylMcpTokenVerifier:
    """Verify MCP Bearer tokens as either JWT or API key."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith("sk_"):
            async with get_session() as session:
                auth = await ApiKeyManager.from_session(session).authenticate(token)
            if auth is None:
                return None
            scopes = list(auth.scopes or []) or ["mcp"]
            if scopes and "mcp" not in scopes:
                return None
            return AccessToken(
                token=token,
                client_id=f"api_key:{auth.api_key_id}",
                scopes=scopes,
            )

        try:
            claims = verify_access_token(token)
        except JwtError:
            return None

        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            return None
        try:
            user_id = UUID(sub)
        except ValueError:
            return None

        exp = claims.get("exp")
        expires_at = exp if isinstance(exp, int) else None

        return AccessToken(
            token=token,
            client_id=f"user:{user_id}",
            scopes=_parse_scopes(claims),
            expires_at=expires_at,
        )
