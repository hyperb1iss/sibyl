"""Auth token storage for the CLI.

Tokens are stored under `servers[api_url]` in ~/.sibyl/auth.json. Context-aware
tokens live below `servers[api_url].scopes[context/org]`, with legacy
server-level tokens kept as a fallback for older installs.

Security:
- File permissions are enforced at 0600 (user read/write only)
- Directory permissions are enforced at 0700 (user only)
- Atomic writes prevent partial file corruption
- Token values are redacted in any logging
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

SCOPES_KEY = "scopes"
DEFAULT_SCOPE_PART = "default"
PENDING_REPLAY_SCOPE_KEY = "pending_replay_scope"


def auth_path() -> Path:
    return Path.home() / ".sibyl" / "auth.json"


@contextmanager
def auth_file_lock(path: Path | None = None):
    p = path or auth_path()
    _ensure_secure_dir(p.parent)
    lock_path = p.with_name(p.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name != "nt":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name != "nt":
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _ensure_secure_dir(path: Path) -> None:
    """Ensure directory exists with secure permissions (0700).

    Creates parent directories if needed, all with 0700 permissions.
    On Windows, this is best-effort (no chmod equivalent).
    """
    if path.exists():
        # Verify and fix permissions if needed
        if os.name != "nt":
            try:
                current_mode = stat.S_IMODE(os.stat(path).st_mode)
                if current_mode != 0o700:
                    os.chmod(path, 0o700)
            except OSError:
                pass
        return

    # Create with secure permissions
    if os.name != "nt":
        # Create with umask to ensure 0700
        old_umask = os.umask(0o077)
        try:
            path.mkdir(parents=True, exist_ok=True)
        finally:
            os.umask(old_umask)
    else:
        path.mkdir(parents=True, exist_ok=True)


def _secure_write(path: Path, content: str) -> None:
    """Write file atomically with secure permissions (0600).

    Uses atomic write pattern: write to temp file, then rename.
    This prevents partial writes and race conditions.
    """
    _ensure_secure_dir(path.parent)

    if os.name != "nt":
        # Unix: use atomic write with secure permissions
        fd = None
        tmp_path = None
        try:
            # Create temp file in same directory (for atomic rename)
            fd, tmp_path = tempfile.mkstemp(
                dir=path.parent,
                prefix=".auth_",
                suffix=".tmp",
            )
            # Set permissions before writing content
            os.fchmod(fd, 0o600)
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            fd = None
            # Atomic rename
            os.rename(tmp_path, path)
            tmp_path = None
        finally:
            if fd is not None:
                os.close(fd)
            if tmp_path is not None and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    else:
        # Windows: best-effort (no atomic rename guarantee)
        path.write_text(content, encoding="utf-8")


def read_auth_data(path: Path | None = None) -> dict[str, Any]:
    p = path or auth_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_auth_data(data: dict[str, Any], path: Path | None = None) -> None:
    p = path or auth_path()
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    _secure_write(p, content)


def normalize_api_url(api_url: str) -> str:
    """Normalize an API base URL key for credential storage."""
    raw = api_url.strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = parts.scheme or "http"
    netloc = parts.netloc or parts.path
    path = parts.path if parts.netloc else ""
    path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def credential_scope(context_name: str | None, org_slug: str | None = None) -> str:
    context = (context_name or "").strip() or DEFAULT_SCOPE_PART
    org = (org_slug or "").strip() or DEFAULT_SCOPE_PART
    return f"context:{context}:org:{org}"


def _split_path_and_scope(
    path: Path | None = None,
    credential_scope: str | None = None,
) -> tuple[Path | None, str | None]:
    if isinstance(path, str):
        return None, path
    return path, credential_scope


def _server_credentials_container(
    data: dict[str, Any],
    api_url: str,
) -> tuple[dict[str, Any], str] | tuple[None, str]:
    servers = data.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    key = normalize_api_url(api_url)
    if not key:
        return None, key
    existing = servers.get(key)
    if not isinstance(existing, dict):
        existing = {}
    servers[key] = existing
    data["servers"] = servers
    return existing, key


def _strip_scopes(creds: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in creds.items() if key != SCOPES_KEY}


def read_server_credentials(
    api_url: str,
    path: Path | None = None,
    *,
    credential_scope: str | None = None,
    fallback_to_server: bool = True,
) -> dict[str, Any]:
    """Read stored credentials for a specific server API URL."""
    path, credential_scope = _split_path_and_scope(path, credential_scope)
    data = read_auth_data(path)
    key = normalize_api_url(api_url)
    servers = data.get("servers")
    if not isinstance(servers, dict) or not key or not isinstance(servers.get(key), dict):
        return {}
    server_creds = dict(servers[key])
    if credential_scope:
        scopes = server_creds.get(SCOPES_KEY)
        if isinstance(scopes, dict) and isinstance(scopes.get(credential_scope), dict):
            return dict(scopes[credential_scope])
        return _strip_scopes(server_creds) if fallback_to_server else {}
    return _strip_scopes(server_creds)


def write_server_credentials(
    api_url: str,
    creds: dict[str, Any],
    path: Path | None = None,
    *,
    credential_scope: str | None = None,
) -> None:
    """Write/merge credentials for a specific server API URL."""
    path, credential_scope = _split_path_and_scope(path, credential_scope)
    data = read_auth_data(path)
    container, _key = _server_credentials_container(data, api_url)
    if container is None:
        return
    if credential_scope:
        scopes = container.get(SCOPES_KEY)
        if not isinstance(scopes, dict):
            scopes = {}
        existing = scopes.get(credential_scope)
        scopes[credential_scope] = {**(existing if isinstance(existing, dict) else {}), **creds}
        container[SCOPES_KEY] = scopes
    else:
        container.update(creds)
    write_auth_data(data, path)


def _replace_server_credentials(
    api_url: str,
    creds: dict[str, Any],
    *,
    remove_keys: tuple[str, ...] = (),
    path: Path | None = None,
    credential_scope: str | None = None,
) -> None:
    path, credential_scope = _split_path_and_scope(path, credential_scope)
    data = read_auth_data(path)
    container, _key = _server_credentials_container(data, api_url)
    if container is None:
        return
    if credential_scope:
        scopes = container.get(SCOPES_KEY)
        if not isinstance(scopes, dict):
            scopes = {}
        existing = scopes.get(credential_scope)
        merged = dict(existing if isinstance(existing, dict) else {})
        for remove_key in remove_keys:
            merged.pop(remove_key, None)
        merged.update(creds)
        scopes[credential_scope] = merged
        container[SCOPES_KEY] = scopes
    else:
        for remove_key in remove_keys:
            container.pop(remove_key, None)
        container.update(creds)
    write_auth_data(data, path)


def get_access_token(
    api_url: str,
    path: Path | None = None,
    *,
    credential_scope: str | None = None,
) -> str | None:
    """Get stored access token for a server."""
    creds = read_server_credentials(api_url, path, credential_scope=credential_scope)
    token = creds.get("access_token")
    return str(token) if token else None


def get_refresh_token(
    api_url: str,
    path: Path | None = None,
    *,
    credential_scope: str | None = None,
) -> str | None:
    """Get stored refresh token for a server."""
    creds = read_server_credentials(api_url, path, credential_scope=credential_scope)
    token = creds.get("refresh_token")
    return str(token) if token else None


def get_pending_replay_scope(
    api_url: str,
    path: Path | None = None,
    *,
    credential_scope: str | None = None,
) -> str | None:
    """Get the durable pending-write owner for a stored credential lineage."""
    creds = read_server_credentials(api_url, path, credential_scope=credential_scope)
    replay_scope = creds.get(PENDING_REPLAY_SCOPE_KEY)
    return str(replay_scope) if replay_scope else None


def get_access_token_expires_at(
    api_url: str,
    path: Path | None = None,
    *,
    credential_scope: str | None = None,
) -> int | None:
    """Get access token expiry timestamp for a server."""
    creds = read_server_credentials(api_url, path, credential_scope=credential_scope)
    expires_at = creds.get("access_token_expires_at")
    return int(expires_at) if expires_at is not None else None


def is_access_token_expired(
    api_url: str,
    path: Path | None = None,
    buffer_seconds: int = 60,
    *,
    credential_scope: str | None = None,
) -> bool:
    """Check if access token is expired or about to expire."""
    expires_at = get_access_token_expires_at(api_url, path, credential_scope=credential_scope)
    if expires_at is None:
        return False  # Assume not expired if no expiry stored
    return time.time() >= (expires_at - buffer_seconds)


def set_tokens(
    api_url: str,
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
    path: Path | None = None,
    *,
    lock: bool = True,
    credential_scope: str | None = None,
    pending_replay_scope: str | None = None,
) -> None:
    """Store tokens for a specific server and credential lineage.

    Explicit token writes start a new lineage by default. A server-confirmed
    refresh passes the existing replay scope so queued writes retain their
    owner without rewriting the queue.
    """
    path, credential_scope = _split_path_and_scope(path, credential_scope)
    creds: dict[str, Any] = {
        "access_token": access_token,
        PENDING_REPLAY_SCOPE_KEY: pending_replay_scope or f"credential:{uuid4().hex}",
    }
    remove_keys: list[str] = []
    if refresh_token is not None and refresh_token:
        creds["refresh_token"] = refresh_token
    else:
        remove_keys.append("refresh_token")
    if expires_in is not None:
        creds["access_token_expires_at"] = int(time.time()) + expires_in
    else:
        remove_keys.append("access_token_expires_at")
    if lock:
        with auth_file_lock(path):
            _replace_server_credentials(
                api_url,
                creds,
                remove_keys=tuple(remove_keys),
                path=path,
                credential_scope=credential_scope,
            )
        return
    _replace_server_credentials(
        api_url,
        creds,
        remove_keys=tuple(remove_keys),
        path=path,
        credential_scope=credential_scope,
    )


def clear_tokens(
    api_url: str,
    path: Path | None = None,
    *,
    credential_scope: str | None = None,
) -> None:
    """Clear tokens for a server or a specific credential scope."""
    path, credential_scope = _split_path_and_scope(path, credential_scope)
    with auth_file_lock(path):
        data = read_auth_data(path)
        servers = data.get("servers")
        if not isinstance(servers, dict):
            return
        key = normalize_api_url(api_url)
        if not key or key not in servers:
            return
        if credential_scope and isinstance(servers[key], dict):
            scopes = servers[key].get(SCOPES_KEY)
            if isinstance(scopes, dict):
                scopes.pop(credential_scope, None)
                servers[key][SCOPES_KEY] = scopes
                data["servers"] = servers
                write_auth_data(data, path)
            return
        del servers[key]
        data["servers"] = servers
        write_auth_data(data, path)


def clear_all_tokens(path: Path | None = None) -> None:
    """Clear all stored credentials (all servers)."""
    p = path or auth_path()
    with auth_file_lock(path):
        if p.exists():
            p.unlink()


def migrate_legacy_tokens(path: Path | None = None) -> None:
    """Remove legacy root-level tokens (one-time cleanup)."""
    data = read_auth_data(path)

    # Check if there are legacy root-level tokens to remove
    if not data.get("access_token"):
        return  # Nothing to migrate

    # Remove legacy fields
    data.pop("access_token", None)
    data.pop("refresh_token", None)
    data.pop("access_token_expires_at", None)

    write_auth_data(data, path)
