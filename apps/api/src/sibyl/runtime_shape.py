"""Pure runtime-shape helpers shared across settings and CLI flows."""

from __future__ import annotations

from typing import Literal, cast

ConfiguredCoordinationBackend = Literal["auto", "local", "redis"]
ResolvedCoordinationBackend = Literal["local", "redis"]


def resolve_coordination_backend(
    *,
    coordination_backend: ConfiguredCoordinationBackend,
) -> ResolvedCoordinationBackend:
    if coordination_backend == "auto":
        return "local"
    return coordination_backend


def resolve_object_coordination_backend(
    value: object,
) -> ResolvedCoordinationBackend:
    backend = getattr(value, "resolved_coordination_backend", None)
    if backend in {"local", "redis"}:
        return cast("ResolvedCoordinationBackend", backend)

    configured_backend = getattr(value, "coordination_backend", None)
    if configured_backend not in {"auto", "local", "redis"}:
        configured_backend = "auto"

    return resolve_coordination_backend(
        coordination_backend=cast("ConfiguredCoordinationBackend", configured_backend),
    )
