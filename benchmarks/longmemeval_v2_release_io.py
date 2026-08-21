"""Atomic local file publication for LongMemEval-V2 release artifacts."""

from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import suppress
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from pathlib import Path
from typing import Any
from uuid import uuid4

RENAME_EXCL = 0x00000004


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


def write_json_once_atomic_at(directory_fd: int, name: str, payload: dict[str, Any]) -> None:
    """Publish one JSON object relative to an already owned directory fd."""

    if Path(name).name != name:
        raise ValueError("fd-owned JSON publication requires one safe relative name")
    temporary = f".{name}.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("fd-owned JSON temporary is not a regular file")
        content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        while content:
            written = os.write(descriptor, content)
            content = content[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)


def write_bytes_once_atomic_at(directory_fd: int, name: str, content: bytes) -> None:
    """Publish immutable bytes relative to an already owned directory fd."""

    if Path(name).name != name:
        raise ValueError("fd-owned byte publication requires one safe relative name")
    temporary = f".{name}.{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
        dir_fd=directory_fd,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("fd-owned byte temporary is not a regular file")
        remaining = content
        while remaining:
            written = os.write(descriptor, remaining)
            remaining = remaining[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)


def rename_once_atomic_at(
    source_fd: int,
    source_name: str,
    destination_fd: int,
    destination_name: str,
) -> None:
    """Atomically rename one macOS directory without replacing a sibling."""

    if (
        sys.platform != "darwin"
        or Path(source_name).name != source_name
        or Path(destination_name).name != destination_name
    ):
        raise OSError("exclusive fd-owned rename requires safe macOS names")
    libc = CDLL(None, use_errno=True)
    rename = libc.renameatx_np
    rename.argtypes = [c_int, c_char_p, c_int, c_char_p, c_uint]
    rename.restype = c_int
    result = rename(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        RENAME_EXCL,
    )
    if result != 0:
        error = get_errno()
        raise OSError(error, os.strerror(error), destination_name)


def set_fd_flags(descriptor: int, flags: int) -> None:
    """Set Darwin file flags through an already owned descriptor."""

    if sys.platform != "darwin":
        raise OSError("immutable fd-owned publication requires macOS")
    libc = CDLL(None, use_errno=True)
    fchflags = libc.fchflags
    fchflags.argtypes = [c_int, c_uint]
    fchflags.restype = c_int
    if fchflags(descriptor, flags) != 0:
        error = get_errno()
        raise OSError(error, os.strerror(error))
