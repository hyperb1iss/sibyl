"""Deterministic content-addressed publication for official arm packages."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    require_exact_keys,
)
from tools.bench import longmemeval_v2_rig as rig

PUBLICATION_SCHEMA_VERSION = "sibyl-longmemeval-v2-arm-publication-v1"
PUBLICATION_KEYS = frozenset(
    {
        "schema_version",
        "stage_plan_sha256",
        "arm_id",
        "status",
        "package_object",
        "package_manifest_sha256",
        "executed_status",
        "arm_run",
        "arm_package",
        "actual_cost_usd",
        "publication_receipt_sha256",
    }
)
OBJECT_FILE_MODE = 0o400
AUTHORITY_FILE_MODE = 0o400
AUTHORITY_DIRECTORY_MODE = 0o500
AUTHORITY_NAME = "authority.json"


@dataclass(frozen=True)
class AuthoritySnapshot:
    device: int
    inode: int
    mode: int
    ctime_ns: int
    mtime_ns: int
    size: int
    flags: int


@dataclass(frozen=True)
class FileSnapshot:
    device: int
    inode: int
    mode: int
    ctime_ns: int
    mtime_ns: int
    size: int
    flags: int


@dataclass
class AuthorityHandle:
    directory: AuthoritySnapshot
    authority_fd: int
    authority: FileSnapshot
    authority_content: bytes
    object_fd: int
    package_object: FileSnapshot
    object_content: bytes

    def close(self) -> None:
        first_error: BaseException | None = None
        for descriptor, snapshot in (
            (self.object_fd, self.package_object),
            (self.authority_fd, self.authority),
        ):
            error = _close_frozen_file(descriptor, snapshot)
            if first_error is None and error is not None:
                first_error = error
        if first_error is not None:
            if isinstance(first_error, Exception):
                raise StagePlanError("official arm file descriptor cleanup failed") from first_error
            raise first_error


def _close_frozen_file(
    descriptor: int,
    snapshot: FileSnapshot,
) -> BaseException | None:
    try:
        current = os.fstat(descriptor)
    except BaseException as exc:
        return exc
    if (current.st_dev, current.st_ino) != (snapshot.device, snapshot.inode):
        return StagePlanError("official arm file descriptor ownership changed")
    try:
        os.close(descriptor)
    except BaseException as exc:
        return exc
    return None


def publication_path(root: Path, arm_id: str) -> Path:
    return root / "arms" / arm_id / AUTHORITY_NAME


def object_path(root: Path, arm_id: str, sha256: str) -> Path:
    digest = sha256.removeprefix("sha256:")
    return root / "arms" / arm_id / f"{digest}.tar.gz"


def object_binding(root: Path, arm_id: str, content: bytes) -> dict[str, Any]:
    """Bind one future content-addressed object below its immutable arm root."""

    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    return package_tree.bind_owned_content(
        content,
        path=object_path(root, arm_id, digest),
    )


def publish_arm_authority(
    lease: package_root.PackageLease,
    *,
    arm_id: str,
    content: bytes,
    receipt: dict[str, Any],
) -> None:
    """Freeze and exclusively rename one immutable two-file arm authority."""

    require_publication_receipt(receipt)
    binding = object_binding(lease.parent.path, arm_id, content)
    if receipt.get("package_object") != binding:
        raise StagePlanError("official arm publication object binding changed")
    package_tree.clear_staging_tree(lease)
    object_name = Path(binding["path"]).name
    release_io.write_bytes_once_atomic_at(lease.arm_fd, object_name, content)
    release_io.write_json_once_atomic_at(lease.arm_fd, AUTHORITY_NAME, receipt)
    for name in (object_name, AUTHORITY_NAME):
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=lease.arm_fd,
        )
        try:
            package_root.freeze_descriptor(
                descriptor,
                mode=OBJECT_FILE_MODE,
                name=f"official arm authority file {name}",
            )
        finally:
            os.close(descriptor)
    os.fsync(lease.arm_fd)
    files, directories = package_tree.read_owned_directory(lease.arm, lease.arm_fd)
    if set(files) != {AUTHORITY_NAME, object_name} or directories:
        raise StagePlanError("official arm authority inventory is not exact")
    if (
        package_tree.read_owned_file(
            lease.arm_fd,
            AUTHORITY_NAME,
            expected_mode=AUTHORITY_FILE_MODE,
        )
        != files[AUTHORITY_NAME]
        or package_tree.read_owned_file(
            lease.arm_fd,
            object_name,
            expected_mode=OBJECT_FILE_MODE,
        )
        != content
    ):
        raise StagePlanError("official arm authority content changed")
    package_root.close_staging_for_rename(lease)
    try:
        release_io.rename_once_atomic_at(
            lease.staging_parent_fd,
            lease.arm.path.name,
            lease.arms_fd,
            arm_id,
        )
        published_owner, published_fd = package_root.open_owned_child(
            lease.arms,
            lease.arms_fd,
            arm_id,
            create=False,
        )
        try:
            if (published_owner.device, published_owner.inode) != (
                lease.arm.device,
                lease.arm.inode,
            ):
                raise StagePlanError("published arm authority inode changed")
            package_root.freeze_descriptor(
                published_fd,
                mode=AUTHORITY_DIRECTORY_MODE,
                name="published arm authority",
            )
            _authority_snapshot(published_fd)
        finally:
            published_error = package_root.close_owned_directory(
                published_fd,
                published_owner,
                name="published arm authority",
            )
            if published_error is not None:
                raise StagePlanError(
                    "published arm descriptor ownership changed"
                ) from published_error
        os.fsync(lease.arms_fd)
        os.fsync(lease.parent_fd)
    except OSError as exc:
        raise StagePlanError("official arm authority publication failed") from exc


def build_publication_receipt(
    *,
    stage_plan_sha256: str,
    arm_id: str,
    package_object: dict[str, Any],
    package_manifest_sha256: str,
    executed_status: dict[str, Any],
    arm_run: dict[str, Any],
    arm_package: dict[str, Any],
    actual_cost_usd: float,
) -> dict[str, Any]:
    return state.sealed(
        {
            "schema_version": PUBLICATION_SCHEMA_VERSION,
            "stage_plan_sha256": stage_plan_sha256,
            "arm_id": arm_id,
            "status": "PASS",
            "package_object": package_object,
            "package_manifest_sha256": package_manifest_sha256,
            "executed_status": executed_status,
            "arm_run": arm_run,
            "arm_package": arm_package,
            "actual_cost_usd": actual_cost_usd,
        },
        "publication_receipt_sha256",
    )


def require_publication_receipt(raw: dict[str, Any]) -> None:
    require_exact_keys(raw, PUBLICATION_KEYS, name="official arm publication")
    unsigned = {key: value for key, value in raw.items() if key != "publication_receipt_sha256"}
    if (
        raw.get("schema_version") != PUBLICATION_SCHEMA_VERSION
        or raw.get("status") != "PASS"
        or raw.get("publication_receipt_sha256") != rig.canonical_sha256(unsigned)
    ):
        raise StagePlanError("official arm publication identity is invalid")


def _authority_snapshot(descriptor: int) -> AuthoritySnapshot:
    metadata = package_root.require_frozen_descriptor(
        descriptor,
        mode=AUTHORITY_DIRECTORY_MODE,
        name="official arm authority directory",
    )
    if not stat.S_ISDIR(metadata.st_mode):
        raise StagePlanError("official arm authority is not a directory")
    return AuthoritySnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        ctime_ns=metadata.st_ctime_ns,
        mtime_ns=metadata.st_mtime_ns,
        size=metadata.st_size,
        flags=metadata.st_flags,
    )


def _file_snapshot(descriptor: int, *, mode: int, name: str) -> FileSnapshot:
    metadata = package_root.require_frozen_descriptor(
        descriptor,
        mode=mode,
        name=name,
    )
    if not stat.S_ISREG(metadata.st_mode):
        raise StagePlanError(f"{name} is not a regular file")
    return FileSnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        ctime_ns=metadata.st_ctime_ns,
        mtime_ns=metadata.st_mtime_ns,
        size=metadata.st_size,
        flags=metadata.st_flags,
    )


def _open_frozen_file(
    directory_fd: int,
    name: str,
    *,
    mode: int,
) -> tuple[int, bytes, FileSnapshot]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise StagePlanError(f"official arm authority file {name!r} is unsafe") from exc
    try:
        before = _file_snapshot(descriptor, mode=mode, name=name)
        content = _read_descriptor(descriptor)
        after = _file_snapshot(descriptor, mode=mode, name=name)
    except BaseException:
        os.close(descriptor)
        raise
    if before != after or len(content) != before.size:
        os.close(descriptor)
        raise StagePlanError(f"official arm authority file {name!r} changed")
    return descriptor, content, after


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    blocks: list[bytes] = []
    while block := os.read(descriptor, 1024 * 1024):
        blocks.append(block)
    return b"".join(blocks)


def _require_frozen_file(
    descriptor: int,
    snapshot: FileSnapshot,
    content: bytes,
    *,
    name: str,
) -> None:
    current = _file_snapshot(descriptor, mode=snapshot.mode, name=name)
    if current != snapshot or _read_descriptor(descriptor) != content:
        raise StagePlanError(f"{name} changed during validation")
    if _file_snapshot(descriptor, mode=snapshot.mode, name=name) != snapshot:
        raise StagePlanError(f"{name} changed during validation")


def open_arm_authority(
    parent: package_root.OwnedDirectory,
    parent_fd: int,
    arm_id: str,
) -> tuple[package_root.OwnedDirectory, int]:
    """Open one canonical immutable arm directory without following links."""

    arms_owner, arms_fd = package_root.open_owned_child(
        parent,
        parent_fd,
        "arms",
        create=False,
    )
    arm_fd: int | None = None
    try:
        arm_owner, arm_fd = package_root.open_owned_child(
            arms_owner,
            arms_fd,
            arm_id,
            create=False,
        )
        _authority_snapshot(arm_fd)
    except BaseException:
        if arm_fd is not None:
            package_root.close_owned_directory(
                arm_fd,
                arm_owner,
                name="official arm authority",
            )
        raise
    finally:
        error = package_root.close_owned_directory(
            arms_fd,
            arms_owner,
            name="official arms root",
        )
        if error is not None:
            if arm_fd is not None:
                package_root.close_owned_directory(
                    arm_fd,
                    arm_owner,
                    name="official arm authority",
                )
            raise StagePlanError("official arms descriptor ownership changed") from error
    return arm_owner, arm_fd


def _require_authority_header(
    authority_content: bytes,
    *,
    files: dict[str, bytes],
    directories: frozenset[str],
    packages_root: Path,
    arm_id: str,
) -> tuple[dict[str, Any], dict[str, Any], Path, str]:
    try:
        raw = json.loads(authority_content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagePlanError("official arm publication is invalid JSON") from exc
    require_publication_receipt(raw)
    binding = raw.get("package_object")
    if not isinstance(binding, dict) or not isinstance(binding.get("sha256"), str):
        raise StagePlanError("official arm publication object binding is invalid")
    expected_object = object_path(packages_root, arm_id, binding["sha256"])
    object_name = expected_object.name
    if binding.get("path") != str(expected_object):
        raise StagePlanError("official arm publication object path changed")
    if set(files) != {AUTHORITY_NAME, object_name} or directories:
        raise StagePlanError("official arm authority inventory is not exact")
    if files[AUTHORITY_NAME] != authority_content:
        raise StagePlanError("official arm authority receipt changed")
    return raw, binding, expected_object, object_name


def _require_opened_authority(
    descriptor: int,
    before: AuthoritySnapshot,
    *,
    files: dict[str, bytes],
    binding: dict[str, Any],
    expected_object: Path,
    content: bytes,
) -> None:
    if (
        files[expected_object.name] != content
        or package_tree.bind_owned_content(content, path=expected_object) != binding
    ):
        raise StagePlanError("official arm publication object changed")
    if _authority_snapshot(descriptor) != before:
        raise StagePlanError("official arm authority changed during validation")


def read_arm_authority(
    owner: package_root.OwnedDirectory,
    descriptor: int,
    *,
    packages_root: Path,
    arm_id: str,
) -> tuple[dict[str, Any], bytes, AuthorityHandle]:
    """Read one stable exact two-file authority through its pinned descriptor."""

    before = _authority_snapshot(descriptor)
    files, directories = package_tree.read_owned_directory(owner, descriptor)
    authority_fd, authority_content, authority_snapshot = _open_frozen_file(
        descriptor,
        AUTHORITY_NAME,
        mode=AUTHORITY_FILE_MODE,
    )
    object_fd: int | None = None
    object_snapshot: FileSnapshot | None = None
    try:
        raw, binding, expected_object, object_name = _require_authority_header(
            authority_content,
            files=files,
            directories=directories,
            packages_root=packages_root,
            arm_id=arm_id,
        )
        object_fd, content, object_snapshot = _open_frozen_file(
            descriptor,
            object_name,
            mode=OBJECT_FILE_MODE,
        )
        _require_opened_authority(
            descriptor,
            before,
            files=files,
            binding=binding,
            expected_object=expected_object,
            content=content,
        )
    except BaseException:
        object_error = (
            _close_frozen_file(object_fd, object_snapshot)
            if object_fd is not None and object_snapshot is not None
            else None
        )
        error = _close_frozen_file(authority_fd, authority_snapshot)
        if object_error is not None:
            raise StagePlanError("official object descriptor cleanup failed") from object_error
        if error is not None:
            raise StagePlanError("official authority descriptor cleanup failed") from error
        raise
    if object_fd is None or object_snapshot is None:
        raise StagePlanError("official arm package object was not opened")
    return (
        raw,
        content,
        AuthorityHandle(
            directory=before,
            authority_fd=authority_fd,
            authority=authority_snapshot,
            authority_content=authority_content,
            object_fd=object_fd,
            package_object=object_snapshot,
            object_content=content,
        ),
    )


def require_arm_authority_unchanged(
    descriptor: int,
    handle: AuthorityHandle,
) -> None:
    _require_frozen_file(
        handle.authority_fd,
        handle.authority,
        handle.authority_content,
        name="official arm authority receipt",
    )
    _require_frozen_file(
        handle.object_fd,
        handle.package_object,
        handle.object_content,
        name="official arm package object",
    )
    if _authority_snapshot(descriptor) != handle.directory:
        raise StagePlanError("official arm authority changed during validation")
