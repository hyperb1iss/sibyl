"""Readiness probe helpers.

Liveness (`/health`) only asserts the process is up. Readiness asks the
harder question: can this process actually serve traffic? For Sibyl that
means the SurrealDB runtime and active coordination broker are reachable.
The database check connects a dedicated auth client against the static
`sibyl_auth` namespace and returns the pooled connection without issuing a
query, so it never touches a per-org namespace or the tenant query path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

log = structlog.get_logger()


@dataclass(slots=True, frozen=True)
class DependencyStatus:
    """Result of probing a single serving dependency."""

    name: str
    ready: bool
    detail: str | None = None
    latency_ms: float | None = None
    backend: str | None = None


@dataclass(slots=True, frozen=True)
class ReadinessReport:
    """Aggregate readiness across every serving dependency."""

    ready: bool
    dependencies: list[DependencyStatus]

    def as_payload(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "dependencies": [
                {
                    "name": dep.name,
                    "ready": dep.ready,
                    **({"detail": dep.detail} if dep.detail is not None else {}),
                    **({"latency_ms": dep.latency_ms} if dep.latency_ms is not None else {}),
                    **({"backend": dep.backend} if dep.backend is not None else {}),
                }
                for dep in self.dependencies
            ],
        }


async def check_surreal_ready() -> DependencyStatus:
    """Probe SurrealDB reachability with a connect-only handshake.

    Builds the auth client (static namespace, never per-org) and checks out
    a pooled connection. `connect()` performs the websocket handshake,
    signin, and namespace selection without running a query, so it proves
    the runtime can serve traffic without holding the tenant query path.
    """
    from sibyl.persistence.surreal.auth import build_surreal_auth_client

    started = time.perf_counter()
    client = build_surreal_auth_client()
    try:
        await client.connect()
    except Exception as exc:
        log.warning("readiness_surreal_unreachable", error=str(exc))
        return DependencyStatus(
            name="surrealdb",
            ready=False,
            detail="SurrealDB runtime unreachable",
        )
    finally:
        try:
            await client.close()
        except Exception as exc:
            log.debug("readiness_surreal_close_error", error=str(exc))

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return DependencyStatus(name="surrealdb", ready=True, latency_ms=latency_ms)


def check_schema_bootstrap_ready() -> DependencyStatus | None:
    from sibyl.surreal_runtime_startup import get_runtime_schema_bootstrap_status

    status = get_runtime_schema_bootstrap_status()
    if not status.attempted:
        return None
    if status.ready:
        return DependencyStatus(name="schemas", ready=True)
    detail = "; ".join(
        f"{failure.plane} v{failure.target_version}: {failure.error}" for failure in status.failures
    )
    return DependencyStatus(
        name="schemas",
        ready=False,
        detail=detail or "Surreal schema bootstrap incomplete",
    )


async def check_coordination_ready() -> DependencyStatus:
    """Probe the active broker required to accept background work."""
    from sibyl.coordination import get_coordination_backend, get_coordination_health

    backend = get_coordination_backend()
    started = time.perf_counter()
    health = await get_coordination_health()
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    ready = health.get("status") == "healthy" and health.get("queue_healthy") is True
    return DependencyStatus(
        name="coordination",
        ready=ready,
        detail=None if ready else f"{backend} coordination broker unavailable",
        latency_ms=latency_ms,
        backend=backend,
    )


async def check_readiness() -> ReadinessReport:
    """Aggregate readiness across the dependencies needed to serve traffic."""
    dependencies = [await check_surreal_ready(), await check_coordination_ready()]
    schema_status = check_schema_bootstrap_ready()
    if schema_status is not None:
        dependencies.append(schema_status)
    return ReadinessReport(
        ready=all(dep.ready for dep in dependencies),
        dependencies=dependencies,
    )


__all__ = [
    "DependencyStatus",
    "ReadinessReport",
    "check_coordination_ready",
    "check_readiness",
    "check_schema_bootstrap_ready",
    "check_surreal_ready",
]
