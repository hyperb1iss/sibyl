"""FastMCP token verification for Sibyl.

FastMCP can be configured as an OAuth Resource Server. We don't run a full OAuth
authorization server yet, but we still want MCP endpoints to require a valid
Bearer token.

Accepted tokens:
- JWT access tokens issued by Sibyl (/api/auth/*)
- API keys starting with "sk_" (hashed in the active auth runtime)
"""

from __future__ import annotations

from uuid import UUID

from mcp.server.auth.provider import AccessToken

from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.persistence.auth_runtime import authenticate_api_key, validate_access_session


def _parse_scopes(claims: dict[str, object]) -> list[str]:
    scopes = claims.get("scopes")
    if isinstance(scopes, list):
        parsed_scopes = [item for item in scopes if isinstance(item, str)]
        if len(parsed_scopes) == len(scopes):
            return parsed_scopes
    scope = claims.get("scope")
    if isinstance(scope, str) and scope.strip():
        return scope.split()
    return ["mcp"]


class SibylMcpTokenVerifier:
    """Verify MCP Bearer tokens as either JWT or API key."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith("sk_"):
            auth = await authenticate_api_key(token)
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
        try:
            is_active = await validate_access_session(token)
        except TimeoutError:
            return None
        if not is_active:
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
