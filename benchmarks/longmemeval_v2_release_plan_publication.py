"""Immutable fd-owned publication for sealed LongMemEval release plans."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from threading import current_thread, main_thread
from typing import Any, cast
from uuid import uuid4

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_plan_safety as plan_safety

PLAN_FILE_MODE = 0o400
PLAN_FILE_FLAGS = getattr(stat, "UF_IMMUTABLE", 0x00000002)
PLAN_DIRECTORY_MODE = 0o500
PLAN_DIRECTORY_FLAGS = PLAN_FILE_FLAGS
MINIMUM_OWNED_PARENT_CHAIN = 2


@dataclass(frozen=True)
class OwnedPathDirectory:
    descriptor: int
    identity: release_io.DescriptorIdentity
    path: Path


@contextmanager
def _defer_sigint() -> Iterator[None]:
    """Defer real SIGINT across raw-fd acquisition and ownership registration.

    Python dispatches process signals through the main-thread handler even when
    another thread receives the kernel signal. A tracing callback which raises
    ``KeyboardInterrupt`` directly is not a SIGINT and cannot be made atomic
    with a following Python bytecode store without moving the open into native
    code.
    """

    if current_thread() is not main_thread():
        yield
        return
    pending: list[tuple[int, Any]] = []

    def defer(signum: int, frame: Any) -> None:
        if not pending:
            pending.append((signum, frame))

    previous = signal.signal(signal.SIGINT, defer)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if pending and callable(previous):
            handler = cast("Callable[[int, Any], Any]", previous)
            handler(*pending[0])
        elif pending and previous == signal.SIG_DFL:
            signal.default_int_handler(*pending[0])


def _open_registered(
    holder: plan_safety.OwnedPlanFileHolder,
    path: str,
    flags: int,
    mode: int = 0o777,
    *,
    directory_fd: int | None = None,
) -> release_io.DescriptorIdentity:
    with _defer_sigint():
        descriptor = os.open(path, flags, mode, dir_fd=directory_fd)
        holder.descriptor = descriptor
        try:
            identity = release_io.capture_descriptor_identity(descriptor)
        except BaseException:
            holder.descriptor = None
            raise
        holder.identity = identity
        return identity


def _owned_directory(
    holder: plan_safety.OwnedPlanFileHolder,
    path: Path,
) -> OwnedPathDirectory:
    if holder.descriptor is None or holder.identity is None:
        raise OSError("plan directory acquisition lost its owned descriptor")
    if not isinstance(holder.identity, release_io.DescriptorIdentity):
        raise OSError("plan directory acquisition returned an invalid identity")
    return OwnedPathDirectory(holder.descriptor, holder.identity, path)


def _require_dedicated_parent(directories: list[OwnedPathDirectory]) -> None:
    if len(directories) < MINIMUM_OWNED_PARENT_CHAIN:
        raise OSError("stage plan requires a fresh dedicated parent directory")


def _require_descriptor(
    holder: plan_safety.OwnedPlanFileHolder,
    *,
    message: str,
) -> int:
    if holder.descriptor is None:
        raise OSError(message)
    return holder.descriptor


def _require_owned_path_directories(directories: list[OwnedPathDirectory]) -> None:
    for index, owned in enumerate(directories):
        metadata = os.fstat(owned.descriptor)
        if (
            metadata.st_dev != owned.identity.device
            or metadata.st_ino != owned.identity.inode
            or stat.S_IFMT(metadata.st_mode) != owned.identity.file_type
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            raise OSError("plan publication directory ownership changed")
        if (
            descriptor_path := plan_safety.descriptor_path(owned.descriptor)
        ) and descriptor_path != owned.path:
            raise OSError("plan publication directory location changed")
        if index:
            parent = directories[index - 1]
            live = os.stat(owned.path.name, dir_fd=parent.descriptor, follow_symlinks=False)
            if (
                live.st_dev != owned.identity.device
                or live.st_ino != owned.identity.inode
                or not stat.S_ISDIR(live.st_mode)
                or stat.S_ISLNK(live.st_mode)
            ):
                raise OSError("plan publication directory chain changed")


def _close_holders(
    holders: list[plan_safety.OwnedPlanFileHolder],
) -> BaseException | None:
    first_error: BaseException | None = None
    for holder in reversed(holders):
        if holder.descriptor is None or holder.identity is None:
            continue
        if not isinstance(holder.identity, release_io.DescriptorIdentity):
            continue
        error = release_io.close_owned_descriptor(holder.descriptor, holder.identity)
        if first_error is None and error is not None:
            first_error = error
    return first_error


def _remove_owned_empty_directory(
    parent: OwnedPathDirectory,
    owned: OwnedPathDirectory,
) -> None:
    try:
        live = os.stat(owned.path.name, dir_fd=parent.descriptor, follow_symlinks=False)
        if (
            live.st_dev != owned.identity.device
            or live.st_ino != owned.identity.inode
            or not stat.S_ISDIR(live.st_mode)
        ):
            return
        os.rmdir(owned.path.name, dir_fd=parent.descriptor)
        os.fsync(parent.descriptor)
    except BaseException:
        return


def _open_owned_plan_parent(
    target: Path,
) -> tuple[
    list[OwnedPathDirectory],
    list[plan_safety.OwnedPlanFileHolder],
]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directories: list[OwnedPathDirectory] = []
    holders: list[plan_safety.OwnedPlanFileHolder] = []
    try:
        root = plan_safety.OwnedPlanFileHolder()
        holders.append(root)
        _open_registered(root, target.anchor, flags)
        directories.append(_owned_directory(root, Path(target.anchor)))
        components = target.parent.parts[1:]
        for index, component in enumerate(components):
            parent = directories[-1]
            holder = plan_safety.OwnedPlanFileHolder()
            holders.append(holder)
            final_parent = index == len(components) - 1
            if final_parent:
                with _defer_sigint():
                    os.mkdir(component, mode=0o700, dir_fd=parent.descriptor)
                    try:
                        _open_registered(
                            holder,
                            component,
                            flags,
                            directory_fd=parent.descriptor,
                        )
                        directories.append(_owned_directory(holder, parent.path / component))
                    except BaseException:
                        with suppress(OSError):
                            os.rmdir(component, dir_fd=parent.descriptor)
                        raise
                continue
            else:
                with suppress(FileExistsError):
                    os.mkdir(component, mode=0o700, dir_fd=parent.descriptor)
                _open_registered(
                    holder,
                    component,
                    flags,
                    directory_fd=parent.descriptor,
                )
            directories.append(_owned_directory(holder, parent.path / component))
        _require_dedicated_parent(directories)
        _require_owned_path_directories(directories)
    except BaseException:
        if len(directories) >= MINIMUM_OWNED_PARENT_CHAIN:
            owned = directories[-1]
            if owned.path == target.parent:
                _remove_owned_empty_directory(directories[-2], owned)
        _close_holders(holders)
        raise
    return directories, holders


def _write_descriptor(descriptor: int, content: bytes) -> None:
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise OSError("fd-owned plan temporary is not a regular file")
    remaining = content
    while remaining:
        written = os.write(descriptor, remaining)
        remaining = remaining[written:]
    os.fsync(descriptor)


def _write_json_once_rename_atomic_at(
    directory_fd: int,
    name: str,
    payload: dict[str, Any],
    *,
    authority_holder: plan_safety.OwnedPlanFileHolder,
) -> bytes:
    if Path(name).name != name:
        raise ValueError("fd-owned JSON publication requires one safe relative name")
    if sys.platform != "darwin":
        raise OSError("owned release plan publication requires macOS")
    temporary = f".{name}.{uuid4().hex}.tmp"
    writer = plan_safety.OwnedPlanFileHolder()
    content = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    try:
        identity = _open_registered(
            writer,
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            directory_fd=directory_fd,
        )
        writer_descriptor = _require_descriptor(
            writer,
            message="plan temporary acquisition lost its descriptor",
        )
        _write_descriptor(writer_descriptor, content)
        release_io.rename_once_atomic_at(directory_fd, temporary, directory_fd, name)
        os.fsync(directory_fd)
        _open_owned_plan_file(
            directory_fd,
            name,
            expected=content,
            renamed_identity=identity,
            holder=authority_holder,
        )
        release_io._close_writer(writer_descriptor, identity)
        writer.descriptor = None
        writer.identity = None
    except BaseException:
        owned_descriptor = authority_holder.descriptor or writer.descriptor
        owned_identity = authority_holder.identity or writer.identity
        if owned_descriptor is not None and isinstance(
            owned_identity, release_io.DescriptorIdentity
        ):
            plan_safety.discard_owned_plan_target(
                directory_fd,
                name,
                owned_descriptor,
                owned_identity,
                set_flags=release_io.set_fd_flags,
            )
        raise
    finally:
        if writer.descriptor is not None and isinstance(
            writer.identity, release_io.DescriptorIdentity
        ):
            release_io.close_owned_descriptor(writer.descriptor, writer.identity)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
    return content


def _open_owned_plan_file(
    directory_fd: int,
    name: str,
    *,
    expected: bytes,
    renamed_identity: release_io.DescriptorIdentity,
    holder: plan_safety.OwnedPlanFileHolder,
) -> None:
    identity = _open_registered(
        holder,
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        directory_fd=directory_fd,
    )
    if holder.descriptor is None:
        raise OSError("plan authority acquisition lost its descriptor")
    plan_safety.require_same_identity(identity, renamed_identity)
    _, content = plan_safety.stable_plan_file_read(
        holder.descriptor,
        frozen=False,
        frozen_mode=PLAN_FILE_MODE,
        frozen_flags=PLAN_FILE_FLAGS,
    )
    if content != expected:
        raise OSError("published plan content changed")
    os.fchmod(holder.descriptor, PLAN_FILE_MODE)
    release_io.set_fd_flags(holder.descriptor, PLAN_FILE_FLAGS)
    os.fsync(holder.descriptor)
    metadata, content = plan_safety.stable_plan_file_read(
        holder.descriptor,
        frozen=True,
        frozen_mode=PLAN_FILE_MODE,
        frozen_flags=PLAN_FILE_FLAGS,
    )
    holder.authority = plan_safety.OwnedPlanFile(
        descriptor=holder.descriptor,
        identity=identity,
        mode=stat.S_IMODE(metadata.st_mode),
        size=metadata.st_size,
        ctime_ns=metadata.st_ctime_ns,
        flags=package_root.file_flags(metadata),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    _require_owned_plan_file(holder.authority, directory_fd, name, expected=expected)


def _require_owned_plan_file(
    authority: plan_safety.OwnedPlanFile,
    directory_fd: int,
    name: str,
    *,
    expected: bytes,
    expected_path: Path | None = None,
) -> None:
    metadata, content = plan_safety.stable_plan_file_read(
        authority.descriptor,
        frozen=True,
        frozen_mode=PLAN_FILE_MODE,
        frozen_flags=PLAN_FILE_FLAGS,
    )
    if (
        metadata.st_dev != authority.identity.device
        or metadata.st_ino != authority.identity.inode
        or stat.S_IFMT(metadata.st_mode) != authority.identity.file_type
        or stat.S_IMODE(metadata.st_mode) != authority.mode
        or metadata.st_size != authority.size
        or metadata.st_ctime_ns != authority.ctime_ns
        or package_root.file_flags(metadata) != authority.flags
        or hashlib.sha256(content).hexdigest() != authority.sha256
        or content != expected
    ):
        raise OSError("published plan authority changed")
    live = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        live.st_dev != authority.identity.device
        or live.st_ino != authority.identity.inode
        or stat.S_IFMT(live.st_mode) != authority.identity.file_type
        or stat.S_IMODE(live.st_mode) != authority.mode
        or live.st_size != authority.size
        or live.st_ctime_ns != authority.ctime_ns
        or getattr(live, "st_flags", None) != authority.flags
    ):
        raise OSError("published plan target identity changed")
    if (
        expected_path is not None
        and plan_safety.descriptor_path(authority.descriptor) != expected_path
    ):
        raise OSError("published plan target location changed")


def _freeze_plan_parent(parent: OwnedPathDirectory) -> None:
    os.fchmod(parent.descriptor, PLAN_DIRECTORY_MODE)
    release_io.set_fd_flags(parent.descriptor, PLAN_DIRECTORY_FLAGS)
    os.fsync(parent.descriptor)
    metadata = os.fstat(parent.descriptor)
    if (
        metadata.st_dev != parent.identity.device
        or metadata.st_ino != parent.identity.inode
        or stat.S_IMODE(metadata.st_mode) != PLAN_DIRECTORY_MODE
        or getattr(metadata, "st_flags", None) != PLAN_DIRECTORY_FLAGS
        or plan_safety.descriptor_path(parent.descriptor) != parent.path
    ):
        raise OSError("dedicated plan directory failed to become immutable")


def _thaw_plan_parent(parent: OwnedPathDirectory) -> None:
    with suppress(BaseException):
        metadata = os.fstat(parent.descriptor)
        if metadata.st_dev == parent.identity.device and metadata.st_ino == parent.identity.inode:
            if getattr(metadata, "st_flags", 0):
                release_io.set_fd_flags(parent.descriptor, 0)
            os.fchmod(parent.descriptor, 0o700)


def _discard_plan_publication(
    directories: list[OwnedPathDirectory],
    holder: plan_safety.OwnedPlanFileHolder,
    name: str,
    *,
    parent_frozen: bool,
) -> None:
    parent = directories[-1]
    if parent_frozen:
        _thaw_plan_parent(parent)
    if holder.authority is not None:
        plan_safety.discard_owned_plan_target(
            parent.descriptor,
            name,
            holder.authority.descriptor,
            holder.authority.identity,
            set_flags=release_io.set_fd_flags,
        )
    if len(directories) < MINIMUM_OWNED_PARENT_CHAIN:
        return
    _remove_owned_empty_directory(directories[-2], parent)


def _require_authority(holder: plan_safety.OwnedPlanFileHolder) -> plan_safety.OwnedPlanFile:
    if holder.authority is None:
        raise OSError("plan publisher returned without an owned authority")
    return holder.authority


def write_json_once_owned_path(path: Path, payload: dict[str, Any]) -> None:
    """Publish one immutable plan inside a fresh immutable parent directory."""

    if not path.is_absolute() or path.name in {"", ".", ".."}:
        raise ValueError("fd-owned JSON publication requires one canonical absolute target")
    directories, directory_holders = _open_owned_plan_parent(path)
    parent = directories[-1]
    holder = plan_safety.OwnedPlanFileHolder()
    primary_error: BaseException | None = None
    parent_frozen = False
    try:
        _require_owned_path_directories(directories)
        expected = _write_json_once_rename_atomic_at(
            parent.descriptor,
            path.name,
            payload,
            authority_holder=holder,
        )
        authority = _require_authority(holder)
        _require_owned_path_directories(directories)
        _freeze_plan_parent(parent)
        parent_frozen = True
        _require_owned_plan_file(
            authority,
            parent.descriptor,
            path.name,
            expected=expected,
            expected_path=path,
        )
        _require_owned_path_directories(directories)
        _require_owned_plan_file(
            authority,
            parent.descriptor,
            path.name,
            expected=expected,
            expected_path=path,
        )
    except BaseException as exc:
        primary_error = exc
        _discard_plan_publication(
            directories,
            holder,
            path.name,
            parent_frozen=parent_frozen,
        )
        raise
    finally:
        file_cleanup_error = None
        if holder.descriptor is not None and isinstance(
            holder.identity, release_io.DescriptorIdentity
        ):
            file_cleanup_error = release_io.close_owned_descriptor(
                holder.descriptor,
                holder.identity,
            )
        cleanup_error = _close_holders(directory_holders)
        if primary_error is None and (file_cleanup_error or cleanup_error):
            cause = file_cleanup_error or cleanup_error
            raise OSError("plan publication authority cleanup failed") from cause
