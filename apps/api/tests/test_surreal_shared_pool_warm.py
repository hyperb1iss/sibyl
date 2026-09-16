"""Shared Surreal singletons open their sockets on the build, not on a request."""

from __future__ import annotations

from typing import Any

import pytest

from sibyl.persistence.surreal import auth as auth_module, content as content_module
from sibyl_core.backends.surreal.connection import SurrealConnectTimeout


class FakeClient:
    def __init__(self, *, fail_warm: bool = False) -> None:
        self.warmed = 0
        self.closed = 0
        self.fail_warm = fail_warm

    async def warm_pool(self) -> None:
        self.warmed += 1
        if self.fail_warm:
            raise SurrealConnectTimeout(
                url="ws://localhost:8612/rpc", attempt=3, timeout_seconds=3.0
            )

    async def close(self) -> None:
        self.closed += 1


@pytest.fixture(autouse=True)
def _reset_singletons() -> Any:
    auth_module._shared_auth_client_state.client = None
    content_module._shared_content_client_state.client = None
    yield
    auth_module._shared_auth_client_state.client = None
    content_module._shared_content_client_state.client = None


@pytest.mark.asyncio
async def test_auth_singleton_warms_its_pool_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(auth_module, "build_surreal_auth_client", lambda: fake)

    first = await auth_module.get_shared_surreal_auth_client()
    second = await auth_module.get_shared_surreal_auth_client()

    assert first is fake
    assert second is fake
    assert fake.warmed == 1


@pytest.mark.asyncio
async def test_auth_singleton_survives_a_warm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(fail_warm=True)
    monkeypatch.setattr(auth_module, "build_surreal_auth_client", lambda: fake)

    client = await auth_module.get_shared_surreal_auth_client()

    # A cold pool still serves; it reconnects lazily rather than failing auth.
    assert client is fake
    assert fake.warmed == 1


@pytest.mark.asyncio
async def test_content_singleton_warms_its_pool_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(content_module, "build_surreal_content_client", lambda: fake)

    first = await content_module.get_shared_surreal_content_client()
    second = await content_module.get_shared_surreal_content_client()

    assert first is fake
    assert second is fake
    assert fake.warmed == 1


@pytest.mark.asyncio
async def test_content_singleton_survives_a_warm_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeClient(fail_warm=True)
    monkeypatch.setattr(content_module, "build_surreal_content_client", lambda: fake)

    client = await content_module.get_shared_surreal_content_client()

    assert client is fake
    assert fake.warmed == 1
