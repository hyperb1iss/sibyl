"""Sealed-path isolation policy for official release packages."""

from __future__ import annotations

import fcntl
import os
import platform
import shutil
import stat
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    require_exact_keys,
    require_positive_int,
    require_string,
)

SIBYL_ROOT = Path(__file__).resolve().parents[1]
DARWIN_F_GETPATH = 50
DARWIN_PATH_BUFFER_SIZE = 1024
DARWIN_IMMUTABLE_FLAG = getattr(stat, "UF_IMMUTABLE", 0x00000002)
MINIMUM_MACOS_MAJOR = 26
RELEASE_HOST_KEYS = frozenset(
    {
        "platform",
        "macos_major",
        "filesystem_device",
        "immutable_descendant_rename",
    }
)


def require_release_host_binding(raw: object) -> dict[str, Any]:
    """Validate the sealed host capability required by package publication."""

    if not isinstance(raw, dict):
        raise StagePlanError("release host binding is missing")
    require_exact_keys(raw, RELEASE_HOST_KEYS, name="release host")
    if require_string(raw.get("platform"), name="release host platform") != "darwin":
        raise StagePlanError("release host platform must be Darwin")
    major = require_positive_int(raw.get("macos_major"), name="release host macOS major")
    if major < MINIMUM_MACOS_MAJOR:
        raise StagePlanError(f"release host requires macOS {MINIMUM_MACOS_MAJOR} or newer")
    require_positive_int(raw.get("filesystem_device"), name="release host filesystem device")
    if raw.get("immutable_descendant_rename") is not True:
        raise StagePlanError("release host lacks immutable descendant rename support")
    return dict(raw)


def _existing_directory(path: Path) -> Path:
    ancestor = path.parent
    while not ancestor.exists():
        if ancestor.parent == ancestor:
            raise StagePlanError("release output has no existing filesystem ancestor")
        ancestor = ancestor.parent
    if not ancestor.is_dir() or ancestor.is_symlink():
        raise StagePlanError("release output filesystem ancestor is unsafe")
    return ancestor


def _remove_release_host_probe(probe_root: Path) -> None:
    setter = getattr(os, "chflags", None)
    if callable(setter):
        for candidate in (
            probe_root / "pending/sealed/artifact.json",
            probe_root / "pending/sealed",
            probe_root / "published/sealed/artifact.json",
            probe_root / "published/sealed",
        ):
            with suppress(OSError):
                setter(candidate, 0)
    try:
        shutil.rmtree(probe_root)
    except OSError as exc:
        raise StagePlanError("release host capability probe cleanup failed") from exc


def _probe_immutable_descendant_rename(probe_root: Path) -> int:
    source = probe_root / "pending"
    descendant = source / "sealed"
    artifact = descendant / "artifact.json"
    published = probe_root / "published"
    try:
        probe_device = probe_root.stat().st_dev
        descendant.mkdir(parents=True)
        artifact.write_text("{}\n", encoding="utf-8")
        artifact_fd = os.open(artifact, os.O_RDONLY)
        try:
            descendant_fd = os.open(descendant, os.O_RDONLY)
            try:
                release_io.set_fd_flags(artifact_fd, DARWIN_IMMUTABLE_FLAG)
                release_io.set_fd_flags(descendant_fd, DARWIN_IMMUTABLE_FLAG)
            finally:
                os.close(descendant_fd)
        finally:
            os.close(artifact_fd)
        root_fd = os.open(probe_root, os.O_RDONLY)
        try:
            release_io.rename_once_atomic_at(root_fd, source.name, root_fd, published.name)
        finally:
            os.close(root_fd)
    except OSError as exc:
        raise StagePlanError(
            "release host cannot publish immutable descendants atomically"
        ) from exc
    finally:
        _remove_release_host_probe(probe_root)
    return probe_device


def probe_release_host(path: Path) -> dict[str, Any]:
    """Prove the destination can atomically rename immutable descendants."""

    if sys.platform != "darwin":
        raise StagePlanError(f"release publication requires macOS {MINIMUM_MACOS_MAJOR} or newer")
    version = platform.mac_ver()[0]
    try:
        major = int(version.split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise StagePlanError("release host macOS version is unavailable") from exc
    if major < MINIMUM_MACOS_MAJOR:
        raise StagePlanError(f"release publication requires macOS {MINIMUM_MACOS_MAJOR} or newer")

    destination = canonical_path(path, name="release output root")
    ancestor = _existing_directory(destination)
    try:
        probe_root = Path(tempfile.mkdtemp(prefix=".sibyl-release-host-", dir=ancestor))
    except OSError as exc:
        raise StagePlanError("release host capability probe could not be created") from exc
    probe_device = _probe_immutable_descendant_rename(probe_root)

    return require_release_host_binding(
        {
            "platform": "darwin",
            "macos_major": major,
            "filesystem_device": probe_device,
            "immutable_descendant_rename": True,
        }
    )


def require_current_release_host(path: Path, expected: object) -> dict[str, Any]:
    """Re-probe the sealed output filesystem before any provider work."""

    binding = require_release_host_binding(expected)
    current = probe_release_host(path)
    if current != binding:
        raise StagePlanError("current release host differs from the sealed host capability")
    return current


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
