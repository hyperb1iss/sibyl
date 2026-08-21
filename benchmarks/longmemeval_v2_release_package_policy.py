"""Sealed-path isolation policy for official release packages."""

from __future__ import annotations

import fcntl
import os
import stat
import sys
from pathlib import Path
from typing import Any

from benchmarks.longmemeval_v2_release_inputs import StagePlanError

SIBYL_ROOT = Path(__file__).resolve().parents[1]
DARWIN_F_GETPATH = 50
DARWIN_PATH_BUFFER_SIZE = 1024


def _filesystem_path(path: Path, *, name: str) -> Path:
    try:
        before = path.stat(follow_symlinks=False)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise StagePlanError(f"{name} has no safe existing ancestor") from exc
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise StagePlanError(f"{name} existing ancestor changed during validation")
        if sys.platform != "darwin":
            return path.resolve(strict=True)
        raw = fcntl.fcntl(descriptor, DARWIN_F_GETPATH, bytes(DARWIN_PATH_BUFFER_SIZE))
        canonical = Path(os.fsdecode(raw.split(b"\0", 1)[0]))
    except OSError as exc:
        raise StagePlanError(f"{name} existing ancestor could not be identified") from exc
    else:
        if not canonical.is_absolute():
            raise StagePlanError(f"{name} existing ancestor has no canonical filesystem path")
        return canonical
    finally:
        os.close(descriptor)


def canonical_path(path: Path, *, name: str) -> Path:
    """Resolve casing from filesystem identity and preserve a missing safe tail."""

    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise StagePlanError(f"{name} must be absolute")
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise StagePlanError(f"{name} is not canonical") from exc
    if str(candidate).casefold() != str(resolved).casefold():
        raise StagePlanError(f"{name} traverses a symlink or noncanonical component")

    ancestor = candidate
    tail: list[str] = []
    while True:
        try:
            metadata = ancestor.stat(follow_symlinks=False)
        except FileNotFoundError as exc:
            if ancestor.parent == ancestor:
                raise StagePlanError(f"{name} has no existing filesystem ancestor") from exc
            tail.append(ancestor.name)
            ancestor = ancestor.parent
            continue
        except OSError as exc:
            raise StagePlanError(f"{name} existing ancestor is unreadable") from exc
        break
    if stat.S_ISLNK(metadata.st_mode) or (tail and not stat.S_ISDIR(metadata.st_mode)):
        raise StagePlanError(f"{name} traverses an unsafe filesystem ancestor")

    canonical_ancestor = _filesystem_path(ancestor, name=name)
    resolved_ancestor = ancestor.resolve(strict=True)
    if str(canonical_ancestor).casefold() != str(resolved_ancestor).casefold():
        raise StagePlanError(f"{name} existing ancestor is not canonical")
    return canonical_ancestor.joinpath(*reversed(tail))


def _binding_paths(raw: object) -> set[Path]:
    paths: set[Path] = set()
    if isinstance(raw, dict):
        path = raw.get("path")
        if isinstance(path, str) and path.strip():
            paths.add(Path(path))
        for value in raw.values():
            paths.update(_binding_paths(value))
    elif isinstance(raw, list | tuple):
        for value in raw:
            paths.update(_binding_paths(value))
    return paths


def _optional_paths(raw: object) -> set[Path]:
    if not isinstance(raw, dict):
        return set()
    return {Path(value) for value in raw.values() if isinstance(value, str) and value.strip()}


def sealed_paths(plan: dict[str, Any]) -> set[Path]:
    """Return every execution input a package root must not overlap."""

    paths = {SIBYL_ROOT, Path(plan["output_root"])}
    official = plan.get("official_source")
    if isinstance(official, dict) and isinstance(official.get("path"), str):
        paths.add(Path(official["path"]))
    dataset = plan.get("dataset")
    if isinstance(dataset, dict) and isinstance(dataset.get("root"), str):
        paths.add(Path(dataset["root"]))
        paths.update(_binding_paths(dataset.get("artifacts")))
    paths.update(_binding_paths(plan.get("spec_artifact")))
    paths.update(_binding_paths(plan.get("package_inputs")))
    paths.update(_binding_paths(plan.get("memory_bindings")))
    paths.update(_binding_paths(plan.get("upstream_bindings")))
    spec = plan.get("spec")
    if isinstance(spec, dict):
        paths.update(_optional_paths(spec.get("upstream")))
        memory_roots = spec.get("memory_roots")
        if isinstance(memory_roots, dict):
            for roots in memory_roots.values():
                paths.update(_optional_paths(roots))
    return {canonical_path(path, name="sealed execution input") for path in paths}


def overlaps(left: Path, right: Path) -> bool:
    """Return whether either canonical path contains the other."""

    canonical_left = canonical_path(left, name="candidate release path")
    canonical_right = canonical_path(right, name="sealed release path")
    return (
        canonical_left == canonical_right
        or canonical_left.is_relative_to(canonical_right)
        or canonical_right.is_relative_to(canonical_left)
    )
