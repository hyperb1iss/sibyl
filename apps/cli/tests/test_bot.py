"""Tests for the `sibyl bot` commands."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli import client_transport
from sibyl_cli.bot import app

BOT_ID = "00000000-0000-0000-0000-00000000b07b"
_BOT = {
    "id": BOT_ID,
    "name": "oncall",
    "kind": "service",
    "status": "active",
    "sponsor": {"id": "00000000-0000-0000-0000-00000000a5a5", "name": "Bliss"},
    "created_at": "2026-11-04T12:00:00Z",
}


class _FakeClientContext:
    def __init__(self, client: MagicMock) -> None:
        self._client = client

    async def __aenter__(self) -> MagicMock:
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        return None


def _client(**methods: object) -> MagicMock:
    client = MagicMock()
    client.list_bots = AsyncMock(return_value={"bots": [_BOT]})
    for name, value in methods.items():
        setattr(client, name, AsyncMock(return_value=value))
    return client


def _invoke(client: MagicMock, *args: str, stdin: str | None = None):
    with patch("sibyl_cli.bot.get_client", return_value=_FakeClientContext(client)):
        return CliRunner().invoke(app, list(args), input=stdin)


def test_create_prints_the_bot_and_the_next_steps() -> None:
    client = _client(create_bot=_BOT)
    result = _invoke(client, "create", "oncall")
    assert result.exit_code == 0, result.output
    client.create_bot.assert_awaited_once_with("oncall")
    assert BOT_ID in result.output
    assert "sibyl bot grant" in result.output


def test_list_shows_sponsor_and_status() -> None:
    result = _invoke(_client(), "list")
    assert result.exit_code == 0, result.output
    assert "oncall" in result.output
    assert "Bliss" in result.output
    assert "active" in result.output


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("contributor", "project_contributor"),
        ("viewer", "project_viewer"),
        ("project_viewer", "project_viewer"),
    ],
)
def test_grant_takes_a_bot_name_and_a_short_role(given: str, sent: str) -> None:
    client = _client(
        grant_bot_project={"bot_id": BOT_ID, "project_id": "project_alpha", "role": sent}
    )
    result = _invoke(client, "grant", "oncall", "project_alpha", "--role", given)
    assert result.exit_code == 0, result.output
    client.grant_bot_project.assert_awaited_once_with(BOT_ID, "project_alpha", sent)


def test_an_unknown_bot_name_is_reported_not_guessed() -> None:
    client = _client(grant_bot_project={})
    result = _invoke(client, "grant", "nightly", "project_alpha")
    assert result.exit_code == 1
    client.grant_bot_project.assert_not_awaited()


def test_key_create_prints_the_secret_once_as_json() -> None:
    issued = {"id": "key-1", "bot_id": BOT_ID, "api_key": "sk_live_secret", "scopes": ["mcp"]}
    client = _client(create_bot_api_key=issued)
    result = _invoke(
        client, "key", "create", BOT_ID, "--name", "pager", "--projects", "project_alpha"
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["api_key"] == "sk_live_secret"
    kwargs = client.create_bot_api_key.await_args.kwargs
    assert kwargs["project_ids"] == ["project_alpha"]
    assert kwargs["scopes"] == ["mcp", "api:read", "api:write"]
    # An ID skips the name lookup entirely.
    client.list_bots.assert_not_awaited()


def test_delete_asks_first_unless_told_not_to() -> None:
    client = _client(delete_bot={"id": BOT_ID, "api_keys_revoked": 2})
    refused = _invoke(client, "delete", "oncall", stdin="n\n")
    assert refused.exit_code == 1
    client.delete_bot.assert_not_awaited()
    result = _invoke(client, "delete", "oncall", "--yes")
    assert result.exit_code == 0, result.output
    client.delete_bot.assert_awaited_once_with(BOT_ID)


def test_bot_writes_are_never_buffered_for_replay() -> None:
    # A replayed key mint would create a key whose secret nobody ever sees.
    for path in ("/bots", f"/bots/{BOT_ID}/keys", f"/bots/{BOT_ID}/projects"):
        assert client_transport._should_buffer_request("POST", path) is False, path
    assert client_transport._should_buffer_request("DELETE", f"/bots/{BOT_ID}") is False
