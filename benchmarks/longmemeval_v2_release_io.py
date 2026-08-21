"""Atomic local file publication for LongMemEval-V2 release artifacts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4


def _temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid4().hex}.tmp")


def _write_temporary(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(path)
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return temporary


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically replace one JSON object and sync its directory entry."""

    temporary = _write_temporary(path, payload)
    try:
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_once_atomic(path: Path, payload: dict[str, Any]) -> None:
    """Atomically publish one JSON object without replacing an existing file."""

    temporary = _write_temporary(path, payload)
    try:
        os.link(temporary, path)
        temporary.unlink()
        _fsync_parent(path)
    finally:
        temporary.unlink(missing_ok=True)
