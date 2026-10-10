"""Repeated logouts with one token re-announce at most once per window.

A logout with a validly signed token for a session that is already revoked
re-announces the revocation, which heals a replica that missed the first
announcement. The session row records when it was last announced, and one
conditional UPDATE claims each re-announcement, so however many logouts reach
however many replicas, the fleet hears about the session once per window.

The embedded engine runs every case; with SIBYL_LIVE_SURREAL_TESTS=1 and a
server SIBYL_SURREAL_URL, two replicas with their own connection pools race
for the claim on a server.
"""

from __future__ import annotations

import asyncio
import itertools
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sibyl.auth.jwt import create_access_token
from sibyl.auth.session_cache import access_session_cache
from sibyl.config import settings
from sibyl.persistence.surreal.auth_runtime import (
    _common as auth_common,
    sessions as session_runtime,
)
from sibyl.persistence.surreal.auth_runtime._common import (
    REANNOUNCE_REVOCATION_AFTER,
    SurrealSessionRepository,
)
from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema
from sibyl_core.backends.surreal.records import utcnow
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url

CREDENTIALS = {
    "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
    "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
}


def engine_url(engine: str) -> str:
    if engine == "embedded":
        return "memory://"
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


@pytest.fixture(params=["embedded", "live"])
async def replicas(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[list[SurrealAuthClient]]:
    """One auth client per replica; each logout below lands on the next one."""
    url = engine_url(request.param)
    namespace = f"verify_reannounce_{uuid4().hex}"
    count = 1 if request.param == "embedded" else 2
    clients = [SurrealAuthClient(url=url, namespace=namespace, **CREDENTIALS) for _ in range(count)]
    await bootstrap_auth_schema(clients[0])
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("reannouncement-proof-secret-" * 3))
    rotation = itertools.cycle(clients)

    @asynccontextmanager
    async def next_replica() -> AsyncIterator[SurrealAuthClient]:
        yield next(rotation)

    monkeypatch.setattr(session_runtime, "_auth_client_scope", next_replica)
    access_session_cache.clear()
    try:
        yield clients
    finally:
        access_session_cache.clear()
        for client in clients:
            await client.close()
        if request.param == "live":
            from surrealdb import AsyncSurreal

            with suppress(Exception):
                admin = AsyncSurreal(url)
                await admin.signin(
                    {"username": CREDENTIALS["username"], "password": CREDENTIALS["password"]}
                )
                await admin.query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
                await admin.close()


async def _logout_many(token: str, times: int) -> None:
    await asyncio.gather(*(session_runtime.revoke_access_session(token) for _ in range(times)))


async def test_repeated_logouts_announce_once_per_window(
    replicas: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    announced = AsyncMock()
    monkeypatch.setattr(auth_common, "announce_sessions_invalidated", announced)
    repo = SurrealSessionRepository(replicas[0])
    user_id, org_id, session_id = uuid4(), uuid4(), uuid4()
    await repo.create_session(
        user_id=user_id,
        organization_id=org_id,
        session_id=session_id,
        token=f"access-{uuid4().hex}",
        expires_at=utcnow() + timedelta(minutes=60),
        refresh_token=f"refresh-{uuid4().hex}",
        refresh_token_expires_at=utcnow() + timedelta(days=30),
    )
    # The caller's own token, expired but validly signed.
    token = create_access_token(
        user_id=user_id,
        organization_id=org_id,
        session_id=session_id,
        expires_in=timedelta(seconds=-60),
    )

    await session_runtime.revoke_access_session(token)
    assert announced.await_count == 1
    generation = access_session_cache.generation

    await _logout_many(token, 100)

    assert announced.await_count == 1  # 100 more logouts, no more announcements
    assert access_session_cache.generation == generation

    # Once the window has passed, revoking again heals a replica that missed it,
    # and only one of the logouts racing for the claim re-announces.
    await replicas[0].execute_query(
        "UPDATE user_sessions SET revocation_announced_at = $past WHERE uuid = $uuid;",
        uuid=str(session_id),
        past=utcnow() - REANNOUNCE_REVOCATION_AFTER - timedelta(seconds=1),
    )
    await _logout_many(token, 20)

    assert announced.await_count == 2
    assert announced.await_args is not None
    assert announced.await_args.kwargs == {"session_ids": [session_id]}
    assert access_session_cache.generation == generation + 1
    assert access_session_cache.get(session_id) is False

    # Revoking from the sessions page within the window stays quiet too.
    assert await repo.revoke_session(session_id, user_id) is False
    assert announced.await_count == 2


async def test_a_session_revoked_before_the_window_existed_reannounces_once(
    replicas: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows revoked before the column existed carry no timestamp; the first claim sets it."""
    announced = AsyncMock()
    monkeypatch.setattr(auth_common, "announce_sessions_invalidated", announced)
    repo = SurrealSessionRepository(replicas[0])
    user_id, session_id = uuid4(), uuid4()
    await repo.create_session(
        user_id=user_id,
        session_id=session_id,
        token=f"access-{uuid4().hex}",
        expires_at=utcnow() + timedelta(minutes=60),
    )
    await replicas[0].execute_query(
        "UPDATE user_sessions SET revoked_at = $now, revocation_announced_at = NONE "
        "WHERE uuid = $uuid;",
        uuid=str(session_id),
        now=utcnow(),
    )
    token = create_access_token(user_id=user_id, session_id=session_id)

    await _logout_many(token, 10)

    assert announced.await_count == 1
