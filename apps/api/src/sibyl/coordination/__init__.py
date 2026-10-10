"""Coordination backend resolution and health helpers."""

from __future__ import annotations

from typing import Any, Literal

from sibyl.config import settings
from sibyl_core.observability import telemetry_registry

CoordinationBackend = Literal["local", "redis"]


def get_coordination_backend() -> CoordinationBackend:
    """Return the resolved coordination backend for the current process."""
    return settings.resolved_coordination_backend


async def get_coordination_health() -> dict[str, Any]:
    """Return coordination health metadata for admin and jobs surfaces."""
    backend = get_coordination_backend()
    from sibyl.coordination.broker import get_queue_backend

    queue_backend = get_queue_backend()
    health: dict[str, Any] = {
        "backend": backend,
        "store": settings.store,
        "durable": backend == "redis",
        "queue_backend": queue_backend,
        "queue_durable": queue_backend == "redis",
        "single_process": backend == "local",
        "queue_healthy": False,
        "worker_healthy": False,
        "queue_depth": 0,
    }

    try:
        from sibyl.coordination.broker import get_broker

        broker_health = await get_broker().health()
    except Exception:
        return {
            **health,
            "status": "unhealthy",
            "error": "Health check failed",
        }

    merged: dict[str, Any] = {**health, **broker_health}
    try:
        from sibyl.cache_invalidation import cache_invalidation_status

        merged["cache_invalidation"] = cache_invalidation_status()
    except Exception:
        merged["cache_invalidation"] = {"state": "unknown"}
    telemetry_registry().record_queue_health(
        backend=str(merged.get("queue_backend") or backend),
        queue_depth=int(merged.get("queue_depth") or 0),
        queue_healthy=bool(merged.get("queue_healthy")),
        worker_healthy=bool(merged.get("worker_healthy")),
    )
    return merged
