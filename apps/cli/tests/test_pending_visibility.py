"""Buffered writes must be visible wherever a user looks.

A write the server refuses is filed into ~/.config/sibyl/pending_writes/
and the command otherwise carries on. Nothing about that queue reached the
user before: `sibyl doctor` did not check it and `sibyl debug status` needs
the OWNER role.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli import auth_store, common, pending_writes
from sibyl_cli import doctor as doctor_module
from sibyl_cli.main import app as cli_app
from sibyl_cli.main import main as cli_main

# The destination `resolve_api_base_url` picks with no context configured.
CURRENT_BASE_URL = "http://localhost:3334/api"
FOREIGN_BASE_URL = "http://localhost:3364/api"
IDENTITY = {
    "version": 1,
    "server_instance_id": "11111111-1111-1111-1111-111111111111",
    "user_id": "22222222-2222-2222-2222-222222222222",
    "organization_id": "33333333-3333-3333-3333-333333333333",
    "credential": {
        "kind": "session",
        "api_key_id": None,
        "scopes": [],
        "project_ids": None,
        "memory_space_ids": None,
        "memory_scope_keys": None,
    },
}


def _sign_in(base_url: str = CURRENT_BASE_URL) -> None:
    """Give the sandbox a stored login, so buffered writes have an owner."""
    auth_store.set_tokens(base_url, "stored-token", "stored-refresh")
    auth_store.cache_pending_replay_identity(base_url, "stored-token", IDENTITY)


def _buffer(
    count: int,
    *,
    base_url: str = FOREIGN_BASE_URL,
    identity: dict[str, Any] | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    items = []
    for index in range(count):
        item = pending_writes.create_pending_write(
            method="POST",
            path="/memory/raw",
            base_url=base_url,
            json_payload={"title": f"queued {index}"},
            params=None,
            replay_identity=identity,
        )
        if status == "attention":
            item = pending_writes.record_pending_failure(
                str(item["id"]),
                category="rejected",
                status_code=422,
                error_code="validation_error",
                message="name must be at most 200 characters",
            )
        items.append(item)
    return items


def _buffer_retrying(count: int) -> list[dict[str, Any]]:
    """Writes this login owns at the destination this command is talking to."""
    _sign_in()
    return _buffer(count, base_url=CURRENT_BASE_URL, identity=IDENTITY)


@pytest.fixture(autouse=True)
def _unclaimed_queue_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real invocation is one command per process; a test session is not."""
    monkeypatch.setattr(common, "_pending_writes_reported", False)


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the queue, the credential store, and the context config together.

    The end-of-command notice classifies the queue against the current
    destination, so a test that only redirected the queue would grade itself
    against the developer's own contexts.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def test_a_parked_write_warns_on_stderr_at_command_completion(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _buffer(2, status="attention")
    monkeypatch.setattr("sys.argv", ["sibyl", "version"])

    with pytest.raises(SystemExit):
        cli_main()

    captured = capsys.readouterr()
    assert "Buffered writes need a decision: 2 for another server" in captured.err
    assert "sibyl pending-writes list" in captured.err
    assert "need a decision" not in captured.out


def test_a_young_owned_write_prints_nothing(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The healthy path: buffered, owned, seconds old, retrying on its own."""
    _buffer_retrying(3)
    monkeypatch.setattr("sys.argv", ["sibyl", "version"])

    with pytest.raises(SystemExit):
        cli_main()

    captured = capsys.readouterr()
    assert "buffered" not in captured.err
    assert "need a decision" not in captured.err


def test_a_retrying_write_past_the_grace_period_gets_one_line(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _buffer_retrying(1)[0]
    aged = datetime.now(UTC) - timedelta(hours=3)
    path = pending_writes.resolve_pending_write_path(str(item["id"]))
    path.write_text(
        json.dumps({**item, "created_at": aged.isoformat()}),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["sibyl", "version"])

    with pytest.raises(SystemExit):
        cli_main()

    captured = capsys.readouterr()
    assert "1 buffered write has not reached the server yet (oldest 3h)" in captured.err
    assert "need a decision" not in captured.err


def test_the_notice_names_every_parked_class_in_one_line(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real queue shape: rejected payloads, dead ownership, another server."""
    _sign_in()
    _buffer(2, base_url=CURRENT_BASE_URL, identity=IDENTITY, status="attention")
    _buffer(8, base_url=CURRENT_BASE_URL)
    _buffer(3, base_url=FOREIGN_BASE_URL)
    monkeypatch.setattr("sys.argv", ["sibyl", "version"])

    with pytest.raises(SystemExit):
        cli_main()

    err = capsys.readouterr().err
    # Rich wraps the console line, so compare on collapsed whitespace.
    assert (
        "Buffered writes need a decision: 2 rejected by the server, "
        "8 with no owner this login can replay, 3 for another server "
        "(http://localhost:3364/api). "
        "Run 'sibyl pending-writes list', then 'adopt' or 'discard'." in " ".join(err.split())
    )
    assert err.count("need a decision") == 1


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

    assert "1 for another server" in capsys.readouterr().err


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


def test_doctor_warns_on_a_parked_write(sandbox_home: Path) -> None:
    _buffer(1)

    check = doctor_module._check_pending_writes()

    assert check.status == "warn"
    assert check.message == "1 write buffered locally: 1 for another server."
    assert check.detail is not None
    assert "need a decision" in check.detail
    assert "Local files:" in check.detail


def test_doctor_passes_on_a_queue_that_is_merely_in_flight(sandbox_home: Path) -> None:
    """A warning nobody can act on trains the operator to ignore the check."""
    _buffer_retrying(2)

    check = doctor_module._check_pending_writes()

    assert check.status == "pass"
    assert check.message == "2 writes buffered locally: 2 retrying."


def test_doctor_passes_on_an_empty_queue(sandbox_home: Path) -> None:
    check = doctor_module._check_pending_writes()

    assert check.status == "pass"


def test_an_unreadable_home_does_not_break_a_working_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The notice is best-effort. It must never be the thing that fails a command."""

    def no_home() -> Path:
        raise RuntimeError("Could not determine home directory")

    monkeypatch.setattr(pending_writes.Path, "home", no_home)
    monkeypatch.setattr("sys.argv", ["sibyl", "version"])

    with pytest.raises(SystemExit) as exc:
        cli_main()

    assert exc.value.code == 0
    assert "buffered locally" not in capsys.readouterr().err


def _run_entry_point(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the real console-script path so the finally-block notice runs."""
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit):
        cli_main()


def test_health_reports_the_queue_exactly_once(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`sibyl health` renders the queue itself, so the global notice must stand down."""
    _buffer(1)
    client = _health_client({"status": "healthy", "server_name": "sibyl"})

    with patch("sibyl_cli.main.get_client", return_value=client):
        _run_entry_point(["sibyl", "health"], monkeypatch)

    captured = capsys.readouterr()
    assert captured.out.count("1 write buffered locally") == 1
    assert "buffered locally" not in captured.err


def test_health_json_does_not_add_a_stderr_notice(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    _buffer(1)
    client = _health_client({"status": "healthy", "server_name": "sibyl"})

    with patch("sibyl_cli.main.get_client", return_value=client):
        _run_entry_point(["sibyl", "health", "--json"], monkeypatch)

    captured = capsys.readouterr()
    assert json.loads(captured.out)["pending_writes"]["count"] == 1
    assert "buffered locally" not in captured.err


def test_the_pending_writes_group_reports_the_queue_exactly_once(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _buffer(1)

    _run_entry_point(["sibyl", "pending-writes", "list"], monkeypatch)

    assert "buffered locally" not in capsys.readouterr().err


def test_a_value_that_merely_looks_like_the_group_name_still_gets_the_notice(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Suppression follows the command that ran, not a string anywhere in argv."""
    _buffer(1)

    _run_entry_point(["sibyl", "search", "pending-writes"], monkeypatch)

    assert "1 for another server" in capsys.readouterr().err


def test_the_quick_context_fast_path_still_warns(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Agent hooks take this path, and it never reaches sibyl_cli.main."""
    from sibyl_cli import entrypoint

    _buffer(1)
    monkeypatch.setattr("sys.argv", ["sibyl", "context", "--quick", "--json"])

    entrypoint.main()

    assert "1 for another server" in capsys.readouterr().err


def test_discarding_an_unknown_id_leaves_the_queue_reported(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """discard can leave the queue untouched, so it must not claim the report."""
    _buffer(1)
    monkeypatch.setattr("sys.argv", ["sibyl", "pending-writes", "discard"])

    _run_entry_point(["sibyl", "pending-writes", "discard"], monkeypatch)

    assert "1 for another server" in capsys.readouterr().err


def test_the_notice_prints_only_once_per_process(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two entry points nest around one command, so the notice has to be idempotent."""
    from sibyl_cli import entrypoint

    _buffer(2)
    monkeypatch.setattr("sys.argv", ["sibyl", "version"])

    with pytest.raises(SystemExit):
        entrypoint.main()

    assert capsys.readouterr().err.count("2 for another server") == 1


def test_a_broken_stderr_cannot_change_the_exit_status(
    sandbox_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rich reports a closed stream as ValueError and a broken pipe as SystemExit."""
    _buffer(1)

    for boom in (
        ValueError("I/O operation on closed file"),
        SystemExit(1),
        BrokenPipeError(),
    ):

        def explode(*_args: object, **_kwargs: object) -> None:
            raise boom

        monkeypatch.setattr(common, "_pending_writes_reported", False)
        monkeypatch.setattr(common.err_console, "print", explode)

        common.notify_pending_writes()
