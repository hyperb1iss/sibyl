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

    ctx: dict[str, object] = {}

    await worker_module.startup(ctx)

    assert "start_time" in ctx
    assert startup_events == ["settings", "llm", "core_ports"]
