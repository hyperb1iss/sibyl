"""Tests for the API key CLI commands."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli.auth import app
from sibyl_cli.client import SibylClientError

_SECRET = "secret-canary-0123456789abcdef-never-printed"

_ACTIVE_KEY = {
    "id": "key-active-0001",
    "name": "claude-mcp",
    "prefix": "sk_live_0123",
    "scopes": ["mcp", "api:read"],
    "project_ids": ["proj_a", "proj_b"],
    "memory_space_ids": ["space_main"],
    "expires_at": "2999-01-01T00:00:00+00:00",
    "revoked_at": None,
    "last_used_at": "2026-09-30T08:15:00+00:00",
    "created_at": "2026-09-01T12:00:00+00:00",
}
_EXPIRED_KEY = {
    "id": "key-expired-0002",
    "name": "ci-readonly",
    "prefix": "sk_test_4567",
    "scopes": ["api:read"],
    "project_ids": [],
    "memory_space_ids": [],
    "expires_at": "2020-01-01T00:00:00Z",
    "revoked_at": None,
    "last_used_at": None,
    "created_at": "2019-12-01T00:00:00Z",
}
_REVOKED_KEY = {
    "id": "key-revoked-0003",
    "name": "old-laptop",
    "prefix": "sk_live_89ab",
    "scopes": ["mcp"],
    "project_ids": [],
    "memory_space_ids": [],
    "expires_at": None,
    "revoked_at": "2026-08-01T00:00:00+00:00",
    "last_used_at": None,
    "created_at": "2026-07-01T00:00:00+00:00",
}


def _listing_with_secret_fields() -> dict:
    # A server that ever adds secret material to the listing must not leak it
    # through the CLI, so each key carries fields the CLI has to drop.
    return {
        "keys": [
            {**key, "key": _SECRET, "key_hash": f"hash-of-{_SECRET}"}
            for key in (_ACTIVE_KEY, _EXPIRED_KEY, _REVOKED_KEY)
        ]
    }


def _api_key_client(listing: dict) -> MagicMock:
    mock_client = MagicMock()
    mock_client.list_api_keys = AsyncMock(return_value=listing)
    return mock_client


@pytest.mark.parametrize("flag", ["--json", "-j"])
def test_api_key_list_json_carries_listing_fields_and_no_secret(flag: str) -> None:
    client = _api_key_client(_listing_with_secret_fields())
    with patch("sibyl_cli.auth.get_client", return_value=client):
        result = CliRunner().invoke(app, ["api-key", "list", flag])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"keys": [_ACTIVE_KEY, _EXPIRED_KEY, _REVOKED_KEY]}
    assert _SECRET not in result.stdout
    assert "key_hash" not in result.stdout


def test_api_key_list_defaults_to_a_table_without_the_secret() -> None:
    client = _api_key_client(_listing_with_secret_fields())
    with patch("sibyl_cli.auth.get_client", return_value=client):
        result = CliRunner().invoke(app, ["api-key", "list"])

    assert result.exit_code == 0, result.stdout
    assert '"keys"' not in result.stdout
    assert _SECRET not in result.stdout
    for value in (
        "API Keys",
        "claude-mcp",
        "key-active-0001",
        "sk_live_0123",
        "mcp, api:read",
        "2 projects, 1 space",
        "2026-09-01",
        "2026-09-30",
        "active",
        "ci-readonly",
        "expired",
        "old-laptop",
        "revoked",
    ):
        assert value in result.stdout


def test_api_key_list_says_so_when_there_are_no_keys() -> None:
    with patch("sibyl_cli.auth.get_client", return_value=_api_key_client({"keys": []})):
        result = CliRunner().invoke(app, ["api-key", "list"])

    assert result.exit_code == 0, result.stdout
    assert "No API keys found" in result.stdout


@pytest.mark.parametrize("argv", [["api-key", "list"], ["api-key", "list", "--json"]])
def test_api_key_list_exits_non_zero_when_the_api_rejects_it(argv: list[str]) -> None:
    mock_client = MagicMock()
    mock_client.list_api_keys = AsyncMock(
        side_effect=SibylClientError("API error: forbidden", status_code=403)
    )
    with patch("sibyl_cli.auth.get_client", return_value=mock_client):
        result = CliRunner().invoke(app, argv)

    assert result.exit_code == 1
    assert "API error: forbidden" in result.stdout
