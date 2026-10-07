"""Compatibility shim for coordination event backends."""

from __future__ import annotations

from typing import Any

from sibyl.api.event_types import WSEvent
from sibyl.coordination.events import EventBus, get_events
from sibyl_core.services.graph_cache_invalidation import invalidate_graph_caches

# Events that mean an organization's graph rows or their protected evidence
# moved. The graph client's write seam already covers writers in this
# process; the bus subscription below covers writers in other pods.
GRAPH_MUTATION_EVENTS: frozenset[str] = frozenset(
    {
        WSEvent.ENTITY_CREATED,
        WSEvent.ENTITY_UPDATED,
        WSEvent.ENTITY_DELETED,
        WSEvent.NOTE_CREATED,
        WSEvent.GRAPH_UPDATED,
        WSEvent.CRAWL_COMPLETE,
        WSEvent.CRAWL_SYNC_COMPLETE,
        WSEvent.RAW_CAPTURE_CHANGED,
        WSEvent.SOURCE_IMPORT_UPDATED,
    }
)


def get_pubsub() -> EventBus:
    """Return the active event bus backend."""
    return get_events()


def _invalidate_graph_caches_for_event(event: str, org_id: str | None) -> None:
    if org_id is not None and event in GRAPH_MUTATION_EVENTS:
        invalidate_graph_caches(org_id)


async def _graph_cache_subscriber(event: str, data: dict[str, Any], org_id: str | None) -> None:
    """Retire this pod's graph caches for mutations announced by any pod."""
    del data
    _invalidate_graph_caches_for_event(event, org_id)


async def init_pubsub(local_broadcast_callback: Any) -> None:
    """Initialize the active event bus on server startup."""
    bus = get_pubsub()
    await bus.connect()
    await bus.subscribe(_graph_cache_subscriber)
    await bus.subscribe(local_broadcast_callback)


async def shutdown_pubsub() -> None:
    """Shutdown the active event bus on server shutdown."""
    bus = get_pubsub()
    await bus.disconnect()


async def publish_event(event: str, data: dict[str, Any], *, org_id: str | None = None) -> None:
    """Publish an event through the active coordination backend."""
    _invalidate_graph_caches_for_event(event, org_id)
    bus = get_pubsub()
    await bus.publish(event, data, org_id)
