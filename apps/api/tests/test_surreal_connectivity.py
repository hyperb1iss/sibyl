from __future__ import annotations

import asyncio

import pytest

from sibyl.services import surreal_connectivity


class FakeDedicatedClient:
    def __init__(self) -> None:
        self.warmed = 0
        self.pings = 0
        self.fail_ping = False

    async def warm_pool(self) -> None:
        self.warmed += 1

    async def ping(self) -> None:
        self.pings += 1
        if self.fail_ping:
            raise TimeoutError("timed out during opening handshake")


@pytest.mark.asyncio
async def test_warm_shared_surreal_clients_warms_auth_and_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = FakeDedicatedClient()
    content = FakeDedicatedClient()
    core_content = FakeDedicatedClient()

    async def auth_client() -> FakeDedicatedClient:
        return auth

    async def content_client() -> FakeDedicatedClient:
        return content

    async def core_content_client() -> FakeDedicatedClient:
        return core_content

    monkeypatch.setattr(surreal_connectivity, "_auth_client", auth_client)
    monkeypatch.setattr(surreal_connectivity, "_content_client", content_client)
    monkeypatch.setattr(surreal_connectivity, "_core_content_client", core_content_client)

    await surreal_connectivity.warm_shared_surreal_clients()

    assert auth.warmed == 1
    assert content.warmed == 1
    assert core_content.warmed == 1


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
    core_content = FakeDedicatedClient()

    async def auth_client() -> FakeDedicatedClient:
        return auth

    async def content_client() -> FakeDedicatedClient:
        return content

    async def core_content_client() -> FakeDedicatedClient:
        return core_content

    monkeypatch.setattr(surreal_connectivity, "_auth_client", auth_client)
    monkeypatch.setattr(surreal_connectivity, "_content_client", content_client)
    monkeypatch.setattr(surreal_connectivity, "_core_content_client", core_content_client)
    monkeypatch.setattr(surreal_connectivity, "_CHECK_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(surreal_connectivity, "_monitor_task", None)

    surreal_connectivity.start_surreal_connectivity_monitor()
    try:
        for _ in range(50):
            if auth.pings and content.pings and core_content.pings:
                break
            await asyncio.sleep(0.001)
    finally:
        await surreal_connectivity.stop_surreal_connectivity_monitor()

    assert auth.pings >= 1
    assert content.pings >= 1
    assert core_content.pings >= 1


@pytest.mark.asyncio
async def test_surreal_connectivity_monitor_keeps_running_after_ping_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = FakeDedicatedClient()
    content = FakeDedicatedClient()
    core_content = FakeDedicatedClient()
    auth.fail_ping = True

    async def auth_client() -> FakeDedicatedClient:
        return auth

    async def content_client() -> FakeDedicatedClient:
        return content

    async def core_content_client() -> FakeDedicatedClient:
        return core_content

    monkeypatch.setattr(surreal_connectivity, "_auth_client", auth_client)
    monkeypatch.setattr(surreal_connectivity, "_content_client", content_client)
    monkeypatch.setattr(surreal_connectivity, "_core_content_client", core_content_client)

    await surreal_connectivity._ping_client("auth", auth_client)
    await surreal_connectivity._ping_client("content", content_client)
    await surreal_connectivity._ping_client("core_content", core_content_client)

    assert auth.pings == 1
    assert content.pings == 1
    assert core_content.pings == 1
