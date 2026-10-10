"""Device-code exchange across API replicas.

A CLI polls ``/auth/device/token`` until the browser approves its request, and
behind a load balancer consecutive polls land on different replicas. Once the
request is approved, two polls in flight at the same moment (a retry racing
the original, or two replicas answering one client) must not both mint a
session: the approved request is claimed once, by whichever exchange commits
first, and every other exchange gets ``invalid_grant``.

The embedded engine runs every case; with SIBYL_LIVE_SURREAL_TESTS=1 and a
server SIBYL_SURREAL_URL the same cases run against a server, where each
replica holds its own connection pool and the claim is decided by the
server's write-conflict detection rather than by one shared connection.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from itertools import count
from uuid import uuid4

import pytest
from pydantic import SecretStr

from sibyl.auth.primitives import DeviceTokenError
from sibyl.config import settings
from sibyl.persistence.surreal import auth_runtime as surreal_auth_runtime
from sibyl.persistence.surreal.auth import SurrealUserRepository
from sibyl.persistence.surreal.auth_runtime import device_authorization
from sibyl.persistence.surreal.auth_runtime._common import _SurrealRepository
from sibyl_core.backends.surreal import SurrealAuthClient, bootstrap_auth_schema
from sibyl_core.backends.surreal.records import normalize_records, utcnow
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url

RACE_REPLICAS = 8
RACE_ROUNDS = 12
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


async def _drop_namespace(url: str, namespace: str) -> None:
    from surrealdb import AsyncSurreal

    client = AsyncSurreal(url)
    try:
        await client.signin(
            {"username": CREDENTIALS["username"], "password": CREDENTIALS["password"]}
        )
        await client.query(f"REMOVE NAMESPACE IF EXISTS {namespace};")
    finally:
        await client.close()


@pytest.fixture(params=["embedded", "live"])
async def store_clients(request: pytest.FixtureRequest) -> AsyncIterator[list[SurrealAuthClient]]:
    """One auth-store client per replica, all on one namespace.

    A memory:// store lives inside its client, so embedded replicas share one
    client. Against a server every replica gets its own client and pool.
    """
    url = engine_url(request.param)
    namespace = f"verify_device_code_{uuid4().hex}"
    replicas = 1 if request.param == "embedded" else RACE_REPLICAS
    clients = [
        SurrealAuthClient(url=url, namespace=namespace, **CREDENTIALS) for _ in range(replicas)
    ]
    await bootstrap_auth_schema(clients[0])
    try:
        yield clients
    finally:
        for client in clients:
            await client.close()
        if request.param == "live":
            with suppress(Exception):
                await _drop_namespace(url, namespace)


@pytest.fixture(autouse=True)
def _jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", SecretStr("replica-device-proof-secret-" * 3))


def _round_robin_scope(clients: list[SurrealAuthClient]):
    """Each call takes the next replica's client, as successive requests would."""
    turn = count()

    @asynccontextmanager
    async def scope() -> AsyncIterator[SurrealAuthClient]:
        yield clients[next(turn) % len(clients)]

    return scope


def _reads_wait_for(loaded: asyncio.Barrier) -> type[_SurrealRepository]:
    """A repository whose device-code reads wait until every replica has read."""

    class ReadThenWait(_SurrealRepository):
        async def select_one(self, query: str, **params: object):
            record = await super().select_one(query, **params)
            if "device_code_hash" in query:
                await loaded.wait()
            return record

    return ReadThenWait


async def _approved_device_code(client: SurrealAuthClient, *, user_id: str, org_id: str) -> str:
    request_row, device_code = await surreal_auth_runtime.start_device_authorization(
        client_name="replica-race",
        scope="mcp",
        expires_in=timedelta(minutes=10),
        poll_interval_seconds=5,
    )
    now = utcnow()
    await client.execute_query(
        "UPDATE device_authorization_requests SET status = 'approved', "
        "approved_at = $now, user_id = $user_id, organization_id = $org_id, "
        "updated_at = $now WHERE uuid = $uuid;",
        now=now,
        user_id=user_id,
        org_id=org_id,
        uuid=str(request_row.id),
    )
    return device_code


async def test_concurrent_exchanges_of_one_device_code_mint_one_session(
    store_clients: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = str(uuid4())
    org_id = str(uuid4())
    monkeypatch.setattr(
        surreal_auth_runtime, "_auth_client_scope", _round_robin_scope(store_clients)
    )

    for _ in range(RACE_ROUNDS):
        device_code = await _approved_device_code(store_clients[0], user_id=user_id, org_id=org_id)
        # Every replica reads the approved request before any of them claims
        # it, so the claims collide on the row instead of trailing one another.
        reads_then_wait = _reads_wait_for(asyncio.Barrier(RACE_REPLICAS))

        async def exchange(device_code: str = device_code) -> str:
            try:
                await surreal_auth_runtime.exchange_device_code(device_code=device_code)
            except DeviceTokenError as exc:
                return exc.error
            return "tokens"

        with pytest.MonkeyPatch.context() as race:
            race.setattr(device_authorization, "_SurrealRepository", reads_then_wait)
            outcomes = await asyncio.gather(*(exchange() for _ in range(RACE_REPLICAS)))
        assert outcomes.count("tokens") == 1, outcomes
        assert set(outcomes) == {"tokens", "invalid_grant"}, outcomes

    sessions = normalize_records(
        await store_clients[0].execute_query(
            "SELECT uuid FROM user_sessions WHERE user_id = $user_id;", user_id=user_id
        )
    )
    assert len(sessions) == RACE_ROUNDS


async def test_a_consumed_device_code_is_refused_on_every_replica(
    store_clients: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = str(uuid4())
    monkeypatch.setattr(
        surreal_auth_runtime, "_auth_client_scope", _round_robin_scope(store_clients)
    )
    device_code = await _approved_device_code(
        store_clients[0], user_id=user_id, org_id=str(uuid4())
    )

    tokens = await surreal_auth_runtime.exchange_device_code(device_code=device_code)
    assert tokens["token_type"] == "Bearer"

    for _ in range(RACE_REPLICAS):
        with pytest.raises(DeviceTokenError) as refused:
            await surreal_auth_runtime.exchange_device_code(device_code=device_code)
        assert refused.value.error == "invalid_grant"


async def test_a_stale_approval_cannot_re_arm_a_used_device_code(
    store_clients: list[SurrealAuthClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two browser tabs approve one request; the CLI exchanges in between."""
    monkeypatch.setattr(
        surreal_auth_runtime, "_auth_client_scope", _round_robin_scope(store_clients)
    )
    user = await SurrealUserRepository.from_client(store_clients[0]).create_local_user(
        email=f"{uuid4().hex}@example.com", password="replica-proof-password", name="Approver"
    )
    request_row, device_code = await surreal_auth_runtime.start_device_authorization(
        client_name="replica-race",
        scope="mcp",
        expires_in=timedelta(minutes=10),
        poll_interval_seconds=5,
    )

    # The first tab reads the pending request, then stalls before deciding.
    read_pending = asyncio.Event()
    resume = asyncio.Event()
    load = device_authorization._load_device_authorization_user_and_request
    held: list[bool] = []

    async def load_and_hold_once(client, **kwargs):
        loaded = await load(client, **kwargs)
        if not held:
            held.append(True)
            read_pending.set()
            await resume.wait()
        return loaded

    monkeypatch.setattr(
        device_authorization, "_load_device_authorization_user_and_request", load_and_hold_once
    )

    async def approve():
        return await surreal_auth_runtime.approve_device_authorization(
            user_id=user.id, user_code=request_row.user_code, request=None
        )

    stale_tab = asyncio.create_task(approve())
    await read_pending.wait()

    # The second tab approves, and the CLI exchanges the code.
    assert await approve() is not None
    tokens = await surreal_auth_runtime.exchange_device_code(device_code=device_code)
    assert tokens["token_type"] == "Bearer"

    # The first tab's decision lands on a request that is no longer pending.
    resume.set()
    assert await stale_tab is None

    with pytest.raises(DeviceTokenError) as again:
        await surreal_auth_runtime.exchange_device_code(device_code=device_code)
    assert again.value.error == "invalid_grant"
    sessions = normalize_records(
        await store_clients[0].execute_query(
            "SELECT uuid FROM user_sessions WHERE user_id = $user_id;", user_id=str(user.id)
        )
    )
    assert len(sessions) == 1
