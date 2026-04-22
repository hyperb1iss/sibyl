from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl.api import app as api_app_module


@pytest.mark.asyncio
async def test_surreal_mode_bootstraps_legacy_postgres_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap_legacy_postgres_support = AsyncMock(return_value=True)
    init_pubsub = AsyncMock()
    shutdown_pubsub = AsyncMock()
    init_locks = AsyncMock()
    shutdown_locks = AsyncMock()
    enable_pubsub = MagicMock()
    disable_pubsub = MagicMock()
    broker = SimpleNamespace(startup=AsyncMock(), shutdown=AsyncMock())
    scheduler = SimpleNamespace(startup=AsyncMock(), shutdown=AsyncMock())

    monkeypatch.setattr(api_app_module.settings, "store", "surreal")
    monkeypatch.setattr(
        api_app_module,
        "bootstrap_legacy_postgres_support",
        bootstrap_legacy_postgres_support,
    )
    monkeypatch.setattr("sibyl.api.pubsub.init_pubsub", init_pubsub)
    monkeypatch.setattr("sibyl.api.pubsub.shutdown_pubsub", shutdown_pubsub)
    monkeypatch.setattr("sibyl.locks.init_locks", init_locks)
    monkeypatch.setattr("sibyl.locks.shutdown_locks", shutdown_locks)
    monkeypatch.setattr("sibyl.api.websocket.enable_pubsub", enable_pubsub)
    monkeypatch.setattr("sibyl.api.websocket.disable_pubsub", disable_pubsub)
    monkeypatch.setattr("sibyl.coordination.broker.get_broker", lambda: broker)
    monkeypatch.setattr("sibyl.coordination.scheduler.get_scheduler", lambda: scheduler)
    monkeypatch.setattr(
        "sibyl_core.graph.client.get_graph_client",
        AsyncMock(return_value=SimpleNamespace(is_connected=True)),
    )

    app = api_app_module.create_api_app()

    async with app.router.lifespan_context(app):
        pass

    bootstrap_legacy_postgres_support.assert_awaited_once()
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
