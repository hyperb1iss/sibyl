from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl import runtime_services as runtime_services_module
from sibyl.api import app as api_app_module


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
    recover_stuck_sources = AsyncMock(
        return_value={"recovered": 0, "completed": 0, "reset_to_pending": 0}
    )
    startup_events: list[str] = []

    async def bootstrap_surreal_runtime() -> bool:
        startup_events.append("surreal")
        return True

    monkeypatch.setattr(api_app_module.settings, "store", "surreal")
    monkeypatch.setattr(api_app_module.settings, "auth_store", "surreal")
    monkeypatch.setattr(
        runtime_services_module,
        "bootstrap_surreal_runtime_schemas",
        bootstrap_surreal_runtime,
    )
    monkeypatch.setattr(runtime_services_module, "install_llm_db_config_source", MagicMock())
    monkeypatch.setattr("sibyl.api.pubsub.init_pubsub", init_pubsub)
    monkeypatch.setattr("sibyl.api.pubsub.shutdown_pubsub", shutdown_pubsub)
    monkeypatch.setattr("sibyl.locks.init_locks", init_locks)
    monkeypatch.setattr("sibyl.locks.shutdown_locks", shutdown_locks)
    monkeypatch.setattr("sibyl.api.websocket.enable_pubsub", enable_pubsub)
    monkeypatch.setattr("sibyl.api.websocket.disable_pubsub", disable_pubsub)
    monkeypatch.setattr("sibyl.coordination.broker.get_broker", lambda: broker)
    monkeypatch.setattr("sibyl.coordination.scheduler.get_scheduler", lambda: scheduler)
    monkeypatch.setattr("sibyl.api.routes.admin.recover_stuck_sources", recover_stuck_sources)

    app = api_app_module.create_api_app()

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
    recover_stuck_sources.assert_awaited_once()
