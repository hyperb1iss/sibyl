"""Session revocations reach every replica's validity cache.

Each replica here is its own ``AccessSessionCache`` and ``CacheInvalidationBus``
over one shared channel, the way separate API processes share one Redis
channel. ``FakeChannel`` delivers every publish to every subscriber, the
sender included, as Redis pub/sub does. Announcing only queues a message, and
each topic is applied by its own worker, so tests ``settle`` the buses before
asserting what a peer did.

Redis-backed runs of the same proofs live at the end of the file and need
SIBYL_LIVE_REDIS_HOST and SIBYL_LIVE_REDIS_PORT: a dropped subscription, a
silently partitioned subscriber and publisher behind a black-holing proxy, and
a replica that starts while Redis is unreachable.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from sibyl import cache_invalidation
from sibyl.api.readiness import check_cache_invalidation_ready
from sibyl.auth.jwt import create_access_token
from sibyl.auth.session_cache import AccessSessionCache, access_session_cache
from sibyl.cache_invalidation import (
    SESSIONS_TOPIC,
    announce_sessions_invalidated,
    apply_session_invalidation,
)
from sibyl.coordination import invalidation as invalidation_module
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


async def settle(*replicas: Replica) -> None:
    """Publish every queued announcement, then apply every received one."""
    for replica in replicas:
        await replica.bus.flush()
    for replica in replicas:
        await replica.bus.drain()


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
    await settle(a, b)

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
    await settle(a, b)

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
    await settle(a, b)

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
    await settle(a, b)
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
    await process_bus.flush()
    await peer.bus.drain()

    assert peer.cache.get(session.id) is None


class _StalledTransport(FakeTransport):
    """A transport whose publishes hang, like a socket into a silent partition."""

    def __init__(self, channel: FakeChannel) -> None:
        super().__init__(channel)
        self.healed = asyncio.Event()
        self.attempts = 0

    async def publish(self, event: str, data: dict[str, Any], org_id: str | None = None) -> None:
        self.attempts += 1
        await self.healed.wait()
        await super().publish(event, data, org_id)


async def test_announcing_never_waits_on_an_unreachable_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(invalidation_module, "PUBLISH_ATTEMPT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(invalidation_module, "PUBLISH_RETRY_MAX_SECONDS", 0.05)
    table = SessionTable()
    channel = FakeChannel()
    a = await _replica(table, None)
    stalled = _StalledTransport(channel)
    await a.bus.attach(stalled)
    b = await _replica(table, channel)
    first, second = _session(), _session()
    for session in (first, second):
        table.active[session.id] = session
        assert await b.validate(session.id) is True

    started = time.monotonic()
    await a.revoke(first)
    await a.revoke(second)
    assert time.monotonic() - started < 0.05  # the revoking request is not held up

    await asyncio.sleep(0.3)
    assert stalled.attempts >= 2  # each attempt is cut off and retried, not abandoned
    assert a.bus.status()["pending_announcements"] == 2
    assert a.bus.status()["publish_failures"] >= 1
    assert b.cache.get(first.id) is True  # not delivered yet

    stalled.healed.set()
    await settle(a, b)
    assert b.cache.get(first.id) is None
    assert b.cache.get(second.id) is None
    assert a.bus.status()["pending_announcements"] == 0


async def test_a_slow_topic_does_not_hold_up_session_revocations() -> None:
    table = SessionTable()
    channel = FakeChannel()
    a, b = await _replica(table, channel), await _replica(table, channel)
    rebuilding, release = asyncio.Event(), asyncio.Event()

    async def slow_rebuild(payload: dict[str, Any]) -> None:
        rebuilding.set()
        await release.wait()

    b.bus.register("settings.runtime.changed", slow_rebuild, coalesce=True)
    session = _session()
    table.active[session.id] = session
    assert await b.validate(session.id) is True

    await a.bus.announce("settings.runtime.changed", {"keys": ["embedding_model"]})
    await a.revoke(session)
    await a.bus.flush()
    await rebuilding.wait()
    await b.bus._workers[SESSIONS_TOPIC].drain()

    assert b.cache.get(session.id) is None  # applied while the rebuild still runs
    release.set()
    await settle(a, b)


async def test_a_coalescing_topic_runs_once_for_a_burst() -> None:
    bus = CacheInvalidationBus()
    runs: list[dict[str, Any]] = []
    gate = asyncio.Event()

    async def handler(payload: dict[str, Any]) -> None:
        runs.append(payload)
        await gate.wait()

    bus.register("burst", handler, coalesce=True)
    bus.submit_local("burst", {"n": 1})
    await asyncio.sleep(0)  # the first run starts and holds
    for n in range(2, 6):
        bus.submit_local("burst", {"n": n})
    gate.set()
    await bus.drain()

    # One run that was in flight, and one more that covers everything after it.
    assert [payload["n"] for payload in runs] == [1, 2]


async def test_start_keeps_retrying_until_the_channel_is_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cache_invalidation, "ATTACH_RETRY_INITIAL_SECONDS", 0.01)
    bus = CacheInvalidationBus()
    monkeypatch.setattr(cache_invalidation, "get_cache_invalidation_bus", lambda: bus)
    channel = FakeChannel()
    attempts = 0

    class _Unreachable(FakeTransport):
        async def connect(self) -> None:
            raise ConnectionError("connection refused")

    def build(_bus: CacheInvalidationBus) -> FakeTransport:
        nonlocal attempts
        attempts += 1
        return _Unreachable(channel) if attempts <= 3 else FakeTransport(channel)

    monkeypatch.setattr(cache_invalidation, "build_invalidation_transport", build)
    session = _session()
    try:
        assert await cache_invalidation.start_cache_invalidation() is True
        assert bus.status()["state"] == "connecting"
        not_ready = check_cache_invalidation_ready()
        assert not_ready is not None
        assert not_ready.ready is False
        # Revocations made before joining are queued, not lost.
        await announce_sessions_invalidated(session_ids=[session.id])

        for _ in range(500):
            if bus.attached:
                break
            await asyncio.sleep(0.01)
        assert attempts == 4
        ready = check_cache_invalidation_ready()
        assert ready is not None
        assert ready.ready is True
        await bus.flush()
        assert bus.status()["pending_announcements"] == 0
    finally:
        await cache_invalidation.stop_cache_invalidation()


def test_a_single_process_has_no_invalidation_readiness_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cache_invalidation, "get_cache_invalidation_bus", CacheInvalidationBus)
    assert check_cache_invalidation_ready() is None


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


async def test_revoking_again_reannounces_to_heal_a_peer_that_missed_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    announced = AsyncMock()
    monkeypatch.setattr(auth_common, "announce_sessions_invalidated", announced)
    session = _session()
    already_revoked = session.model_copy(update={"revoked_at": datetime.now(UTC)})
    repo = surreal_auth_runtime.SurrealSessionRepository(_RecordingClient([]))

    assert await repo.revoke_session(session.id, session.user_id) is False
    assert await repo.revoke_loaded_session(already_revoked) is False

    assert [call.kwargs for call in announced.await_args_list] == [
        {"session_ids": [session.id]},
        {"session_ids": [session.id]},
    ]


def _forged_tokens(real: AuthSession) -> list[str]:
    """Tokens an unauthenticated caller can send to logout, all naming a real session."""
    import base64
    import json

    import jwt as pyjwt

    from sibyl.auth.jwt import create_refresh_token

    def b64(obj: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"sub": str(real.user_id), "sid": str(real.id), "typ": "access"}
    now = int(time.time())
    timed = {**claims, "iat": now, "exp": now + 600}
    refresh, _expires = create_refresh_token(
        user_id=real.user_id, organization_id=real.organization_id, session_id=real.id
    )
    return [
        f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64(claims)}.c2lnbmF0dXJl",  # bad signature
        pyjwt.encode(timed, "someone-elses-secret-" * 3, algorithm="HS256"),  # wrong key
        f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(timed)}.",  # unsigned
        refresh,  # validly signed, but a refresh token
        "not-a-token",
    ]


@pytest.fixture
def jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    from sibyl.config import settings

    monkeypatch.setattr(settings, "jwt_secret", SecretStr("logout-verification-proof-" * 3))


class _OneSessionStore:
    """The store as revoke_access_session sees it: one session, maybe already revoked."""

    def __init__(self, session: AuthSession | None) -> None:
        self.session = session
        self.revoked: list[AuthSession] = []

    async def get_session_by_id(
        self, session_id: UUID, *, include_inactive: bool = False
    ) -> AuthSession | None:
        assert include_inactive, "a revoked session must still be found to re-announce it"
        if self.session is not None and self.session.id == session_id:
            return self.session
        return None

    async def get_session_by_token(self, token: str) -> AuthSession | None:
        return None

    async def revoke_loaded_session(self, session: AuthSession) -> bool:
        self.revoked.append(session)
        await auth_common.announce_sessions_invalidated(session_ids=[session.id])
        return session.revoked_at is None


def _use_store(monkeypatch: pytest.MonkeyPatch, store: _OneSessionStore) -> None:
    from sibyl.persistence.surreal.auth_runtime import sessions as session_runtime

    class _Scope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    monkeypatch.setattr(session_runtime, "_auth_client_scope", _Scope)
    monkeypatch.setattr(
        session_runtime.SurrealSessionRepository, "from_client", lambda client: store
    )


async def test_a_forged_logout_touches_no_cache_and_announces_nothing(
    monkeypatch: pytest.MonkeyPatch, jwt_secret: None
) -> None:
    from sibyl.persistence.surreal.auth_runtime import sessions as session_runtime

    announced = AsyncMock()
    monkeypatch.setattr(auth_common, "announce_sessions_invalidated", announced)
    real = _session()

    def no_store() -> object:
        raise AssertionError("a forged token must not reach the session store")

    monkeypatch.setattr(session_runtime, "_auth_client_scope", no_store)
    generation = access_session_cache.generation

    for token in _forged_tokens(real) * 40:
        await session_runtime.revoke_access_session(token)

    assert access_session_cache.generation == generation
    announced.assert_not_awaited()


async def test_a_forged_logout_through_the_route_announces_nothing(
    monkeypatch: pytest.MonkeyPatch, jwt_secret: None
) -> None:
    from starlette.requests import Request

    from sibyl.api.routes import auth as auth_routes

    announced = AsyncMock()
    monkeypatch.setattr(auth_common, "announce_sessions_invalidated", announced)
    monkeypatch.setattr(auth_routes, "log_audit_event", AsyncMock())
    generation = access_session_cache.generation
    for token in _forged_tokens(_session()):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/auth/logout",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
            }
        )
        response = await auth_routes.logout(request=request)
        assert response.status_code == 204

    assert access_session_cache.generation == generation
    announced.assert_not_awaited()


async def test_a_signed_logout_for_a_session_that_does_not_exist_announces_nothing(
    monkeypatch: pytest.MonkeyPatch, jwt_secret: None
) -> None:
    from sibyl.persistence.surreal.auth_runtime import sessions as session_runtime

    announced = AsyncMock()
    monkeypatch.setattr(auth_common, "announce_sessions_invalidated", announced)
    _use_store(monkeypatch, _OneSessionStore(None))
    ghost = _session()
    token = create_access_token(
        user_id=ghost.user_id, organization_id=ghost.organization_id, session_id=ghost.id
    )
    generation = access_session_cache.generation

    await session_runtime.revoke_access_session(token)

    assert access_session_cache.generation == generation
    announced.assert_not_awaited()


async def test_a_signed_logout_cannot_end_another_users_session(
    monkeypatch: pytest.MonkeyPatch, jwt_secret: None
) -> None:
    from sibyl.persistence.surreal.auth_runtime import sessions as session_runtime

    announced = AsyncMock()
    monkeypatch.setattr(auth_common, "announce_sessions_invalidated", announced)
    victim = _session()
    store = _OneSessionStore(victim)
    _use_store(monkeypatch, store)
    token = create_access_token(user_id=uuid4(), organization_id=None, session_id=victim.id)

    await session_runtime.revoke_access_session(token)

    assert store.revoked == []
    announced.assert_not_awaited()


@pytest.mark.parametrize("already_revoked", [False, True], ids=["active", "already-revoked"])
async def test_a_signed_logout_for_a_real_session_revokes_and_announces(
    monkeypatch: pytest.MonkeyPatch, jwt_secret: None, already_revoked: bool
) -> None:
    from sibyl.persistence.surreal.auth_runtime import sessions as session_runtime

    announced = AsyncMock()
    monkeypatch.setattr(auth_common, "announce_sessions_invalidated", announced)
    session = _session()
    if already_revoked:
        session = session.model_copy(update={"revoked_at": datetime.now(UTC)})
    store = _OneSessionStore(session)
    _use_store(monkeypatch, store)
    # Expired tokens still end their session: logout must not need a refresh first.
    token = create_access_token(
        user_id=session.user_id,
        organization_id=session.organization_id,
        session_id=session.id,
        expires_in=timedelta(seconds=-60),
    )

    await session_runtime.revoke_access_session(token)

    assert store.revoked == [session]
    announced.assert_awaited_once_with(session_ids=[session.id])
    assert access_session_cache.get(session.id) is False


# --- Redis -------------------------------------------------------------------


def _live_redis() -> tuple[str, int]:
    host = os.environ.get("SIBYL_LIVE_REDIS_HOST", "")
    port = os.environ.get("SIBYL_LIVE_REDIS_PORT", "")
    if not host or not port:
        pytest.skip("Redis-backed invalidation tests need SIBYL_LIVE_REDIS_HOST/PORT")
    return host, int(port)


async def _eventually(predicate, *, seconds: float = 10.0) -> None:
    """Poll until ``predicate`` holds: pub/sub delivery and reconnects are asynchronous."""
    for _ in range(int(seconds * 100)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition not reached within {seconds}s")


@pytest.fixture
def live_redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, int]]:
    """Point the event bus at the live Redis, with timings short enough to test.

    Settings are written around pydantic's assignment hook and restored with
    their set-field record: an assigned Redis field would otherwise switch
    every later test in the session to the redis coordination backend.
    """
    from sibyl.config import settings
    from sibyl.coordination._redis import events as redis_events

    host, port = _live_redis()
    names = ("redis_host", "redis_port", "coordination_backend")
    original = {name: getattr(settings, name) for name in names}
    fields_set = set(settings.model_fields_set)
    object.__setattr__(settings, "redis_host", host)
    object.__setattr__(settings, "redis_port", port)
    monkeypatch.setattr(redis_events, "PING_INTERVAL_SECONDS", 0.2)
    monkeypatch.setattr(redis_events, "DEAD_AFTER_SECONDS", 1.0)
    monkeypatch.setattr(redis_events, "SOCKET_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(redis_events, "LISTEN_RETRY_MAX_SECONDS", 0.3)
    monkeypatch.setattr(invalidation_module, "PUBLISH_ATTEMPT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(invalidation_module, "PUBLISH_RETRY_MAX_SECONDS", 0.3)
    yield host, port
    for name, value in original.items():
        object.__setattr__(settings, name, value)
    settings.model_fields_set.clear()
    settings.model_fields_set.update(fields_set)


class BlackHoleProxy:
    """A TCP proxy to Redis that can silently swallow traffic, like a partition.

    While partitioned it keeps every socket open and holds every byte, so
    neither side sees an error or a close: only a timeout or a missing reply
    reveals the fault.
    """

    def __init__(self, upstream: tuple[str, int]) -> None:
        self._upstream = upstream
        self._open = asyncio.Event()
        self._open.set()
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def partitioned(self) -> bool:
        return not self._open.is_set()

    @partitioned.setter
    def partitioned(self, value: bool) -> None:
        if value:
            self._open.clear()
        else:
            self._open.set()

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for writer in self._writers:
            writer.close()
        for task in self._tasks:
            task.cancel()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection(*self._upstream)
        self._writers += [writer, up_writer]
        for source, sink in ((reader, up_writer), (up_reader, writer)):
            task = asyncio.create_task(self._pipe(source, sink))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _pipe(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            while data := await source.read(65536):
                await self._open.wait()
                sink.write(data)
                await sink.drain()


async def _redis_replica(
    table: SessionTable, *, port: int | None = None
) -> tuple[Replica, list[float]]:
    """A replica on the live channel, optionally through a proxy, counting resets."""
    from sibyl.config import settings
    from sibyl.coordination._redis.events import RedisEventBus
    from sibyl.coordination.invalidation import INVALIDATION_CHANNEL

    replica = await _replica(table, None)
    resets: list[float] = []

    async def reset() -> None:
        resets.append(time.monotonic())
        await replica.bus.reset()

    if port is not None:
        object.__setattr__(settings, "redis_port", port)
    transport = RedisEventBus(channel=INVALIDATION_CHANNEL, on_subscribed=reset)
    await replica.bus.attach(transport)
    await _eventually(lambda: len(resets) >= 1)
    return replica, resets


async def test_redis_channel_carries_revocations_and_survives_a_dropped_subscription(
    live_redis: tuple[str, int],
) -> None:
    from redis.asyncio import Redis

    from sibyl.coordination._redis.events import PUBSUB_CHANNEL, PUBSUB_DB, RedisEventBus
    from sibyl.coordination.invalidation import INVALIDATION_CHANNEL

    host, port = live_redis
    table = SessionTable()
    a, _ = await _redis_replica(table)
    b, b_resets = await _redis_replica(table)
    browser_events: list[str] = []

    async def browser(event: str, data: dict[str, Any], org_id: str | None) -> None:
        browser_events.append(event)

    websocket_bus = RedisEventBus()
    await websocket_bus.subscribe(browser)
    admin = Redis(host=host, port=port, db=PUBSUB_DB, decode_responses=True)
    try:
        first = _session()
        table.active[first.id] = first
        assert await b.validate(first.id) is True
        await a.revoke(first)
        await _eventually(lambda: b.cache.get(first.id) is None)
        assert await b.validate(first.id) is False

        # Drop every pub/sub connection; Redis keeps no backlog for them.
        resets_before = len(b_resets)
        await admin.client_kill_filter(_type="pubsub")
        second = _session()
        table.active[second.id] = second
        await _eventually(lambda: len(b_resets) > resets_before)

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


async def test_a_partitioned_subscriber_notices_reconnects_and_resets(
    live_redis: tuple[str, int],
) -> None:
    """The subscriber's socket stays open and silent; it must not wait for TCP to give up."""
    host, port = live_redis
    proxy = BlackHoleProxy((host, port))
    await proxy.start()
    table = SessionTable()
    a, _ = await _redis_replica(table, port=port)
    b, b_resets = await _redis_replica(table, port=proxy.port)
    try:
        assert b.bus.subscribed
        lost = _session()
        table.active[lost.id] = lost
        assert await b.validate(lost.id) is True

        proxy.partitioned = True
        await a.revoke(lost)  # B never receives this one
        await _eventually(lambda: not b.bus.subscribed, seconds=5)
        not_ready = b.bus.status()
        assert not_ready["state"] == "reconnecting"
        assert b.cache.get(lost.id) is True  # still stale while cut off

        resets_before = len(b_resets)
        proxy.partitioned = False
        await _eventually(lambda: len(b_resets) > resets_before and b.bus.subscribed)
        # The resubscription forgot what the missed message would have dropped.
        assert b.cache.get(lost.id) is None
        assert await b.validate(lost.id) is False

        after = _session()
        table.active[after.id] = after
        assert await b.validate(after.id) is True
        await a.revoke(after)
        await _eventually(lambda: b.cache.get(after.id) is None)
    finally:
        await a.bus.detach()
        await b.bus.detach(flush_timeout=0.1)
        await proxy.stop()


async def test_logout_returns_promptly_while_the_publisher_is_partitioned(
    live_redis: tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    host, port = live_redis
    proxy = BlackHoleProxy((host, port))
    await proxy.start()
    table = SessionTable()
    publisher, _ = await _redis_replica(table, port=proxy.port)
    peer, _ = await _redis_replica(table, port=port)
    monkeypatch.setattr(cache_invalidation, "get_cache_invalidation_bus", lambda: publisher.bus)
    session = _session()
    table.active[session.id] = session
    assert await peer.validate(session.id) is True
    repo = surreal_auth_runtime.SurrealSessionRepository(_RecordingClient([{"uuid": "x"}]))
    try:
        proxy.partitioned = True
        started = time.monotonic()
        # The real revocation path: database update, own cache, announcement.
        assert await repo.revoke_session(session.id, session.user_id) is True
        assert time.monotonic() - started < 0.5
        await asyncio.sleep(1.0)
        assert peer.cache.get(session.id) is True  # nothing got through the partition
        assert publisher.bus.status()["pending_announcements"] == 1

        proxy.partitioned = False
        await _eventually(lambda: peer.cache.get(session.id) is None)
        await _eventually(lambda: publisher.bus.status()["pending_announcements"] == 0)
    finally:
        await publisher.bus.detach(flush_timeout=0.1)
        await peer.bus.detach()
        await proxy.stop()


async def test_a_replica_started_while_redis_is_down_joins_once_it_is_up(
    live_redis: tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    from sibyl.config import settings

    _host, port = live_redis
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
    monkeypatch.setattr(cache_invalidation, "ATTACH_RETRY_INITIAL_SECONDS", 0.05)
    monkeypatch.setattr(cache_invalidation, "ATTACH_RETRY_MAX_SECONDS", 0.2)
    object.__setattr__(settings, "coordination_backend", "redis")
    object.__setattr__(settings, "redis_port", dead_port)
    bus = CacheInvalidationBus()
    monkeypatch.setattr(invalidation_module, "_bus", bus)
    monkeypatch.setattr(cache_invalidation, "get_cache_invalidation_bus", lambda: bus)
    table = SessionTable()
    peer, _ = await _redis_replica(table, port=port)
    object.__setattr__(settings, "redis_port", dead_port)
    stale = _session()
    access_session_cache.store_session(stale)
    try:
        assert await cache_invalidation.start_cache_invalidation() is True
        await asyncio.sleep(0.5)
        assert bus.status()["state"] == "connecting"
        assert access_session_cache.get(stale.id) is True

        object.__setattr__(settings, "redis_port", port)
        await _eventually(lambda: bus.subscribed)
        # Joining ran the reset: whatever it cached before joining is gone.
        await _eventually(lambda: access_session_cache.get(stale.id) is None)

        cached = _session()
        access_session_cache.store_session(cached)
        await peer.bus.announce(SESSIONS_TOPIC, {"session_ids": [str(cached.id)]})
        await _eventually(lambda: access_session_cache.get(cached.id) is None)
    finally:
        await cache_invalidation.stop_cache_invalidation()
        await peer.bus.detach()


def test_process_bus_is_a_singleton() -> None:
    assert get_cache_invalidation_bus() is get_cache_invalidation_bus()
