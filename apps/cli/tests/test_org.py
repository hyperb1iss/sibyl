"""Tests for organization CLI commands."""

from __future__ import annotations

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


@pytest.mark.parametrize(
    ("client_method", "argv"),
    [
        ("list_orgs", ["list"]),
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
