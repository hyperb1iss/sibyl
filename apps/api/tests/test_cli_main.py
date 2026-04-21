from __future__ import annotations

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
