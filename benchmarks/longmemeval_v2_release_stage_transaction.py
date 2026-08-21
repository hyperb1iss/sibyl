"""Descriptor-owned construction and recovery of a stage package tree."""

from __future__ import annotations

import json
import os
import stat
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks import longmemeval_v2_release_stage_io as stage_io
from benchmarks.longmemeval_v2_release_inputs import StagePlanError

FILE_MODE = 0o400
DIRECTORY_MODE = 0o500


def _safe_relative(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if (
        path.is_absolute()
        or len(path.parts) not in {1, 2}
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise StagePlanError("stage package artifact path is unsafe")
    return path.parts


@dataclass
class PendingStageTransaction:
    output_root: Path
    name: str
    output_fd: int
    output_snapshot: stage_io.DescriptorSnapshot
    root_fd: int
    root_snapshot: stage_io.DescriptorSnapshot
    children: dict[str, tuple[int, stage_io.DescriptorSnapshot]] = field(default_factory=dict)
    published: bool = False
    closed: bool = False

    @property
    def path(self) -> Path:
        return self.output_root / ("packages" if self.published else self.name)

    def _require(self) -> None:
        if self.closed:
            raise StagePlanError("stage package transaction is closed")
        stage_io.require_path_identity(
            self.output_root,
            self.output_fd,
            self.output_snapshot,
            name="claimed stage output root",
        )
        live = os.stat(
            "packages" if self.published else self.name,
            dir_fd=self.output_fd,
            follow_symlinks=False,
        )
        if (live.st_dev, live.st_ino) != (
            self.root_snapshot.device,
            self.root_snapshot.inode,
        ):
            raise StagePlanError("stage package transaction identity changed")
        current = stage_io.snapshot_descriptor(self.root_fd)
        if (current.device, current.inode) != (
            self.root_snapshot.device,
            self.root_snapshot.inode,
        ):
            raise StagePlanError("stage package transaction descriptor changed")

    def ensure_directory(self, name: str) -> int:
        if Path(name).name != name:
            raise StagePlanError("stage package directory name is unsafe")
        self._require()
        existing = self.children.get(name)
        if existing is not None:
            descriptor, expected = existing
            current = stage_io.snapshot_descriptor(descriptor)
            if (current.device, current.inode) != (expected.device, expected.inode):
                raise StagePlanError("stage package child descriptor changed")
            return descriptor
        try:
            os.mkdir(name, mode=0o700, dir_fd=self.root_fd)
            os.fsync(self.root_fd)
        except FileExistsError:
            pass
        descriptor, expected = stage_io.open_child_directory(self.root_fd, name)
        try:
            _require_opened_directory(
                self.root_fd,
                name,
                expected,
                error="stage package child identity changed",
            )
        except BaseException:
            stage_io.close_owned_descriptor(
                descriptor,
                expected,
                name="stage package child",
            )
            raise
        self.children[name] = (descriptor, expected)
        return descriptor

    def _parent(self, relative: str) -> tuple[int, str]:
        parts = _safe_relative(relative)
        if len(parts) == 1:
            return self.root_fd, parts[0]
        return self.ensure_directory(parts[0]), parts[1]

    def write_json(self, relative: str, payload: dict[str, Any]) -> None:
        self._require()
        parent_fd, name = self._parent(relative)
        expected = stage_io.json_bytes(payload)
        with suppress(FileExistsError):
            release_io.write_json_once_atomic_at(parent_fd, name, payload)
        if package_tree.read_owned_file(parent_fd, name) != expected:
            raise StagePlanError("stage package artifact differs from its transaction")
        self._require()

    def content(self, relative: str) -> bytes:
        self._require()
        parent_fd, name = self._parent(relative)
        content = package_tree.read_owned_file(parent_fd, name)
        self._require()
        return content

    def json(self, relative: str) -> dict[str, Any]:
        try:
            raw = json.loads(self.content(relative))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StagePlanError(f"stage package artifact {relative!r} is invalid JSON") from exc
        if not isinstance(raw, dict):
            raise StagePlanError(f"stage package artifact {relative!r} is not an object")
        return raw

    def binding(self, relative: str, *, public: bool = True) -> dict[str, Any]:
        root_name = "packages" if public else self.name
        path = self.output_root / root_name / relative
        return package_tree.bind_owned_content(self.content(relative), path=path)

    def inventory(self) -> tuple[set[str], set[str]]:
        self._require()
        files, directories = package_tree._read_tree_at(self.root_fd)
        return set(files), set(directories)

    def require_inventory(self, files: set[str], directories: set[str]) -> None:
        if self.inventory() != (files, directories):
            raise StagePlanError("stage package transaction inventory is not exact")

    def publish(self) -> Path:
        self._require()
        package_tree.freeze_publication_children(
            self.root_fd,
            file_mode=FILE_MODE,
            directory_mode=DIRECTORY_MODE,
        )
        self._require()
        try:
            release_io.rename_once_atomic_at(
                self.output_fd,
                self.name,
                self.output_fd,
                "packages",
            )
            os.fsync(self.output_fd)
        except OSError as exc:
            raise StagePlanError("stage package publication failed") from exc
        self.published = True
        self.finish_publication()
        return self.path

    def finish_publication(self) -> None:
        if not self.published:
            raise StagePlanError("stage package transaction is not public")
        self._require()
        package_root.freeze_descriptor(
            self.root_fd,
            mode=DIRECTORY_MODE,
            name="stage package root",
        )
        self._require()

    def close(self) -> None:
        if self.closed:
            return
        first_error: BaseException | None = None
        for name, (descriptor, expected) in reversed(tuple(self.children.items())):
            error = stage_io.close_owned_descriptor(
                descriptor,
                expected,
                name=f"stage package directory {name}",
            )
            if first_error is None and error is not None:
                first_error = error
        for descriptor, expected, name in (
            (self.root_fd, self.root_snapshot, "stage package transaction"),
            (self.output_fd, self.output_snapshot, "claimed stage output root"),
        ):
            error = stage_io.close_owned_descriptor(descriptor, expected, name=name)
            if first_error is None and error is not None:
                first_error = error
        self.closed = True
        if first_error is not None:
            if isinstance(first_error, Exception):
                raise StagePlanError("stage package descriptor cleanup failed") from first_error
            raise first_error

    def __enter__(self) -> PendingStageTransaction:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _pending_name(output_fd: int, prefix: str) -> str:
    entries = sorted(entry.name for entry in os.scandir(output_fd) if entry.name.startswith(prefix))
    if len(entries) > 1:
        raise StagePlanError("multiple package transaction prefixes exist")
    if not entries:
        name = f"{prefix}{uuid4()}"
        os.mkdir(name, mode=0o700, dir_fd=output_fd)
        os.fsync(output_fd)
        return name
    name = entries[0]
    try:
        UUID(name.removeprefix(prefix))
    except ValueError as exc:
        raise StagePlanError("package transaction prefix is invalid") from exc
    return name


def _open_root(
    output_fd: int,
    name: str,
) -> tuple[int, stage_io.DescriptorSnapshot]:
    descriptor, snapshot = stage_io.open_child_directory(output_fd, name)
    try:
        _require_opened_directory(
            output_fd,
            name,
            snapshot,
            error="stage package transaction identity changed",
        )
    except BaseException:
        stage_io.close_owned_descriptor(descriptor, snapshot, name="stage package transaction")
        raise
    return descriptor, snapshot


def _require_opened_directory(
    parent_fd: int,
    name: str,
    snapshot: stage_io.DescriptorSnapshot,
    *,
    error: str,
) -> None:
    live = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if (
        not stat.S_ISDIR(live.st_mode)
        or live.st_dev != snapshot.device
        or live.st_ino != snapshot.inode
        or stat.S_IFMT(live.st_mode) != snapshot.file_type
    ):
        raise StagePlanError(error)


def open_pending_transaction(
    output_root: Path,
    *,
    prefix: str,
) -> PendingStageTransaction:
    output_fd, output_snapshot = stage_io.open_directory(
        output_root,
        name="claimed stage output root",
    )
    try:
        name = _pending_name(output_fd, prefix)
        root_fd, root_snapshot = _open_root(output_fd, name)
    except BaseException:
        stage_io.close_owned_descriptor(
            output_fd,
            output_snapshot,
            name="claimed stage output root",
        )
        raise
    return PendingStageTransaction(
        output_root=output_root,
        name=name,
        output_fd=output_fd,
        output_snapshot=output_snapshot,
        root_fd=root_fd,
        root_snapshot=root_snapshot,
    )


def open_published_transaction(output_root: Path) -> PendingStageTransaction:
    """Retain a visible package inode so interrupted root freezing can resume."""

    output_fd, output_snapshot = stage_io.open_directory(
        output_root,
        name="claimed stage output root",
    )
    try:
        _require_no_pending_transaction(output_fd)
        root_fd, root_snapshot = _open_root(output_fd, "packages")
    except BaseException:
        stage_io.close_owned_descriptor(
            output_fd,
            output_snapshot,
            name="claimed stage output root",
        )
        raise
    return PendingStageTransaction(
        output_root=output_root,
        name="packages",
        output_fd=output_fd,
        output_snapshot=output_snapshot,
        root_fd=root_fd,
        root_snapshot=root_snapshot,
        published=True,
    )


def _require_no_pending_transaction(output_fd: int) -> None:
    if any(entry.name.startswith("packages.pending.") for entry in os.scandir(output_fd)):
        raise StagePlanError("public stage package conflicts with a pending transaction")
