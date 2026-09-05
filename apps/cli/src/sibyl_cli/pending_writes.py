"""Local pending-write buffer for CLI requests."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from sibyl_cli.pending_identity import normalize_replay_identity

# Every buffered write leaves the queue through exactly one of these outcomes,
# so `attempted` should equal their sum plus whatever is still queued. A gap
# means writes are vanishing without an accounted reason. Reads project onto
# these names, so an unlisted one would be written and then silently dropped.
PendingMetric = Literal["attempted", "completed", "replayed", "dropped", "discarded"]
PENDING_METRIC_NAMES: tuple[PendingMetric, ...] = get_args(PendingMetric)
CORRUPT_PENDING_WRITE_STATUS = "corrupt"
PendingFailureCategory = Literal[
    "authentication", "transport", "server", "rejected", "conflict", "dependency"
]
_SAFE_FAILURE_CODES = frozenset(
    {
        "authentication_error",
        "unauthorized",
        "forbidden",
        "not_found",
        "conflict",
        "validation_error",
        "constraint_violation",
        "internal_error",
        "token_refresh_failed",
        "client_too_old",
        "service_unavailable",
        "idempotency_conflict",
        "idempotency_in_progress",
        "replay_identity_mismatch",
        "pending_dependency",
        "response_unconfirmed",
    }
)
_REQUIRED_STRING_FIELDS = (
    "id",
    "idempotency_key",
    "created_at",
    "base_url",
    "method",
    "path",
)
_CANONICAL_WRITE_ID = re.compile(r"^[0-9a-f]{32}$")
_CANONICAL_WRITE_ID_PREFIX = re.compile(r"^[0-9a-f]{1,32}$")
_REPLAYABLE_METHODS = {"POST", "PATCH", "DELETE"}


def pending_writes_dir() -> Path:
    return Path.home() / ".config" / "sibyl" / "pending_writes"


def pending_metrics_path() -> Path:
    return Path.home() / ".config" / "sibyl" / "pending_writes_metrics.json"


@contextmanager
def pending_replay_lock() -> Iterator[bool]:
    """Try to serialize pending-write replay without delaying the caller."""
    root = pending_writes_dir()
    _ensure_secure_dir(root)
    path = root / ".replay.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            yield False
            return
        yield True
    finally:
        if locked and os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif locked:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
    if not is_canonical_pending_write_id(write_id):
        raise ValueError(f"Invalid pending write ID: {write_id}")
    return pending_writes_dir() / f"{write_id}.json"


def is_canonical_pending_write_id(write_id: str, *, allow_prefix: bool = False) -> bool:
    pattern = _CANONICAL_WRITE_ID_PREFIX if allow_prefix else _CANONICAL_WRITE_ID
    return pattern.fullmatch(write_id) is not None


def _corrupt_pending_write(path: Path, error: str) -> dict[str, Any]:
    return {
        "id": path.stem,
        "status": CORRUPT_PENDING_WRITE_STATUS,
        "filename": path.name,
        "error": error,
    }


def is_corrupt_pending_write(item: dict[str, Any]) -> bool:
    return (
        item.get("status") == CORRUPT_PENDING_WRITE_STATUS
        and isinstance(item.get("filename"), str)
        and isinstance(item.get("error"), str)
    )


def _read_pending_path(path: Path) -> dict[str, Any]:
    if not is_canonical_pending_write_id(path.stem):
        return _corrupt_pending_write(path, "Queue filename is not a canonical pending write ID")
    try:
        data = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_nonstandard_json_constant,
        )
    except json.JSONDecodeError as exc:
        return _corrupt_pending_write(
            path,
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}",
        )
    except ValueError as exc:
        return _corrupt_pending_write(path, f"Invalid JSON: {exc}")
    except (OSError, UnicodeError) as exc:
        return _corrupt_pending_write(path, f"{type(exc).__name__}: {exc}")

    if not isinstance(data, dict):
        return _corrupt_pending_write(
            path,
            f"Expected a JSON object, found {type(data).__name__}",
        )
    missing = [
        field
        for field in _REQUIRED_STRING_FIELDS
        if not isinstance(data.get(field), str) or not data[field]
    ]
    if missing:
        return _corrupt_pending_write(
            path,
            f"Missing or invalid required fields: {', '.join(missing)}",
        )
    if data["id"] != path.stem:
        return _corrupt_pending_write(path, "Stored id does not match the queue filename")
    try:
        idempotency_key = str(UUID(data["idempotency_key"]))
    except ValueError:
        return _corrupt_pending_write(path, "Missing or invalid required field: idempotency_key")
    if idempotency_key != data["idempotency_key"]:
        return _corrupt_pending_write(path, "Missing or invalid required field: idempotency_key")

    if data["method"] not in _REPLAYABLE_METHODS:
        return _corrupt_pending_write(path, "Missing or invalid required field: method")
    if not data["path"].startswith("/") or data["path"].startswith("//"):
        return _corrupt_pending_write(path, "Missing or invalid required field: path")
    try:
        parsed_url = urlsplit(data["base_url"])
    except ValueError:
        return _corrupt_pending_write(path, "Missing or invalid required field: base_url")
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        return _corrupt_pending_write(path, "Missing or invalid required field: base_url")

    payload = data.get("json")
    if payload is not None and not isinstance(payload, dict):
        return _corrupt_pending_write(path, "Missing or invalid required field: json")
    params = data.get("params")
    if params is not None and (not isinstance(params, dict) or not _valid_query_params(params)):
        return _corrupt_pending_write(path, "Missing or invalid required field: params")
    attempts = data.get("attempts", 0)
    if type(attempts) is not int or attempts < 0:
        return _corrupt_pending_write(path, "Missing or invalid required field: attempts")
    if (
        data.get("replay_identity") is not None
        and normalize_replay_identity(data["replay_identity"]) is None
    ):
        return _corrupt_pending_write(path, "Missing or invalid required field: replay_identity")
    if data.get("status", "pending") not in ("pending", "attention"):
        return _corrupt_pending_write(path, "Missing or invalid required field: status")
    return data


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard numeric constant: {value}")


def _valid_query_params(params: dict[str, Any]) -> bool:
    scalar_types = (str, int, float, bool, type(None))
    for key, value in params.items():
        if not isinstance(key, str):
            return False
        if isinstance(value, scalar_types):
            continue
        if isinstance(value, list) and all(isinstance(item, scalar_types) for item in value):
            continue
        return False
    return True


def create_pending_write(
    *,
    method: str,
    path: str,
    base_url: str,
    json_payload: dict[str, Any] | None,
    params: dict[str, Any] | None,
    replay_scope: str | None = None,
    replay_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    write_id = uuid4().hex
    idempotency_key = str(uuid4())
    data: dict[str, Any] = {
        "id": write_id,
        "idempotency_key": idempotency_key,
        "created_at": datetime.now(UTC).isoformat(),
        "base_url": base_url,
        "replay_scope": replay_scope,
        "method": method.upper(),
        "path": path,
        "json": json_payload,
        "params": params,
        "attempts": 0,
        "status": "pending",
    }
    if replay_identity is not None:
        normalized = normalize_replay_identity(replay_identity)
        if normalized is None:
            raise ValueError("Invalid pending write replay identity")
        data["replay_identity"] = normalized
    _secure_write_json(_pending_path(write_id), data)
    record_pending_metric("attempted")
    return data


def read_pending_write(write_id: str) -> dict[str, Any]:
    path = resolve_pending_write_path(write_id)
    return _read_pending_path(path)


def list_pending_writes() -> list[dict[str, Any]]:
    root = pending_writes_dir()
    if not root.exists():
        return []
    return [_read_pending_path(path) for path in sorted(root.glob("*.json"))]


def pending_write_count() -> int:
    """Count queued writes without parsing them.

    Runs after every command, so it stays a directory listing, and an
    unreadable or undeterminable home never turns a successful command into
    a failing one. Path.home() raises RuntimeError when it cannot resolve.
    """
    try:
        root = pending_writes_dir()
        if not root.exists():
            return 0
        return sum(1 for _ in root.glob("*.json"))
    except (OSError, RuntimeError):
        return 0


def delete_pending_write(write_id: str) -> bool:
    try:
        path = resolve_pending_write_path(write_id)
    except FileNotFoundError:
        return False
    path.unlink()
    return True


def resolve_pending_write_path(write_id: str) -> Path:
    if not is_canonical_pending_write_id(write_id, allow_prefix=True):
        raise ValueError(f"Invalid pending write ID or prefix: {write_id}")
    root = pending_writes_dir()
    if is_canonical_pending_write_id(write_id):
        direct = _pending_path(write_id)
        if direct.exists():
            return direct
        raise FileNotFoundError(write_id)
    matches = (
        sorted(
            path
            for path in root.glob(f"{write_id}*.json")
            if is_canonical_pending_write_id(path.stem)
        )
        if root.exists()
        else []
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"Ambiguous pending write ID prefix: {write_id}")
    raise FileNotFoundError(write_id)


def increment_attempts(write_id: str) -> dict[str, Any]:
    path = resolve_pending_write_path(write_id)
    data = _read_pending_path(path)
    if is_corrupt_pending_write(data):
        raise ValueError(f"Cannot update corrupt pending write {data['filename']}: {data['error']}")
    data["attempts"] = int(data.get("attempts") or 0) + 1
    data["last_attempt_at"] = datetime.now(UTC).isoformat()
    _secure_write_json(path, data)
    return data


def record_pending_failure(
    write_id: str,
    *,
    category: PendingFailureCategory,
    status_code: int | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Retain the operation with a diagnostic that cannot contain response secrets."""
    if category not in get_args(PendingFailureCategory):
        raise ValueError("Invalid pending write failure category")
    if status_code is not None and (type(status_code) is not int or not 400 <= status_code <= 599):
        raise ValueError("Invalid pending write failure status code")
    path = resolve_pending_write_path(write_id)
    data = _read_pending_path(path)
    if is_corrupt_pending_write(data):
        raise ValueError("Cannot record a failure for a corrupt pending write")
    data["status"] = "attention" if category in {"rejected", "conflict"} else "pending"
    data["last_failure"] = {
        "category": category,
        "status_code": status_code,
        "error_code": error_code if error_code in _SAFE_FAILURE_CODES else None,
        "at": datetime.now(UTC).isoformat(),
    }
    _secure_write_json(path, data)
    return data


def retry_pending_write(write_id: str) -> dict[str, Any]:
    """Explicitly retry a retained operation without changing its identity or payload."""
    path = resolve_pending_write_path(write_id)
    data = _read_pending_path(path)
    if is_corrupt_pending_write(data):
        raise ValueError("Cannot retry a corrupt pending write")
    data["status"] = "pending"
    data.pop("last_attempt_at", None)
    _secure_write_json(path, data)
    return data


def pending_write_resource(item: dict[str, Any]) -> str:
    """Group ordered mutations of one resource, leaving independent creates unblocked."""
    parts = [part for part in str(item.get("path", "")).split("/") if part]
    method = str(item.get("method", "")).upper()
    if method == "POST" and parts in (["memory", "raw"], ["tasks"], ["entities"]):
        payload = item.get("json")
        if parts in (["tasks"], ["entities"]) and isinstance(payload, dict) and payload.get("id"):
            return f"entity:{payload['id']}"
        return f"create:{item['id']}"
    if len(parts) >= 2 and parts[1] in {"bulk", "batch", "import", "export"}:
        return "*"
    if len(parts) >= 2 and parts[0] in {"tasks", "entities"}:
        return f"entity:{parts[1]}"
    if len(parts) >= 2 and parts[0] == "projects":
        return f"project:{parts[1]}"
    return f"path:{'/'.join(parts[:2])}"


def bind_pending_write_identity(
    write_id: str,
    replay_identity: dict[str, Any],
    *,
    replay_scope: str,
) -> dict[str, Any]:
    """Migrate only a write still owned by the caller's original credential lineage."""
    normalized = normalize_replay_identity(replay_identity)
    if not replay_scope or normalized is None:
        raise ValueError("Invalid pending write replay identity or credential scope")
    replay_identity = normalized
    path = resolve_pending_write_path(write_id)
    data = _read_pending_path(path)
    if is_corrupt_pending_write(data):
        raise ValueError("Cannot bind a corrupt pending write")
    existing = data.get("replay_identity")
    if existing is not None:
        if existing != replay_identity:
            raise ValueError("Pending write already belongs to another identity")
        return data
    if data.get("replay_scope") != replay_scope:
        raise ValueError("Pending write belongs to another credential lineage")
    data["replay_identity"] = replay_identity
    _secure_write_json(path, data)
    return data


def claim_pending_write_replay_scope(
    write_id: str,
    replay_scope: str,
    *,
    replay_identity: dict[str, Any] | None = None,
    adopt_unverified: bool = False,
) -> dict[str, Any]:
    """Bind an ambiguous legacy write after explicit operator approval."""
    if not replay_scope:
        raise ValueError("Pending write replay scope cannot be empty")
    path = resolve_pending_write_path(write_id)
    data = _read_pending_path(path)
    if is_corrupt_pending_write(data):
        raise ValueError(f"Cannot update corrupt pending write {data['filename']}: {data['error']}")
    if data.get("replay_identity") is not None:
        raise ValueError("A verified write owner cannot be reassigned")
    identity = normalize_replay_identity(replay_identity)
    if replay_identity is not None and identity is None:
        raise ValueError("Invalid replay identity")
    if adopt_unverified and identity is None:
        raise ValueError("Adopting an unverified write requires a verified server identity")
    current_scope = data.get("replay_scope")
    if current_scope == replay_scope and identity is None:
        return data
    if (
        current_scope != replay_scope
        and current_scope is not None
        and not str(current_scope).startswith("context:")
        and not adopt_unverified
    ):
        raise ValueError("Pending write already belongs to another credential")
    data["previous_replay_scope"] = current_scope
    data["replay_scope"] = replay_scope
    if identity is not None:
        data["replay_identity"] = identity
        data["ownership_confirmed_at"] = datetime.now(UTC).isoformat()
    _secure_write_json(path, data)
    return data


def read_pending_metrics() -> dict[str, int]:
    path = pending_metrics_path()
    defaults: dict[str, int] = dict.fromkeys(PENDING_METRIC_NAMES, 0)
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return defaults
    metrics = {key: int(data.get(key) or 0) for key in defaults}
    # `expired` was retired in favour of the outcome names above. Folding its
    # historical count into `discarded` keeps the sum invariant true across the
    # upgrade; projecting it away would leave `attempted` describing writes with
    # no recorded outcome.
    metrics["discarded"] += int(data.get("expired") or 0)
    return metrics


def record_pending_metric(name: PendingMetric, count: int = 1) -> dict[str, int]:
    metrics = read_pending_metrics()
    metrics[name] = int(metrics.get(name) or 0) + count
    _secure_write_json(pending_metrics_path(), metrics)
    return metrics


def pending_write_status() -> dict[str, Any]:
    writes = list_pending_writes()
    return {
        "count": len(writes),
        "pending": sum(item.get("status", "pending") == "pending" for item in writes),
        "attention": sum(item.get("status") == "attention" for item in writes),
        "failures": [
            {"filename": item["filename"], "error": item["error"]}
            for item in writes
            if is_corrupt_pending_write(item)
        ],
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
