from __future__ import annotations

import sys
from unittest.mock import MagicMock

from typer.testing import CliRunner

from sibyl.cli.main import app
from sibyl.config import settings

runner = CliRunner()


def test_worker_command_exits_cleanly_in_local_mode(monkeypatch) -> None:
    monkeypatch.setattr(settings, "store", "surreal")
    monkeypatch.setattr(settings, "coordination_backend", "auto")

    result = runner.invoke(app, ["worker"])

    assert result.exit_code == 0
    assert "runs background jobs in-process under" in result.output


def test_worker_command_keeps_arq_path_in_redis_mode(monkeypatch) -> None:
    run_worker = MagicMock()

    monkeypatch.setattr(settings, "store", "legacy")
    monkeypatch.setattr(settings, "coordination_backend", "auto")
    monkeypatch.setattr("arq.run_worker", run_worker)

    result = runner.invoke(app, ["worker", "--burst"])

    assert result.exit_code == 0
    run_worker.assert_called_once()


def test_serve_with_reload_enables_dev_diagnostics(monkeypatch) -> None:
    cli_main = sys.modules["sibyl.cli.main"]
    process = MagicMock()
    process.wait.return_value = 0
    popen = MagicMock(return_value=process)

    monkeypatch.setattr("subprocess.Popen", popen)

    cli_main._serve_with_reload("localhost", 3334)

    kwargs = popen.call_args.kwargs
    env = kwargs["env"]
    assert env["PYTHONFAULTHANDLER"] == "1"
    assert env["SIBYL_DEV_DIAGNOSTICS"] == "1"
