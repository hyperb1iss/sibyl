"""Derived sibylnet presence in both coordination backends.

The local registry runs everywhere. The Redis one runs when
SIBYL_LIVE_REDIS_HOST and SIBYL_LIVE_REDIS_PORT name a live Redis or Valkey,
including the case it exists for: two API replicas, each holding its own
connection, agreeing on who is watching. Times are epoch seconds the caller
passes in, so no case sleeps.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sibyl.config import settings
from sibyl.coordination._local.presence import LocalPresenceRegistry
from sibyl.coordination._redis.presence import RedisPresenceRegistry, _keys
from sibyl.coordination.presence import Watcher, WatcherSeen

NOW = 1_800_000_000.0


def watcher(
    channel: str | None = "@ada",
    user: str = "user-ada",
    agent: str | None = "claude-code",
    session: str | None = "session-1",
) -> Watcher:
    return Watcher(channel=channel, user_id=user, agent=agent, session=session)


def listed(seen: list[WatcherSeen]) -> dict[Watcher, float]:
    return {entry.watcher: entry.last_seen for entry in seen}


@pytest.fixture
def live_redis_settings() -> Iterator[None]:
    """Point Redis clients at the live server without switching the coordination backend.

    Written around pydantic's assignment hook and restored with the set-field
    record, as the event-bus tests do: an assigned Redis field would switch
    every later test in the session to the redis backend.
    """
    host = os.environ.get("SIBYL_LIVE_REDIS_HOST", "")
    port = os.environ.get("SIBYL_LIVE_REDIS_PORT", "")
    if not host or not port:
        pytest.skip("Redis-backed presence tests need SIBYL_LIVE_REDIS_HOST/PORT")
    names = ("redis_host", "redis_port")
    original = {name: getattr(settings, name) for name in names}
    fields_set = set(settings.model_fields_set)
    object.__setattr__(settings, "redis_host", host)
    object.__setattr__(settings, "redis_port", int(port))
    yield
    for name, value in original.items():
        object.__setattr__(settings, name, value)
    settings.model_fields_set.clear()
    settings.model_fields_set.update(fields_set)


@pytest.fixture(params=["local", "redis"])
async def replicas(request: pytest.FixtureRequest) -> AsyncIterator[SimpleNamespace]:
    """Two replicas' registries and two fresh organizations.

    One process holds one local registry, so both local "replicas" are the
    same object; the Redis replicas are separate clients on separate
    connections.
    """
    orgs = SimpleNamespace(a=f"org-{uuid4().hex}", b=f"org-{uuid4().hex}")
    if request.param == "local":
        registry = LocalPresenceRegistry()
        yield SimpleNamespace(first=registry, second=registry, orgs=orgs, redis=False)
        return
    request.getfixturevalue("live_redis_settings")
    first, second = RedisPresenceRegistry(), RedisPresenceRegistry()
    yield SimpleNamespace(first=first, second=second, orgs=orgs, redis=True)
    client = await first._get_redis()
    await client.delete(*_keys(orgs.a), *_keys(orgs.b))
    for registry in (first, second):
        if registry._redis is not None:
            await registry._redis.aclose()


async def test_a_watcher_is_listed_until_its_window_closes(replicas: SimpleNamespace) -> None:
    ada = watcher()
    await replicas.first.note(replicas.orgs.a, [ada], now=NOW, until=NOW + 145)

    assert listed(await replicas.second.watching(replicas.orgs.a, now=NOW + 144.5)) == {ada: NOW}
    assert await replicas.second.watching(replicas.orgs.a, now=NOW + 145) == []


async def test_a_watcher_noted_on_one_replica_is_seen_from_the_other(
    replicas: SimpleNamespace,
) -> None:
    on_first, on_second = watcher(session="first"), watcher(session="second")
    await replicas.first.note(replicas.orgs.a, [on_first], now=NOW, until=NOW + 120)
    await replicas.second.note(replicas.orgs.a, [on_second], now=NOW + 1, until=NOW + 121)

    for registry in (replicas.first, replicas.second):
        assert listed(await registry.watching(replicas.orgs.a, now=NOW + 2)) == {
            on_first: NOW,
            on_second: NOW + 1,
        }


async def test_a_shorter_later_read_never_closes_a_longer_window_early(
    replicas: SimpleNamespace,
) -> None:
    ada = watcher()
    # A 25 second long poll, then a peek from the same session on another
    # replica: the peek moves the sighting forward but not the window back.
    await replicas.first.note(replicas.orgs.a, [ada], now=NOW, until=NOW + 145)
    await replicas.second.note(replicas.orgs.a, [ada], now=NOW + 5, until=NOW + 125)
    # A late write of an older sighting moves nothing backwards either.
    await replicas.first.note(replicas.orgs.a, [ada], now=NOW + 1, until=NOW + 121)

    assert listed(await replicas.first.watching(replicas.orgs.a, now=NOW + 130)) == {ada: NOW + 5}
    assert await replicas.first.watching(replicas.orgs.a, now=NOW + 145) == []


async def test_each_channel_agent_and_session_is_its_own_watcher(
    replicas: SimpleNamespace,
) -> None:
    readers = [
        watcher(channel="@ada"),
        watcher(channel="#reviews"),
        watcher(channel=None),
        watcher(agent="codex"),
        watcher(session="session-2"),
        watcher(agent=None, session=None),
        watcher(user="user-grace"),
    ]
    await replicas.first.note(replicas.orgs.a, readers, now=NOW, until=NOW + 120)

    assert listed(await replicas.second.watching(replicas.orgs.a, now=NOW)) == dict.fromkeys(
        readers, NOW
    )


async def test_organizations_never_see_each_others_watchers(replicas: SimpleNamespace) -> None:
    ada, ozzy = watcher(), watcher(user="user-ozzy")
    await replicas.first.note(replicas.orgs.a, [ada], now=NOW, until=NOW + 120)
    await replicas.second.note(replicas.orgs.b, [ozzy], now=NOW, until=NOW + 120)

    assert listed(await replicas.first.watching(replicas.orgs.a, now=NOW)) == {ada: NOW}
    assert listed(await replicas.first.watching(replicas.orgs.b, now=NOW)) == {ozzy: NOW}


async def test_expired_watchers_are_dropped_on_the_next_write(replicas: SimpleNamespace) -> None:
    gone, staying = watcher(session="gone"), watcher(session="staying")
    await replicas.first.note(replicas.orgs.a, [gone], now=NOW, until=NOW + 120)
    await replicas.first.note(replicas.orgs.a, [staying], now=NOW + 200, until=NOW + 320)

    assert listed(await replicas.second.watching(replicas.orgs.a, now=NOW + 200)) == {
        staying: NOW + 200
    }
    if replicas.redis:
        # Nothing of the expired watcher is left behind in either set, and
        # both keys expire on their own once the organization goes quiet.
        client = await replicas.first._get_redis()
        for key in _keys(replicas.orgs.a):
            assert await client.zcard(key) == 1
            assert 0 < await client.pttl(key) <= 120_000
    else:
        windows = replicas.first._orgs[replicas.orgs.a]
        assert set(windows) == {staying}


async def test_an_organization_nobody_watches_holds_nothing(replicas: SimpleNamespace) -> None:
    assert await replicas.first.watching(replicas.orgs.a, now=NOW) == []
    await replicas.first.note(replicas.orgs.a, [], now=NOW, until=NOW + 120)
    assert await replicas.first.watching(replicas.orgs.a, now=NOW) == []
    if not replicas.redis:
        assert replicas.orgs.a not in replicas.first._orgs
