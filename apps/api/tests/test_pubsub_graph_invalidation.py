"""Entity events retire the graph caches of the organization they name."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sibyl.api import pubsub
from sibyl.api.event_types import WSEvent
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
