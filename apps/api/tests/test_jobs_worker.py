from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sibyl.config import settings
from sibyl.jobs import worker as worker_module
from sibyl.jobs.worker import WorkerSettings


def test_worker_settings_uses_resolved_max_jobs() -> None:
    assert WorkerSettings.max_jobs == settings.resolved_worker_max_jobs


@pytest.mark.asyncio
async def test_worker_startup_installs_core_runtime_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    startup_events: list[str] = []

    monkeypatch.setattr("sibyl.banner.log_banner", MagicMock())
    monkeypatch.setattr("sibyl_core.logging.configure_logging", MagicMock())
    monkeypatch.setattr(
        "sibyl.services.settings.load_api_keys_from_db",
        AsyncMock(side_effect=lambda: startup_events.append("settings")),
    )
    monkeypatch.setattr(
        "sibyl.ai.llm.service.install_db_config_source",
        MagicMock(side_effect=lambda: startup_events.append("llm")),
    )
    monkeypatch.setattr(
        "sibyl.core_runtime_ports.install_core_runtime_ports",
        MagicMock(side_effect=lambda: startup_events.append("core_ports")),
    )
    monkeypatch.setattr(
        "sibyl.services.surreal_connectivity.start_surreal_connectivity_monitor",
        MagicMock(side_effect=lambda: startup_events.append("pool_sweep")),
    )

    ctx: dict[str, object] = {}

    await worker_module.startup(ctx)

    assert "start_time" in ctx
    # The worker runs the same pool health sweep as the API, so dead sockets
    # and retired org clients are handled there too.
    assert startup_events == ["settings", "llm", "core_ports", "pool_sweep"]


@pytest.mark.asyncio
async def test_worker_shutdown_stops_the_pool_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = AsyncMock()
    monkeypatch.setattr(
        "sibyl.services.surreal_connectivity.stop_surreal_connectivity_monitor", stopped
    )

    await worker_module.shutdown({})

    stopped.assert_awaited_once()
