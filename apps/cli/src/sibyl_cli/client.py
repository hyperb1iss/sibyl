"""Public HTTP client contract for the Sibyl CLI."""

import os

import httpx

from sibyl_cli.auth_store import normalize_api_url
from sibyl_cli.client_admin import ClientAdminMixin
from sibyl_cli.client_auth import ClientAuthMixin
from sibyl_cli.client_graph import ClientGraphMixin
from sibyl_cli.client_memory import ClientMemoryMixin
from sibyl_cli.client_sources import ClientSourcesMixin
from sibyl_cli.client_transport import (
    _FAILURE_WINDOWS,
    BUFFERED_WRITE_METHODS,
    DEFAULT_SERVER_PORT,
    FAILURE_THRESHOLD,
    FAILURE_WINDOW_SECONDS,
    INIT_REMEDIATION,
    PENDING_WRITE_REMEDIATION,
    READ_LIKE_POST_PATHS,
    UNAPPLIED_WRITE_STATUS_CODES,
    ClientTransportMixin,
    ErrorPayload,
    SibylClientError,
    _auth_credential_scope,
    _auth_replay_scope,
    _is_read_like_post,
    _is_refresh_revoked,
    _load_default_auth_token,
    _paired_automation_api_url,
    anyio_sleep,
    resolve_api_base_url,
)
from sibyl_cli.client_work import ClientWorkMixin

__all__ = [
    "BUFFERED_WRITE_METHODS",
    "DEFAULT_SERVER_PORT",
    "FAILURE_THRESHOLD",
    "FAILURE_WINDOW_SECONDS",
    "INIT_REMEDIATION",
    "PENDING_WRITE_REMEDIATION",
    "READ_LIKE_POST_PATHS",
    "UNAPPLIED_WRITE_STATUS_CODES",
    "_FAILURE_WINDOWS",
    "ErrorPayload",
    "SibylClient",
    "SibylClientError",
    "_is_read_like_post",
    "_is_refresh_revoked",
    "anyio_sleep",
    "clear_client_cache",
    "get_client",
    "resolve_api_base_url",
]


class SibylClient(
    ClientAuthMixin,
    ClientGraphMixin,
    ClientWorkMixin,
    ClientMemoryMixin,
    ClientSourcesMixin,
    ClientAdminMixin,
    ClientTransportMixin,
):
    """HTTP client for all Sibyl REST API operations."""

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
        self.base_url = normalize_api_url(base_url or resolve_api_base_url(context_name))
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
        replay_credential_scope = self.credential_scope if self._uses_stored_auth else None
        self._replay_scope = _auth_replay_scope(replay_credential_scope, self.auth_token)
        self._client: httpx.AsyncClient | None = None
        # Load insecure setting from context
        self.insecure = self._get_insecure_from_context(context_name)


# Client cache by context name (None = default/active context)
_clients: dict[str | None, SibylClient] = {}


def get_client(context_name: str | None = None) -> SibylClient:
    """Get a client instance for the given context.

    Clients are cached by context name. Passing None resolves the effective
    context from the override, the directory pin, then the active context.

    Priority for context resolution:
    1. Explicit context_name parameter
    2. Global --context flag override
    3. SIBYL_CONTEXT environment variable
    4. Directory pin for the current working directory
    5. Active context from config

    Args:
        context_name: Optional context name. None = resolve from environment.

    Returns:
        SibylClient configured for the specified context.
    """
    global _clients

    # Resolve the effective context when one isn't explicitly provided.
    if context_name is None and _paired_automation_api_url() is None:
        from sibyl_cli import config_store

        context_name = config_store.resolve_context_name()

    cache_key = context_name

    if cache_key not in _clients:
        _clients[cache_key] = SibylClient(context_name=context_name)

    return _clients[cache_key]


def clear_client_cache() -> None:
    """Clear the client cache. Useful when context settings change."""
    global _clients
    _clients.clear()
