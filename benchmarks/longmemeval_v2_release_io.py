"""Atomic local file publication for LongMemEval-V2 release artifacts."""

from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import suppress
from ctypes import CDLL, c_char_p, c_int, c_uint, get_errno
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

RENAME_EXCL = 0x00000004


@dataclass(frozen=True)
class DescriptorIdentity:
    device: int
    inode: int
    file_type: int


def capture_descriptor_identity(descriptor: int) -> DescriptorIdentity:
    """Capture one newly opened descriptor or close it on interrupted acquisition."""

    try:
        metadata = os.fstat(descriptor)
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise
    return DescriptorIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        file_type=stat.S_IFMT(metadata.st_mode),
    )


def close_owned_descriptor(
    descriptor: int,
    identity: DescriptorIdentity,
) -> BaseException | None:
    """Close only when a descriptor still names its originally captured inode."""

    try:
        metadata = os.fstat(descriptor)
    except BaseException as exc:
        return exc
    if (
        metadata.st_dev != identity.device
        or metadata.st_ino != identity.inode
        or stat.S_IFMT(metadata.st_mode) != identity.file_type
    ):
        return OSError("descriptor ownership changed before cleanup")
    try:
        os.close(descriptor)
    except BaseException as exc:
        return exc
    return None


def _close_writer(descriptor: int, identity: DescriptorIdentity) -> None:
    if error := close_owned_descriptor(descriptor, identity):
        raise OSError("fd-owned temporary descriptor changed") from error


def _write_descriptor(descriptor: int, content: bytes, *, name: str) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise OSError(f"fd-owned {name} temporary is not a regular file")
    remaining = content
    while remaining:
        written = os.write(descriptor, remaining)
        remaining = remaining[written:]
    os.fsync(descriptor)


def _temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid4().hex}.tmp")


def _write_temporary(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary_path(path)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        identity = capture_descriptor_identity(descriptor)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    try:
        content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        _write_descriptor(descriptor, content, name="path JSON")
    except BaseException:
        close_owned_descriptor(descriptor, identity)
        temporary.unlink(missing_ok=True)
        raise
    else:
        try:
            _close_writer(descriptor, identity)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary


def _fsync_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    identity = capture_descriptor_identity(descriptor)
    try:
        os.fsync(descriptor)
    except BaseException:
        close_owned_descriptor(descriptor, identity)
        raise
    else:
        _close_writer(descriptor, identity)


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
        identity = capture_descriptor_identity(descriptor)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise
    try:
        try:
            content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
            _write_descriptor(descriptor, content, name="JSON")
        except BaseException:
            close_owned_descriptor(descriptor, identity)
            raise
        else:
            _close_writer(descriptor, identity)
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
        identity = capture_descriptor_identity(descriptor)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        raise
    try:
        try:
            _write_descriptor(descriptor, content, name="byte")
        except BaseException:
            close_owned_descriptor(descriptor, identity)
            raise
        else:
            _close_writer(descriptor, identity)
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
