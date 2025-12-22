"""Auth token storage for the CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


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

