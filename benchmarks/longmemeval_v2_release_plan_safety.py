"""Filesystem safety helpers for sealed release-plan publication."""

from __future__ import annotations

import fcntl
import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

DARWIN_F_GETPATH = 50
DARWIN_PATH_BUFFER_SIZE = 1024


class DescriptorIdentity(Protocol):
    @property
    def device(self) -> int: ...

    @property
    def inode(self) -> int: ...

    @property
    def file_type(self) -> int: ...


@dataclass(frozen=True)
class OwnedPlanFile:
    descriptor: int
    identity: DescriptorIdentity
    mode: int
    size: int
    ctime_ns: int
    flags: int
    sha256: str


@dataclass
class OwnedPlanFileHolder:
    descriptor: int | None = None
    identity: DescriptorIdentity | None = None
    authority: OwnedPlanFile | None = None


def require_same_identity(
    identity: DescriptorIdentity,
    expected: DescriptorIdentity,
) -> None:
    if identity != expected:
        raise OSError("published plan target is not the renamed inode")


def descriptor_path(descriptor: int) -> Path | None:
    """Return the kernel-owned canonical path for one Darwin descriptor."""

    if sys.platform != "darwin":
        return None
    raw = fcntl.fcntl(descriptor, DARWIN_F_GETPATH, bytes(DARWIN_PATH_BUFFER_SIZE))
    path = Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    if not path.is_absolute():
        raise OSError("owned descriptor has no canonical path")
    return path


def stable_plan_file_read(
    descriptor: int,
    *,
    frozen: bool,
    frozen_mode: int,
    frozen_flags: int,
) -> tuple[os.stat_result, bytes]:
    before = os.fstat(descriptor)
    expected_mode = frozen_mode if frozen else 0o600
    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) != expected_mode:
        raise OSError("published plan file has unsafe type or mode")
    expected_flags = frozen_flags if frozen else 0
    if getattr(before, "st_flags", None) != expected_flags:
        raise OSError("published plan file has unsafe immutable state")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    remaining = before.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            raise OSError("published plan file was truncated during validation")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise OSError("published plan file grew during validation")
    after = os.fstat(descriptor)
    stable_fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_ctime_ns", "st_flags")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise OSError("published plan file changed during validation")
    return after, b"".join(chunks)


def discard_owned_plan_target(
    directory_fd: int,
    name: str,
    descriptor: int,
    identity: DescriptorIdentity,
    *,
    set_flags: Callable[[int, int], None],
) -> None:
    """Best-effort removal of only the exact plan inode owned by a failed writer."""

    try:
        metadata = os.fstat(descriptor)
        live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            metadata.st_dev != identity.device
            or metadata.st_ino != identity.inode
            or stat.S_IFMT(metadata.st_mode) != identity.file_type
            or live.st_dev != identity.device
            or live.st_ino != identity.inode
        ):
            return
        if getattr(metadata, "st_flags", 0):
            set_flags(descriptor, 0)
        os.fchmod(descriptor, 0o600)
        os.unlink(name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except BaseException:
        return
