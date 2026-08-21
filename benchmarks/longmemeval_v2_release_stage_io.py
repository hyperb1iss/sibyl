"""Descriptor-owned stage package transactions and frozen authorities."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks.longmemeval_v2_release_inputs import StagePlanError

FILE_MODE = 0o400
DIRECTORY_MODE = 0o500


@dataclass(frozen=True)
class DescriptorSnapshot:
    device: int
    inode: int
    file_type: int
    mode: int
    ctime_ns: int
    mtime_ns: int
    size: int
    flags: int


def snapshot_descriptor(descriptor: int) -> DescriptorSnapshot:
    metadata = os.fstat(descriptor)
    return DescriptorSnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        file_type=stat.S_IFMT(metadata.st_mode),
        mode=stat.S_IMODE(metadata.st_mode),
        ctime_ns=metadata.st_ctime_ns,
        mtime_ns=metadata.st_mtime_ns,
        size=metadata.st_size,
        flags=metadata.st_flags,
    )


def open_directory(path: Path, *, name: str) -> tuple[int, DescriptorSnapshot]:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StagePlanError(f"{name} could not be opened safely") from exc
    identity = release_io.capture_descriptor_identity(descriptor)
    try:
        opened = _opened_directory_snapshot(path, descriptor, identity, name=name)
    except BaseException:
        release_io.close_owned_descriptor(descriptor, identity)
        raise
    else:
        return descriptor, opened


def _opened_directory_snapshot(
    path: Path,
    descriptor: int,
    identity: release_io.DescriptorIdentity,
    *,
    name: str,
) -> DescriptorSnapshot:
    opened = snapshot_descriptor(descriptor)
    live = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(live.st_mode)
        or live.st_dev != identity.device
        or live.st_ino != identity.inode
        or opened.file_type != identity.file_type
    ):
        raise StagePlanError(f"{name} identity changed")
    return opened


def open_child_directory(parent_fd: int, name: str) -> tuple[int, DescriptorSnapshot]:
    descriptor = package_root._open_child_directory(parent_fd, name)
    identity = release_io.capture_descriptor_identity(descriptor)
    try:
        return descriptor, snapshot_descriptor(descriptor)
    except BaseException:
        release_io.close_owned_descriptor(descriptor, identity)
        raise


def require_path_identity(
    path: Path,
    descriptor: int,
    expected: DescriptorSnapshot,
    *,
    name: str,
) -> None:
    current = snapshot_descriptor(descriptor)
    live = path.stat(follow_symlinks=False)
    if (
        current.device != expected.device
        or current.inode != expected.inode
        or current.file_type != expected.file_type
        or (live.st_dev, live.st_ino) != (expected.device, expected.inode)
        or not stat.S_ISDIR(live.st_mode)
    ):
        raise StagePlanError(f"{name} identity changed")


def close_owned_descriptor(
    descriptor: int,
    expected: DescriptorSnapshot,
    *,
    name: str,
) -> BaseException | None:
    try:
        current = snapshot_descriptor(descriptor)
    except BaseException as exc:
        return exc
    if (
        current.device != expected.device
        or current.inode != expected.inode
        or current.file_type != expected.file_type
    ):
        return StagePlanError(f"{name} descriptor ownership changed")
    try:
        os.close(descriptor)
    except BaseException as exc:
        return exc
    return None


def json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


@dataclass
class _FrozenFile:
    relative: str
    descriptor: int
    snapshot: DescriptorSnapshot
    content: bytes


@dataclass
class FrozenStageAuthority:
    output_root: Path
    output_fd: int
    output_snapshot: DescriptorSnapshot
    package_fd: int
    package_snapshot: DescriptorSnapshot
    directories: dict[str, tuple[int, DescriptorSnapshot]]
    files: dict[str, _FrozenFile]
    receipt: _FrozenFile | None
    closed: bool = False

    def content(self, relative: str) -> bytes:
        try:
            return self.files[relative].content
        except KeyError as exc:
            raise StagePlanError(f"stage package artifact {relative!r} is missing") from exc

    def json(self, relative: str) -> dict[str, Any]:
        try:
            raw = json.loads(self.content(relative))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StagePlanError(f"stage package artifact {relative!r} is invalid JSON") from exc
        if not isinstance(raw, dict):
            raise StagePlanError(f"stage package artifact {relative!r} is not an object")
        return raw

    def binding(self, relative: str) -> dict[str, Any]:
        return package_tree.bind_owned_content(
            self.content(relative),
            path=self.output_root / "packages" / relative,
        )

    def receipt_json(self) -> dict[str, Any]:
        if self.receipt is None:
            raise StagePlanError("stage receipt authority is missing")
        try:
            raw = json.loads(self.receipt.content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StagePlanError("stage receipt authority is invalid JSON") from exc
        if not isinstance(raw, dict):
            raise StagePlanError("stage receipt authority is not an object")
        return raw

    def receipt_binding(self) -> dict[str, Any]:
        if self.receipt is None:
            raise StagePlanError("stage receipt authority is missing")
        return package_tree.bind_owned_content(
            self.receipt.content,
            path=self.output_root / "stage_receipt.json",
        )

    def require_inventory(self, files: set[str], directories: set[str]) -> None:
        if set(self.files) != files or set(self.directories) != directories:
            raise StagePlanError("frozen stage package inventory is not exact")

    def _directory_metadata_changed(self) -> bool:
        return snapshot_descriptor(self.package_fd) != self.package_snapshot or any(
            snapshot_descriptor(descriptor) != expected
            for descriptor, expected in self.directories.values()
        )

    def _require_files_unchanged(self) -> None:
        handles = list(self.files.values())
        if self.receipt is not None:
            handles.append(self.receipt)
        for handle in handles:
            package_root.require_frozen_descriptor(
                handle.descriptor,
                mode=FILE_MODE,
                name=f"stage package file {handle.relative}",
            )
            if snapshot_descriptor(handle.descriptor) != handle.snapshot:
                raise StagePlanError("stage package file changed")
            os.lseek(handle.descriptor, 0, os.SEEK_SET)
            blocks: list[bytes] = []
            while block := os.read(handle.descriptor, 1024 * 1024):
                blocks.append(block)
            if b"".join(blocks) != handle.content:
                raise StagePlanError("stage package file content changed")

    def require_unchanged(self) -> None:
        require_path_identity(
            self.output_root,
            self.output_fd,
            self.output_snapshot,
            name="claimed stage output root",
        )
        paths = [("packages", self.package_snapshot)]
        if self.receipt is not None:
            paths.append(("stage_receipt.json", self.receipt.snapshot))
        for name, expected in paths:
            live = os.stat(name, dir_fd=self.output_fd, follow_symlinks=False)
            if (live.st_dev, live.st_ino) != (expected.device, expected.inode):
                raise StagePlanError("stage package authority path changed")
        package_root.require_frozen_descriptor(
            self.package_fd,
            mode=DIRECTORY_MODE,
            name="stage package root",
        )
        if snapshot_descriptor(self.package_fd) != self.package_snapshot:
            raise StagePlanError("stage package root changed")
        current_files, current_directories = package_tree._read_tree_at(self.package_fd)
        if current_files != {
            relative: handle.content for relative, handle in self.files.items()
        } or set(current_directories) != set(self.directories):
            raise StagePlanError("stage package inventory changed")
        for relative, (descriptor, expected) in self.directories.items():
            package_root.require_frozen_descriptor(
                descriptor,
                mode=DIRECTORY_MODE,
                name=f"stage package directory {relative}",
            )
            if snapshot_descriptor(descriptor) != expected:
                raise StagePlanError("stage package directory changed")
        self._require_files_unchanged()
        if self._directory_metadata_changed():
            raise StagePlanError("stage package directory changed")

    def close(self) -> None:
        if self.closed:
            return
        first_error: BaseException | None = None
        handles = list(self.files.values())
        if self.receipt is not None:
            handles.append(self.receipt)
        for handle in handles:
            error = close_owned_descriptor(
                handle.descriptor,
                handle.snapshot,
                name=f"stage package file {handle.relative}",
            )
            if first_error is None and error is not None:
                first_error = error
        for relative, (descriptor, expected) in reversed(tuple(self.directories.items())):
            error = close_owned_descriptor(
                descriptor,
                expected,
                name=f"stage package directory {relative}",
            )
            if first_error is None and error is not None:
                first_error = error
        for descriptor, expected, name in (
            (self.package_fd, self.package_snapshot, "stage package root"),
            (self.output_fd, self.output_snapshot, "claimed stage output root"),
        ):
            error = close_owned_descriptor(descriptor, expected, name=name)
            if first_error is None and error is not None:
                first_error = error
        self.closed = True
        if first_error is not None:
            if isinstance(first_error, Exception):
                raise StagePlanError("stage package authority cleanup failed") from first_error
            raise first_error

    def __enter__(self) -> FrozenStageAuthority:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _read_frozen_file(parent_fd: int, name: str, *, relative: str) -> _FrozenFile:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    identity = release_io.capture_descriptor_identity(descriptor)
    try:
        package_root.require_frozen_descriptor(
            descriptor,
            mode=FILE_MODE,
            name=f"stage package file {relative}",
        )
        snapshot = snapshot_descriptor(descriptor)
        blocks: list[bytes] = []
        while block := os.read(descriptor, 1024 * 1024):
            blocks.append(block)
        content = b"".join(blocks)
        _require_frozen_content(descriptor, snapshot, content)
        return _FrozenFile(relative, descriptor, snapshot, content)
    except BaseException:
        release_io.close_owned_descriptor(descriptor, identity)
        raise


def _require_frozen_content(
    descriptor: int,
    snapshot: DescriptorSnapshot,
    content: bytes,
) -> None:
    if snapshot_descriptor(descriptor) != snapshot or len(content) != snapshot.size:
        raise StagePlanError("stage package file changed while opening authority")


def _open_frozen_tree(
    parent_fd: int,
    *,
    prefix: str = "",
) -> tuple[dict[str, tuple[int, DescriptorSnapshot]], dict[str, _FrozenFile]]:
    directories: dict[str, tuple[int, DescriptorSnapshot]] = {}
    files: dict[str, _FrozenFile] = {}
    try:
        for entry in sorted(os.scandir(parent_fd), key=lambda item: item.name):
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            metadata = entry.stat(follow_symlinks=False)
            kind = _frozen_entry_kind(metadata)
            if kind == "directory":
                descriptor, snapshot = open_child_directory(parent_fd, entry.name)
                directories[relative] = (descriptor, snapshot)
                package_root.require_frozen_descriptor(
                    descriptor,
                    mode=DIRECTORY_MODE,
                    name=f"stage package directory {relative}",
                )
                child_directories, child_files = _open_frozen_tree(
                    descriptor,
                    prefix=relative,
                )
                directories.update(child_directories)
                files.update(child_files)
            else:
                handle = _read_frozen_file(parent_fd, entry.name, relative=relative)
                files[relative] = handle
    except BaseException:
        _close_frozen_tree(directories, files)
        raise
    return directories, files


def _frozen_entry_kind(metadata: os.stat_result) -> str:
    if stat.S_ISLNK(metadata.st_mode):
        raise StagePlanError("frozen stage package contains a symlink")
    if stat.S_ISDIR(metadata.st_mode):
        return "directory"
    if stat.S_ISREG(metadata.st_mode):
        return "file"
    raise StagePlanError("frozen stage package contains a foreign entry")


def _close_frozen_tree(
    directories: dict[str, tuple[int, DescriptorSnapshot]],
    files: dict[str, _FrozenFile],
) -> None:
    for handle in files.values():
        close_owned_descriptor(
            handle.descriptor,
            handle.snapshot,
            name=f"stage package file {handle.relative}",
        )
    for relative, (descriptor, expected) in reversed(tuple(directories.items())):
        close_owned_descriptor(
            descriptor,
            expected,
            name=f"stage package directory {relative}",
        )


def _open_frozen_authority(
    output_root: Path,
    *,
    require_receipt: bool,
) -> FrozenStageAuthority:
    output_fd, output_snapshot = open_directory(output_root, name="claimed stage output root")
    package_fd: int | None = None
    package_snapshot: DescriptorSnapshot | None = None
    receipt: _FrozenFile | None = None
    directories: dict[str, tuple[int, DescriptorSnapshot]] = {}
    files: dict[str, _FrozenFile] = {}
    try:
        package_fd, package_snapshot = open_child_directory(output_fd, "packages")
        package_root.require_frozen_descriptor(
            package_fd,
            mode=DIRECTORY_MODE,
            name="stage package root",
        )
        directories, files = _open_frozen_tree(package_fd)
        if require_receipt:
            receipt = _read_frozen_file(
                output_fd,
                "stage_receipt.json",
                relative="stage_receipt.json",
            )
        return FrozenStageAuthority(
            output_root=output_root,
            output_fd=output_fd,
            output_snapshot=output_snapshot,
            package_fd=package_fd,
            package_snapshot=package_snapshot,
            directories=directories,
            files=files,
            receipt=receipt,
        )
    except BaseException:
        _close_frozen_tree(directories, files)
        if receipt is not None:
            close_owned_descriptor(
                receipt.descriptor,
                receipt.snapshot,
                name="stage receipt authority",
            )
        if package_fd is not None and package_snapshot is not None:
            close_owned_descriptor(
                package_fd,
                package_snapshot,
                name="stage package root",
            )
        close_owned_descriptor(
            output_fd,
            output_snapshot,
            name="claimed stage output root",
        )
        raise


def open_frozen_package_authority(output_root: Path) -> FrozenStageAuthority:
    """Retain an immutable package tree before its receipt is published."""

    return _open_frozen_authority(output_root, require_receipt=False)


def open_frozen_stage_authority(output_root: Path) -> FrozenStageAuthority:
    """Retain the immutable package tree and top-level receipt together."""

    return _open_frozen_authority(output_root, require_receipt=True)
