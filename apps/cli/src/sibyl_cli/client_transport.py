"""Shared HTTP transport, error handling, and pending-write behavior."""

import os
import random
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, Self, cast

import httpx

from sibyl_cli.auth_store import (
    credential_scope,
    get_access_token,
    is_access_token_expired,
    read_server_credentials,
)
from sibyl_cli.pending_writes import (
    PendingMetric,
    create_pending_write,
    delete_pending_write,
    increment_attempts,
    is_corrupt_pending_write,
    list_pending_writes,
    pending_replay_lock,
    record_pending_metric,
)

# Default server port (matches sibyl-server default)
DEFAULT_SERVER_PORT = 3334
FAILURE_WINDOW_SECONDS = 10.0
FAILURE_THRESHOLD = 3
_FAILURE_WINDOWS: dict[tuple[str, str], deque[float]] = {}
BUFFERED_WRITE_METHODS = {"POST", "PATCH", "DELETE"}
AUTO_REPLAY_LIMIT = 8
AUTO_REPLAY_GRACE_SECONDS = 30.0
AUTO_REPLAY_BACKOFF_BASE_SECONDS = 2.0
AUTO_REPLAY_BACKOFF_MAX_SECONDS = 300.0
PENDING_WRITE_REMEDIATION = (
    "Run 'sibyl auth login'; buffered writes retry after the next successful API request."
)
INIT_REMEDIATION = "Run 'sibyl init' for local mode or 'sibyl init --remote <url>'."


@dataclass
class ErrorPayload:
    message: str
    error: str | None = None
    request_id: str | None = None
    remediation: str | None = None
    details: dict[str, object] | None = None


def _paired_automation_api_url() -> str | None:
    env_url = os.environ.get("SIBYL_API_URL", "").strip()
    env_token = os.environ.get("SIBYL_AUTH_TOKEN", "").strip()
    return env_url if env_url and env_token else None


def resolve_api_base_url(context_name: str | None = None) -> str:
    """Get API URL from context, config file, env var, or default.

    Priority:
    1. Explicit context (if provided)
    2. Paired automation environment (SIBYL_API_URL + SIBYL_AUTH_TOKEN)
    3. Active context's server_url
    4. Environment variable (SIBYL_API_URL)
    5. Legacy config file (server.url)
    6. Default (http://localhost:3334/api)

    This is the single source of truth for CLI server selection. `sibyl auth`
    resolves through it too, so a login stores its token under the same server
    key every later command reads.

    Args:
        context_name: Optional context name to use instead of active context.
    """
    # Lazy import to avoid circular dependency
    from sibyl_cli import config_store

    # 1. If explicit context provided, use that. A named context that does not
    #    exist stops the command rather than falling through, because falling
    #    through retargets the request at whatever server is active.
    if context_name:
        config_store.require_known_context(context_name)
        ctx = config_store.get_context(context_name)
        if ctx:
            return f"{ctx.server_url}/api"

    # 2. A paired URL and token are an explicit, self-contained automation
    #    target. Letting an interactive context override only the URL would
    #    send that token to the wrong server.
    if automation_url := _paired_automation_api_url():
        return automation_url

    # 3. Try active context
    ctx = config_store.get_active_context()
    if ctx:
        return f"{ctx.server_url}/api"

    # 4. Try env var
    if fallback_env_url := os.environ.get("SIBYL_API_URL", "").strip():
        return fallback_env_url

    # 5. Try legacy config file
    if config_store.config_exists():
        url = config_store.get_server_url()
        if url:
            return f"{url}/api"

    # 6. Default
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


def _auth_replay_scope(
    stored_replay_scope: str | None,
    auth_token: str | None,
) -> str | None:
    if stored_replay_scope:
        return stored_replay_scope
    if auth_token:
        digest = sha256(auth_token.encode("utf-8")).hexdigest()
        return f"token-sha256:{digest}"
    return None


def _load_default_replay_scope(
    api_base_url: str,
    credential_scope_name: str | None,
    auth_token: str | None,
) -> str | None:
    if os.environ.get("SIBYL_AUTH_TOKEN", "").strip():
        return _auth_replay_scope(None, auth_token)
    stored_credentials = read_server_credentials(
        api_base_url,
        credential_scope=credential_scope_name,
    )
    stored_token = str(stored_credentials.get("access_token") or "").strip()
    stored_scope = str(stored_credentials.get("pending_replay_scope") or "").strip()
    if stored_token != auth_token:
        stored_scope = ""
    return _auth_replay_scope(stored_scope, auth_token)


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
    return (
        "session not found" in normalized
        or "revoked" in normalized
        or "invalid refresh token" in normalized
    )


# Read-like POSTs (search, recall, context-pack assembly) carry no durable
# write, so a failed one is simply re-run, never replayed. Buffering them
# flooded the pending-write queue with hundreds of /search and /context/pack
# entries. /context/reflect and /memory/raw are intentionally absent: they can
# persist, so they stay buffered.
READ_LIKE_POST_PATHS = (
    "/search",
    "/rag/search",
    "/rag/hybrid-search",
    "/rag/code-examples",
    "/context/pack",
    "/memory/raw/recall",
)


def _is_read_like_post(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in READ_LIKE_POST_PATHS)


def _should_buffer_request(method: str, path: str) -> bool:
    if method.upper() not in BUFFERED_WRITE_METHODS:
        return False
    if path.startswith("/auth/"):
        return False
    return not _is_read_like_post(path)


def _ready_for_auto_replay(item: dict[str, Any]) -> bool:
    try:
        created_at = datetime.fromisoformat(str(item.get("created_at") or ""))
    except ValueError:
        return False
    if created_at.tzinfo is None:
        return False
    age = datetime.now(UTC) - created_at
    if age.total_seconds() < AUTO_REPLAY_GRACE_SECONDS:
        return False

    attempts = int(item.get("attempts") or 0)
    last_attempt_text = item.get("last_attempt_at")
    if attempts == 0 or not isinstance(last_attempt_text, str):
        return True
    try:
        last_attempt_at = datetime.fromisoformat(last_attempt_text)
    except ValueError:
        return False
    if last_attempt_at.tzinfo is None:
        return False
    backoff_seconds = min(
        AUTO_REPLAY_BACKOFF_BASE_SECONDS * (2 ** min(attempts - 1, 16)),
        AUTO_REPLAY_BACKOFF_MAX_SECONDS,
    )
    return (datetime.now(UTC) - last_attempt_at).total_seconds() >= backoff_seconds


def _requires_initialized_context(method: str, path: str) -> bool:
    if method.upper() not in BUFFERED_WRITE_METHODS:
        return False
    return not path.startswith("/auth/")


# Discarding a buffered write destroys the only copy of its payload: the server
# stores a request hash, never the body. So the queue only drops a write when
# the status proves the server rejected the payload itself and no retry could
# ever land it. Everything else stays buffered, including 409 - a stranded
# idempotency reservation reports 409 for a write that may already have applied,
# and the client cannot tell the two apart.
UNAPPLIED_WRITE_STATUS_CODES = {400, 422}


def _should_keep_pending_write(status_code: int) -> bool:
    return status_code not in UNAPPLIED_WRITE_STATUS_CODES


def _resolve_pending_write(write_id: str | None, outcome: PendingMetric | None) -> None:
    """Drop a buffered write from the queue and record why it left.

    A replay is accounted by the flush command that drives it, so it resolves
    with no outcome of its own here.
    """
    if write_id is None:
        return
    if delete_pending_write(write_id) and outcome is not None:
        record_pending_metric(outcome)


def _refresh_failure_status_code(message: str | None) -> int | None:
    if not message:
        return None
    normalized = message.lower()
    if "temporarily unavailable" in normalized or "timeout" in normalized:
        return 503
    if "revoked" in normalized or "invalid refresh token" in normalized:
        return 401
    return None


def _refresh_failure_remediation(*, pending_write_id: str | None) -> str:
    if pending_write_id:
        return PENDING_WRITE_REMEDIATION
    return (
        "Retry once Sibyl is healthy, or run 'sibyl auth login' if the refresh token was revoked."
    )


async def anyio_sleep(delay: float) -> None:
    import asyncio

    await asyncio.sleep(delay)


class ClientTransportMixin:
    """Shared HTTP transport, error handling, and pending-write behavior."""

    def _get_insecure_from_context(self, context_name: str | None) -> bool:
        """Get insecure setting from context config."""
        from sibyl_cli import config_store

        if context_name:
            config_store.require_known_context(context_name)
            ctx = config_store.get_context(context_name)
            if ctx:
                return ctx.insecure
        # Check active context
        ctx = config_store.get_active_context()
        if ctx:
            return ctx.insecure
        return False

    def _default_headers(self) -> dict[str, str]:
        from sibyl_cli.version_drift import client_version
        from sibyl_core.version_contract import CLIENT_VERSION_HEADER

        version = client_version()
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            # Named so the server can log or refuse stale callers, and so
            # access logs show which clients are in the field.
            CLIENT_VERSION_HEADER: version,
            "User-Agent": f"sibyl-dev/{version}",
        }
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    def _check_version_drift(self, response: httpx.Response) -> None:
        """Report client/server drift, and refuse to run below a declared floor."""
        from sibyl_cli.version_drift import ClientTooOldError, check_response_headers

        try:
            check_response_headers(response.headers, base_url=self.base_url)
        except ClientTooOldError as exc:
            raise SibylClientError(
                str(exc),
                status_code=426,
                error_code="client_too_old",
                remediation=(
                    "Run `sibyl update` to upgrade, or `git pull && moon run install-dev` "
                    "for a source install."
                ),
            ) from exc

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

    async def _maybe_replay_pending_writes(self, *, ignore_backoff: bool = False) -> None:
        """Replay a bounded batch after this client proves the API is healthy."""
        try:
            if not list_pending_writes():
                return
            with pending_replay_lock() as acquired:
                if not acquired:
                    return
                matching = [
                    item
                    for item in list_pending_writes()
                    if not is_corrupt_pending_write(item)
                    and str(item.get("base_url")) == self.base_url
                    and item.get("replay_scope") == self._replay_scope
                    and self._replay_scope is not None
                    and not (
                        str(item.get("method") or "").upper() == "POST"
                        and _is_read_like_post(str(item.get("path") or ""))
                    )
                ]
                matching.sort(key=lambda item: str(item.get("created_at") or ""))
                batch: list[dict[str, Any]] = []
                for item in matching:
                    if not ignore_backoff and not _ready_for_auto_replay(item):
                        break
                    batch.append(item)
                    if len(batch) == AUTO_REPLAY_LIMIT:
                        break
                for item in batch:
                    write_id = str(item["id"])
                    try:
                        current = increment_attempts(write_id)
                        await self._request(
                            str(current["method"]),
                            str(current["path"]),
                            json=current.get("json"),
                            params=current.get("params"),
                            _buffer_pending=False,
                            _pending_write_id=write_id,
                            _idempotency_key=str(current["idempotency_key"]),
                        )
                        record_pending_metric("replayed")
                    except SibylClientError as exc:
                        if exc.status_code not in UNAPPLIED_WRITE_STATUS_CODES:
                            break
                    except (FileNotFoundError, OSError, ValueError):
                        break
        except Exception:
            return

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        await self.close()

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
        _pending_write_created: bool = False,
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
        pending_write_created = _pending_write_created
        idempotency_key = _idempotency_key
        if _buffer_pending and pending_write_id is None and _should_buffer_request(method, path):
            pending = create_pending_write(
                method=method,
                path=path,
                base_url=self.base_url,
                json_payload=json,
                params=params,
                replay_scope=self._replay_scope,
            )
            pending_write_id = str(pending["id"])
            pending_write_created = True
            idempotency_key = str(pending["idempotency_key"])

        refresh_failure: str | None = None

        # Proactively refresh if token is about to expire.
        if (
            self._uses_stored_auth
            and self.auth_token
            and is_access_token_expired(
                self.base_url,
                credential_scope=self.credential_scope,
            )
        ):
            refreshed, refresh_failure = await self._refresh_token()
            if not refreshed:
                raise SibylClientError(
                    "Stored access token is expired and automatic token refresh failed.",
                    status_code=_refresh_failure_status_code(refresh_failure),
                    detail=refresh_failure,
                    error_code="token_refresh_failed",
                    remediation=_refresh_failure_remediation(pending_write_id=pending_write_id),
                )

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
            self._check_version_drift(response)

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
                        _pending_write_created=pending_write_created,
                        _idempotency_key=idempotency_key,
                    )

            # Handle error responses
            if response.status_code >= 400:
                if not _should_keep_pending_write(response.status_code):
                    _resolve_pending_write(pending_write_id, "dropped")
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

            applied_outcome = "completed" if pending_write_created else None

            # Return empty dict for 204 No Content
            if response.status_code == 204:
                _resolve_pending_write(pending_write_id, applied_outcome)
                _record_success(breaker_key)
                if pending_write_id is None or pending_write_created:
                    await self._maybe_replay_pending_writes()
                return {}

            data = response.json()
            _resolve_pending_write(pending_write_id, applied_outcome)
            _record_success(breaker_key)
            if pending_write_id is None or pending_write_created:
                await self._maybe_replay_pending_writes()
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
