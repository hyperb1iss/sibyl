"""Session revocations reach every replica's validity cache.

Each replica here is its own ``AccessSessionCache`` and ``CacheInvalidationBus``
over one shared channel, the way separate API processes share one Redis
channel. ``FakeChannel`` delivers every publish to every subscriber, the
sender included, as Redis pub/sub does. A Redis-backed run of the same proof,
including a dropped subscription, lives at the end of the file and needs
SIBYL_LIVE_REDIS_HOST and SIBYL_LIVE_REDIS_PORT.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from sibyl import cache_invalidation
from sibyl.auth.jwt import create_access_token
from sibyl.auth.session_cache import AccessSessionCache, access_session_cache
from sibyl.cache_invalidation import (
    SESSIONS_TOPIC,
    announce_sessions_invalidated,
    apply_session_invalidation,
)
from sibyl.coordination.invalidation import CacheInvalidationBus, get_cache_invalidation_bus
from sibyl.persistence.surreal import auth_runtime as surreal_auth_runtime
from sibyl.persistence.surreal.auth_runtime import _common as auth_common
from sibyl_core.auth import AuthSession
from tests.invalidation_channel import FakeChannel, FakeTransport


def _session(*, user_id: UUID | None = None, organization_id: UUID | None = None) -> AuthSession:
    now = datetime.now(UTC)
    return AuthSession(
        id=uuid4(),
        user_id=user_id or uuid4(),
        organization_id=organization_id or uuid4(),
        expires_at=now + timedelta(minutes=60),
        refresh_token_expires_at=now + timedelta(days=30),
        last_active_at=now,
    )


@dataclass
class SessionTable:
    """The shared database: which sessions exist and are not revoked."""

    active: dict[UUID, AuthSession] = field(default_factory=dict)
    reads: int = 0


@dataclass
class Replica:
    cache: AccessSessionCache
    bus: CacheInvalidationBus
    table: SessionTable

    async def validate(self, session_id: UUID) -> bool:
        """The cache-then-database shape of ``validate_access_session``."""
        cached = self.cache.get(session_id)
        if cached is not None:
            return cached
        generation = self.cache.generation
        self.table.reads += 1
        session = self.table.active.get(session_id)
        if session is None:
            self.cache.mark_revoked(session_id)
            return False
        self.cache.store_session(session, generation=generation)
        return True

    async def revoke(self, session: AuthSession) -> None:
        """The order every revocation site follows: database, own cache, then peers."""
        self.table.active.pop(session.id, None)
        self.cache.mark_revoked(session.id, user_id=session.user_id)
        await self.bus.announce(SESSIONS_TOPIC, {"session_ids": [str(session.id)]})


async def _replica(table: SessionTable, channel: FakeChannel | None) -> Replica:
    cache = AccessSessionCache()
    bus = CacheInvalidationBus()
    bus.register(SESSIONS_TOPIC, lambda payload: apply_session_invalidation(cache, payload))
    bus.on_reset(cache.clear)
    if channel is not None:
        await bus.attach(FakeTransport(channel))
    return Replica(cache=cache, bus=bus, table=table)


@pytest.fixture(autouse=True)
def _clear_global_cache() -> Iterator[None]:
    access_session_cache.clear()
    yield
    access_session_cache.clear()


async def test_without_a_channel_a_peer_keeps_vouching_for_a_revoked_session() -> None:
    """The single-process behaviour, and what multi-replica deployments got before."""
    table = SessionTable()
    a, b = await _replica(table, None), await _replica(table, None)
    session = _session()
    table.active[session.id] = session

    assert await b.validate(session.id) is True
    await a.revoke(session)

    assert await a.validate(session.id) is False
    assert await b.validate(session.id) is True  # stale until the entry expires


async def test_a_revocation_on_one_replica_is_refused_by_the_other_at_once() -> None:
    table = SessionTable()
    channel = FakeChannel()
    a, b = await _replica(table, channel), await _replica(table, channel)
    session = _session()
    table.active[session.id] = session

    assert await b.validate(session.id) is True
    reads_before = table.reads
    assert await b.validate(session.id) is True
    assert table.reads == reads_before  # served from B's cache

    await a.revoke(session)

    assert await b.validate(session.id) is False
    assert table.reads == reads_before + 1  # B re-read the row instead of trusting its cache
    # The announcement echoes back to its sender, which keeps its own revoked marker.
    assert a.cache.get(session.id) is False


async def test_user_and_organization_invalidations_drop_every_matching_session() -> None:
    table = SessionTable()
    channel = FakeChannel()
    a, b = await _replica(table, channel), await _replica(table, channel)
    user_id, org_id = uuid4(), uuid4()
    mine = [_session(user_id=user_id), _session(user_id=user_id)]
    org_sessions = [_session(organization_id=org_id), _session(organization_id=org_id)]
    bystander = _session()
    for session in (*mine, *org_sessions, bystander):
        table.active[session.id] = session
        assert await b.validate(session.id) is True

    for session in (*mine, *org_sessions):
        table.active.pop(session.id)
    await a.bus.announce(
        SESSIONS_TOPIC, {"user_ids": [str(user_id)], "organization_ids": [str(org_id)]}
    )

    for session in (*mine, *org_sessions):
        assert await b.validate(session.id) is False
    assert b.cache.get(bystander.id) is True


async def test_a_fill_that_raced_an_invalidation_is_not_cached() -> None:
    """B read the row just before A's revocation committed; its answer must not stick."""
    table = SessionTable()
    channel = FakeChannel()
    a, b = await _replica(table, channel), await _replica(table, channel)
    session = _session()
    table.active[session.id] = session

    generation = b.cache.generation
    read_before_commit = table.active[session.id]
    await a.revoke(session)
    b.cache.store_session(read_before_commit, generation=generation)

    assert b.cache.get(session.id) is None
    assert await b.validate(session.id) is False


async def test_resubscribing_forgets_everything_a_missed_message_could_have_changed() -> None:
    table = SessionTable()
    b = await _replica(table, FakeChannel())
    session = _session()
    table.active[session.id] = session
    assert await b.validate(session.id) is True

    await b.bus.reset()

    assert b.cache.get(session.id) is None


async def test_announcing_without_a_transport_is_a_no_op() -> None:
    bus = CacheInvalidationBus()
    handler = AsyncMock()
    bus.register(SESSIONS_TOPIC, handler)

    await bus.announce(SESSIONS_TOPIC, {"session_ids": [str(uuid4())]})

    assert bus.broadcasting is False
    handler.assert_not_awaited()


async def test_announce_helper_reaches_a_peer_through_the_process_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = FakeChannel()
    process_bus = CacheInvalidationBus()
    monkeypatch.setattr(cache_invalidation, "get_cache_invalidation_bus", lambda: process_bus)
    await process_bus.attach(FakeTransport(channel))
    peer = await _replica(SessionTable(), channel)
    session = _session()
    peer.table.active[session.id] = session
    assert await peer.validate(session.id) is True

    await announce_sessions_invalidated(session_ids=[session.id, None])

    assert peer.cache.get(session.id) is None


class _GatedSessions:
    """A session repository whose read waits until the test releases it."""

    def __init__(self, session: AuthSession) -> None:
        self.session = session
        self.reading = asyncio.Event()
        self.release = asyncio.Event()

    async def get_session_by_id(self, session_id: UUID) -> AuthSession | None:
        self.reading.set()
        await self.release.wait()
        return self.session if session_id == self.session.id else None


async def test_validate_access_session_does_not_cache_a_read_that_raced_a_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pydantic import SecretStr

    from sibyl.config import settings
    from sibyl.persistence.surreal.auth_runtime import sessions as session_runtime

    monkeypatch.setattr(settings, "jwt_secret", SecretStr("revocation-race-proof-secret-" * 3))
    session = _session()
    token = create_access_token(
        user_id=session.user_id, organization_id=session.organization_id, session_id=session.id
    )
    gated = _GatedSessions(session)

    class _Scope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(session_runtime, "_auth_client_scope", _Scope)
    monkeypatch.setattr(
        session_runtime.SurrealSessionRepository, "from_client", lambda client: gated
    )

    validation = asyncio.create_task(session_runtime.validate_access_session(token))
    await gated.reading.wait()
    # Another replica revoked the session while this read was in flight.
    apply_session_invalidation(access_session_cache, {"session_ids": [str(session.id)]})
    gated.release.set()

    assert await validation is True  # the read itself predates the revocation
    assert access_session_cache.get(session.id) is None  # but it does not stick


class _RecordingClient:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    async def execute_query(self, query: str, **params: object) -> object:
        self.queries.append(query)
        return self.rows


async def test_repository_revocations_announce_to_peers(monkeypatch: pytest.MonkeyPatch) -> None:
    announced = AsyncMock()
    monkeypatch.setattr(auth_common, "announce_sessions_invalidated", announced)
    session = _session()
    repo = surreal_auth_runtime.SurrealSessionRepository(_RecordingClient([{"uuid": "x"}]))

    await repo.revoke_session(session.id, session.user_id)
    await repo.revoke_loaded_session(session)
    await repo.revoke_all_sessions(session.user_id)

    assert [call.kwargs for call in announced.await_args_list] == [
        {"session_ids": [session.id]},
        {"session_ids": [session.id]},
        {"user_ids": [session.user_id]},
    ]


# --- Redis -------------------------------------------------------------------


def _live_redis() -> tuple[str, int]:
    host = os.environ.get("SIBYL_LIVE_REDIS_HOST", "")
    port = os.environ.get("SIBYL_LIVE_REDIS_PORT", "")
    if not host or not port:
        pytest.skip("Redis-backed invalidation tests need SIBYL_LIVE_REDIS_HOST/PORT")
    return host, int(port)


async def _eventually(predicate) -> None:
    """Poll for up to five seconds: pub/sub delivery is asynchronous."""
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached within five seconds")


async def test_redis_channel_carries_revocations_and_survives_a_dropped_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from redis.asyncio import Redis

    from sibyl.config import settings
    from sibyl.coordination._redis.events import PUBSUB_CHANNEL, PUBSUB_DB, RedisEventBus
    from sibyl.coordination.invalidation import INVALIDATION_CHANNEL

    host, port = _live_redis()
    monkeypatch.setattr(settings, "redis_host", host)
    monkeypatch.setattr(settings, "redis_port", port)

    table = SessionTable()
    a, b = await _replica(table, None), await _replica(table, None)
    b_resets = 0

    async def b_reset() -> None:
        nonlocal b_resets
        b_resets += 1
        await b.bus.reset()

    await a.bus.attach(RedisEventBus(channel=INVALIDATION_CHANNEL, on_subscribed=a.bus.reset))
    await b.bus.attach(RedisEventBus(channel=INVALIDATION_CHANNEL, on_subscribed=b_reset))
    browser_events: list[str] = []

    async def browser(event: str, data: dict[str, Any], org_id: str | None) -> None:
        browser_events.append(event)

    websocket_bus = RedisEventBus()
    await websocket_bus.subscribe(browser)
    admin = Redis(host=host, port=port, db=PUBSUB_DB, decode_responses=True)
    try:
        await _eventually(lambda: b_resets >= 1)

        first = _session()
        table.active[first.id] = first
        assert await b.validate(first.id) is True
        await a.revoke(first)
        await _eventually(lambda: b.cache.get(first.id) is None)
        assert await b.validate(first.id) is False

        # Drop every pub/sub connection; Redis keeps no backlog for them.
        await admin.client_kill_filter(_type="pubsub")
        second = _session()
        table.active[second.id] = second
        await _eventually(lambda: b_resets >= 2)

        assert await b.validate(second.id) is True
        await a.revoke(second)
        await _eventually(lambda: b.cache.get(second.id) is None)

        await websocket_bus.publish("probe", {})
        await _eventually(lambda: "probe" in browser_events)
        assert browser_events == ["probe"]  # invalidations never reach the browser channel
        assert INVALIDATION_CHANNEL != PUBSUB_CHANNEL
    finally:
        await admin.aclose()
        await websocket_bus.disconnect()
        await a.bus.detach()
        await b.bus.detach()


def test_process_bus_is_a_singleton() -> None:
    assert get_cache_invalidation_bus() is get_cache_invalidation_bus()
