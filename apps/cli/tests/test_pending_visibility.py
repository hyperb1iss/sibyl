"""Buffered writes must be visible wherever a user looks.

A write the server refuses is filed into ~/.config/sibyl/pending_writes/
and the command otherwise carries on. Nothing about that queue reached the
user before: `sibyl doctor` did not check it and `sibyl debug status` needs
the OWNER role.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli import doctor as doctor_module
from sibyl_cli import pending_writes
from sibyl_cli.main import app as cli_app
from sibyl_cli.main import main as cli_main


def _buffer(count: int) -> None:
    for index in range(count):
        pending_writes.create_pending_write(
            method="POST",
            path="/memory/raw",
            base_url="http://testserver/api",
            json_payload={"title": f"queued {index}"},
            params=None,
        )


@pytest.fixture
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(pending_writes.Path, "home", lambda: tmp_path)
    return tmp_path


def test_a_queued_write_warns_on_stderr_at_command_completion(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _buffer(2)
    monkeypatch.setattr("sys.argv", ["sibyl", "version"])

    with pytest.raises(SystemExit):
        cli_main()

    captured = capsys.readouterr()
    assert "2 writes buffered locally" in captured.err
    assert "sibyl pending-writes flush" in captured.err
    assert "buffered locally" not in captured.out


def test_an_empty_queue_stays_quiet(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["sibyl", "version"])

    with pytest.raises(SystemExit):
        cli_main()

    assert "buffered locally" not in capsys.readouterr().err


def test_the_notice_survives_a_failing_command(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The commands that fill the queue are exactly the ones that exit nonzero."""
    _buffer(1)
    monkeypatch.setattr("sys.argv", ["sibyl", "no-such-command"])

    with pytest.raises(SystemExit):
        cli_main()

    assert "1 write buffered locally" in capsys.readouterr().err


def test_the_pending_writes_commands_do_not_double_report(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _buffer(1)
    monkeypatch.setattr("sys.argv", ["sibyl", "pending-writes", "list"])

    with pytest.raises(SystemExit):
        cli_main()

    assert "buffered locally" not in capsys.readouterr().err


def _health_client(payload: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=payload)
    return client


def test_health_reports_a_non_empty_buffer(sandbox_home: Path) -> None:
    _buffer(3)
    client = _health_client({"status": "healthy", "server_name": "sibyl"})

    with patch("sibyl_cli.main.get_client", return_value=client):
        result = CliRunner().invoke(cli_app, ["health"])

    assert result.exit_code == 0
    assert "3 writes buffered locally" in result.stdout


def test_health_reports_an_empty_buffer(sandbox_home: Path) -> None:
    client = _health_client({"status": "healthy", "server_name": "sibyl"})

    with patch("sibyl_cli.main.get_client", return_value=client):
        result = CliRunner().invoke(cli_app, ["health"])

    assert result.exit_code == 0
    assert "Pending writes: 0" in result.stdout


def test_health_json_carries_the_queue_depth(sandbox_home: Path) -> None:
    import json

    _buffer(2)
    client = _health_client({"status": "healthy", "server_name": "sibyl"})

    with patch("sibyl_cli.main.get_client", return_value=client):
        result = CliRunner().invoke(cli_app, ["health", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "healthy"
    assert payload["pending_writes"]["count"] == 2


def test_doctor_checks_the_queue(sandbox_home: Path) -> None:
    _buffer(1)

    check = doctor_module._check_pending_writes()

    assert check.status == "warn"
    assert "1 write buffered locally" in check.message
    assert check.detail is not None
    assert "sibyl pending-writes flush" in check.detail


def test_doctor_passes_on_an_empty_queue(sandbox_home: Path) -> None:
    check = doctor_module._check_pending_writes()

    assert check.status == "pass"
