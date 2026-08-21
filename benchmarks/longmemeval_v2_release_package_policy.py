"""Sealed-path isolation policy for official release packages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

SIBYL_ROOT = Path(__file__).resolve().parents[1]


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
    return {path.expanduser().resolve() for path in paths}


def overlaps(left: Path, right: Path) -> bool:
    """Return whether either canonical path contains the other."""

    return left == right or left.is_relative_to(right) or right.is_relative_to(left)
