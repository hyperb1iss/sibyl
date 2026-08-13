"""FastMCP token verification for Sibyl.

FastMCP can be configured as an OAuth Resource Server. We don't run a full OAuth
authorization server yet, but we still want MCP endpoints to require a valid
Bearer token.

Accepted tokens:
- JWT access tokens issued by Sibyl (/api/auth/*)
- API keys starting with "sk_" (hashed in the active auth runtime)
"""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from mcp.server.auth.provider import AccessToken

from sibyl.auth.jwt import JwtError, verify_access_token
from sibyl.persistence.auth_runtime import authenticate_api_key, validate_access_session

# Scope vocabulary for the MCP surface.
#
# `mcp` admits a credential to the MCP surface at all. `api:read` / `api:write`
# are the same scopes REST enforces (auth/dependencies.py), reused here so a
# single key describes one capability on both surfaces: a key that REST refuses
# a mutation must not be handed the same mutation through an MCP tool.
#
# Keys that carry `mcp` and no granular scope keep unrestricted MCP access.
# That is the default scope set every key has been issued with, and revoking
# their writes would break every deployed agent.
MCP_SURFACE_SCOPE = "mcp"
MCP_READ_SCOPE = "api:read"
MCP_WRITE_SCOPE = "api:write"
MCP_GRANULAR_SCOPES = frozenset({MCP_READ_SCOPE, MCP_WRITE_SCOPE})


def normalize_scopes(scopes: Iterable[str] | None) -> set[str]:
    """Return the non-empty, whitespace-stripped string scopes in ``scopes``."""
    if not scopes:
        return set()
    return {scope.strip() for scope in scopes if isinstance(scope, str) and scope.strip()}


def mcp_scopes_allow(scopes: Iterable[str] | None, *, write: bool) -> bool:
    """Report whether ``scopes`` may invoke a read or write MCP tool."""
    normalized = normalize_scopes(scopes)
    if MCP_SURFACE_SCOPE not in normalized:
        return False
    if not write:
        return True
    if not normalized & MCP_GRANULAR_SCOPES:
        return True
    return MCP_WRITE_SCOPE in normalized


def insufficient_mcp_scope_message(scopes: Iterable[str] | None, *, write: bool) -> str:
    """Build the denial message for a credential that fails ``mcp_scopes_allow``."""
    normalized = normalize_scopes(scopes)
    if MCP_SURFACE_SCOPE not in normalized:
        expected = MCP_SURFACE_SCOPE
    else:
        expected = f"{MCP_SURFACE_SCOPE} and {MCP_WRITE_SCOPE}"
    actual = ", ".join(sorted(normalized)) or "none"
    verb = "write" if write else "read"
    return (
        f"API key is missing the scope required for this MCP {verb} tool. "
        f"Expected {expected}; key has {actual}."
    )


def _parse_scopes(claims: dict[str, object]) -> list[str]:
    scopes = claims.get("scopes")
    if isinstance(scopes, list):
        parsed_scopes = [item for item in scopes if isinstance(item, str)]
        if len(parsed_scopes) == len(scopes):
            return parsed_scopes
    scope = claims.get("scope")
    if isinstance(scope, str) and scope.strip():
        return scope.split()
    return []


class SibylMcpTokenVerifier:
    """Verify MCP Bearer tokens as either JWT or API key."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if token.startswith("sk_"):
            auth = await authenticate_api_key(token)
            if auth is None:
                return None
            scopes = [scope for scope in (auth.scopes or []) if isinstance(scope, str)]
            if MCP_SURFACE_SCOPE not in scopes:
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
        scopes = _parse_scopes(claims)
        if MCP_SURFACE_SCOPE not in scopes:
            return None

        exp = claims.get("exp")
        expires_at = exp if isinstance(exp, int) else None

        return AccessToken(
            token=token,
            client_id=f"user:{user_id}",
            scopes=scopes,
            expires_at=expires_at,
        )
