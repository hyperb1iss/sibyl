"""Exact fd-owned trees for LongMemEval-V2 package transactions."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks.longmemeval_v2_release_inputs import StagePlanError


def read_owned_directory(
    owner: package_root.OwnedDirectory,
    descriptor: int,
) -> tuple[dict[str, bytes], frozenset[str]]:
    """Read stable file and directory inventories through an owned descriptor."""

    package_root._require_descriptor_identity(
        descriptor,
        owner,
        name="official package directory",
    )
    before = _read_tree_at(descriptor)
    package_root._require_descriptor_identity(
        descriptor,
        owner,
        name="official package directory",
    )
    after = _read_tree_at(descriptor)
    if before != after:
        raise StagePlanError("official package directory changed during inventory")
    return before


def _read_regular_at(directory_fd: int, name: str, metadata: os.stat_result) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise StagePlanError(f"official package file {name!r} is unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
            metadata.st_dev,
            metadata.st_ino,
        ):
            raise StagePlanError("official package file identity changed")
        blocks: list[bytes] = []
        while block := os.read(descriptor, 1024 * 1024):
            blocks.append(block)
        content = b"".join(blocks)
        after = os.fstat(descriptor)
        if (
            before.st_size != len(content)
            or after.st_size != len(content)
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise StagePlanError("official package file changed while being read")
        return content
    finally:
        os.close(descriptor)


def _read_tree_at(
    directory_fd: int,
    *,
    prefix: str = "",
) -> tuple[dict[str, bytes], frozenset[str]]:
    try:
        entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
    except OSError as exc:
        raise StagePlanError("official package directory is unreadable") from exc
    files: dict[str, bytes] = {}
    directories: set[str] = set()
    for entry in entries:
        if entry.name in {".", ".."} or "/" in entry.name:
            raise StagePlanError("official package entry name is unsafe")
        metadata = entry.stat(follow_symlinks=False)
        relative = f"{prefix}/{entry.name}" if prefix else entry.name
        if stat.S_ISLNK(metadata.st_mode):
            raise StagePlanError("official package contains a symlink")
        if stat.S_ISDIR(metadata.st_mode):
            directories.add(relative)
            child_fd = package_root._open_child_directory(directory_fd, entry.name)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise StagePlanError("official package directory identity changed")
                child_files, child_directories = _read_tree_at(child_fd, prefix=relative)
                files.update(child_files)
                directories.update(child_directories)
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            files[relative] = _read_regular_at(directory_fd, entry.name, metadata)
        else:
            raise StagePlanError("official package contains a foreign entry")
    return files, frozenset(directories)


def read_owned_tree(lease: package_root.PackageLease) -> dict[str, bytes]:
    """Read one exact symlink-free staging tree through its pinned descriptor."""

    package_root.require_lease(lease)
    before = _read_tree_at(lease.arm_fd)
    package_root.require_lease(lease)
    after = _read_tree_at(lease.arm_fd)
    if before != after:
        raise StagePlanError("official package changed during owned inventory")
    files, directories = before
    expected_directories = {
        parent.as_posix()
        for name in files
        for parent in PurePosixPath(name).parents
        if parent != PurePosixPath(".")
    }
    if directories != expected_directories:
        raise StagePlanError("official package directory inventory is not exact")
    return files


def _remove_tree_at(directory_fd: int) -> None:
    entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
    for entry in entries:
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            child_fd = package_root._open_child_directory(directory_fd, entry.name)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                    raise StagePlanError("official package changed during cleanup")
                _remove_tree_at(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(entry.name, dir_fd=directory_fd)
        else:
            os.unlink(entry.name, dir_fd=directory_fd)


def clear_staging_tree(lease: package_root.PackageLease) -> None:
    """Empty one staging inode while retaining its owned directory descriptor."""

    package_root.retire_package_children(lease)
    _remove_tree_at(lease.arm_fd)
    files, directories = _read_tree_at(lease.arm_fd)
    if files or directories:
        raise StagePlanError("official staging package could not be cleared")
    os.fsync(lease.arm_fd)


def discard_staging(lease: package_root.PackageLease) -> None:
    """Remove only the descriptor-owned unpublished staging tree."""

    package_root.require_lease(lease)
    _remove_tree_at(lease.arm_fd)
    package_root.require_lease_descriptors(lease)
    os.rmdir(lease.arm.path.name, dir_fd=lease.staging_parent_fd)
    os.fsync(lease.staging_parent_fd)


def read_owned_file(
    directory_fd: int,
    name: str,
    *,
    expected_mode: int | None = None,
) -> bytes:
    """Read one regular, no-follow file relative to an owned directory fd."""

    if Path(name).name != name:
        raise StagePlanError("fd-owned package file name is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise StagePlanError(f"fd-owned package file {name!r} is missing") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise StagePlanError(f"fd-owned package file {name!r} is not regular")
        if expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode:
            raise StagePlanError(f"fd-owned package file {name!r} mode changed")
        blocks: list[bytes] = []
        while block := os.read(descriptor, 1024 * 1024):
            blocks.append(block)
        content = b"".join(blocks)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or not stat.S_ISREG(after.st_mode)
            or stat.S_IMODE(before.st_mode) != stat.S_IMODE(after.st_mode)
            or before.st_ctime_ns != after.st_ctime_ns
            or before.st_size != len(content)
            or after.st_size != len(content)
            or (expected_mode is not None and stat.S_IMODE(after.st_mode) != expected_mode)
        ):
            raise StagePlanError(f"fd-owned package file {name!r} changed while being read")
        return content
    finally:
        os.close(descriptor)


def bind_owned_file(directory_fd: int, name: str, *, path: Path) -> dict[str, Any]:
    """Bind bytes through an owned fd while retaining their canonical public path."""

    return bind_owned_content(read_owned_file(directory_fd, name), path=path)


def bind_owned_content(content: bytes, *, path: Path) -> dict[str, Any]:
    """Build one public artifact binding for bytes read through an owned fd."""

    return {
        "path": str(path),
        "sha256": f"sha256:{hashlib.sha256(content).hexdigest()}",
        "size_bytes": len(content),
    }
