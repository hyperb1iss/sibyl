"""Canonical ownership for isolated LongMemEval-V2 package roots."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_package_policy as package_policy
from benchmarks.longmemeval_v2_release_inputs import StagePlanError

IMMUTABLE_FLAG = getattr(stat, "UF_IMMUTABLE", 0x00000002)


def _close_descriptors(descriptors: tuple[int | None, ...]) -> BaseException | None:
    first_error: BaseException | None = None
    for descriptor in descriptors:
        if descriptor is None:
            continue
        try:
            os.close(descriptor)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    return first_error


@dataclass(frozen=True)
class OwnedDirectory:
    """One canonical directory pinned to its filesystem identity."""

    path: Path
    device: int
    inode: int


@dataclass
class PackageLease:
    """Open descriptors that keep one package transaction on owned inodes."""

    parent: OwnedDirectory
    arms: OwnedDirectory
    staging_parent: OwnedDirectory
    arm: OwnedDirectory
    parent_fd: int
    arms_fd: int
    staging_parent_fd: int
    arm_fd: int
    logs: OwnedDirectory
    logs_fd: int
    receipts: OwnedDirectory | None = None
    receipts_fd: int | None = None
    children_closed: bool = False
    arm_closed: bool = False
    closed: bool = False

    def close(self) -> None:
        if self.closed:
            return
        descriptors: list[tuple[int, OwnedDirectory, str]] = []
        if not self.children_closed and self.receipts_fd is not None and self.receipts is not None:
            descriptors.append((self.receipts_fd, self.receipts, "official command receipts"))
        if not self.children_closed:
            descriptors.append((self.logs_fd, self.logs, "official command logs"))
        if not self.arm_closed:
            descriptors.append((self.arm_fd, self.arm, "official staging package"))
        descriptors.append(
            (
                self.staging_parent_fd,
                self.staging_parent,
                "official staging parent",
            )
        )
        descriptors.append((self.arms_fd, self.arms, "official arms root"))
        descriptors.append((self.parent_fd, self.parent, "official packages root"))
        first_error: BaseException | None = None
        for descriptor, owner, name in descriptors:
            error = close_owned_directory(descriptor, owner, name=name)
            if first_error is None and error is not None:
                first_error = error
        self.closed = True
        if first_error is not None:
            if isinstance(first_error, Exception):
                raise StagePlanError(
                    "official package descriptor ownership changed"
                ) from first_error
            raise first_error


def _directory_identity(path: Path, *, name: str) -> OwnedDirectory:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise StagePlanError(f"{name} is missing or unreadable") from exc
    if (
        not path.is_absolute()
        or path != resolved
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
    ):
        raise StagePlanError(f"{name} is not one canonical directory")
    return OwnedDirectory(path=resolved, device=metadata.st_dev, inode=metadata.st_ino)


def require_owned_directory(owner: OwnedDirectory, *, name: str) -> None:
    current = _directory_identity(owner.path, name=name)
    if current != owner:
        raise StagePlanError(f"{name} filesystem identity changed")


def _open_owned_directory(owner: OwnedDirectory, *, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(owner.path, flags)
    except OSError as exc:
        raise StagePlanError(f"{name} could not be opened safely") from exc
    metadata = os.fstat(descriptor)
    if (metadata.st_dev, metadata.st_ino) != (owner.device, owner.inode):
        os.close(descriptor)
        raise StagePlanError(f"{name} changed before descriptor ownership")
    return descriptor


def _open_child_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise StagePlanError(f"official package directory {name!r} is unsafe") from exc


def _require_descriptor_identity(descriptor: int, owner: OwnedDirectory, *, name: str) -> None:
    try:
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise StagePlanError(f"{name} descriptor is closed or unreadable") from exc
    if (metadata.st_dev, metadata.st_ino) != (owner.device, owner.inode):
        raise StagePlanError(f"{name} changed before descriptor ownership")


def _file_flags(metadata: os.stat_result) -> int:
    flags = getattr(metadata, "st_flags", None)
    if not isinstance(flags, int):
        raise StagePlanError("immutable official publication requires Darwin file flags")
    return flags


def freeze_descriptor(descriptor: int, *, mode: int, name: str) -> os.stat_result:
    """Set and verify one descriptor's exact mode and immutable flag."""

    before = os.fstat(descriptor)
    if _file_flags(before) & IMMUTABLE_FLAG:
        if stat.S_IMODE(before.st_mode) != mode:
            raise StagePlanError(f"{name} immutable mode is invalid")
        return before
    try:
        os.fchmod(descriptor, mode)
        release_io.set_fd_flags(descriptor, IMMUTABLE_FLAG)
        os.fsync(descriptor)
    except OSError as exc:
        raise StagePlanError(f"{name} could not be frozen") from exc
    after = os.fstat(descriptor)
    if stat.S_IMODE(after.st_mode) != mode or not (_file_flags(after) & IMMUTABLE_FLAG):
        raise StagePlanError(f"{name} did not become immutable")
    return after


def require_frozen_descriptor(descriptor: int, *, mode: int, name: str) -> os.stat_result:
    """Require one open descriptor to retain its frozen publication state."""

    metadata = os.fstat(descriptor)
    if stat.S_IMODE(metadata.st_mode) != mode or not (_file_flags(metadata) & IMMUTABLE_FLAG):
        raise StagePlanError(f"{name} immutable state changed")
    return metadata


def thaw_descriptor(
    descriptor: int,
    *,
    frozen_mode: int,
    writable_mode: int,
    name: str,
) -> None:
    """Temporarily thaw one owned publication directory under its sibling lock."""

    require_frozen_descriptor(descriptor, mode=frozen_mode, name=name)
    try:
        release_io.set_fd_flags(descriptor, 0)
        os.fchmod(descriptor, writable_mode)
        os.fsync(descriptor)
    except OSError as exc:
        raise StagePlanError(f"{name} could not be thawed for publication") from exc
    metadata = os.fstat(descriptor)
    if stat.S_IMODE(metadata.st_mode) != writable_mode or _file_flags(metadata):
        raise StagePlanError(f"{name} did not become writable for publication")


def _lock_path(owner: OwnedDirectory) -> Path:
    return owner.path.with_name(f".{owner.path.name}.longmemeval-v2.lock")


@contextmanager
def publication_lock(owner: OwnedDirectory) -> Iterator[None]:
    """Serialize sibling publication metadata outside the immutable package root."""

    require_owned_directory(owner, name="official packages root")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(_lock_path(owner), flags, 0o600)
    except OSError as exc:
        raise StagePlanError("official package publication lock is unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise StagePlanError("official package publication lock is not regular")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        require_owned_directory(owner, name="official packages root")
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def close_owned_directory(
    descriptor: int,
    owner: OwnedDirectory,
    *,
    name: str,
) -> BaseException | None:
    try:
        _require_descriptor_identity(descriptor, owner, name=name)
    except BaseException as exc:
        return exc
    try:
        os.close(descriptor)
    except BaseException as exc:
        return exc
    return None


def require_lease_descriptors(lease: PackageLease) -> None:
    """Require every open lease descriptor to retain its original directory."""

    if lease.closed:
        raise StagePlanError("official package lease is closed")
    _require_descriptor_identity(
        lease.parent_fd,
        lease.parent,
        name="official packages root",
    )
    _require_descriptor_identity(
        lease.arms_fd,
        lease.arms,
        name="official arms root",
    )
    _require_descriptor_identity(
        lease.staging_parent_fd,
        lease.staging_parent,
        name="official staging parent",
    )
    if not lease.arm_closed:
        _require_descriptor_identity(
            lease.arm_fd,
            lease.arm,
            name="official staging package",
        )
    if not lease.children_closed:
        _require_descriptor_identity(
            lease.logs_fd,
            lease.logs,
            name="official command logs",
        )
        if lease.receipts_fd is not None:
            if lease.receipts is None:
                raise StagePlanError("official receipt descriptor has no owner")
            _require_descriptor_identity(
                lease.receipts_fd,
                lease.receipts,
                name="official command receipts",
            )


def bind_packages_root(
    plan: dict[str, Any],
    runs: tuple[dict[str, Any], ...],
    path: Path,
) -> OwnedDirectory:
    """Bind one empty-or-sibling-only package parent outside every sealed input."""

    root = _directory_identity(path.expanduser(), name="official packages root")
    if any(
        package_policy.overlaps(root.path, sealed) for sealed in package_policy.sealed_paths(plan)
    ):
        raise StagePlanError("official packages root overlaps sealed execution inputs")
    children = tuple(root.path.iterdir())
    for child in children:
        if child.is_symlink():
            raise StagePlanError("official packages root contains an unsafe entry")
        if child.name == "arms" and child.is_dir():
            continue
        raise StagePlanError("official packages root contains a foreign package entry")
    arm_ids = {run["arm_id"] for run in runs}
    arms_root = root.path / "arms"
    if arms_root.is_dir():
        for child in arms_root.iterdir():
            if child.name not in arm_ids:
                raise StagePlanError("official packages root contains a foreign arm")
    require_owned_directory(root, name="official packages root")
    return root


def initialize_packages_root(owner: OwnedDirectory) -> None:
    """Create the immutable root and arms layout exactly once."""

    with publication_lock(owner):
        descriptor = _open_owned_directory(owner, name="official packages root")
        arms_fd: int | None = None
        try:
            entries = tuple(os.scandir(descriptor))
            if not entries:
                os.mkdir("arms", mode=0o700, dir_fd=descriptor)
                arms = _directory_identity(
                    owner.path / "arms",
                    name="official arms root",
                )
                arms_fd = _open_child_directory(descriptor, "arms")
                _require_descriptor_identity(arms_fd, arms, name="official arms root")
                freeze_descriptor(arms_fd, mode=0o500, name="official arms root")
                freeze_descriptor(descriptor, mode=0o500, name="official packages root")
                return
            if len(entries) != 1 or entries[0].name != "arms":
                raise StagePlanError("official packages root inventory is not exact")
            arms, arms_fd = open_owned_child(owner, descriptor, "arms", create=False)
            require_frozen_descriptor(arms_fd, mode=0o500, name="official arms root")
            require_frozen_descriptor(
                descriptor,
                mode=0o500,
                name="official packages root",
            )
        finally:
            _close_descriptors((arms_fd, descriptor))


def create_arm_lease(owner: OwnedDirectory, arm_id: str) -> PackageLease:
    """Create one unpublished staging directory beside the immutable parent."""

    if Path(arm_id).name != arm_id or not arm_id:
        raise StagePlanError("official package arm ID is not path-safe")
    require_owned_directory(owner, name="official packages root")
    initialize_packages_root(owner)
    if (owner.path / "arms" / arm_id).exists():
        raise StagePlanError("official arm package is already published")
    staging_prefix = f".{owner.path.name}.staging-{arm_id}-"
    if any(child.name.startswith(staging_prefix) for child in owner.path.parent.iterdir()):
        raise StagePlanError("official arm package has stale unpublished staging")
    descriptor = _open_owned_directory(owner, name="official packages root")
    staging_name = f"{staging_prefix}{uuid4().hex}"
    arms_fd: int | None = None
    staging_parent_fd: int | None = None
    arm_fd: int | None = None
    logs_fd: int | None = None
    lease: PackageLease | None = None
    try:
        arms, arms_fd = open_owned_child(owner, descriptor, "arms", create=False)
        require_frozen_descriptor(arms_fd, mode=0o500, name="official arms root")
        require_frozen_descriptor(descriptor, mode=0o500, name="official packages root")
        staging_parent = _directory_identity(
            owner.path.parent,
            name="official staging parent",
        )
        staging_parent_fd = _open_owned_directory(
            staging_parent,
            name="official staging parent",
        )
        try:
            os.mkdir(staging_name, mode=0o700, dir_fd=staging_parent_fd)
        except FileExistsError as exc:
            raise StagePlanError("official arm package requires one fresh canonical root") from exc
        except OSError as exc:
            raise StagePlanError("official arm package root could not be created") from exc
        require_owned_directory(owner, name="official packages root")
        arm = _directory_identity(
            staging_parent.path / staging_name,
            name="official staging package",
        )
        arm_fd = _open_child_directory(staging_parent_fd, staging_name)
        _require_descriptor_identity(arm_fd, arm, name="official arm package")
        os.mkdir("logs", mode=0o700, dir_fd=arm_fd)
        logs_fd = _open_child_directory(arm_fd, "logs")
        logs = _directory_identity(arm.path / "logs", name="official command logs")
        _require_descriptor_identity(logs_fd, logs, name="official command logs")
        require_package_roots(owner, arm)
        lease = PackageLease(
            parent=owner,
            arms=arms,
            staging_parent=staging_parent,
            arm=arm,
            parent_fd=descriptor,
            arms_fd=arms_fd,
            staging_parent_fd=staging_parent_fd,
            arm_fd=arm_fd,
            logs=logs,
            logs_fd=logs_fd,
        )
        require_lease_descriptors(lease)
    except BaseException:
        _close_descriptors((logs_fd, arm_fd, staging_parent_fd, arms_fd, descriptor))
        raise
    else:
        return lease


def require_receipts_fd(lease: PackageLease) -> int:
    """Open the receipt directory lazily after the first command audit."""

    require_lease_descriptors(lease)
    if lease.receipts_fd is None:
        with suppress(FileExistsError):
            os.mkdir("command_receipts", mode=0o700, dir_fd=lease.arm_fd)
        descriptor = _open_child_directory(lease.arm_fd, "command_receipts")
        try:
            owner = _directory_identity(
                lease.arm.path / "command_receipts",
                name="official command receipts",
            )
            _require_descriptor_identity(
                descriptor,
                owner,
                name="official command receipts",
            )
        except BaseException:
            _close_descriptors((descriptor,))
            raise
        lease.receipts = owner
        lease.receipts_fd = descriptor
        require_lease_descriptors(lease)
    return lease.receipts_fd


def retire_package_children(lease: PackageLease) -> None:
    """Close validated child descriptors before freezing the authority root."""

    require_lease_descriptors(lease)
    if lease.receipts_fd is not None and lease.receipts is not None:
        error = close_owned_directory(
            lease.receipts_fd,
            lease.receipts,
            name="official command receipts",
        )
        if error is not None:
            raise StagePlanError("official receipt descriptor ownership changed") from error
        lease.receipts_fd = None
        lease.receipts = None
    error = close_owned_directory(
        lease.logs_fd,
        lease.logs,
        name="official command logs",
    )
    if error is not None:
        raise StagePlanError("official log descriptor ownership changed") from error
    lease.children_closed = True
    require_lease_descriptors(lease)


def require_frozen_package_layout(lease: PackageLease) -> None:
    """Require the package parent and arms collection to be immutable."""

    require_lease_descriptors(lease)
    entries = tuple(os.scandir(lease.parent_fd))
    if len(entries) != 1 or entries[0].name != "arms":
        raise StagePlanError("official packages root inventory is not exact")
    metadata = entries[0].stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != (lease.arms.device, lease.arms.inode)
    ):
        raise StagePlanError("official arms root identity changed")
    require_frozen_descriptor(
        lease.arms_fd,
        mode=0o500,
        name="official arms root",
    )
    require_frozen_descriptor(
        lease.parent_fd,
        mode=0o500,
        name="official packages root",
    )


def thaw_arms_for_publication(lease: PackageLease) -> None:
    """Thaw only the sibling collection while the external lock is held."""

    require_frozen_package_layout(lease)
    thaw_descriptor(
        lease.arms_fd,
        frozen_mode=0o500,
        writable_mode=0o700,
        name="official arms root",
    )


def refreeze_arms_after_publication(lease: PackageLease) -> None:
    """Restore immutable sibling metadata before leaving publication."""

    freeze_descriptor(lease.arms_fd, mode=0o500, name="official arms root")
    require_frozen_package_layout(lease)


def close_staging_for_rename(lease: PackageLease) -> None:
    """Close the verified staging fd for filesystems that reject open renames."""

    if not lease.children_closed or lease.arm_closed:
        raise StagePlanError("official staging package is not ready for publication")
    error = close_owned_directory(
        lease.arm_fd,
        lease.arm,
        name="official staging package",
    )
    if error is not None:
        raise StagePlanError("official staging descriptor ownership changed") from error
    lease.arm_closed = True
    require_lease_descriptors(lease)


def require_package_roots(parent: OwnedDirectory, arm: OwnedDirectory) -> None:
    """Revalidate both root identities before or after a package command."""

    require_owned_directory(parent, name="official packages root")
    require_owned_directory(arm, name="official arm package root")
    expected_prefix = f".{parent.path.name}.staging-"
    if arm.path.parent != parent.path.parent or not arm.path.name.startswith(expected_prefix):
        raise StagePlanError("official staging package escaped its packages root")
    require_owned_directory(parent, name="official packages root")


def require_lease(lease: PackageLease) -> None:
    """Revalidate path and descriptor ownership for one active transaction."""

    require_lease_descriptors(lease)
    require_package_roots(lease.parent, lease.arm)
    require_lease_descriptors(lease)


def open_owned_child(
    parent: OwnedDirectory,
    parent_fd: int,
    name: str,
    *,
    create: bool,
) -> tuple[OwnedDirectory, int]:
    """Open one no-follow child and bind it to the pinned parent."""

    if Path(name).name != name or not name:
        raise StagePlanError("official package child name is unsafe")
    _require_descriptor_identity(parent_fd, parent, name="official packages root")
    if create:
        with suppress(FileExistsError):
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    child = _directory_identity(parent.path / name, name=f"official package {name}")
    descriptor = _open_child_directory(parent_fd, name)
    try:
        _require_descriptor_identity(descriptor, child, name=f"official package {name}")
        _require_descriptor_identity(parent_fd, parent, name="official packages root")
    except BaseException:
        _close_descriptors((descriptor,))
        raise
    return child, descriptor


def open_owned_directory(owner: OwnedDirectory, *, name: str) -> int:
    """Open one previously bound directory and verify its descriptor identity."""

    return _open_owned_directory(owner, name=name)
