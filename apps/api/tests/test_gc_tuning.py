"""Long-running processes freeze their startup heap; short-lived commands keep GC defaults."""

from __future__ import annotations

import ast
import gc
import weakref
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from sibyl import gc_tuning, runtime_services as runtime_services_module
from sibyl.cli.main import app as sibyld_cli
from sibyl.jobs import worker as worker_module

# The conftest stubs the hook for every test; these tests exercise the real one.
_TUNE = gc_tuning.tune_gc_for_long_running_process
_SRC = Path(gc_tuning.__file__).parent


class _Cycle:
    def __init__(self) -> None:
        self.me = self


@pytest.fixture
def restored_gc() -> Iterator[None]:
    thresholds = gc.get_threshold()
    gc.unfreeze()
    try:
        yield
    finally:
        gc.unfreeze()
        gc.set_threshold(*thresholds)


def test_tuning_freezes_the_startup_heap_and_paces_collections(restored_gc: None) -> None:
    startup_garbage = _Cycle()
    garbage_ref = weakref.ref(startup_garbage)
    del startup_garbage

    _TUNE()

    assert gc.get_freeze_count() > 0
    assert gc.get_threshold() == gc_tuning.GC_THRESHOLDS
    # Garbage left by startup is collected before the freeze, never kept by it.
    assert garbage_ref() is None
    # Whatever is built after the freeze stays collectable.
    later = _Cycle()
    later_ref = weakref.ref(later)
    del later
    gc.collect()
    assert later_ref() is None


@pytest.mark.asyncio
async def test_api_startup_tunes_gc_after_every_other_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def record(name: str):
        return AsyncMock(side_effect=lambda *_args, **_kwargs: events.append(name))

    monkeypatch.setattr(
        runtime_services_module, "bootstrap_surreal_runtime_schemas", record("schemas")
    )
    monkeypatch.setattr(
        runtime_services_module, "load_runtime_settings_from_db", record("settings")
    )
    monkeypatch.setattr(
        runtime_services_module,
        "install_llm_db_config_source",
        MagicMock(side_effect=lambda: events.append("llm")),
    )
    monkeypatch.setattr(
        runtime_services_module,
        "install_core_runtime_ports",
        MagicMock(side_effect=lambda: events.append("core_ports")),
    )
    monkeypatch.setattr(
        "sibyl.services.surreal_connectivity.initialize_shared_surreal_connectivity",
        record("connectivity"),
    )
    services = runtime_services_module.RuntimeServices(log=MagicMock())
    for step in (
        "_startup_broker",
        "_startup_scheduler",
        "_startup_pubsub",
        "_startup_locks",
        "_startup_live_queries",
        "_recover_stuck_sources",
    ):
        monkeypatch.setattr(services, step, record(step))
    monkeypatch.setattr(
        gc_tuning,
        "tune_gc_for_long_running_process",
        MagicMock(side_effect=lambda: events.append("gc")),
    )

    await services.startup()

    assert events[-1] == "gc"
    assert events.count("gc") == 1
    assert {"schemas", "core_ports", "connectivity", "_recover_stuck_sources"} <= set(events)


@pytest.mark.asyncio
async def test_worker_startup_tunes_gc_after_every_other_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr("sibyl.banner.log_banner", MagicMock())
    monkeypatch.setattr("sibyl_core.logging.configure_logging", MagicMock())
    monkeypatch.setattr(
        "sibyl.services.settings.load_api_keys_from_db",
        AsyncMock(side_effect=lambda: events.append("settings")),
    )
    monkeypatch.setattr(
        "sibyl.ai.llm.service.install_db_config_source",
        MagicMock(side_effect=lambda: events.append("llm")),
    )
    monkeypatch.setattr(
        "sibyl.core_runtime_ports.install_core_runtime_ports",
        MagicMock(side_effect=lambda: events.append("core_ports")),
    )
    monkeypatch.setattr(
        "sibyl.services.surreal_connectivity.start_surreal_connectivity_monitor",
        MagicMock(side_effect=lambda: events.append("pool_sweep")),
    )
    monkeypatch.setattr(
        gc_tuning,
        "tune_gc_for_long_running_process",
        MagicMock(side_effect=lambda: events.append("gc")),
    )

    await worker_module.startup({})

    assert events == ["settings", "llm", "core_ports", "pool_sweep", "gc"]


def test_sibyld_cli_commands_keep_the_interpreter_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook = MagicMock()
    monkeypatch.setattr(gc_tuning, "tune_gc_for_long_running_process", hook)
    thresholds = gc.get_threshold()

    result = CliRunner().invoke(sibyld_cli, ["--help"])

    assert result.exit_code == 0
    hook.assert_not_called()
    assert gc.get_threshold() == thresholds


def test_only_the_server_and_worker_startup_call_the_hook() -> None:
    callers = set()
    for path in _SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute | ast.Name)
                and getattr(node.func, "attr", getattr(node.func, "id", None))
                == "tune_gc_for_long_running_process"
            ):
                callers.add(path.relative_to(_SRC).as_posix())

    assert callers == {"runtime_services.py", "jobs/worker.py"}
