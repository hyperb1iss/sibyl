"""Auth token storage for the CLI."""

from __future__ import annotations

import json
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


def set_access_token(token: str, path: Path | None = None) -> None:
    data = read_auth_data(path)
    data["access_token"] = token
    write_auth_data(data, path)


def set_tokens(
    access_token: str,
    refresh_token: str | None = None,
    expires_in: int | None = None,
    path: Path | None = None,
) -> None:
    """Store access token and optionally refresh token."""
    data = read_auth_data(path)
    data["access_token"] = access_token
    if refresh_token:
        data["refresh_token"] = refresh_token
    if expires_in:
        import time

        data["access_token_expires_at"] = int(time.time()) + expires_in
    write_auth_data(data, path)


def get_refresh_token(path: Path | None = None) -> str | None:
    """Get stored refresh token."""
    data = read_auth_data(path)
    return data.get("refresh_token")


def get_access_token_expires_at(path: Path | None = None) -> int | None:
    """Get access token expiry timestamp."""
    data = read_auth_data(path)
    return data.get("access_token_expires_at")


def is_access_token_expired(path: Path | None = None, buffer_seconds: int = 60) -> bool:
    """Check if access token is expired or about to expire."""
    expires_at = get_access_token_expires_at(path)
    if expires_at is None:
        return False  # Assume not expired if no expiry stored
    import time

    return time.time() >= (expires_at - buffer_seconds)


def clear_access_token(path: Path | None = None) -> None:
    data = read_auth_data(path)
    data.pop("access_token", None)
    if not data:
        p = path or auth_path()
        if p.exists():
            p.unlink()
        return
    write_auth_data(data, path)


def clear_tokens(path: Path | None = None) -> None:
    """Clear all tokens (access and refresh)."""
    data = read_auth_data(path)
    data.pop("access_token", None)
    data.pop("refresh_token", None)
    data.pop("access_token_expires_at", None)
    if not data:
        p = path or auth_path()
        if p.exists():
            p.unlink()
        return
    write_auth_data(data, path)
