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
    "id": "key-0001",
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
    "id": "key-0002",
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
    "id": "key-0003",
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
_NAIVE_EXPIRY_KEY = {
    "id": "key-0004",
    "name": "naive-clock",
    "prefix": "sk_test_cdef",
    "scopes": ["mcp"],
    "project_ids": [],
    "memory_space_ids": [],
    "expires_at": "2020-06-01T00:00:00",
    "revoked_at": None,
    "last_used_at": None,
    "created_at": "2020-05-01T00:00:00",
}
_GARBLED_EXPIRY_KEY = {
    "id": "key-0005",
    "name": "garbled-clock",
    "prefix": "sk_test_9876",
    "scopes": ["mcp"],
    "project_ids": [],
    "memory_space_ids": [],
    "expires_at": "not-a-date",
    "revoked_at": None,
    "last_used_at": None,
    "created_at": "2026-07-01T00:00:00+00:00",
}
_KEYS = (_ACTIVE_KEY, _EXPIRED_KEY, _REVOKED_KEY, _NAIVE_EXPIRY_KEY, _GARBLED_EXPIRY_KEY)


def _listing_with_secret_fields() -> dict:
    # A server that ever adds secret material to the listing must not leak it
    # through the CLI, so each key carries fields the CLI has to drop.
    return {"keys": [{**key, "key": _SECRET, "key_hash": f"hash-of-{_SECRET}"} for key in _KEYS]}


def _api_key_client(listing: object) -> MagicMock:
    mock_client = MagicMock()
    mock_client.list_api_keys = AsyncMock(return_value=listing)
    return mock_client


@pytest.mark.parametrize("flag", ["--json", "-j"])
def test_api_key_list_json_carries_listing_fields_and_no_secret(flag: str) -> None:
    client = _api_key_client(_listing_with_secret_fields())
    with patch("sibyl_cli.auth.get_client", return_value=client):
        result = CliRunner().invoke(app, ["api-key", "list", flag])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == {"keys": list(_KEYS)}
    assert _SECRET not in result.stdout
    assert "key_hash" not in result.stdout


def test_api_key_list_defaults_to_a_table_without_the_secret() -> None:
    client = _api_key_client(_listing_with_secret_fields())
    with patch("sibyl_cli.auth.get_client", return_value=client):
        result = CliRunner().invoke(app, ["api-key", "list"])

    assert result.exit_code == 0, result.stdout
    assert '"keys"' not in result.stdout
    assert _SECRET not in result.stdout
    assert "API Keys" in result.stdout
    expected_rows = {
        "claude-mcp": (
            "key-0001",
            "sk_live_0123",
            "mcp, api:read",
            "2 projects, 1 space",
            "2026-09-01",
            "2026-09-30",
            "2999-01-01",
            "active",
        ),
        "ci-readonly": ("key-0002", "sk_test_4567", "2020-01-01", "expired"),
        "old-laptop": ("key-0003", "sk_live_89ab", "revoked"),
        # A timestamp without an offset is read as UTC, not skipped.
        "naive-clock": ("key-0004", "2020-06-01", "expired"),
        # An expiry the CLI cannot read is not reported as active.
        "garbled-clock": ("key-0005", "not-a-date", "unknown"),
    }
    for name, values in expected_rows.items():
        row = _table_row(result.stdout, name)
        for value in values:
            assert value in row, (name, value, row)


def _table_row(output: str, name: str) -> str:
    rows = [line for line in output.splitlines() if name in line]
    assert len(rows) == 1, (name, output)
    return rows[0]


def test_api_key_list_renders_bracketed_names_literally() -> None:
    listing = {
        "keys": [
            {**_ACTIVE_KEY, "name": "[/]broken"},
            {**_EXPIRED_KEY, "name": "[red]prod"},
        ]
    }
    with patch("sibyl_cli.auth.get_client", return_value=_api_key_client(listing)):
        result = CliRunner().invoke(app, ["api-key", "list"])

    assert result.exit_code == 0, result.output
    assert "[/]broken" in result.stdout
    assert "[red]prod" in result.stdout


_MALFORMED_KEY_LISTINGS = [
    [],
    None,
    {},
    {"keys": None},
    {"keys": {"id": "x"}},
    {"keys": [_ACTIVE_KEY, "not-a-key"]},
]


@pytest.mark.parametrize("flags", [[], ["--json"]])
@pytest.mark.parametrize("listing", _MALFORMED_KEY_LISTINGS)
def test_api_key_list_fails_on_a_malformed_listing(listing: object, flags: list[str]) -> None:
    # Reporting a broken listing as "no keys" would hide a server fault.
    with patch("sibyl_cli.auth.get_client", return_value=_api_key_client(listing)):
        result = CliRunner().invoke(app, ["api-key", "list", *flags])

    assert result.exit_code == 1, result.output
    assert "Server returned a malformed API key listing" in result.stdout
    assert '"keys"' not in result.stdout
    assert "No API keys found" not in result.stdout


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
