"""Entity events retire the graph caches of the organization they name."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl.api import pubsub
from sibyl.api.event_types import WSEvent
from sibyl_core.runtime_ports import install_graph_update_announcer
from sibyl_core.services import graph_cache_invalidation as invalidation
from sibyl_core.services.graph_cache_invalidation import graph_generation

pytestmark = pytest.mark.asyncio


@pytest.fixture
def bus(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    subscribers: list = []

    async def subscribe(subscriber) -> None:
        subscribers.append(subscriber)

    fake = SimpleNamespace(
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        publish=AsyncMock(),
        subscribe=subscribe,
        subscribers=subscribers,
    )
    monkeypatch.setattr(pubsub, "get_pubsub", lambda: fake)
    return fake


async def test_publishing_an_entity_event_retires_the_organization_graph(bus) -> None:
    org = "org-events"
    before = graph_generation(org)

    await pubsub.publish_event(WSEvent.ENTITY_UPDATED, {"id": "task-1"}, org_id=org)

    assert graph_generation(org) == before + 1
    bus.publish.assert_awaited_once_with(WSEvent.ENTITY_UPDATED, {"id": "task-1"}, org)


async def test_events_without_graph_meaning_leave_the_caches_alone(bus) -> None:
    org = "org-quiet"
    before = graph_generation(org)

    await pubsub.publish_event(WSEvent.ENTITY_PENDING, {}, org_id=org)
    await pubsub.publish_event(WSEvent.HEALTH_UPDATE, {}, org_id=org)
    await pubsub.publish_event(WSEvent.ENTITY_DELETED, {}, org_id=None)

    assert graph_generation(org) == before


async def test_bus_subscription_retires_caches_for_events_from_other_pods(bus) -> None:
    await pubsub.init_pubsub(AsyncMock())
    assert pubsub._graph_cache_subscriber in bus.subscribers

    org = "org-remote"
    before = graph_generation(org)
    await pubsub._graph_cache_subscriber(WSEvent.ENTITY_CREATED, {"id": "x"}, org)

    assert graph_generation(org) == before + 1


@pytest.fixture
def announcer():
    """The API announcer, over a pending set that starts and ends empty.

    The set is process-global and every structural write in the suite marks
    it, so a flush here must see only what this test wrote.
    """
    invalidation._pending_announcements.clear()
    install_graph_update_announcer(pubsub.announce_graph_update)
    yield
    install_graph_update_announcer(None)
    invalidation._pending_announcements.clear()


async def test_structural_writes_are_announced_once_at_the_next_flush(bus, announcer) -> None:
    org = "org-announce"
    invalidation.invalidate_graph_caches(org)
    assert org in invalidation.pending_graph_updates()

    announced = await invalidation.announce_graph_updates()

    assert org in announced
    bus.publish.assert_awaited_once_with(WSEvent.GRAPH_UPDATED, {"organization_id": org}, org)
    assert org not in invalidation.pending_graph_updates()
    bus.publish.reset_mock()
    assert await invalidation.announce_graph_updates() == frozenset()
    bus.publish.assert_not_awaited()


async def test_an_announcement_retires_caches_without_announcing_again(bus, announcer) -> None:
    org = "org-echo"
    before = graph_generation(org)

    await pubsub._graph_cache_subscriber(WSEvent.GRAPH_UPDATED, {"organization_id": org}, org)
    await pubsub.publish_event(WSEvent.ENTITY_UPDATED, {"id": "x"}, org_id=org)

    assert graph_generation(org) == before + 2
    assert org not in invalidation.pending_graph_updates()
    assert await invalidation.announce_graph_updates(org) == frozenset()


async def test_worker_job_end_flushes_pending_announcements(bus, announcer) -> None:
    from sibyl.jobs import worker

    org = "org-worker"
    invalidation.invalidate_graph_caches(org)

    await worker.job_end({})

    bus.publish.assert_awaited_once_with(WSEvent.GRAPH_UPDATED, {"organization_id": org}, org)


async def test_background_lease_release_announces_the_organization(
    bus, announcer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sibyl_core.services import graph_client

    org = "org-lease"
    invalidation.invalidate_graph_caches(org)
    invalidation.invalidate_graph_caches("org-other")

    async def closed(group_id: str, lease) -> None:
        return None

    monkeypatch.setattr(graph_client, "_close_background_lease", closed)
    lease = graph_client._BackgroundLease(client=object(), users=1)  # type: ignore[arg-type]
    await graph_client._release_background_lease(org, lease)

    bus.publish.assert_awaited_once_with(WSEvent.GRAPH_UPDATED, {"organization_id": org}, org)
    assert "org-other" in invalidation.pending_graph_updates()
    invalidation._pending_announcements.discard("org-other")
