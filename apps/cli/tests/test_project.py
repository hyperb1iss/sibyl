"""Tests for project CLI helpers."""

from unittest.mock import AsyncMock, MagicMock, patch

from typer.testing import CliRunner

from sibyl_cli.project import app


def test_project_relink_accepts_slug_reference() -> None:
    mock_client = MagicMock()
    mock_client.explore = AsyncMock(
        return_value={"entities": [{"id": "project_123", "name": "Hypercolor"}]}
    )

    with (
        patch("sibyl_cli.project.get_client", return_value=mock_client),
        patch("sibyl_cli.project.set_path_mapping") as mock_set_path_mapping,
    ):
        result = CliRunner().invoke(
            app,
            ["relink", "--id", "hypercolor", "--path", "/tmp/hypercolor"],
        )

    assert result.exit_code == 0
    assert "Relinked" in result.stdout
    mock_set_path_mapping.assert_called_once_with(
        "/tmp/hypercolor", "project_123", context=mock_client.context_name
    )


def test_project_relink_rejects_inaccessible_direct_id() -> None:
    mock_client = MagicMock()
    mock_client.explore = AsyncMock(
        return_value={"entities": [{"id": "project_123", "name": "Hypercolor"}]}
    )

    with (
        patch("sibyl_cli.project.get_client", return_value=mock_client),
        patch("sibyl_cli.project.set_path_mapping") as mock_set_path_mapping,
    ):
        result = CliRunner().invoke(
            app,
            ["relink", "--id", "project_missing", "--path", "/tmp/hypercolor"],
        )

    assert result.exit_code == 0
    assert "Project not found: project_missing" in result.stdout
    mock_set_path_mapping.assert_not_called()


def test_project_relink_auto_matches_current_directory() -> None:
    mock_client = MagicMock()
    mock_client.explore = AsyncMock(
        return_value={"entities": [{"id": "project_123", "name": "Hypercolor"}]}
    )

    with (
        patch("sibyl_cli.project.get_client", return_value=mock_client),
        patch("sibyl_cli.project.set_path_mapping") as mock_set_path_mapping,
    ):
        result = CliRunner().invoke(app, ["relink", "--path", "/tmp/hypercolor"])

    assert result.exit_code == 0
    assert "Relinked" in result.stdout
    mock_set_path_mapping.assert_called_once_with(
        "/tmp/hypercolor", "project_123", context=mock_client.context_name
    )


def test_project_relink_auto_matches_repository_url_basename() -> None:
    mock_client = MagicMock()
    mock_client.explore = AsyncMock(
        return_value={
            "entities": [
                {
                    "id": "project_123",
                    "name": "Other",
                    "metadata": {"repository_url": "git@github.com:hyperb1iss/hypercolor.git"},
                }
            ]
        }
    )

    with (
        patch("sibyl_cli.project.get_client", return_value=mock_client),
        patch("sibyl_cli.project.set_path_mapping") as mock_set_path_mapping,
    ):
        result = CliRunner().invoke(app, ["relink", "--path", "/tmp/hypercolor"])

    assert result.exit_code == 0
    assert "Relinked" in result.stdout
    mock_set_path_mapping.assert_called_once_with(
        "/tmp/hypercolor", "project_123", context=mock_client.context_name
    )


def test_project_relink_requires_explicit_id_for_ambiguous_matches() -> None:
    mock_client = MagicMock()
    mock_client.explore = AsyncMock(
        return_value={
            "entities": [
                {"id": "project_123", "name": "Hypercolor"},
                {"id": "project_456", "name": "hypercolor"},
            ]
        }
    )

    with (
        patch("sibyl_cli.project.get_client", return_value=mock_client),
        patch("sibyl_cli.project.set_path_mapping") as mock_set_path_mapping,
    ):
        result = CliRunner().invoke(app, ["relink", "--path", "/tmp/hypercolor"])

    assert result.exit_code == 0
    assert "Multiple accessible projects matched" in result.stdout
    assert "project_123" in result.stdout
    assert "project_456" in result.stdout
    mock_set_path_mapping.assert_not_called()
