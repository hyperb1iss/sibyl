"""Tests for `sibyl task assign` and `sibyl task unassign`."""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli.client import SibylClient, SibylClientError
from sibyl_cli.main import app

TASK = "13364346-8475-4664-8b52-eb963af2fda7"
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture(autouse=True)
def outside_any_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests often run inside a coding harness, which exports the very
    # variables agent detection reads.
    for variable in (
        "SIBYL_AGENT",
        "SIBYL_SESSION",
        "CODEX_THREAD_ID",
        "CLAUDECODE",
        "CLAUDE_CODE_SESSION_ID",
    ):
        monkeypatch.delenv(variable, raising=False)


def _response(*, changed: bool = True, posted: bool = True) -> dict[str, Any]:
    return {
        "task": {"id": TASK, "name": "Fix the flaky fixture", "metadata": {"assignees": ["@ada"]}},
        "changed": changed,
        "message": (
            {"id": "msg-1", "channel": "@ada", "thread": f"task:{TASK}", "body": "Assigned: x"}
            if posted
            else None
        ),
        "mutation_receipt": {
            "operation_id": "op-1",
            "applied": changed or posted,
            "revision": 3,
            "affected_records": [],
        },
    }


def _invoke(client: MagicMock, args: list[str]) -> Any:
    with patch("sibyl_cli.task.get_client", return_value=client):
        return CliRunner().invoke(app, ["task", *args])


def _plain(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


@pytest.mark.asyncio
async def test_handoffs_are_unbuffered_posts_with_a_fresh_key_each() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="token")
    request = AsyncMock(return_value={})
    client._request = request  # type: ignore[method-assign]

    await client.assign_task(TASK, "@ada", note="Over to you", agent="codex", session="t-9")
    first_key = request.await_args.kwargs["_idempotency_key"]
    request.assert_awaited_once_with(
        "POST",
        f"/tasks/{TASK}/assign",
        json={"to": "@ada", "note": "Over to you", "agent": "codex", "session": "t-9"},
        _buffer_pending=False,
        _idempotency_key=first_key,
    )
    request.reset_mock()

    await client.unassign_task(TASK, "@ada")
    second_key = request.await_args.kwargs["_idempotency_key"]
    request.assert_awaited_once_with(
        "POST",
        f"/tasks/{TASK}/unassign",
        json={"assignee": "@ada"},
        _buffer_pending=False,
        _idempotency_key=second_key,
    )
    assert first_key and second_key and first_key != second_key


def test_assign_hands_the_task_over_and_says_where_it_posted() -> None:
    client = MagicMock()
    client.assign_task = AsyncMock(return_value=_response())

    result = _invoke(client, ["assign", TASK, "@Ada", "-m", "Fixture fix is on the branch"])

    assert result.exit_code == 0, result.stdout
    client.assign_task.assert_awaited_once_with(
        TASK, "@Ada", note="Fixture fix is on the branch", agent=None, session=None
    )
    output = _plain(result.stdout)
    assert f"Assigned {TASK} to @ada" in output
    assert f"Posted msg-1 to @ada task:{TASK}" in output
    assert "Revision: 3" in output


def test_assigning_again_says_the_handoff_was_posted_again() -> None:
    client = MagicMock()
    client.assign_task = AsyncMock(return_value=_response(changed=False))

    result = _invoke(client, ["assign", TASK, "@ada"])

    assert result.exit_code == 0, result.stdout
    assert "@ada was already assigned; posted the handoff again" in _plain(result.stdout)


def test_assign_carries_the_detected_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-4")
    client = MagicMock()
    client.assign_task = AsyncMock(return_value=_response())

    result = _invoke(client, ["assign", TASK, "@ada"])

    assert result.exit_code == 0, result.stdout
    client.assign_task.assert_awaited_once_with(
        TASK, "@ada", note=None, agent="codex", session="thread-4"
    )


def test_assign_json_prints_the_server_response() -> None:
    client = MagicMock()
    client.assign_task = AsyncMock(return_value=_response())

    result = _invoke(client, ["assign", TASK, "@ada", "--json"])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == _response()


def test_a_refused_assign_exits_nonzero() -> None:
    client = MagicMock()
    client.assign_task = AsyncMock(
        side_effect=SibylClientError("api_key_project_denied", status_code=403)
    )

    result = _invoke(client, ["assign", TASK, "@ada"])

    assert result.exit_code != 0


def test_unassign_reports_the_notice_or_that_nothing_changed() -> None:
    client = MagicMock()
    client.unassign_task = AsyncMock(return_value=_response())

    result = _invoke(client, ["unassign", TASK, "@Ada"])

    assert result.exit_code == 0, result.stdout
    client.unassign_task.assert_awaited_once_with(TASK, "@Ada", agent=None, session=None)
    output = _plain(result.stdout)
    assert f"Unassigned @ada from {TASK}" in output
    assert f"Posted msg-1 to @ada task:{TASK}" in output

    client.unassign_task = AsyncMock(return_value=_response(changed=False, posted=False))
    result = _invoke(client, ["unassign", TASK, "@ada"])
    assert result.exit_code == 0, result.stdout
    assert f"@ada was not assigned to {TASK}; nothing posted" in _plain(result.stdout)
