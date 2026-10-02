"""Tests for organization CLI commands."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli.client import SibylClientError
from sibyl_cli.org import app

_REJECTED = SibylClientError(
    "API error: forbidden",
    status_code=403,
    detail="Access denied",
)


_ORG_LISTING = {
    "orgs": [
        {
            "id": "org-personal-0001",
            "slug": "bliss",
            "name": "Bliss Personal",
            "is_personal": True,
            "role": "owner",
        },
        {
            "id": "org-team-0002",
            "slug": "hypercolor",
            "name": "Hypercolor Team",
            "is_personal": False,
            "role": "member",
        },
    ]
}


def _org_list_client(listing: dict) -> MagicMock:
    mock_client = MagicMock()
    mock_client.list_orgs = AsyncMock(return_value=listing)
    return mock_client


@pytest.mark.parametrize("flag", ["--json", "-j"])
def test_org_list_json_prints_the_server_listing(flag: str) -> None:
    with patch("sibyl_cli.org.get_client", return_value=_org_list_client(_ORG_LISTING)):
        result = CliRunner().invoke(app, ["list", flag])

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout) == _ORG_LISTING


def test_org_list_defaults_to_a_table_of_the_same_orgs() -> None:
    with patch("sibyl_cli.org.get_client", return_value=_org_list_client(_ORG_LISTING)):
        result = CliRunner().invoke(app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert '"orgs"' not in result.stdout
    for value in (
        "Organizations",
        "bliss",
        "Bliss Personal",
        "owner",
        "personal",
        "org-personal-0001",
        "hypercolor",
        "Hypercolor Team",
        "member",
        "team",
        "org-team-0002",
    ):
        assert value in result.stdout


def test_org_list_renders_bracketed_names_literally() -> None:
    listing = {
        "orgs": [
            {**_ORG_LISTING["orgs"][0], "name": "[/]broken"},
            {**_ORG_LISTING["orgs"][1], "name": "[red]prod"},
        ]
    }
    with patch("sibyl_cli.org.get_client", return_value=_org_list_client(listing)):
        result = CliRunner().invoke(app, ["list"])

    assert result.exit_code == 0, result.output
    assert "[/]broken" in result.stdout
    assert "[red]prod" in result.stdout


def test_org_list_says_so_when_the_caller_has_no_orgs() -> None:
    with patch("sibyl_cli.org.get_client", return_value=_org_list_client({"orgs": []})):
        result = CliRunner().invoke(app, ["list"])

    assert result.exit_code == 0, result.stdout
    assert "No organizations found" in result.stdout


@pytest.mark.parametrize(
    ("client_method", "argv"),
    [
        ("list_orgs", ["list"]),
        ("list_orgs", ["list", "--json"]),
        ("create_org", ["create", "--name", "Hypercolor"]),
        ("switch_org", ["switch", "hypercolor"]),
        ("list_org_members", ["members", "list", "hypercolor"]),
        ("add_org_member", ["members", "add", "hypercolor", "user_1"]),
        ("remove_org_member", ["members", "remove", "hypercolor", "user_1", "--force"]),
        ("update_org_member_role", ["members", "role", "hypercolor", "user_1", "admin"]),
    ],
)
def test_org_commands_exit_non_zero_when_the_api_rejects_them(
    client_method: str,
    argv: list[str],
) -> None:
    mock_client = MagicMock()
    setattr(mock_client, client_method, AsyncMock(side_effect=_REJECTED))

    with patch("sibyl_cli.org.get_client", return_value=mock_client):
        result = CliRunner().invoke(app, argv)

    assert result.exit_code == 1
    assert "✗ API error: forbidden" in result.stdout


def test_switch_pins_the_context_to_the_org_it_switched_to(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Switched tokens are stored under the new org's credential scope; a
    # context still naming no org would keep reading the old token, so the
    # switch would change nothing for the next command.
    from sibyl_cli import auth_store, config_store

    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "sibyl_cli.pending_identity.warm_pending_replay_identity", lambda *_a, **_k: None
    )
    config_store.create_context("team", "https://sibyl.team.example", set_active=True)
    mock_client = MagicMock()
    mock_client.base_url = "https://sibyl.team.example/api"
    mock_client.switch_org = AsyncMock(
        return_value={
            "organization": {"id": "org-team", "slug": "acme", "name": "Acme"},
            "access_token": "token-for-acme",
            "refresh_token": "refresh-for-acme",
            "expires_in": 3600,
        }
    )

    with patch("sibyl_cli.org.get_client", return_value=mock_client):
        result = CliRunner().invoke(app, ["switch", "acme"])

    assert result.exit_code == 0, result.stdout
    ctx = config_store.get_context("team")
    assert ctx is not None and ctx.org_slug == "acme"
    stored = auth_store.read_server_credentials(
        "https://sibyl.team.example/api",
        credential_scope=auth_store.credential_scope("team", "acme"),
    )
    assert stored.get("access_token") == "token-for-acme"


def test_an_environment_targeted_switch_leaves_the_local_context_alone(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Paired SIBYL_API_URL + SIBYL_AUTH_TOKEN aim the client at another server;
    # the switch must not refile or repoint the unrelated active context.
    from sibyl_cli import auth_store, config_store

    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "sibyl_cli.pending_identity.warm_pending_replay_identity", lambda *_a, **_k: None
    )
    monkeypatch.setenv("SIBYL_API_URL", "https://remote.example/api")
    monkeypatch.setenv("SIBYL_AUTH_TOKEN", "automation-token")
    config_store.create_context(
        "local", "http://localhost:3334", org_slug="local-org", set_active=True
    )
    mock_client = MagicMock()
    mock_client.base_url = "https://remote.example/api"
    mock_client.switch_org = AsyncMock(
        return_value={
            "organization": {"id": "org-b", "slug": "team-b", "name": "Team B"},
            "access_token": "switched-token",
            "refresh_token": None,
            "expires_in": 3600,
        }
    )

    with patch("sibyl_cli.org.get_client", return_value=mock_client):
        result = CliRunner().invoke(app, ["switch", "team-b"])

    assert result.exit_code == 0, result.stdout
    ctx = config_store.get_context("local")
    assert ctx is not None and ctx.org_slug == "local-org"
    assert "SIBYL_AUTH_TOKEN authenticates this server" in result.stdout
    for org in ("team-b", "local-org"):
        assert not auth_store.read_server_credentials(
            "https://remote.example/api",
            credential_scope=auth_store.credential_scope("local", org),
            fallback_to_server=False,
        ).get("access_token")


def test_create_switches_into_the_new_org_using_its_reported_slug(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The server nests the new slug under "organization"; reading a top-level
    # slug left a context pinned to the old org while the new token was filed
    # under the default scope, so the "switch" changed nothing.
    from sibyl_cli import auth_store, config_store

    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        "sibyl_cli.pending_identity.warm_pending_replay_identity", lambda *_a, **_k: None
    )
    config_store.create_context(
        "team", "https://sibyl.team.example", org_slug="old-team", set_active=True
    )
    mock_client = MagicMock()
    mock_client.base_url = "https://sibyl.team.example/api"
    mock_client.create_org = AsyncMock(
        return_value={
            "organization": {"id": "org-new", "slug": "new-team", "name": "New Team"},
            "access_token": "token-for-new-team",
            "refresh_token": "refresh-for-new-team",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
    )

    with patch("sibyl_cli.org.get_client", return_value=mock_client):
        result = CliRunner().invoke(app, ["create", "--name", "New Team"])

    assert result.exit_code == 0, result.stdout
    ctx = config_store.get_context("team")
    assert ctx is not None and ctx.org_slug == "new-team"
    stored = auth_store.read_server_credentials(
        "https://sibyl.team.example/api",
        credential_scope=auth_store.credential_scope("team", "new-team"),
        fallback_to_server=False,
    )
    assert stored.get("access_token") == "token-for-new-team"
