"""HTTP client for CLI to communicate with Sibyl REST API.

The CLI is a thin client - all operations go through the REST API,
ensuring consistent event broadcasting and state management.
"""

import os
import random
import sys
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import httpx

from sibyl_cli.auth_store import (
    auth_file_lock,
    credential_scope,
    get_access_token,
    get_refresh_token,
    is_access_token_expired,
    normalize_api_url,
    read_server_credentials,
    set_tokens,
)
from sibyl_cli.pending_writes import create_pending_write, delete_pending_write

# Default server port (matches sibyl-server default)
DEFAULT_SERVER_PORT = 3334
FAILURE_WINDOW_SECONDS = 10.0
FAILURE_THRESHOLD = 3
_FAILURE_WINDOWS: dict[tuple[str, str], deque[float]] = {}
BUFFERED_WRITE_METHODS = {"POST", "PATCH", "DELETE"}
PENDING_WRITE_REMEDIATION = "Run 'sibyl auth login' then 'sibyl pending-writes flush'."
INIT_REMEDIATION = "Run 'sibyl init' for local mode or 'sibyl init --remote <url>'."


@dataclass
class ErrorPayload:
    message: str
    error: str | None = None
    request_id: str | None = None
    remediation: str | None = None
    details: dict[str, object] | None = None


def _get_default_api_url(context_name: str | None = None) -> str:
    """Get API URL from context, config file, env var, or default.

    Priority:
    1. Explicit context (if provided)
    2. Active context's server_url
    3. Environment variable (SIBYL_API_URL)
    4. Legacy config file (server.url)
    5. Default (http://localhost:3334/api)

    Args:
        context_name: Optional context name to use instead of active context.
    """
    # Lazy import to avoid circular dependency
    from sibyl_cli import config_store

    # 1. If explicit context provided, use that
    if context_name:
        ctx = config_store.get_context(context_name)
        if ctx:
            return f"{ctx.server_url}/api"
        # Context not found - fall through to other options

    # 2. Try active context
    ctx = config_store.get_active_context()
    if ctx:
        return f"{ctx.server_url}/api"

    # 3. Try env var
    env_url = os.environ.get("SIBYL_API_URL", "").strip()
    if env_url:
        return env_url

    # 4. Try legacy config file
    if config_store.config_exists():
        url = config_store.get_server_url()
        if url:
            return f"{url}/api"

    # 5. Default
    return f"http://localhost:{DEFAULT_SERVER_PORT}/api"


def _auth_credential_scope(context_name: str | None = None) -> str | None:
    from sibyl_cli import config_store

    ctx = (
        config_store.get_context(context_name)
        if context_name
        else config_store.get_active_context()
    )
    if ctx is None:
        return None
    return credential_scope(ctx.name, ctx.org_slug)


def _load_default_auth_token(
    api_base_url: str,
    credential_scope_name: str | None = None,
) -> str | None:
    """Load auth token for the given API URL.

    Priority:
    1. SIBYL_AUTH_TOKEN environment variable
    2. Stored access token for the specific server
    """
    env_token = os.environ.get("SIBYL_AUTH_TOKEN", "").strip()
    if env_token:
        return env_token

    return get_access_token(api_base_url, credential_scope=credential_scope_name)


class SibylClientError(Exception):
    """Error from Sibyl API."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        detail: str | None = None,
        *,
        error_code: str | None = None,
        request_id: str | None = None,
        remediation: str | None = None,
        details: dict[str, object] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail
        self.error_code = error_code
        self.request_id = request_id
        self.remediation = remediation
        self.details = details or {}


def _parse_error_payload(data: object) -> ErrorPayload:
    if isinstance(data, dict):
        payload = cast("dict[str, object]", data)
        detail = payload.get("detail")
        if isinstance(detail, dict):
            payload = cast("dict[str, object]", detail)

        if payload.get("error") or payload.get("message"):
            details = payload.get("details")
            return ErrorPayload(
                message=str(payload.get("message") or payload.get("error") or "Request failed"),
                error=str(payload["error"]) if payload.get("error") else None,
                request_id=str(payload["request_id"]) if payload.get("request_id") else None,
                remediation=str(payload["remediation"]) if payload.get("remediation") else None,
                details=cast("dict[str, object]", details) if isinstance(details, dict) else None,
            )

        if detail is not None:
            return ErrorPayload(message=_format_error_detail(detail))

    return ErrorPayload(message=_format_error_detail(data))


def _format_error_detail(detail: object) -> str:
    if isinstance(detail, dict):
        payload = cast("dict[str, object]", detail)
        message = payload.get("message")
        error_code = payload.get("error")
        details = payload.get("details")
        parts: list[str] = []
        if message:
            parts.append(str(message))
        elif error_code:
            parts.append(str(error_code))
        if isinstance(details, dict):
            detail_fields = cast("dict[str, object]", details)
            project_id = detail_fields.get("project_id")
            required_role = detail_fields.get("required_role")
            if project_id:
                parts.append(f"project={project_id}")
            if required_role:
                parts.append(f"required_role={required_role}")
        if parts:
            return " ".join(parts)
    return str(detail)


def _subcommand_key() -> str:
    parts = [arg for arg in sys.argv[1:3] if arg and not arg.startswith("-")]
    return " ".join(parts) or "sibyl"


def _failure_key(base_url: str) -> tuple[str, str]:
    return (_subcommand_key(), base_url)


def _prune_failures(window: deque[float], now: float) -> None:
    while window and now - window[0] > FAILURE_WINDOW_SECONDS:
        window.popleft()


async def _maybe_wait_for_circuit_breaker(key: tuple[str, str]) -> None:
    window = _FAILURE_WINDOWS.get(key)
    if not window:
        return
    now = time.monotonic()
    _prune_failures(window, now)
    if len(window) >= FAILURE_THRESHOLD:
        await anyio_sleep(1.0 + random.random())


def _record_failure(key: tuple[str, str]) -> None:
    now = time.monotonic()
    window = _FAILURE_WINDOWS.setdefault(key, deque())
    _prune_failures(window, now)
    window.append(now)


def _record_success(key: tuple[str, str]) -> None:
    _FAILURE_WINDOWS.pop(key, None)


def _is_refresh_revoked(message: str | None) -> bool:
    if not message:
        return False
    normalized = message.lower()
    return "session not found" in normalized or "revoked" in normalized


def _should_buffer_request(method: str, path: str) -> bool:
    if method.upper() not in BUFFERED_WRITE_METHODS:
        return False
    return not path.startswith("/auth/")


def _requires_initialized_context(method: str, path: str) -> bool:
    if method.upper() not in BUFFERED_WRITE_METHODS:
        return False
    return not path.startswith("/auth/")


def _should_keep_pending_write(status_code: int) -> bool:
    return status_code in {401, 408, 429} or status_code >= 500


async def anyio_sleep(delay: float) -> None:
    import asyncio

    await asyncio.sleep(delay)


class SibylClient:
    """HTTP client for Sibyl REST API.

    Provides typed methods for all API operations.
    Handles connection errors, retries, and error responses.
    """

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 30.0,
        auth_token: str | None = None,
        context_name: str | None = None,
    ):
        """Initialize the client.

        Args:
            base_url: API base URL. Defaults to context, then env var, then localhost.
            timeout: Request timeout in seconds.
            auth_token: Optional bearer token or API key to send as Authorization header.
            context_name: Optional context name to use for URL and auth resolution.
        """
        self.context_name = context_name
        self._explicit_base_url = base_url is not None
        self.base_url = normalize_api_url(base_url or _get_default_api_url(context_name))
        self.credential_scope = (
            _auth_credential_scope(context_name)
            if context_name or not self._explicit_base_url
            else None
        )
        self.timeout = timeout
        self._uses_stored_auth = (
            auth_token is None and not os.environ.get("SIBYL_AUTH_TOKEN", "").strip()
        )
        self.auth_token = (
            auth_token
            if auth_token is not None
            else _load_default_auth_token(self.base_url, self.credential_scope)
        )
        self._client: httpx.AsyncClient | None = None
        # Load insecure setting from context
        self.insecure = self._get_insecure_from_context(context_name)

    def _get_insecure_from_context(self, context_name: str | None) -> bool:
        """Get insecure setting from context config."""
        from sibyl_cli import config_store

        if context_name:
            ctx = config_store.get_context(context_name)
            if ctx:
                return ctx.insecure
        # Check active context
        ctx = config_store.get_active_context()
        if ctx:
            return ctx.insecure
        return False

    def _default_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    async def _silent_local_relogin(self, creds: dict[str, Any]) -> tuple[bool, str | None]:
        email = str(creds.get("local_login_email") or "").strip()
        password = str(creds.get("local_login_password") or "").strip()
        if not email or not password:
            return False, "No stored local login credentials are available."

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                verify=not self.insecure,
            ) as client:
                response = await client.post(
                    "/auth/local/login",
                    json={"email": email, "password": password},
                )
                if response.status_code != 200:
                    return False, f"Local login returned HTTP {response.status_code}."
                data = response.json()
        except Exception as exc:
            return False, f"Local login failed: {exc}"

        new_access_token = str(data.get("access_token") or "").strip()
        if not new_access_token:
            return False, "Local login response did not include an access token."

        set_tokens(
            self.base_url,
            new_access_token,
            refresh_token=str(data.get("refresh_token") or "").strip() or None,
            expires_in=int(data["expires_in"]) if data.get("expires_in") else None,
            lock=False,
            credential_scope=self.credential_scope,
        )
        self.auth_token = new_access_token
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None
        return True, None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create async HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=self._default_headers(),
                verify=not self.insecure,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "SibylClient":
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.close()

    async def _refresh_token(self) -> tuple[bool, str | None]:
        """Attempt to refresh the access token using stored refresh token.

        Returns:
            Tuple of (success, failure_reason)
        """
        if not self._uses_stored_auth:
            return False, "Automatic renewal is only available for stored CLI login tokens."

        try:
            with auth_file_lock():
                creds = read_server_credentials(
                    self.base_url,
                    credential_scope=self.credential_scope,
                )
                stored_access_token = str(creds.get("access_token") or "").strip()
                expires_at = creds.get("access_token_expires_at")
                if (
                    stored_access_token
                    and stored_access_token != self.auth_token
                    and not is_access_token_expired(
                        self.base_url,
                        credential_scope=self.credential_scope,
                    )
                ):
                    self.auth_token = stored_access_token
                    if self._client and not self._client.is_closed:
                        await self._client.aclose()
                    self._client = None
                    return True, None

                refresh_token = get_refresh_token(
                    self.base_url,
                    credential_scope=self.credential_scope,
                )
                if not refresh_token:
                    return False, "No refresh token is available for automatic renewal."

                if (
                    stored_access_token
                    and expires_at is None
                    and stored_access_token != self.auth_token
                ):
                    self.auth_token = stored_access_token
                    if self._client and not self._client.is_closed:
                        await self._client.aclose()
                    self._client = None
                    return True, None

                async with httpx.AsyncClient(
                    base_url=self.base_url,
                    timeout=self.timeout,
                    verify=not self.insecure,
                ) as client:
                    response = await client.post(
                        "/auth/refresh",
                        json={"refresh_token": refresh_token},
                    )

                    if response.status_code != 200:
                        try:
                            detail = response.json().get("detail")
                        except Exception:
                            detail = response.text
                        detail_text = str(detail).strip() if detail is not None else ""
                        if not detail_text:
                            detail_text = f"Refresh request returned HTTP {response.status_code}."
                        if _is_refresh_revoked(detail_text):
                            relogged, relogin_failure = await self._silent_local_relogin(creds)
                            if relogged:
                                return True, None
                            if relogin_failure:
                                detail_text = (
                                    f"{detail_text} Silent re-login failed: {relogin_failure}"
                                )
                        return False, detail_text

                    data = response.json()
                    new_access_token = data.get("access_token")
                    new_refresh_token = data.get("refresh_token")
                    expires_in = data.get("expires_in")

                    if not new_access_token:
                        return False, "Refresh response did not include a new access token."

                    set_tokens(
                        self.base_url,
                        new_access_token,
                        refresh_token=new_refresh_token,
                        expires_in=expires_in,
                        lock=False,
                        credential_scope=self.credential_scope,
                    )

                    self.auth_token = new_access_token

                    if self._client and not self._client.is_closed:
                        await self._client.aclose()
                    self._client = None

                    return True, None

        except Exception as exc:
            return False, f"Refresh request failed: {exc}"

    async def _request(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        *,
        _retry_on_401: bool = True,
        _buffer_pending: bool = True,
        _pending_write_id: str | None = None,
        _idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Make an HTTP request to the API.

        Args:
            method: HTTP method (GET, POST, PATCH, DELETE)
            path: API path (e.g., /entities, /tasks/123/start)
            json: JSON body for POST/PATCH requests
            params: Query parameters
            _retry_on_401: Internal flag to prevent infinite retry loops

        Returns:
            Response JSON as dict

        Raises:
            SibylClientError: On API errors or connection issues
        """
        refresh_failure: str | None = None

        # Proactively refresh if token is about to expire
        if self.auth_token and is_access_token_expired(
            self.base_url,
            credential_scope=self.credential_scope,
        ):
            _, refresh_failure = await self._refresh_token()

        method = method.upper()
        if (
            not self._explicit_base_url
            and _requires_initialized_context(method, path)
            and not os.environ.get("SIBYL_API_URL", "").strip()
        ):
            from sibyl_cli import config_store

            if not config_store.config_exists():
                raise SibylClientError(
                    "No Sibyl context is configured; refusing to write to implicit localhost.",
                    remediation=INIT_REMEDIATION,
                )

        pending_write_id = _pending_write_id
        idempotency_key = _idempotency_key
        if _buffer_pending and pending_write_id is None and _should_buffer_request(method, path):
            pending = create_pending_write(
                method=method,
                path=path,
                base_url=self.base_url,
                json_payload=json,
                params=params,
            )
            pending_write_id = str(pending["id"])
            idempotency_key = str(pending["idempotency_key"])

        client = await self._get_client()
        breaker_key = _failure_key(self.base_url)
        await _maybe_wait_for_circuit_breaker(breaker_key)

        try:
            headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
            response = await client.request(
                method=method,
                url=path,
                json=json,
                params=params,
                headers=headers,
            )

            # Handle 401 - try to refresh token and retry once
            if response.status_code == 401 and _retry_on_401:
                refreshed, refresh_failure = await self._refresh_token()
                if refreshed:
                    return await self._request(
                        method,
                        path,
                        json=json,
                        params=params,
                        _retry_on_401=False,
                        _buffer_pending=False,
                        _pending_write_id=pending_write_id,
                        _idempotency_key=idempotency_key,
                    )

            # Handle error responses
            if response.status_code >= 400:
                if pending_write_id and not _should_keep_pending_write(response.status_code):
                    delete_pending_write(pending_write_id)
                try:
                    payload = _parse_error_payload(response.json())
                except Exception:
                    payload = ErrorPayload(message=response.text)

                detail = payload.message

                if response.status_code == 401:
                    if refresh_failure:
                        detail = f"{detail}\n\nAutomatic token refresh failed: {refresh_failure}"
                    if not payload.remediation:
                        payload.remediation = (
                            PENDING_WRITE_REMEDIATION
                            if pending_write_id
                            else ("Auth required. Run 'sibyl auth login' or set SIBYL_AUTH_TOKEN.")
                        )
                elif response.status_code == 403:
                    if not payload.remediation:
                        payload.remediation = "Access denied. Check org and project permissions."

                _record_failure(breaker_key)
                raise SibylClientError(
                    f"API error: {payload.error or detail}: {detail}",
                    status_code=response.status_code,
                    detail=detail,
                    error_code=payload.error,
                    request_id=payload.request_id,
                    remediation=payload.remediation,
                    details=payload.details,
                )

            # Return empty dict for 204 No Content
            if response.status_code == 204:
                if pending_write_id:
                    delete_pending_write(pending_write_id)
                _record_success(breaker_key)
                return {}

            data = response.json()
            if pending_write_id:
                delete_pending_write(pending_write_id)
            _record_success(breaker_key)
            return data

        except httpx.ConnectError as e:
            _record_failure(breaker_key)
            raise SibylClientError(
                f"Cannot connect to Sibyl API at {self.base_url}. Is the server running?",
                detail=str(e),
            ) from e
        except httpx.TimeoutException as e:
            _record_failure(breaker_key)
            raise SibylClientError(
                f"Request timed out after {self.timeout}s",
                detail=str(e),
            ) from e

    # =========================================================================
    # Generic HTTP Methods
    # =========================================================================

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generic GET request."""
        return await self._request("GET", path, params=params)

    async def post(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generic POST request."""
        return await self._request("POST", path, json=json, params=params)

    async def patch(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generic PATCH request."""
        return await self._request("PATCH", path, json=json, params=params)

    async def delete(
        self,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generic DELETE request."""
        return await self._request("DELETE", path, json=json, params=params)

    async def _request_any(
        self,
        method: str,
        paths: list[str],
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Try multiple paths, falling back when an endpoint is not found."""
        last_error: SibylClientError | None = None
        for path in paths:
            try:
                return await self._request(method, path, json=json, params=params)
            except SibylClientError as e:
                if e.status_code == 404:
                    last_error = e
                    continue
                raise

        if last_error:
            raise last_error
        raise SibylClientError("No API path candidates provided")

    # =========================================================================
    # Entity Operations
    # =========================================================================

    async def list_entities(
        self,
        entity_type: str | None = None,
        language: str | None = None,
        category: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """List entities with optional filters."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if entity_type:
            params["entity_type"] = entity_type
        if language:
            params["language"] = language
        if category:
            params["category"] = category

        return await self._request("GET", "/entities", params=params)

    # =========================================================================
    # Auth Operations
    # =========================================================================

    async def list_api_keys(self) -> dict[str, Any]:
        return await self._request("GET", "/auth/api-keys")

    async def create_api_key(
        self,
        *,
        name: str,
        live: bool = True,
        scopes: list[str] | None = None,
        project_ids: list[str] | None = None,
        memory_space_ids: list[str] | None = None,
        expires_days: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "live": live}
        if scopes is not None:
            payload["scopes"] = scopes
        if project_ids is not None:
            payload["project_ids"] = project_ids
        if memory_space_ids is not None:
            payload["memory_space_ids"] = memory_space_ids
        if expires_days is not None:
            payload["expires_days"] = expires_days
        return await self._request("POST", "/auth/api-keys", json=payload)

    async def revoke_api_key(self, api_key_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/auth/api-keys/{api_key_id}/revoke")

    async def local_signup(
        self,
        *,
        email: str,
        password: str,
        name: str,
        redirect: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"email": email, "password": password, "name": name}
        if redirect is not None:
            payload["redirect"] = redirect
        return await self._request("POST", "/auth/local/signup", json=payload)

    async def local_login(
        self,
        *,
        email: str,
        password: str,
        break_glass_reason: str | None = None,
        redirect: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"email": email, "password": password}
        if break_glass_reason is not None:
            payload["break_glass_reason"] = break_glass_reason
        if redirect is not None:
            payload["redirect"] = redirect
        return await self._request("POST", "/auth/local/login", json=payload)

    async def list_orgs(self) -> dict[str, Any]:
        return await self._request("GET", "/orgs")

    async def create_org(self, name: str, slug: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name}
        if slug:
            payload["slug"] = slug
        return await self._request("POST", "/orgs", json=payload)

    async def switch_org(self, slug: str) -> dict[str, Any]:
        return await self._request("POST", f"/orgs/{slug}/switch")

    # -------------------------------------------------------------------------
    # Org Members
    # -------------------------------------------------------------------------

    async def list_org_members(self, slug: str) -> dict[str, Any]:
        """List all members of an organization."""
        return await self._request("GET", f"/orgs/{slug}/members")

    async def add_org_member(self, slug: str, user_id: str, role: str = "member") -> dict[str, Any]:
        """Add a member to an organization."""
        return await self._request(
            "POST", f"/orgs/{slug}/members", json={"user_id": user_id, "role": role}
        )

    async def update_org_member_role(self, slug: str, user_id: str, role: str) -> dict[str, Any]:
        """Update a member's role in an organization."""
        return await self._request("PATCH", f"/orgs/{slug}/members/{user_id}", json={"role": role})

    async def remove_org_member(self, slug: str, user_id: str) -> dict[str, Any]:
        """Remove a member from an organization."""
        return await self._request("DELETE", f"/orgs/{slug}/members/{user_id}")

    async def get_entity(
        self,
        entity_id: str,
        *,
        include_summary: bool = True,
        related_limit: int = 5,
    ) -> dict[str, Any]:
        """Get a single entity by ID with related context."""
        return await self._request(
            "GET",
            f"/entities/{entity_id}",
            params={
                "include_summary": include_summary,
                "related_limit": related_limit,
            },
        )

    async def list_raw_captures(
        self,
        *,
        entity_type: str | None = None,
        capture_surface: str | None = None,
        review_state: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List archived raw quick captures."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if entity_type:
            params["entity_type"] = entity_type
        if capture_surface:
            params["capture_surface"] = capture_surface
        if review_state:
            params["review_state"] = review_state
        return await self._request("GET", "/entities/captures", params=params)

    async def get_raw_capture(self, capture_id: str) -> dict[str, Any]:
        """Get a single archived raw quick capture."""
        return await self._request("GET", f"/entities/captures/{capture_id}")

    async def create_entity(
        self,
        name: str,
        content: str,
        entity_type: str = "episode",
        description: str | None = None,
        category: str | None = None,
        languages: list[str] | None = None,
        tags: list[str] | None = None,
        related_to: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        sync: bool = False,
        skip_conflicts: bool = False,
    ) -> dict[str, Any]:
        """Create a new entity.

        Args:
            sync: If True, wait for entity creation to complete (slower but
                  entity is immediately available for operations like task start).
        """
        data: dict[str, Any] = {
            "name": name,
            "content": content,
            "entity_type": entity_type,
        }
        if description:
            data["description"] = description
        if category:
            data["category"] = category
        if languages:
            data["languages"] = languages
        if tags:
            data["tags"] = tags
        if related_to:
            data["related_to"] = related_to
        if metadata:
            data["metadata"] = metadata
        if skip_conflicts:
            data["skip_conflicts"] = True

        params = {"sync": "true"} if sync else None
        return await self._request("POST", "/entities", json=data, params=params)

    async def update_entity(
        self,
        entity_id: str,
        **updates: Any,
    ) -> dict[str, Any]:
        """Update an entity."""
        return await self._request("PATCH", f"/entities/{entity_id}", json=updates)

    async def delete_entity(self, entity_id: str) -> dict[str, Any]:
        """Delete an entity."""
        return await self._request("DELETE", f"/entities/{entity_id}")

    async def resolve_id_prefix(
        self,
        prefix: str,
        *,
        entity_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Resolve a short graph ID prefix to matching full IDs."""
        params: dict[str, Any] = {"limit": limit}
        if entity_type:
            params["entity_type"] = entity_type
        return await self._request("GET", f"/resolve/{quote(prefix, safe='')}", params=params)

    # =========================================================================
    # Task Workflow Operations
    # =========================================================================

    async def start_task(self, task_id: str, assignee: str | None = None) -> dict[str, Any]:
        """Start working on a task."""
        data = {"assignee": assignee} if assignee else None
        return await self._request("POST", f"/tasks/{task_id}/start", json=data)

    async def block_task(self, task_id: str, reason: str) -> dict[str, Any]:
        """Block a task with a reason."""
        return await self._request("POST", f"/tasks/{task_id}/block", json={"reason": reason})

    async def unblock_task(self, task_id: str) -> dict[str, Any]:
        """Unblock a task."""
        return await self._request("POST", f"/tasks/{task_id}/unblock")

    async def submit_review(
        self,
        task_id: str,
        pr_url: str | None = None,
        commit_shas: list[str] | None = None,
    ) -> dict[str, Any]:
        """Submit a task for review."""
        data: dict[str, Any] = {}
        if pr_url:
            data["pr_url"] = pr_url
        if commit_shas:
            data["commit_shas"] = commit_shas
        return await self._request("POST", f"/tasks/{task_id}/review", json=data or None)

    async def complete_task(
        self,
        task_id: str,
        actual_hours: float | None = None,
        learnings: str | None = None,
    ) -> dict[str, Any]:
        """Complete a task."""
        data: dict[str, Any] = {}
        if actual_hours is not None:
            data["actual_hours"] = actual_hours
        if learnings:
            data["learnings"] = learnings
        return await self._request("POST", f"/tasks/{task_id}/complete", json=data or None)

    async def archive_task(self, task_id: str, reason: str | None = None) -> dict[str, Any]:
        """Archive a task."""
        data = {"reason": reason} if reason else None
        return await self._request("POST", f"/tasks/{task_id}/archive", json=data)

    async def create_task(
        self,
        title: str,
        project_id: str,
        description: str | None = None,
        priority: str = "medium",
        complexity: str = "medium",
        status: str = "todo",
        assignees: list[str] | None = None,
        epic_id: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        technologies: list[str] | None = None,
        depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a task via the dedicated POST /tasks endpoint.

        Uses the task-specific endpoint which handles BELONGS_TO relationships
        and DEPENDS_ON dependencies automatically.
        """
        data: dict[str, Any] = {
            "title": title,
            "project_id": project_id,
            "priority": priority,
            "complexity": complexity,
            "status": status,
        }
        if description:
            data["description"] = description
        if assignees:
            data["assignees"] = assignees
        if epic_id:
            data["epic_id"] = epic_id
        if feature:
            data["feature"] = feature
        if tags:
            data["tags"] = tags
        if technologies:
            data["technologies"] = technologies
        if depends_on:
            data["depends_on"] = depends_on

        return await self._request("POST", "/tasks", json=data)

    async def update_task(
        self,
        task_id: str,
        status: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        title: str | None = None,
        description: str | None = None,
        assignees: list[str] | None = None,
        epic_id: str | None = None,
        feature: str | None = None,
        tags: list[str] | None = None,
        technologies: list[str] | None = None,
        add_depends_on: list[str] | None = None,
        remove_depends_on: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update task fields."""
        data: dict[str, Any] = {}
        if status:
            data["status"] = status
        if priority:
            data["priority"] = priority
        if complexity:
            data["complexity"] = complexity
        if title:
            data["title"] = title
        if description:
            data["description"] = description
        if assignees:
            data["assignees"] = assignees
        if epic_id:
            data["epic_id"] = epic_id
        if feature:
            data["feature"] = feature
        if tags:
            data["tags"] = tags
        if technologies:
            data["technologies"] = technologies
        if add_depends_on:
            data["add_depends_on"] = add_depends_on
        if remove_depends_on:
            data["remove_depends_on"] = remove_depends_on

        return await self._request("PATCH", f"/tasks/{task_id}", json=data)

    # =========================================================================
    # Task Notes Operations
    # =========================================================================

    async def create_note(
        self,
        task_id: str,
        content: str,
        author_type: str = "user",
        author_name: str = "",
    ) -> dict[str, Any]:
        """Create a note on a task."""
        data = {
            "content": content,
            "author_type": author_type,
            "author_name": author_name,
        }
        return await self._request("POST", f"/tasks/{task_id}/notes", json=data)

    async def list_notes(
        self,
        task_id: str,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List notes for a task."""
        params = {"limit": limit}
        return await self._request("GET", f"/tasks/{task_id}/notes", params=params)

    # =========================================================================
    # Search Operations
    # =========================================================================

    async def search(
        self,
        query: str,
        types: list[str] | None = None,
        language: str | None = None,
        category: str | None = None,
        project: str | None = None,
        limit: int = 10,
        offset: int = 0,
        include_content: bool = True,
        include_documents: bool = True,
        include_graph: bool = True,
    ) -> dict[str, Any]:
        """Semantic search across the knowledge graph."""
        data: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "include_content": include_content,
            "include_documents": include_documents,
            "include_graph": include_graph,
        }
        if types:
            data["types"] = types
        if language:
            data["language"] = language
        if category:
            data["category"] = category
        if project:
            data["project"] = project

        return await self._request("POST", "/search", json=data)

    async def remember_raw_memory(
        self,
        *,
        title: str,
        raw_content: str,
        source_id: str | None = None,
        memory_scope: str = "private",
        scope_key: str | None = None,
        diary: bool = False,
        agent_id: str | None = None,
        project_id: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        capture_surface: str = "cli",
    ) -> dict[str, Any]:
        """Store verbatim raw memory."""
        data: dict[str, Any] = {
            "title": title,
            "raw_content": raw_content,
            "memory_scope": memory_scope,
            "diary": diary,
            "tags": tags or [],
            "metadata": metadata or {},
            "provenance": provenance or {},
            "capture_surface": capture_surface,
        }
        if source_id:
            data["source_id"] = source_id
        if scope_key:
            data["scope_key"] = scope_key
        if agent_id:
            data["agent_id"] = agent_id
        if project_id:
            data["project_id"] = project_id
        return await self._request("POST", "/memory/raw", json=data)

    async def recall_raw_memory(
        self,
        *,
        query: str,
        memory_scope: str = "private",
        scope_key: str | None = None,
        diary: bool = False,
        agent_id: str | None = None,
        project_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Recall verbatim raw memories."""
        data: dict[str, Any] = {
            "query": query,
            "memory_scope": memory_scope,
            "diary": diary,
            "limit": limit,
        }
        if scope_key:
            data["scope_key"] = scope_key
        if agent_id:
            data["agent_id"] = agent_id
        if project_id:
            data["project_id"] = project_id
        return await self._request("POST", "/memory/raw/recall", json=data)

    async def memory_audit(
        self,
        *,
        action: str | None = None,
        actor_user_id: str | None = None,
        source_id: str | None = None,
        derived_id: str | None = None,
        memory_scope: str | None = None,
        project_id: str | None = None,
        policy_allowed: bool | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List memory audit events."""
        params: dict[str, Any] = {"limit": limit}
        if action:
            params["action"] = action
        if actor_user_id:
            params["actor_user_id"] = actor_user_id
        if source_id:
            params["source_id"] = source_id
        if derived_id:
            params["derived_id"] = derived_id
        if memory_scope:
            params["memory_scope"] = memory_scope
        if project_id:
            params["project_id"] = project_id
        if policy_allowed is not None:
            params["policy_allowed"] = policy_allowed
        return await self._request("GET", "/memory/audit", params=params)

    async def memory_inspect(self, source_id: str) -> dict[str, Any]:
        """Inspect a raw memory source."""
        encoded_source_id = quote(source_id, safe="")
        return await self._request("GET", f"/memory/inspect/{encoded_source_id}")

    async def source_import_status(self, import_id: str) -> dict[str, Any]:
        """Inspect a source import receipt from the memory surface."""
        encoded_import_id = quote(import_id, safe="")
        return await self._request("GET", f"/memory/source-imports/{encoded_import_id}")

    async def preview_reflection_promotion(
        self,
        *,
        candidate_id: str,
        promote_to_scope: str | None = None,
        promote_to_scope_key: str | None = None,
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
    ) -> dict[str, Any]:
        """Preview reflection candidate promotion without mutating memory."""
        data: dict[str, Any] = {
            "candidate_id": candidate_id,
            "related_to": related_to or [],
        }
        if promote_to_scope:
            data["promote_to_scope"] = promote_to_scope
        if promote_to_scope_key:
            data["promote_to_scope_key"] = promote_to_scope_key
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        return await self._request("POST", "/memory/reflection/promote/preview", json=data)

    async def auto_review_reflection_promotion(
        self,
        *,
        candidate_id: str,
        promote_to_scope: str | None = None,
        promote_to_scope_key: str | None = None,
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
        dry_run: bool = False,
        confidence_threshold: float | None = None,
    ) -> dict[str, Any]:
        """Auto-review a reflection candidate and promote it when safe."""
        data: dict[str, Any] = {
            "candidate_id": candidate_id,
            "dry_run": dry_run,
            "related_to": related_to or [],
        }
        if promote_to_scope:
            data["promote_to_scope"] = promote_to_scope
        if promote_to_scope_key:
            data["promote_to_scope_key"] = promote_to_scope_key
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        if confidence_threshold is not None:
            data["confidence_threshold"] = confidence_threshold
        return await self._request("POST", "/memory/reflection/review/auto", json=data)

    async def drain_reflection_review(
        self,
        *,
        dry_run: bool = True,
        limit: int = 50,
        promote_to_scope: str | None = None,
        promote_to_scope_key: str | None = None,
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
        confidence_threshold: float | None = None,
        archive_exceptions: bool = False,
        archive_exception_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        """Drain pending reflection candidates through automatic review."""
        data: dict[str, Any] = {
            "dry_run": dry_run,
            "limit": limit,
            "related_to": related_to or [],
            "archive_exceptions": archive_exceptions,
            "archive_exception_reasons": archive_exception_reasons
            or ["duplicate_candidate", "stale_candidate"],
        }
        if promote_to_scope:
            data["promote_to_scope"] = promote_to_scope
        if promote_to_scope_key:
            data["promote_to_scope_key"] = promote_to_scope_key
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        if confidence_threshold is not None:
            data["confidence_threshold"] = confidence_threshold
        return await self._request("POST", "/memory/reflection/review/drain", json=data)

    async def list_jobs(
        self,
        *,
        function: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """List background jobs visible to the active organization."""
        params: dict[str, Any] = {"limit": limit}
        if function:
            params["function"] = function
        return await self._request("GET", "/jobs", params=params)

    async def enqueue_reflection_dream_cycle(
        self,
        *,
        dry_run: bool = True,
        source_limit: int = 20,
        candidate_limit: int = 50,
        archive_exceptions: bool = True,
    ) -> dict[str, Any]:
        """Queue an org-scoped automatic reflection maintenance run."""
        return await self._request(
            "POST",
            "/jobs/reflection-dream",
            params={
                "dry_run": dry_run,
                "source_limit": source_limit,
                "candidate_limit": candidate_limit,
                "archive_exceptions": archive_exceptions,
            },
        )

    async def preview_memory_share(
        self,
        *,
        source_ids: list[str],
        target_scope: str,
        target_scope_key: str | None = None,
        recipient_organization_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Preview memory sharing without mutating memory."""
        data: dict[str, Any] = {
            "source_ids": source_ids,
            "target_scope": target_scope,
        }
        if target_scope_key:
            data["target_scope_key"] = target_scope_key
        if recipient_organization_id:
            data["recipient_organization_id"] = recipient_organization_id
        if project_id:
            data["project_id"] = project_id
        return await self._request("POST", "/memory/share/preview", json=data)

    async def preview_memory_space_access(
        self,
        *,
        space_id: str,
        target_principal_type: str,
        target_principal_id: str,
        additional_space_ids: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Preview effective recall for a memory-space principal."""
        data: dict[str, Any] = {
            "target_principal_type": target_principal_type,
            "target_principal_id": target_principal_id,
            "additional_space_ids": additional_space_ids or [],
            "limit": limit,
        }
        return await self._request(
            "POST",
            f"/memory/spaces/{quote(space_id, safe='')}/members/preview",
            json=data,
        )

    def _synthesis_payload(
        self,
        *,
        goal: str,
        output_type: str = "documentation",
        audience: str | None = None,
        depth: str = "standard",
        seed_query: str | None = None,
        project: str | None = None,
        domain: str | None = None,
        entity_ids: list[str] | None = None,
        decision_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        required_sections: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
        max_sections: int = 6,
        include_neighborhoods: bool = True,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "goal": goal,
            "output_type": output_type,
            "depth": depth,
            "entity_ids": entity_ids or [],
            "decision_ids": decision_ids or [],
            "task_ids": task_ids or [],
            "artifact_ids": artifact_ids or [],
            "required_sections": required_sections or [],
            "constraints": constraints or [],
            "max_sections": max_sections,
            "include_neighborhoods": include_neighborhoods,
        }
        if audience:
            data["audience"] = audience
        if seed_query:
            data["seed_query"] = seed_query
        if project:
            data["project"] = project
        if domain:
            data["domain"] = domain
        return data

    async def synthesis_plan(
        self,
        *,
        goal: str,
        output_type: str = "documentation",
        audience: str | None = None,
        depth: str = "standard",
        seed_query: str | None = None,
        project: str | None = None,
        domain: str | None = None,
        entity_ids: list[str] | None = None,
        decision_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        required_sections: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
        max_sections: int = 6,
        include_neighborhoods: bool = True,
    ) -> dict[str, Any]:
        """Plan source-grounded synthesis through the API."""
        data = self._synthesis_payload(
            goal=goal,
            output_type=output_type,
            audience=audience,
            depth=depth,
            seed_query=seed_query,
            project=project,
            domain=domain,
            entity_ids=entity_ids,
            decision_ids=decision_ids,
            task_ids=task_ids,
            artifact_ids=artifact_ids,
            required_sections=required_sections,
            constraints=constraints,
            max_sections=max_sections,
            include_neighborhoods=include_neighborhoods,
        )
        return await self._request("POST", "/synthesis/plan", json=data)

    async def synthesis_draft(
        self,
        *,
        goal: str,
        output_type: str = "documentation",
        audience: str | None = None,
        depth: str = "standard",
        seed_query: str | None = None,
        project: str | None = None,
        domain: str | None = None,
        entity_ids: list[str] | None = None,
        decision_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        required_sections: list[dict[str, Any]] | None = None,
        constraints: list[str] | None = None,
        max_sections: int = 6,
        include_neighborhoods: bool = True,
        output_format: str = "markdown",
        remember: bool = False,
        memory_scope: str = "private",
        scope_key: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Draft and optionally remember source-grounded synthesis."""
        data = self._synthesis_payload(
            goal=goal,
            output_type=output_type,
            audience=audience,
            depth=depth,
            seed_query=seed_query,
            project=project,
            domain=domain,
            entity_ids=entity_ids,
            decision_ids=decision_ids,
            task_ids=task_ids,
            artifact_ids=artifact_ids,
            required_sections=required_sections,
            constraints=constraints,
            max_sections=max_sections,
            include_neighborhoods=include_neighborhoods,
        )
        data.update(
            {
                "output_format": output_format,
                "remember": remember,
                "memory_scope": memory_scope,
                "tags": tags or [],
            }
        )
        if scope_key:
            data["scope_key"] = scope_key
        return await self._request("POST", "/synthesis/draft", json=data)

    async def context_pack(
        self,
        goal: str,
        intent: str = "build",
        layer: str = "recall",
        domain: str | None = None,
        project: str | None = None,
        agent_id: str | None = None,
        limit: int = 24,
        include_related: bool = True,
        related_limit: int = 3,
    ) -> dict[str, Any]:
        """Compile a structured context pack for an agent goal."""
        data: dict[str, Any] = {
            "goal": goal,
            "intent": intent,
            "layer": layer,
            "limit": limit,
            "include_related": include_related,
            "related_limit": related_limit,
        }
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        if agent_id:
            data["agent_id"] = agent_id
        return await self._request("POST", "/context/pack", json=data)

    async def reflect(
        self,
        content: str,
        source_title: str = "Session reflection",
        intent: str = "general",
        domain: str | None = None,
        project: str | None = None,
        related_to: list[str] | None = None,
        persist: bool = False,
        persist_source: bool = True,
        persist_review: bool = False,
        limit: int = 12,
    ) -> dict[str, Any]:
        """Reflect raw notes into durable memory candidates."""
        data: dict[str, Any] = {
            "content": content,
            "source_title": source_title,
            "intent": intent,
            "persist": persist,
            "persist_source": persist_source,
            "persist_review": persist_review,
            "limit": limit,
        }
        if domain:
            data["domain"] = domain
        if project:
            data["project"] = project
        if related_to:
            data["related_to"] = related_to
        return await self._request("POST", "/context/reflect", json=data)

    async def explore(
        self,
        mode: str = "list",
        types: list[str] | None = None,
        entity_id: str | None = None,
        relationship_types: list[str] | None = None,
        depth: int = 1,
        language: str | None = None,
        category: str | None = None,
        project: str | None = None,
        epic: str | None = None,
        no_epic: bool = False,
        status: str | None = None,
        priority: str | None = None,
        complexity: str | None = None,
        feature: str | None = None,
        tags: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Explore and traverse the knowledge graph."""
        data: dict[str, Any] = {"mode": mode, "limit": limit, "offset": offset, "depth": depth}
        if types:
            data["types"] = types
        if entity_id:
            data["entity_id"] = entity_id
        if relationship_types:
            data["relationship_types"] = relationship_types
        if language:
            data["language"] = language
        if category:
            data["category"] = category
        if project:
            data["project"] = project
        if epic:
            data["epic"] = epic
        if no_epic:
            data["no_epic"] = True
        if status:
            data["status"] = status
        if priority:
            data["priority"] = priority
        if complexity:
            data["complexity"] = complexity
        if feature:
            data["feature"] = feature
        if tags:
            data["tags"] = tags

        return await self._request("POST", "/search/explore", json=data)

    async def temporal_query(
        self,
        mode: str = "history",
        entity_id: str | None = None,
        as_of: str | None = None,
        include_expired: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query bi-temporal history of edges.

        Modes:
        - history: Edges as they existed at a point in time
        - timeline: All versions of edges over time
        - conflicts: Find invalidated/superseded facts
        """
        data: dict[str, Any] = {"mode": mode, "limit": limit}
        if entity_id:
            data["entity_id"] = entity_id
        if as_of:
            data["as_of"] = as_of
        if include_expired:
            data["include_expired"] = True

        return await self._request("POST", "/search/temporal", json=data)

    # =========================================================================
    # Admin Operations
    # =========================================================================

    async def health(self) -> dict[str, Any]:
        """Get server health status."""
        return await self._request("GET", "/admin/health")

    async def stats(self) -> dict[str, Any]:
        """Get knowledge graph statistics."""
        return await self._request("GET", "/admin/stats")

    # =========================================================================
    # Knowledge Operations
    # =========================================================================

    async def add_knowledge(
        self,
        title: str,
        content: str,
        entity_type: str = "episode",
        category: str | None = None,
        languages: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add knowledge to the graph (via create_entity with knowledge semantics)."""
        return await self.create_entity(
            name=title,
            content=content,
            entity_type=entity_type,
            category=category,
            languages=languages,
            tags=tags,
        )

    # =========================================================================
    # Crawler Operations
    # =========================================================================

    async def create_crawl_source(
        self,
        name: str,
        url: str,
        source_type: str = "website",
        description: str | None = None,
        crawl_depth: int = 2,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a new crawl source."""
        data: dict[str, Any] = {
            "name": name,
            "url": url,
            "source_type": source_type,
            "crawl_depth": crawl_depth,
        }
        if description:
            data["description"] = description
        if include_patterns:
            data["include_patterns"] = include_patterns
        if exclude_patterns:
            data["exclude_patterns"] = exclude_patterns

        return await self._request("POST", "/sources", json=data)

    async def list_crawl_sources(
        self,
        status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """List crawl sources."""
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        return await self._request("GET", "/sources", params=params)

    async def get_crawl_source(self, source_id: str) -> dict[str, Any]:
        """Get a crawl source by ID."""
        return await self._request("GET", f"/sources/{source_id}")

    async def delete_crawl_source(self, source_id: str) -> dict[str, Any]:
        """Delete a crawl source."""
        return await self._request("DELETE", f"/sources/{source_id}")

    async def start_crawl(
        self,
        source_id: str,
        max_pages: int = 50,
        max_depth: int = 3,
        generate_embeddings: bool = True,
    ) -> dict[str, Any]:
        """Start crawling a source."""
        data = {
            "max_pages": max_pages,
            "max_depth": max_depth,
            "generate_embeddings": generate_embeddings,
        }
        return await self._request("POST", f"/sources/{source_id}/ingest", json=data)

    async def get_crawl_status(self, source_id: str) -> dict[str, Any]:
        """Get status of a crawl job."""
        return await self._request("GET", f"/sources/{source_id}/status")

    async def list_crawl_documents(
        self,
        source_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List crawled documents."""
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if source_id:
            return await self._request("GET", f"/sources/{source_id}/documents", params=params)
        return await self._request("GET", "/sources/documents", params=params)

    async def get_crawl_document(self, document_id: str) -> dict[str, Any]:
        """Get a crawled document by ID."""
        return await self._request("GET", f"/sources/documents/{document_id}")

    async def crawler_stats(self) -> dict[str, Any]:
        """Get crawler statistics."""
        return await self._request("GET", "/sources/stats")

    async def crawler_health(self) -> dict[str, Any]:
        """Get crawler health status."""
        return await self._request("GET", "/sources/health")

    async def link_graph(
        self,
        source_id: str | None = None,
        batch_size: int = 50,
        dry_run: bool = False,
        create_new_entities: bool = False,
    ) -> dict[str, Any]:
        """Link document chunks to knowledge graph via entity extraction.

        Args:
            source_id: Specific source ID, or None for all sources
            batch_size: Chunks per batch
            dry_run: Preview without processing
            create_new_entities: Create graph entities for unlinked extractions

        Returns:
            LinkGraphResponse with stats
        """
        data = {
            "batch_size": batch_size,
            "dry_run": dry_run,
            "create_new_entities": create_new_entities,
        }
        if source_id:
            return await self._request("POST", f"/sources/{source_id}/link-graph", json=data)
        return await self._request("POST", "/sources/link-graph", json=data)

    async def link_graph_status(self) -> dict[str, Any]:
        """Get status of pending graph linking work.

        Returns:
            LinkGraphStatusResponse with pending chunk counts per source
        """
        return await self._request("GET", "/sources/link-graph/status")


# Client cache by context name (None = default/active context)
_clients: dict[str | None, SibylClient] = {}


def get_client(context_name: str | None = None) -> SibylClient:
    """Get a client instance for the given context.

    Clients are cached by context name. Passing None uses the global override,
    then falls back to active context.

    Priority for context resolution:
    1. Explicit context_name parameter
    2. Global --context flag override
    3. SIBYL_CONTEXT environment variable
    4. Active context from config

    Args:
        context_name: Optional context name. None = use override or active context.

    Returns:
        SibylClient configured for the specified context.
    """
    global _clients

    # Check for global override if no explicit context provided
    if context_name is None:
        from sibyl_cli.state import get_context_override

        context_name = get_context_override()

    cache_key = context_name

    if cache_key not in _clients:
        _clients[cache_key] = SibylClient(context_name=context_name)

    return _clients[cache_key]


def clear_client_cache() -> None:
    """Clear the client cache. Useful when context settings change."""
    global _clients
    _clients.clear()
