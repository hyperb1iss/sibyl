from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    startup_events: list[str] = []

    async def bootstrap_surreal_runtime() -> bool:
        startup_events.append("surreal")
        return True

    monkeypatch.setattr(api_app_module.settings, "store", "surreal")
    monkeypatch.setattr(api_app_module.settings, "auth_store", "surreal")
    monkeypatch.setattr(
        api_app_module,
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


def test_api_factories_import_without_graphiti() -> None:
    script = r"""
import builtins

original_import = builtins.__import__


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "graphiti_core" or name.startswith("graphiti_core."):
        raise AssertionError(f"Graphiti import forbidden: {name}")
    return original_import(name, globals, locals, fromlist, level)


builtins.__import__ = guarded_import

import sibyl.api.app as api_app
import sibyl.main as main

api_app.create_api_app()
main.create_combined_app()
"""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False,
        cwd=os.getcwd(),
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr + result.stdout
