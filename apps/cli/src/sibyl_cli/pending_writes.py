"""Local pending-write buffer for CLI requests."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def pending_writes_dir() -> Path:
    return Path.home() / ".config" / "sibyl" / "pending_writes"


def pending_metrics_path() -> Path:
    return Path.home() / ".config" / "sibyl" / "pending_writes_metrics.json"


def _ensure_secure_dir(path: Path) -> None:
    if path.exists():
        if os.name != "nt":
            current_mode = stat.S_IMODE(os.stat(path).st_mode)
            if current_mode != 0o700:
                os.chmod(path, 0o700)
        return

    if os.name != "nt":
        old_umask = os.umask(0o077)
        try:
            path.mkdir(parents=True, exist_ok=True)
        finally:
            os.umask(old_umask)
    else:
        path.mkdir(parents=True, exist_ok=True)


def _secure_write_json(path: Path, data: dict[str, Any]) -> None:
    _ensure_secure_dir(path.parent)
    content = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if os.name == "nt":
        path.write_text(content, encoding="utf-8")
        return

    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".pending_", suffix=".tmp")
        os.fchmod(fd, 0o600)
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = None
        os.rename(tmp_path, path)
        tmp_path = None
    finally:
        if fd is not None:
            os.close(fd)
        if tmp_path is not None and os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _pending_path(write_id: str) -> Path:
    return pending_writes_dir() / f"{write_id}.json"


def create_pending_write(
    *,
    method: str,
    path: str,
    base_url: str,
    json_payload: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    write_id = uuid4().hex
    idempotency_key = str(uuid4())
    data: dict[str, Any] = {
        "id": write_id,
        "idempotency_key": idempotency_key,
        "created_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "method": method.upper(),
        "path": path,
        "json": json_payload,
        "params": params,
        "attempts": 0,
    }
    _secure_write_json(_pending_path(write_id), data)
    record_pending_metric("attempted")
    return data


def read_pending_write(write_id: str) -> dict[str, Any]:
    path = resolve_pending_write_path(write_id)
    return json.loads(path.read_text(encoding="utf-8"))


def list_pending_writes() -> list[dict[str, Any]]:
    root = pending_writes_dir()
    if not root.exists():
        return []
    writes: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            writes.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    return writes


def delete_pending_write(write_id: str) -> bool:
    try:
        path = resolve_pending_write_path(write_id)
    except FileNotFoundError:
        return False
    path.unlink()
    return True


def resolve_pending_write_path(write_id: str) -> Path:
    root = pending_writes_dir()
    direct = _pending_path(write_id)
    if direct.exists():
        return direct
    matches = sorted(root.glob(f"{write_id}*.json")) if root.exists() else []
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous pending write ID prefix: {write_id}")
    raise FileNotFoundError(write_id)


def increment_attempts(write_id: str) -> dict[str, Any]:
    data = read_pending_write(write_id)
    data["attempts"] = int(data.get("attempts") or 0) + 1
    data["last_attempt_at"] = datetime.now(UTC).isoformat()
    _secure_write_json(resolve_pending_write_path(write_id), data)
    return data


def read_pending_metrics() -> dict[str, int]:
    path = pending_metrics_path()
    defaults = {"attempted": 0, "replayed": 0, "expired": 0, "discarded": 0}
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    return {key: int(data.get(key) or 0) for key in defaults}


def record_pending_metric(name: str, count: int = 1) -> dict[str, int]:
    metrics = read_pending_metrics()
    metrics[name] = int(metrics.get(name) or 0) + count
    _secure_write_json(pending_metrics_path(), metrics)
    return metrics


def pending_write_status() -> dict[str, Any]:
    return {
        "count": len(list_pending_writes()),
        "metrics": read_pending_metrics(),
    }


def pending_write_label(item: dict[str, Any]) -> tuple[str, str]:
    payload = item.get("json")
    if not isinstance(payload, dict):
        return ("write", "")

    title = str(payload.get("title") or payload.get("name") or "write")
    kind = str(
        payload.get("entity_type")
        or payload.get("memory_scope")
        or payload.get("author_type")
        or "write"
    )
    return (title, kind)
