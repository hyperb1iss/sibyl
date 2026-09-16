from __future__ import annotations

import asyncio

import pytest

from sibyl.services import surreal_connectivity
from sibyl_core.backends.surreal.connection import SurrealConnectTimeout
from sibyl_core.backends.surreal.dedicated_client import PoolHealth


class FakeDedicatedClient:
    def __init__(self) -> None:
        self.warmed = 0
        self.pings = 0
        self.fail_ping = False
        self.reaped = 0

    async def warm_pool(self) -> None:
        self.warmed += 1

    async def ping(self) -> None:
        self.pings += 1
        if self.fail_ping:
            raise TimeoutError("timed out during opening handshake")

    async def ping_pool(self) -> PoolHealth:
        self.pings += 1
        if self.fail_ping:
            self.reaped += 1
            return PoolHealth(checked=2, reaped=1, failures=("SurrealConnectTimeout",))
        return PoolHealth(checked=2, reaped=0)


@pytest.mark.asyncio
async def test_warm_shared_surreal_clients_warms_auth_and_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = FakeDedicatedClient()
    content = FakeDedicatedClient()

    async def auth_client() -> FakeDedicatedClient:
        return auth

    async def content_client() -> FakeDedicatedClient:
        return content

    monkeypatch.setattr(surreal_connectivity, "_auth_client", auth_client)
    monkeypatch.setattr(surreal_connectivity, "_content_client", content_client)

    await surreal_connectivity.warm_shared_surreal_clients()

    assert auth.warmed == 1
    assert content.warmed == 1


@pytest.mark.asyncio
async def test_initialize_starts_monitor_after_warm_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = False

    async def warm_failure() -> None:
        raise TimeoutError("timed out during opening handshake")

    def start_monitor() -> None:
        nonlocal started
        started = True

    monkeypatch.setattr(surreal_connectivity, "warm_shared_surreal_clients", warm_failure)
    monkeypatch.setattr(surreal_connectivity, "start_surreal_connectivity_monitor", start_monitor)

    await surreal_connectivity.initialize_shared_surreal_connectivity()

    assert started is True


@pytest.mark.asyncio
async def test_surreal_connectivity_monitor_pings_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = FakeDedicatedClient()
    content = FakeDedicatedClient()

    async def auth_client() -> FakeDedicatedClient:
        return auth

    async def content_client() -> FakeDedicatedClient:
        return content

    monkeypatch.setattr(surreal_connectivity, "_auth_client", auth_client)
    monkeypatch.setattr(surreal_connectivity, "_content_client", content_client)
    monkeypatch.setattr(surreal_connectivity, "_health_interval_seconds", lambda: 0.001)
    monkeypatch.setattr(surreal_connectivity, "_monitor_task", None)

    surreal_connectivity.start_surreal_connectivity_monitor()
    try:
        for _ in range(50):
            if auth.pings and content.pings:
                break
            await asyncio.sleep(0.001)
    finally:
        await surreal_connectivity.stop_surreal_connectivity_monitor()

    assert auth.pings >= 1
    assert content.pings >= 1


@pytest.mark.asyncio
async def test_surreal_connectivity_monitor_keeps_running_after_ping_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = FakeDedicatedClient()
    content = FakeDedicatedClient()
    auth.fail_ping = True

    async def auth_client() -> FakeDedicatedClient:
        return auth

    async def content_client() -> FakeDedicatedClient:
        return content

    monkeypatch.setattr(surreal_connectivity, "_auth_client", auth_client)
    monkeypatch.setattr(surreal_connectivity, "_content_client", content_client)

    await surreal_connectivity._sweep_client("auth", auth_client)
    await surreal_connectivity._sweep_client("content", content_client)

    assert auth.pings == 1
    assert content.pings == 1
    assert auth.reaped == 1


@pytest.mark.asyncio
async def test_health_interval_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from sibyl.config import settings

    monkeypatch.setattr(settings, "surreal_pool_health_interval_seconds", 7.5)

    assert surreal_connectivity._health_interval_seconds() == 7.5


@pytest.mark.asyncio
async def test_sweep_reports_reaped_slots_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeDedicatedClient()
    client.fail_ping = True

    async def factory() -> FakeDedicatedClient:
        return client

    await surreal_connectivity._sweep_client("auth", factory)

    assert client.reaped == 1


@pytest.mark.asyncio
async def test_sweep_survives_a_client_that_cannot_connect_at_all() -> None:
    async def factory() -> FakeDedicatedClient:
        raise SurrealConnectTimeout(url="ws://localhost:8612/rpc", attempt=3, timeout_seconds=3.0)

    await surreal_connectivity._sweep_client("content", factory)
