"""Auth token storage for the CLI.

All tokens are stored per-server under `servers[api_url]` in ~/.sibyl/auth.json.
The context system determines which server URL to use.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def auth_path() -> Path:
    return Path.home() / ".sibyl" / "auth.json"


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
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Best-effort hardening: keep tokens readable only by the current user.
    if os.name != "nt":
        try:
            os.chmod(p, 0o600)
            os.chmod(p.parent, 0o700)
        except Exception:
            pass


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


def read_server_credentials(api_url: str, path: Path | None = None) -> dict[str, Any]:
    """Read stored credentials for a specific server API URL."""
    data = read_auth_data(path)
    key = normalize_api_url(api_url)
    servers = data.get("servers")
    if isinstance(servers, dict) and key and isinstance(servers.get(key), dict):
        return dict(servers[key])
    return {}


def write_server_credentials(api_url: str, creds: dict[str, Any], path: Path | None = None) -> None:
    """Write/merge credentials for a specific server API URL."""
    data = read_auth_data(path)
    servers = data.get("servers")
    if not isinstance(servers, dict):
        servers = {}
    key = normalize_api_url(api_url)
    if not key:
        return
    existing = servers.get(key)
    merged = {**(existing if isinstance(existing, dict) else {}), **creds}
    servers[key] = merged
    data["servers"] = servers
    write_auth_data(data, path)


def get_access_token(api_url: str, path: Path | None = None) -> str | None:
    """Get stored access token for a server."""
    creds = read_server_credentials(api_url, path)
    token = creds.get("access_token")
    return str(token) if token else None


def get_refresh_token(api_url: str, path: Path | None = None) -> str | None:
    """Get stored refresh token for a server."""
    creds = read_server_credentials(api_url, path)
    token = creds.get("refresh_token")
    return str(token) if token else None


def get_access_token_expires_at(api_url: str, path: Path | None = None) -> int | None:
    """Get access token expiry timestamp for a server."""
    creds = read_server_credentials(api_url, path)
    expires_at = creds.get("access_token_expires_at")
    return int(expires_at) if expires_at is not None else None


def is_access_token_expired(api_url: str, path: Path | None = None, buffer_seconds: int = 60) -> bool:
    """Check if access token is expired or about to expire."""
    expires_at = get_access_token_expires_at(api_url, path)
    if expires_at is None:
        return False  # Assume not expired if no expiry stored
    return time.time() >= (expires_at - buffer_seconds)


def set_tokens(
    api_url: str,
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
    path: Path | None = None,
) -> None:
    """Store tokens for a specific server."""
    creds: dict[str, Any] = {"access_token": access_token}
    if refresh_token:
        creds["refresh_token"] = refresh_token
    if expires_in:
        creds["access_token_expires_at"] = int(time.time()) + expires_in
    write_server_credentials(api_url, creds, path)


def clear_tokens(api_url: str, path: Path | None = None) -> None:
    """Clear all tokens for a specific server."""
    data = read_auth_data(path)
    servers = data.get("servers")
    if not isinstance(servers, dict):
        return
    key = normalize_api_url(api_url)
    if key and key in servers:
        del servers[key]
        data["servers"] = servers
        write_auth_data(data, path)


def clear_all_tokens(path: Path | None = None) -> None:
    """Clear all stored credentials (all servers)."""
    p = path or auth_path()
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
