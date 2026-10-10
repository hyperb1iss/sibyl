"""OAuth Authorization Server provider for MCPServer.

This enables Codex/MCP clients to authenticate via standard OAuth endpoints
served by MCPServer when `auth_server_provider` is configured:
- `/.well-known/oauth-authorization-server`
- `/authorize`
- `/token`
- `/register` (dynamic client registration)

Implementation notes:
- Dynamic clients are cached in-memory and persisted in auth storage.
- Authorization flow state (pending login, chosen organization, issued code)
  lives in auth storage, so every step of one authorization can be served by
  a different API replica. Codes are short-lived and claimed atomically, so
  each one is exchanged at most once however many replicas race for it.
- Access tokens are Sibyl JWT access tokens (Bearer).
- Refresh tokens are JWT refresh tokens tracked through the active auth runtime.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Protocol, cast
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

import jwt
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl, SkipValidation
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from sibyl import config as config_module
from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.auth.jwt import JwtError, create_access_token, verify_access_token
from sibyl.auth.mcp_auth import effective_api_key_scopes
from sibyl.persistence.auth_runtime import (
    OAuthAuthorizationRecord,
    OAuthAuthorizationStore,
    authenticate_api_key,
    authenticate_local_user,
    create_session_record,
    ensure_personal_organization,
    get_user_by_id,
    list_user_organizations,
    load_oauth_client_registration,
    load_refresh_session_record,
    revoke_refresh_session_record,
    rotate_refresh_session_record,
    save_oauth_client_registration,
    validate_access_session,
)

type JwtClaims = dict[str, object]

OAUTH_SCOPE = "mcp"
# How long a login request, a fresh login awaiting an organization choice, and
# an issued authorization code each stay usable.
AUTHORIZATION_REQUEST_TTL = timedelta(minutes=10)
AUTHENTICATED_LOGIN_TTL = timedelta(minutes=5)
AUTHORIZATION_CODE_TTL = timedelta(minutes=10)
_INVALID_LOGIN_REQUEST_HTML = "<h1>OAuth Login</h1><p>Invalid or expired login request.</p>"


class SibylAccessToken(AccessToken):
    """SDK access token that carries the credential the provider resolved.

    The SDK verifies the bearer once per HTTP request and hands this object
    to tool handlers through ``get_access_token()``. Keeping the resolved
    credential on it lets the tool context reuse that result instead of
    running the API-key KDF and scope reads, or the JWT verify, a second
    time per tool call. Validation is skipped because the values are the
    provider's own output, not wire input.
    """

    api_key_auth: SkipValidation[ApiKeyAuth | None] = None
    jwt_claims: SkipValidation[dict[str, object] | None] = None


def _require_jwt_secret() -> str:
    secret = config_module.settings.jwt_secret.get_secret_value()
    if not secret:
        raise JwtError("JWT secret is not configured (set SIBYL_JWT_SECRET)")
    return secret


def _jwt_encode(payload: JwtClaims) -> str:
    secret = _require_jwt_secret()
    return jwt.encode(payload, secret, algorithm=config_module.settings.jwt_algorithm)


def _jwt_decode(token: str) -> JwtClaims:
    secret = _require_jwt_secret()
    return cast(
        "JwtClaims",
        jwt.decode(
            token,
            secret,
            algorithms=[config_module.settings.jwt_algorithm],
            options={"require": ["sub", "iat", "exp"]},
        ),
    )


def _parse_scopes_from_claims(claims: JwtClaims) -> list[str]:
    scopes = claims.get("scopes")
    if isinstance(scopes, list):
        parsed_scopes = [item for item in scopes if isinstance(item, str)]
        if len(parsed_scopes) == len(scopes):
            return parsed_scopes
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
    session_id: UUID | None = None,
    expires_in: timedelta = timedelta(days=30),
) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + expires_in
    payload: JwtClaims = {
        "sub": str(user_id),
        "typ": "refresh",
        "cid": client_id,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "scope": " ".join(scopes),
    }
    if organization_id is not None:
        payload["org"] = str(organization_id)
    if session_id is not None:
        payload["sid"] = str(session_id)
    return _jwt_encode(payload), expires_at


def _utcnow() -> datetime:
    # Auth storage compares naive UTC datetimes.
    return datetime.now(UTC).replace(tzinfo=None)


class SibylAuthorizationCode(AuthorizationCode):
    user_id: str
    organization_id: str | None = None

    @classmethod
    def from_record(cls, code: str, record: OAuthAuthorizationRecord) -> SibylAuthorizationCode:
        return cls(
            code=code,
            client_id=record.client_id,
            expires_at=record.expires_at.replace(tzinfo=UTC).timestamp(),
            scopes=record.scopes or [OAUTH_SCOPE],
            code_challenge=record.code_challenge,
            redirect_uri=AnyUrl(record.redirect_uri),
            redirect_uri_provided_explicitly=record.redirect_uri_provided_explicitly,
            resource=record.resource,
            user_id=str(record.user_id),
            organization_id=str(record.organization_id) if record.organization_id else None,
        )


def _code_redirect(record: OAuthAuthorizationRecord, code: str) -> RedirectResponse:
    params: dict[str, str] = {"code": code}
    if record.state:
        params["state"] = record.state
    return RedirectResponse(url=_add_query_params(record.redirect_uri, params), status_code=302)


class _OAuthUser(Protocol):
    id: UUID


class _OAuthOrg(Protocol):
    id: UUID
    name: str
    is_personal: bool


class _RefreshSessionRecord(Protocol):
    id: UUID
    user_id: UUID
    organization_id: UUID | None


class SibylMcpOAuthProvider(
    OAuthAuthorizationServerProvider[SibylAuthorizationCode, RefreshToken, AccessToken]
):
    """OAuth provider for MCPServer auth routes."""

    def __init__(self, *, authorization_store: OAuthAuthorizationStore | None = None) -> None:
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._authorizations = authorization_store or OAuthAuthorizationStore()

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        cached = self._clients.get(client_id)
        if cached is not None:
            return cached
        stored = await self._load_oauth_client_registration(client_id)
        if stored is None:
            return None
        try:
            client = OAuthClientInformationFull.model_validate(stored)
        except Exception:
            return None
        if not client.client_id:
            return None
        self._clients[str(client.client_id)] = client
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if not client_info.client_id:
            return
        client_id = str(client_info.client_id)
        self._clients[client_id] = client_info
        await self._save_oauth_client_registration(
            client_id=client_id,
            client_info=client_info.model_dump(mode="json", exclude_none=True),
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        request_id = secrets.token_urlsafe(24)
        await self._authorizations.create_request(
            request_key=request_id,
            client_id=str(client.client_id),
            state=params.state,
            scopes=params.scopes,
            code_challenge=params.code_challenge,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            expires_at=_utcnow() + AUTHORIZATION_REQUEST_TTL,
        )
        issuer = str(config_module.settings.server_url).rstrip("/")
        return _add_query_params(f"{issuer}/_oauth/login", {"req": request_id})

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> SibylAuthorizationCode | None:
        if not authorization_code:
            return None
        record = await self._authorizations.load_code(authorization_code)
        if record is None or record.client_id != str(client.client_id):
            return None
        return SibylAuthorizationCode.from_record(authorization_code, record)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: SibylAuthorizationCode
    ) -> OAuthToken:
        if not await self._claim_authorization_code(
            client_id=str(client.client_id), code=authorization_code.code
        ):
            raise TokenError(
                error="invalid_grant",
                error_description="authorization code was already used or has expired",
            )

        user_id = UUID(authorization_code.user_id)
        org_id = (
            UUID(authorization_code.organization_id) if authorization_code.organization_id else None
        )
        scopes = authorization_code.scopes or [OAUTH_SCOPE]

        session_id = uuid4()
        access = create_access_token(
            user_id=user_id,
            organization_id=org_id,
            session_id=session_id,
            extra_claims={"scope": " ".join(scopes)},
        )
        refresh, refresh_expires_at = _create_refresh_token(
            user_id=user_id,
            organization_id=org_id,
            client_id=str(client.client_id),
            scopes=scopes,
            session_id=session_id,
        )

        access_expires_at = datetime.now(UTC) + timedelta(
            minutes=config_module.settings.access_token_expire_minutes
        )
        await self._create_session_record(
            user_id=user_id,
            token=access,
            expires_at=access_expires_at,
            session_id=session_id,
            organization_id=org_id,
            refresh_token=refresh,
            refresh_token_expires_at=refresh_expires_at,
            device_name="mcp_oauth",
            device_type="mcp",
        )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(
                timedelta(
                    minutes=config_module.settings.access_token_expire_minutes
                ).total_seconds()
            ),
            scope=" ".join(scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        try:
            claims = _jwt_decode(refresh_token)
        except Exception:
            return None
        if claims.get("typ") != "refresh":
            return None
        if str(claims.get("cid", "")) != str(client.client_id):
            return None

        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            return None
        try:
            user_id = UUID(sub)
        except ValueError:
            return None

        org_raw = claims.get("org")
        org_id = None
        if org_raw:
            try:
                org_id = UUID(str(org_raw))
            except ValueError:
                return None

        existing = await self._load_refresh_session_record(refresh_token)
        if existing is None:
            return None
        if existing.user_id != user_id:
            return None
        if org_id != existing.organization_id:
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
        claims = _jwt_decode(refresh_token.token)
        try:
            user_id = UUID(str(claims["sub"]))
            org_raw = claims.get("org")
            org_id = UUID(str(org_raw)) if org_raw else None
        except (KeyError, ValueError) as exc:
            raise TokenError(
                error="invalid_grant", error_description="invalid refresh token claims"
            ) from exc

        allowed_scopes = set(refresh_token.scopes or [])
        requested_scopes = set(scopes or [])
        if requested_scopes and not requested_scopes.issubset(allowed_scopes):
            scopes = list(allowed_scopes)

        existing = await self._load_refresh_session_record(refresh_token.token)
        if existing is None:
            raise TokenError(
                error="invalid_grant", error_description="refresh token does not exist"
            )

        access = create_access_token(
            user_id=user_id,
            organization_id=org_id,
            session_id=existing.id,
            extra_claims={"scope": " ".join(scopes)},
        )
        new_refresh, new_refresh_expires_at = _create_refresh_token(
            user_id=user_id,
            organization_id=org_id,
            client_id=str(client.client_id),
            scopes=scopes,
            session_id=existing.id,
        )

        access_expires_at = datetime.now(UTC) + timedelta(
            minutes=config_module.settings.access_token_expire_minutes
        )
        rotated = await self._rotate_refresh_session_record(
            refresh_token.token,
            new_access_token=access,
            new_access_expires_at=access_expires_at,
            new_refresh_token=new_refresh,
            new_refresh_expires_at=new_refresh_expires_at,
        )
        if rotated is None:
            raise TokenError(
                error="invalid_grant", error_description="refresh token does not exist"
            )
        return OAuthToken(
            access_token=access,
            refresh_token=new_refresh,
            expires_in=int(
                timedelta(
                    minutes=config_module.settings.access_token_expire_minutes
                ).total_seconds()
            ),
            scope=" ".join(scopes),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        if token.startswith("sk_"):
            auth = await self._authenticate_api_key(token)
            if auth is None:
                return None
            scopes = sorted(effective_api_key_scopes(auth.scopes))
            if OAUTH_SCOPE not in scopes:
                return None
            return SibylAccessToken(
                token=token,
                client_id=f"api_key:{auth.api_key_id}",
                scopes=scopes,
                api_key_auth=auth,
            )

        try:
            claims = verify_access_token(token)
        except JwtError:
            return None
        try:
            if not await validate_access_session(token):
                return None
        except TimeoutError:
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
        scopes = _parse_scopes_from_claims(claims)
        return SibylAccessToken(
            token=token,
            client_id=f"user:{user_id}",
            scopes=scopes,
            expires_at=expires_at,
            jwt_claims=claims,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, RefreshToken):
            await self._revoke_refresh_session_record(token.token)

    # ---------------------------------------------------------------------
    # UI helpers (custom routes)
    # ---------------------------------------------------------------------

    async def _get_pending(self, request_id: str) -> OAuthAuthorizationRecord | None:
        if not request_id:
            return None
        return await self._authorizations.load_request(request_id)

    async def _claim_authorization_code(self, *, client_id: str, code: str) -> bool:
        """Consume a code for this client; only one exchange of a code can win."""
        claimed = await self._authorizations.consume_code(code)
        return claimed is not None and claimed.client_id == client_id

    async def _issue_code(
        self,
        request_id: str,
        *,
        user_id: UUID,
        organization_id: UUID,
        require_authenticated: bool,
    ) -> Response:
        code = secrets.token_urlsafe(32)
        issued = await self._authorizations.issue_code(
            request_id,
            code=code,
            user_id=user_id,
            organization_id=organization_id,
            code_expires_at=_utcnow() + AUTHORIZATION_CODE_TTL,
            require_authenticated=require_authenticated,
        )
        if issued is None:
            # The flow expired or another request already completed it.
            return HTMLResponse(_INVALID_LOGIN_REQUEST_HTML, status_code=400)
        return _code_redirect(issued, code)

    async def _create_session_record(self, **kwargs: object) -> object:
        return await create_session_record(**kwargs)

    async def _load_oauth_client_registration(self, client_id: str) -> dict[str, object] | None:
        return await load_oauth_client_registration(client_id)

    async def _save_oauth_client_registration(
        self,
        *,
        client_id: str,
        client_info: dict[str, object],
    ) -> None:
        await save_oauth_client_registration(client_id=client_id, client_info=client_info)

    async def _load_refresh_session_record(
        self, refresh_token: str
    ) -> _RefreshSessionRecord | None:
        return cast(
            "_RefreshSessionRecord | None", await load_refresh_session_record(refresh_token)
        )

    async def _rotate_refresh_session_record(
        self, refresh_token: str, **kwargs: object
    ) -> _RefreshSessionRecord | None:
        return cast(
            "_RefreshSessionRecord | None",
            await rotate_refresh_session_record(refresh_token, **kwargs),
        )

    async def _revoke_refresh_session_record(self, refresh_token: str) -> None:
        await revoke_refresh_session_record(refresh_token)

    async def _authenticate_api_key(self, raw_key: str) -> ApiKeyAuth | None:
        return cast("ApiKeyAuth | None", await authenticate_api_key(raw_key))

    async def _authenticate_local_user(self, *, email: str, password: str) -> _OAuthUser | None:
        return cast(
            "_OAuthUser | None",
            await authenticate_local_user(email=email, password=password),
        )

    async def _list_user_orgs(self, *, user_id: UUID) -> list[_OAuthOrg]:
        return cast("list[_OAuthOrg]", await list_user_organizations(user_id=user_id))

    async def _ensure_personal_org(self, *, user_id: UUID) -> _OAuthOrg | None:
        return cast("_OAuthOrg | None", await ensure_personal_organization(user_id=user_id))

    async def _get_user(self, user_id: UUID) -> _OAuthUser | None:
        return cast("_OAuthUser | None", await get_user_by_id(user_id))

    async def ui_login_get(self, request: Request) -> Response:
        request_id = (request.query_params.get("req") or "").strip()
        pending = await self._get_pending(request_id)
        if pending is None:
            return HTMLResponse(_INVALID_LOGIN_REQUEST_HTML, status_code=400)

        client = await self.get_client(pending.client_id)
        client_name = escape((client.client_name if client else None) or "MCP Client", quote=True)

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
      <input type="hidden" name="req" value="{escape(request_id, quote=True)}" />
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

        pending = await self._get_pending(request_id)
        if pending is None:
            return HTMLResponse(_INVALID_LOGIN_REQUEST_HTML, status_code=400)

        user = await self._authenticate_local_user(email=email, password=password)
        if user is None:
            return RedirectResponse(
                url=_add_query_params("/_oauth/login", {"req": request_id}), status_code=302
            )

        orgs = await self._list_user_orgs(user_id=user.id)

        if not orgs:
            org = await self._ensure_personal_org(user_id=user.id)
            if org is not None:
                orgs = [org]

        if len(orgs) == 1:
            return await self._issue_code(
                request_id,
                user_id=user.id,
                organization_id=orgs[0].id,
                require_authenticated=False,
            )

        if not await self._authorizations.mark_authenticated(
            request_id,
            user_id=user.id,
            authenticated_expires_at=_utcnow() + AUTHENTICATED_LOGIN_TTL,
        ):
            return HTMLResponse(_INVALID_LOGIN_REQUEST_HTML, status_code=400)

        return RedirectResponse(
            url=_add_query_params("/_oauth/org", {"req": request_id}), status_code=302
        )

    async def ui_org_get(self, request: Request) -> Response:
        request_id = (request.query_params.get("req") or "").strip()
        pending = await self._get_pending(request_id)
        if pending is None:
            return HTMLResponse(_INVALID_LOGIN_REQUEST_HTML, status_code=400)

        authed_user_id = pending.authenticated_user(now=_utcnow())
        if authed_user_id is None:
            return RedirectResponse(
                url=_add_query_params("/_oauth/login", {"req": request_id}), status_code=302
            )

        orgs = await self._list_user_orgs(user_id=authed_user_id)

        if not orgs:
            return RedirectResponse(
                url=_add_query_params("/_oauth/login", {"req": request_id}), status_code=302
            )

        options = "\n".join(
            (
                '<label style="display:block;margin:10px 0;padding:10px;border:1px solid #2a2a3a;border-radius:10px;">'
                f'<input type="radio" name="org_id" value="{escape(str(org.id), quote=True)}" required style="margin-right:10px;" />'
                f"<strong>{escape(org.name)}</strong>"
                + (' <span style="color:#a7a7c7">(personal)</span>' if org.is_personal else "")
                + "</label>"
            )
            for org in orgs
        )

        html = f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Select Organization</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; background: #0b0b10; color: #e8e8f0; margin: 0; }}
    .wrap {{ max-width: 640px; margin: 8vh auto; padding: 24px; background: #12121a; border: 1px solid #2a2a3a; border-radius: 14px; }}
    h1 {{ margin: 0 0 8px; font-size: 22px; }}
    .sub {{ color: #a7a7c7; margin-bottom: 18px; }}
    button {{ margin-top: 16px; width: 100%; padding: 10px 12px; border-radius: 10px; border: 1px solid #3a2a6a; background: #5b2bff; color: #fff; font-weight: 600; cursor: pointer; }}
    .secondary {{ margin-top: 10px; background: transparent; border-color: #2a2a3a; color: #e8e8f0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Select an organization</h1>
    <div class="sub">Choose which org to use for this MCP session.</div>
    <form method="post" action="/_oauth/org">
      <input type="hidden" name="req" value="{escape(request_id, quote=True)}" />
      {options}
      <button type="submit">Continue</button>
    </form>
    <form method="post" action="/_oauth/org">
      <input type="hidden" name="req" value="{escape(request_id, quote=True)}" />
      <input type="hidden" name="create_personal" value="1" />
      <button class="secondary" type="submit">Create / use personal org</button>
    </form>
  </div>
</body>
</html>
"""
        return HTMLResponse(html, status_code=200)

    async def ui_org_post(self, request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("req", "")).strip()
        selected_org_id = str(form.get("org_id", "")).strip()
        create_personal = str(form.get("create_personal", "")).strip() == "1"

        pending = await self._get_pending(request_id)
        if pending is None:
            return HTMLResponse(_INVALID_LOGIN_REQUEST_HTML, status_code=400)

        authed_user_id = pending.authenticated_user(now=_utcnow())
        if authed_user_id is None:
            return RedirectResponse(
                url=_add_query_params("/_oauth/login", {"req": request_id}), status_code=302
            )

        if create_personal:
            org = await self._ensure_personal_org(user_id=authed_user_id)
            if org is None:
                return RedirectResponse(
                    url=_add_query_params("/_oauth/login", {"req": request_id}), status_code=302
                )
        else:
            orgs = await self._list_user_orgs(user_id=authed_user_id)
            org = next((o for o in orgs if str(o.id) == selected_org_id), None)
            if org is None:
                return HTMLResponse(
                    "<h1>OAuth Login</h1><p>Invalid organization selection.</p>",
                    status_code=400,
                )

        return await self._issue_code(
            request_id,
            user_id=authed_user_id,
            organization_id=org.id,
            require_authenticated=True,
        )
