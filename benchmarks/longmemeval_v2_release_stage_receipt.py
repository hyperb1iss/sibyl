"""Atomic immutable publication for the top-level release stage receipt."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_package_tree as package_tree
from benchmarks.longmemeval_v2_release_inputs import StagePlanError

FILE_MODE = 0o400


def _require_receipt_content(content: bytes, payload: dict[str, Any]) -> None:
    expected = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    if content != expected:
        raise StagePlanError("stage receipt differs from its publication")


def _require_output_identity(
    output_root: Path,
    before: os.stat_result,
    after: os.stat_result,
) -> None:
    live = output_root.stat(follow_symlinks=False)
    identity = (before.st_dev, before.st_ino)
    if identity != (after.st_dev, after.st_ino) or identity != (
        live.st_dev,
        live.st_ino,
    ):
        raise StagePlanError("claimed stage output root identity changed")


def publish(output_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Publish one receipt through a retained no-follow output descriptor."""

    output_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    output_fd = os.open(output_root, output_flags)
    output_identity = release_io.capture_descriptor_identity(output_fd)
    descriptor: int | None = None
    receipt_identity: release_io.DescriptorIdentity | None = None
    result: dict[str, Any]
    try:
        output_before = os.fstat(output_fd)
        with suppress(FileExistsError):
            release_io.write_json_once_atomic_at(output_fd, "stage_receipt.json", payload)
        descriptor = os.open(
            "stage_receipt.json",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=output_fd,
        )
        receipt_identity = release_io.capture_descriptor_identity(descriptor)
        content = package_tree.read_owned_file(output_fd, "stage_receipt.json")
        _require_receipt_content(content, payload)
        package_root.freeze_descriptor(
            descriptor,
            mode=FILE_MODE,
            name="stage receipt authority",
        )
        output_after = os.fstat(output_fd)
        _require_output_identity(output_root, output_before, output_after)
        result = package_tree.bind_owned_content(
            content,
            path=output_root / "stage_receipt.json",
        )
    except BaseException:
        if descriptor is not None and receipt_identity is not None:
            release_io.close_owned_descriptor(descriptor, receipt_identity)
        release_io.close_owned_descriptor(output_fd, output_identity)
        raise
    errors = []
    if descriptor is not None and receipt_identity is not None:
        errors.append(release_io.close_owned_descriptor(descriptor, receipt_identity))
    errors.append(release_io.close_owned_descriptor(output_fd, output_identity))
    if error := next((item for item in errors if item is not None), None):
        raise StagePlanError("stage receipt descriptor ownership changed") from error
    return result
