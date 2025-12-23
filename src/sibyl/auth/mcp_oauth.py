"""OAuth Authorization Server provider for FastMCP.

This enables Codex/MCP clients to authenticate via standard OAuth endpoints
served by FastMCP when `auth_server_provider` is configured:
- `/.well-known/oauth-authorization-server`
- `/authorize`
- `/token`
- `/register` (dynamic client registration)

Implementation notes:
- Clients are stored in-memory (re-registration is fine).
- Authorization codes are short-lived, in-memory, single-use.
- Access tokens are Sibyl JWT access tokens (Bearer).
- Refresh tokens are JWT refresh tokens (best-effort in-memory revocation).
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID

import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from sibyl import config as config_module
from sibyl.auth.api_keys import ApiKeyManager
from sibyl.auth.jwt import JwtError, create_access_token, verify_access_token
from sibyl.auth.memberships import OrganizationMembershipManager
from sibyl.auth.organizations import OrganizationManager
from sibyl.auth.users import UserManager
from sibyl.db.connection import get_session

OAUTH_SCOPE = "mcp"


def _require_jwt_secret() -> str:
    secret = config_module.settings.jwt_secret.get_secret_value()
    if not secret:
        raise JwtError("JWT secret is not configured (set SIBYL_JWT_SECRET)")
    return secret


def _jwt_encode(payload: dict[str, Any]) -> str:
    secret = _require_jwt_secret()
    return jwt.encode(payload, secret, algorithm=config_module.settings.jwt_algorithm)


def _jwt_decode(token: str) -> dict[str, Any]:
    secret = _require_jwt_secret()
    return jwt.decode(
        token,
        secret,
        algorithms=[config_module.settings.jwt_algorithm],
        options={"require": ["sub", "iat", "exp"]},
    )


def _parse_scopes_from_claims(claims: dict[str, Any]) -> list[str]:
    scopes = claims.get("scopes")
    if isinstance(scopes, list) and all(isinstance(item, str) for item in scopes):
        return scopes
    scope = claims.get("scope")
    if isinstance(scope, str) and scope.strip():
        return scope.split()
    return [OAUTH_SCOPE]


def _add_query_params(url: str, params: dict[str, str]) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def _create_refresh_token(
    *,
    user_id: UUID,
    organization_id: UUID | None,
    client_id: str,
    scopes: list[str],
    expires_in: timedelta = timedelta(days=30),
) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "typ": "refresh",
        "cid": client_id,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "scope": " ".join(scopes),
    }
    if organization_id is not None:
        payload["org"] = str(organization_id)
    return _jwt_encode(payload)


class SibylAuthorizationCode(AuthorizationCode):
    user_id: str
    organization_id: str | None = None


@dataclass(frozen=True)
class _PendingAuth:
    client_id: str
    expires_at: float
    params: AuthorizationParams


class SibylMcpOAuthProvider(
    OAuthAuthorizationServerProvider[SibylAuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth provider for FastMCP auth routes."""

    def __init__(self) -> None:
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, _PendingAuth] = {}
        self._codes: dict[str, SibylAuthorizationCode] = {}
        self._revoked_refresh: set[str] = set()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            return
        self._clients[str(client_info.client_id)] = client_info

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = _PendingAuth(
            client_id=str(client.client_id),
            expires_at=time.time() + 10 * 60,
            params=params,
        )
        issuer = str(config_module.settings.server_url).rstrip("/")
        return _add_query_params(f"{issuer}/_oauth/login", {"req": request_id})

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> SibylAuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code is None:
            return None
        if str(code.client_id) != str(client.client_id):
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: SibylAuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)

        user_id = UUID(authorization_code.user_id)
        org_id = (
            UUID(authorization_code.organization_id) if authorization_code.organization_id else None
        )
        scopes = authorization_code.scopes or [OAUTH_SCOPE]

        access = create_access_token(
            user_id=user_id,
            organization_id=org_id,
            extra_claims={"scope": " ".join(scopes)},
        )
        refresh = _create_refresh_token(
            user_id=user_id,
            organization_id=org_id,
            client_id=str(client.client_id),
            scopes=scopes,
        )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(
                timedelta(hours=config_module.settings.jwt_expiry_hours).total_seconds()
            ),
            scope=" ".join(scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        if refresh_token in self._revoked_refresh:
            return None
        try:
            claims = _jwt_decode(refresh_token)
        except Exception:
            return None
        if claims.get("typ") != "refresh":
            return None
        if str(claims.get("cid", "")) != str(client.client_id):
            return None
        scopes = _parse_scopes_from_claims(claims)
        exp = claims.get("exp")
        expires_at = int(exp) if isinstance(exp, int) else None
        return RefreshToken(
            token=refresh_token,
            client_id=str(client.client_id),
            scopes=scopes,
            expires_at=expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._revoked_refresh.add(refresh_token.token)

        claims = _jwt_decode(refresh_token.token)
        user_id = UUID(str(claims["sub"]))
        org_raw = claims.get("org")
        org_id = UUID(str(org_raw)) if org_raw else None

        access = create_access_token(
            user_id=user_id,
            organization_id=org_id,
            extra_claims={"scope": " ".join(scopes)},
        )
        new_refresh = _create_refresh_token(
            user_id=user_id,
            organization_id=org_id,
            client_id=str(client.client_id),
            scopes=scopes,
        )
        return OAuthToken(
            access_token=access,
            refresh_token=new_refresh,
            expires_in=int(
                timedelta(hours=config_module.settings.jwt_expiry_hours).total_seconds()
            ),
            scope=" ".join(scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith("sk_"):
            async with get_session() as session:
                auth = await ApiKeyManager.from_session(session).authenticate(token)
            if auth is None:
                return None
            return AccessToken(
                token=token, client_id=f"api_key:{auth.api_key_id}", scopes=[OAUTH_SCOPE]
            )

        try:
            claims = verify_access_token(token)
        except JwtError:
            return None

        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            return None

        exp = claims.get("exp")
        expires_at = exp if isinstance(exp, int) else None
        scopes = _parse_scopes_from_claims(claims)
        return AccessToken(
            token=token, client_id=f"user:{sub}", scopes=scopes, expires_at=expires_at
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            self._revoked_refresh.add(token.token)

    # ---------------------------------------------------------------------
    # UI helpers (custom routes)
    # ---------------------------------------------------------------------

    def _get_pending(self, request_id: str) -> _PendingAuth | None:
        pending = self._pending.get(request_id)
        if pending is None:
            return None
        if pending.expires_at < time.time():
            self._pending.pop(request_id, None)
            return None
        return pending

    async def ui_login_get(self, request: Request) -> Response:
        request_id = (request.query_params.get("req") or "").strip()
        pending = self._get_pending(request_id)
        if pending is None:
            return HTMLResponse(
                "<h1>OAuth Login</h1><p>Invalid or expired login request.</p>",
                status_code=400,
            )

        client = await self.get_client(pending.client_id)
        client_name = (client.client_name if client else None) or "MCP Client"

        html = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sibyl Login</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: #0b0b10; color: #e8e8f0; margin: 0; }}
    .wrap {{ max-width: 520px; margin: 8vh auto; padding: 24px; background: #12121a; border: 1px solid #2a2a3a; border-radius: 14px; }}
    h1 {{ margin: 0 0 8px; font-size: 22px; }}
    .sub {{ color: #a7a7c7; margin-bottom: 18px; }}
    label {{ display: block; margin: 12px 0 6px; color: #cfcfe9; }}
    input {{ width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid #2a2a3a; background: #0f0f16; color: #fff; }}
    button {{ margin-top: 16px; width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid #3a2a6a; background: #5b2bff; color: #fff; font-weight: 600; cursor: pointer; }}
    .hint {{ margin-top: 12px; color: #a7a7c7; font-size: 13px; }}
    code {{ color: #80ffea; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Login to Sibyl</h1>
    <div class="sub">Authorize <strong>{client_name}</strong> to access your MCP tools.</div>
    <form method="post" action="/_oauth/login">
      <input type="hidden" name="req" value="{request_id}" />
      <label>Email</label>
      <input name="email" type="email" autocomplete="username" required />
      <label>Password</label>
      <input name="password" type="password" autocomplete="current-password" required />
      <button type="submit">Continue</button>
    </form>
    <div class="hint">No local user yet? Create one at <code>/api/auth/local/signup</code>.</div>
  </div>
</body>
</html>
"""
        return HTMLResponse(html, status_code=200)

    async def ui_login_post(self, request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("req", "")).strip()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", "")).strip()

        pending = self._get_pending(request_id)
        if pending is None:
            return HTMLResponse(
                "<h1>OAuth Login</h1><p>Invalid or expired login request.</p>",
                status_code=400,
            )

        async with get_session() as session:
            user = await UserManager(session).authenticate_local(email=email, password=password)
            if user is None:
                return RedirectResponse(
                    url=_add_query_params("/_oauth/login", {"req": request_id}), status_code=302
                )

            org = await OrganizationManager(session).create_personal_for_user(user)
            await OrganizationMembershipManager(session).add_member(
                organization_id=org.id,
                user_id=user.id,
            )

        code = secrets.token_urlsafe(32)
        auth_code = SibylAuthorizationCode(
            code=code,
            client_id=pending.client_id,
            expires_at=time.time() + 10 * 60,
            scopes=pending.params.scopes or [OAUTH_SCOPE],
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            user_id=str(user.id),
            organization_id=str(org.id),
        )
        self._codes[code] = auth_code
        self._pending.pop(request_id, None)

        params: dict[str, str] = {"code": code}
        if pending.params.state:
            params["state"] = pending.params.state
        return RedirectResponse(
            url=_add_query_params(str(pending.params.redirect_uri), params), status_code=302
        )
