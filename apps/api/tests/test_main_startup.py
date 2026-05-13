from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from starlette.applications import Starlette

from sibyl import main as main_module


class _FakeSessionManager:
    @asynccontextmanager
    async def run(self):
        yield


class _FakeMCPServer:
    def __init__(self) -> None:
        self.session_manager = _FakeSessionManager()

    def streamable_http_app(self) -> Starlette:
        return Starlette()


@pytest.mark.asyncio
async def test_fully_surreal_mode_skips_legacy_postgres_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_pubsub = AsyncMock()
    shutdown_pubsub = AsyncMock()
    init_locks = AsyncMock()
    shutdown_locks = AsyncMock()
    enable_pubsub = MagicMock()
    disable_pubsub = MagicMock()
    broker = SimpleNamespace(startup=AsyncMock(), shutdown=AsyncMock())
    scheduler = SimpleNamespace(startup=AsyncMock(), shutdown=AsyncMock())
    startup_events: list[str] = []

    async def bootstrap_surreal_runtime() -> bool:
        startup_events.append("surreal")
        return True

    monkeypatch.setattr(main_module.settings, "store", "surreal")
    monkeypatch.setattr(main_module.settings, "auth_store", "surreal")
    monkeypatch.setattr("sibyl.api.app.create_api_app", lambda: Starlette())
    monkeypatch.setattr("sibyl.server.create_mcp_server", lambda **_: _FakeMCPServer())
    monkeypatch.setattr(
        main_module,
        "_bootstrap_surreal_runtime_schemas",
        bootstrap_surreal_runtime,
    )
    monkeypatch.setattr("sibyl.api.pubsub.init_pubsub", init_pubsub)
    monkeypatch.setattr("sibyl.api.pubsub.shutdown_pubsub", shutdown_pubsub)
    monkeypatch.setattr("sibyl.locks.init_locks", init_locks)
    monkeypatch.setattr("sibyl.locks.shutdown_locks", shutdown_locks)
    monkeypatch.setattr("sibyl.api.websocket.enable_pubsub", enable_pubsub)
    monkeypatch.setattr("sibyl.api.websocket.disable_pubsub", disable_pubsub)
    monkeypatch.setattr("sibyl.coordination.broker.get_broker", lambda: broker)
    monkeypatch.setattr("sibyl.coordination.scheduler.get_scheduler", lambda: scheduler)

    app = main_module.create_combined_app()

    async with app.router.lifespan_context(app):
        pass

    assert startup_events == ["surreal"]
    init_pubsub.assert_awaited_once()
    init_locks.assert_awaited_once()
    shutdown_pubsub.assert_awaited_once()
    shutdown_locks.assert_awaited_once()
    broker.startup.assert_awaited_once()
    broker.shutdown.assert_awaited_once()
    scheduler.startup.assert_awaited_once()
    scheduler.shutdown.assert_awaited_once()
    enable_pubsub.assert_called_once_with()
    disable_pubsub.assert_called_once_with()
