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
