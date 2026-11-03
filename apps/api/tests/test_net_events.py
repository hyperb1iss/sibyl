"""Long-poll wakeups: only matching waiters wake, once their message has settled."""

from __future__ import annotations

import asyncio

import pytest

from sibyl.api import net_events
from sibyl.api.event_types import WSEvent
from sibyl.api.net_events import NetWakeups, net_wakeup_subscriber

ORG = "org-a"


@pytest.fixture(autouse=True)
def quick_settle(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real wake delay is the feed's two-second settle window; the
    # matching rules are the same at a tenth of a second.
    monkeypatch.setattr(net_events, "WAKE_AFTER_SECONDS", 0.1)


async def test_a_post_wakes_only_matching_waiters_after_the_settle_delay() -> None:
    wakeups = NetWakeups()
    loop = asyncio.get_running_loop()
    with (
        wakeups.watch(ORG, frozenset({"#reviews"}), None) as reviews,
        wakeups.watch(ORG, frozenset({"#builds"}), None) as builds,
        wakeups.watch(ORG, None, "gh:acme/app#42") as one_thread,
        wakeups.watch("org-b", None, None) as elsewhere,
    ):
        started = loop.time()
        assert wakeups.notify(ORG, "#reviews", "gh:acme/app#42") == 2

        await reviews.wait(5)
        assert 0.09 <= loop.time() - started < 1
        await one_thread.wait(5)
        assert not builds.signal.is_set()
        assert not elsewhere.signal.is_set()
    assert wakeups.waiting() == 0


async def test_an_unwoken_waiter_returns_at_its_timeout() -> None:
    wakeups = NetWakeups()
    loop = asyncio.get_running_loop()
    with wakeups.watch(ORG, frozenset({"#quiet"}), None) as waiter:
        started = loop.time()
        wakeups.notify(ORG, "#loud", "")
        await waiter.wait(0.3)
        assert 0.29 <= loop.time() - started < 1


async def test_a_waiter_registered_inside_the_settle_window_still_wakes() -> None:
    wakeups = NetWakeups()
    loop = asyncio.get_running_loop()
    started = loop.time()
    wakeups.notify(ORG, "#reviews", "")

    with wakeups.watch(ORG, frozenset({"#reviews"}), None) as late:
        assert late.signal.is_set()
        await late.wait(5)
        assert loop.time() - started < 1

    # Once the window has passed the announcement is forgotten.
    with wakeups.watch(ORG, frozenset({"#reviews"}), None) as later:
        assert not later.signal.is_set()


async def test_a_post_during_a_read_is_not_lost() -> None:
    wakeups = NetWakeups()
    with wakeups.watch(ORG, None, None) as waiter:
        # The route reads between waits; a post announced then sets the
        # signal, and the next wait returns for it instead of sleeping on.
        wakeups.notify(ORG, "#reviews", "")
        await asyncio.wait_for(waiter.wait(30), 2)


async def test_the_bus_subscriber_feeds_the_process_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wakeups = NetWakeups()
    monkeypatch.setattr(net_events, "net_wakeups", wakeups)
    with wakeups.watch(ORG, frozenset({"#reviews"}), None) as waiter:
        await net_wakeup_subscriber(WSEvent.ENTITY_CREATED, {"channel": "#reviews"}, ORG)
        assert not waiter.signal.is_set()
        await net_wakeup_subscriber(
            WSEvent.NET_MESSAGE_POSTED, {"channel": "#reviews", "thread": ""}, None
        )
        assert not waiter.signal.is_set()
        await net_wakeup_subscriber(
            WSEvent.NET_MESSAGE_POSTED, {"channel": "#reviews", "thread": ""}, ORG
        )
        assert waiter.signal.is_set()
