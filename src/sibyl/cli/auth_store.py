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


def clear_access_token(path: Path | None = None) -> None:
    data = read_auth_data(path)
    data.pop("access_token", None)
    if not data:
        p = path or auth_path()
        if p.exists():
            p.unlink()
        return
    write_auth_data(data, path)
