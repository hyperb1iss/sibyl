"""Exact immutable sibling inventory for official arm publications."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_package_object as package_object
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks.longmemeval_v2_release_inputs import StagePlanError


def require_publication_inventory(
    owner: package_root.OwnedDirectory,
    descriptor: int,
    *,
    arm_ids: set[str],
) -> dict[str, dict[str, Any]]:
    """Require the immutable sibling collection and every arm authority."""

    package_root.require_frozen_descriptor(
        descriptor,
        mode=0o500,
        name="official packages root",
    )
    arms_owner, arms_fd = package_root.open_owned_child(
        owner,
        descriptor,
        "arms",
        create=False,
    )
    try:
        package_root.require_frozen_descriptor(
            arms_fd,
            mode=0o500,
            name="official arms root",
        )
        files, directories = package_tree.read_owned_directory(owner, descriptor)
        receipts: dict[str, dict[str, Any]] = {}
        expected: set[str] = set()
        expected_directories: set[str] = {"arms"}
        for arm_id in sorted(arm_ids):
            arm_directory = f"arms/{arm_id}"
            if arm_directory not in directories:
                continue
            arm_owner, arm_fd = package_object.open_arm_authority(
                owner,
                descriptor,
                arm_id,
            )
            handle: package_object.AuthorityHandle | None = None
            try:
                raw, _content, handle = package_object.read_arm_authority(
                    arm_owner,
                    arm_fd,
                    packages_root=owner.path,
                    arm_id=arm_id,
                )
                if raw.get("arm_id") != arm_id:
                    raise StagePlanError("official arm publication ID changed")
                package_object.require_arm_authority_unchanged(arm_fd, handle)
            finally:
                if handle is not None:
                    handle.close()
                error = package_root.close_owned_directory(
                    arm_fd,
                    arm_owner,
                    name="official arm authority",
                )
                if error is not None:
                    raise StagePlanError("official arm descriptor ownership changed") from error
            binding = raw["package_object"]
            authority_name = (
                package_object.publication_path(
                    owner.path,
                    arm_id,
                )
                .relative_to(owner.path)
                .as_posix()
            )
            object_name = Path(binding["path"]).relative_to(owner.path).as_posix()
            expected.update({authority_name, object_name})
            expected_directories.add(arm_directory)
            receipts[arm_id] = raw
        if set(files) != expected:
            raise StagePlanError("official package publication inventory is not exact")
        if directories != expected_directories:
            raise StagePlanError("official package publication directories are not exact")
        package_root.require_frozen_descriptor(
            arms_fd,
            mode=0o500,
            name="official arms root",
        )
        package_root.require_frozen_descriptor(
            descriptor,
            mode=0o500,
            name="official packages root",
        )
        return receipts
    finally:
        error = package_root.close_owned_directory(
            arms_fd,
            arms_owner,
            name="official arms root",
        )
        if error is not None:
            raise StagePlanError("official arms descriptor ownership changed") from error
