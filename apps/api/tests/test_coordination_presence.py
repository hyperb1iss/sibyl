"""Derived sibylnet presence in both coordination backends.

The local registry runs everywhere, on a clock the test moves by hand. The
Redis one runs when SIBYL_LIVE_REDIS_HOST and SIBYL_LIVE_REDIS_PORT name a
live Redis or Valkey, on Redis's own clock with windows of about a second,
including the case it exists for: two API replicas, each holding its own
connection, agreeing on who is watching.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sibyl.config import settings
from sibyl.coordination._local.presence import LocalPresenceRegistry
from sibyl.coordination._redis.presence import (
    RedisPresenceRegistry,
    _reader_keys,
    presence_keys,
)
from sibyl.coordination.presence import MAX_ROWS_PER_READER, Watcher, WatcherSeen

ADA, GRACE, OZZY = "user-ada", "user-grace", "user-ozzy"


def listed(seen: list[WatcherSeen]) -> set[Watcher]:
    return {entry.watcher for entry in seen}


def watcher(
    channel: str | None = "@ada",
    user: str = ADA,
    agent: str | None = "claude-code",
    session: str | None = "session-1",
) -> Watcher:
    return Watcher(channel=channel, user_id=user, agent=agent, session=session)


async def note(
    registry: object,
    org: str,
    *,
    channels: list[str | None] | None = None,
    user: str = ADA,
    agent: str | None = "claude-code",
    session: str | None = "session-1",
    window: float = 1.0,
) -> None:
    await registry.note(  # type: ignore[attr-defined]
        org,
        user_id=user,
        agent=agent,
        session=session,
        channels=["@ada"] if channels is None else channels,
        window=window,
    )


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
    """Two replicas' registries, two fresh organizations, and a way to let time pass.

    One process holds one local registry, so both local "replicas" are the
    same object; the Redis replicas are separate clients on separate
    connections, and time passes on Redis's clock.
    """
    orgs = SimpleNamespace(a=f"org-{uuid4().hex}", b=f"org-{uuid4().hex}")
    if request.param == "local":
        now = [1_800_000_000.0]
        registry = LocalPresenceRegistry(clock=lambda: now[0])

        async def advance(seconds: float) -> None:
            now[0] += seconds

        yield SimpleNamespace(
            first=registry, second=registry, orgs=orgs, redis=False, advance=advance
        )
        return
    request.getfixturevalue("live_redis_settings")
    first, second = RedisPresenceRegistry(), RedisPresenceRegistry()
    yield SimpleNamespace(first=first, second=second, orgs=orgs, redis=True, advance=asyncio.sleep)
    client = await first._get_redis()
    for org in (orgs.a, orgs.b):
        await client.delete(*presence_keys(org, [ADA, GRACE, OZZY]))
    for registry in (first, second):
        if registry._redis is not None:
            await registry._redis.aclose()


async def test_a_watcher_is_listed_until_its_window_closes(replicas: SimpleNamespace) -> None:
    await note(replicas.first, replicas.orgs.a, window=1.0)

    assert listed(await replicas.second.watching(replicas.orgs.a)) == {watcher()}
    await replicas.advance(1.2)
    assert await replicas.second.watching(replicas.orgs.a) == []


async def test_a_watcher_noted_on_one_replica_is_seen_from_the_other(
    replicas: SimpleNamespace,
) -> None:
    await note(replicas.first, replicas.orgs.a, session="first")
    await note(replicas.second, replicas.orgs.a, session="second", user=GRACE)

    for registry in (replicas.first, replicas.second):
        assert listed(await registry.watching(replicas.orgs.a)) == {
            watcher(session="first"),
            watcher(session="second", user=GRACE),
        }


async def test_a_shorter_later_read_never_closes_a_longer_window_early(
    replicas: SimpleNamespace,
) -> None:
    # A long poll, then a peek from the same session on another replica: the
    # peek moves the sighting forward but not the window back.
    await note(replicas.first, replicas.orgs.a, window=2.0)
    before = (await replicas.first.watching(replicas.orgs.a))[0].last_seen
    await replicas.advance(0.3)
    await note(replicas.second, replicas.orgs.a, window=0.5)
    await replicas.advance(1.0)

    [entry] = await replicas.first.watching(replicas.orgs.a)
    assert entry.watcher == watcher()
    assert entry.last_seen > before
    await replicas.advance(1.0)
    assert await replicas.first.watching(replicas.orgs.a) == []


async def test_each_channel_agent_and_session_is_its_own_watcher(
    replicas: SimpleNamespace,
) -> None:
    await note(replicas.first, replicas.orgs.a, channels=["@ada", "#reviews", None])
    await note(replicas.first, replicas.orgs.a, agent="codex")
    await note(replicas.first, replicas.orgs.a, session="session-2")
    await note(replicas.first, replicas.orgs.a, agent=None, session=None)
    await note(replicas.first, replicas.orgs.a, user=GRACE)

    assert listed(await replicas.second.watching(replicas.orgs.a)) == {
        watcher(channel="@ada"),
        watcher(channel="#reviews"),
        watcher(channel=None),
        watcher(agent="codex"),
        watcher(session="session-2"),
        watcher(agent=None, session=None),
        watcher(user=GRACE),
    }


async def test_organizations_never_see_each_others_watchers(replicas: SimpleNamespace) -> None:
    await note(replicas.first, replicas.orgs.a)
    await note(replicas.second, replicas.orgs.b, user=OZZY)

    assert listed(await replicas.first.watching(replicas.orgs.a)) == {watcher()}
    assert listed(await replicas.first.watching(replicas.orgs.b)) == {watcher(user=OZZY)}


async def test_rotating_sessions_cannot_grow_a_reader_past_the_bound(
    replicas: SimpleNamespace,
) -> None:
    """Labels are whatever a client sends; presence still scales with members."""
    await note(replicas.first, replicas.orgs.a, user=GRACE, window=60)
    channels: list[str | None] = [f"#room-{index}" for index in range(32)]
    for index in range(600):
        await note(
            replicas.first if index % 2 else replicas.second,
            replicas.orgs.a,
            channels=channels,
            session=f"rotating-{index}",
            window=60,
        )

    seen = await replicas.second.watching(replicas.orgs.a)
    adas = [entry.watcher for entry in seen if entry.watcher.user_id == ADA]
    assert len(adas) == MAX_ROWS_PER_READER
    # The newest read is the one kept whole; another member is untouched.
    assert {row.channel for row in adas if row.session == "rotating-599"} == set(channels)
    assert watcher(user=GRACE) in {entry.watcher for entry in seen}
    if replicas.redis:
        client = await replicas.first._get_redis()
        for key in _reader_keys(replicas.orgs.a, ADA):
            assert await client.zcard(key) == MAX_ROWS_PER_READER


async def test_a_readers_expired_rows_are_dropped_on_their_next_write(
    replicas: SimpleNamespace,
) -> None:
    await note(replicas.first, replicas.orgs.a, session="gone", window=0.5)
    await replicas.advance(0.8)
    await note(replicas.first, replicas.orgs.a, session="staying", window=5)

    assert listed(await replicas.second.watching(replicas.orgs.a)) == {watcher(session="staying")}
    if replicas.redis:
        # Nothing of the expired row is left behind, and every key expires on
        # its own once the organization goes quiet.
        client = await replicas.first._get_redis()
        for key in _reader_keys(replicas.orgs.a, ADA):
            assert await client.zcard(key) == 1
        for key in presence_keys(replicas.orgs.a, [ADA]):
            assert 0 < await client.pttl(key) <= 5_000
    else:
        assert set(replicas.first._orgs[replicas.orgs.a][ADA]) == {
            ("@ada", "claude-code", "staying")
        }


async def test_an_organization_nobody_watches_holds_nothing(replicas: SimpleNamespace) -> None:
    assert await replicas.first.watching(replicas.orgs.a) == []
    await note(replicas.first, replicas.orgs.a, channels=[])
    assert await replicas.first.watching(replicas.orgs.a) == []
    if not replicas.redis:
        assert replicas.orgs.a not in replicas.first._orgs
