"""Tests for the self-updater module."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sibyl_cli.update import (
    cli_update_available,
    get_current_cli_version,
    get_latest_cli_version,
    is_dev_mode,
)


class TestDevModeDetection:
    """Tests for is_dev_mode() detection."""

    def test_dev_mode_when_skills_are_symlinks(self, tmp_path: Path) -> None:
        """Detect dev mode when skills directory is a symlink."""
        # Create a fake symlinked skill
        skill_dir = tmp_path / ".claude" / "skills" / "sibyl"
        skill_dir.parent.mkdir(parents=True)
        target = tmp_path / "repo" / "skills" / "sibyl"
        target.mkdir(parents=True)
        skill_dir.symlink_to(target)

        with patch("sibyl_cli.update.Path.home", return_value=tmp_path):
            assert is_dev_mode() is True

    def test_not_dev_mode_when_skills_are_copies(self, tmp_path: Path) -> None:
        """Not dev mode when skills are regular directories."""
        skill_dir = tmp_path / ".claude" / "skills" / "sibyl"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("test")

        with (
            patch("sibyl_cli.update.Path.home", return_value=tmp_path),
            patch("sibyl_cli.update.Path.cwd", return_value=tmp_path / "some" / "other"),
        ):
            # Also need to create the cwd path
            (tmp_path / "some" / "other").mkdir(parents=True)
            assert is_dev_mode() is False

    def test_dev_mode_when_in_repo_directory(self, tmp_path: Path) -> None:
        """Detect dev mode when cwd is in the sibyl repo."""
        repo = tmp_path / "dev" / "sibyl"
        (repo / "apps" / "cli").mkdir(parents=True)
        (repo / "moon.yml").write_text("test")

        # Skills not symlinked
        skill_dir = tmp_path / ".claude" / "skills" / "sibyl"
        skill_dir.mkdir(parents=True)

        with (
            patch("sibyl_cli.update.Path.home", return_value=tmp_path),
            patch("sibyl_cli.update.Path.cwd", return_value=repo),
        ):
            assert is_dev_mode() is True

    def test_dev_mode_when_in_repo_subdirectory(self, tmp_path: Path) -> None:
        """Detect dev mode when cwd is a subdirectory of the repo."""
        repo = tmp_path / "dev" / "sibyl"
        subdir = repo / "apps" / "cli" / "src"
        subdir.mkdir(parents=True)
        (repo / "moon.yml").write_text("test")

        # Skills not symlinked
        skill_dir = tmp_path / ".claude" / "skills" / "sibyl"
        skill_dir.mkdir(parents=True)

        with (
            patch("sibyl_cli.update.Path.home", return_value=tmp_path),
            patch("sibyl_cli.update.Path.cwd", return_value=subdir),
        ):
            assert is_dev_mode() is True


class TestVersionChecking:
    """Tests for version checking functions."""

    def test_get_current_cli_version_returns_version(self) -> None:
        """get_current_cli_version returns the installed version."""
        with patch("sibyl_cli.update.pkg_version", return_value="0.1.0"):
            assert get_current_cli_version() == "0.1.0"

    def test_get_current_cli_version_handles_not_installed(self) -> None:
        """get_current_cli_version returns None if not installed."""
        with patch("sibyl_cli.update.pkg_version", side_effect=Exception("not found")):
            assert get_current_cli_version() is None

    def test_get_latest_cli_version_from_pypi(self) -> None:
        """get_latest_cli_version fetches from PyPI."""
        mock_response = MagicMock()
        mock_response.read.return_value = b'{"info": {"version": "0.2.0"}}'
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("sibyl_cli.update.urllib.request.urlopen", return_value=mock_response):
            assert get_latest_cli_version() == "0.2.0"

    def test_get_latest_cli_version_handles_network_error(self) -> None:
        """get_latest_cli_version returns None on network error."""
        with patch(
            "sibyl_cli.update.urllib.request.urlopen",
            side_effect=Exception("network error"),
        ):
            assert get_latest_cli_version() is None

    def test_cli_update_available_when_newer_version(self) -> None:
        """cli_update_available returns True when newer version exists."""
        with (
            patch("sibyl_cli.update.get_current_cli_version", return_value="0.1.0"),
            patch("sibyl_cli.update.get_latest_cli_version", return_value="0.2.0"),
        ):
            current, latest, available = cli_update_available()
            assert current == "0.1.0"
            assert latest == "0.2.0"
            assert available is True

    def test_cli_update_available_when_same_version(self) -> None:
        """cli_update_available returns False when versions match."""
        with (
            patch("sibyl_cli.update.get_current_cli_version", return_value="0.2.0"),
            patch("sibyl_cli.update.get_latest_cli_version", return_value="0.2.0"),
        ):
            current, latest, available = cli_update_available()
            assert current == "0.2.0"
            assert latest == "0.2.0"
            assert available is False

    def test_cli_update_available_handles_none_versions(self) -> None:
        """cli_update_available handles None versions gracefully."""
        with (
            patch("sibyl_cli.update.get_current_cli_version", return_value=None),
            patch("sibyl_cli.update.get_latest_cli_version", return_value="0.2.0"),
        ):
            current, latest, available = cli_update_available()
            assert current is None
            assert latest == "0.2.0"
            assert available is False

    def test_cli_update_available_handles_prerelease(self) -> None:
        """cli_update_available handles pre-release versions correctly."""
        with (
            patch("sibyl_cli.update.get_current_cli_version", return_value="0.1.0"),
            patch("sibyl_cli.update.get_latest_cli_version", return_value="0.2.0a1"),
        ):
            current, latest, available = cli_update_available()
            # 0.2.0a1 > 0.1.0 according to PEP 440
            assert available is True


class TestContainerUpdates:
    """Tests for container update checking."""

    def test_check_container_updates_no_compose_file(self, tmp_path: Path) -> None:
        """Returns zeros when no compose file exists."""
        from sibyl_cli.update import check_container_updates

        with patch("sibyl_cli.update.SIBYL_LOCAL_COMPOSE", tmp_path / "nonexistent.yml"):
            total, updates, images = check_container_updates()
            assert total == 0
            assert updates == 0
            assert images == []

    def test_get_local_image_digest_returns_digest(self) -> None:
        """get_local_image_digest extracts digest from docker inspect."""
        from sibyl_cli.update import get_local_image_digest

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "falkordb/falkordb@sha256:abc123\n"

        with patch("sibyl_cli.update.subprocess.run", return_value=mock_result):
            digest = get_local_image_digest("falkordb/falkordb")
            assert digest == "sha256:abc123"

    def test_get_local_image_digest_handles_missing_image(self) -> None:
        """get_local_image_digest returns None for missing images."""
        from sibyl_cli.update import get_local_image_digest

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("sibyl_cli.update.subprocess.run", return_value=mock_result):
            digest = get_local_image_digest("nonexistent/image")
            assert digest is None


class TestUpdateFunctions:
    """Tests for update execution functions."""

    def test_update_cli_success(self) -> None:
        """update_cli returns True on successful upgrade."""
        from sibyl_cli.update import update_cli

        mock_result = MagicMock()
        mock_result.returncode = 0

        with (
            patch("sibyl_cli.update.subprocess.run", return_value=mock_result),
            patch("sibyl_cli.update.get_current_cli_version", return_value="0.2.0"),
        ):
            assert update_cli() is True

    def test_update_cli_failure(self) -> None:
        """update_cli returns False on failed upgrade."""
        from sibyl_cli.update import update_cli

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error message"

        with patch("sibyl_cli.update.subprocess.run", return_value=mock_result):
            assert update_cli() is False

    def test_update_containers_no_compose(self, tmp_path: Path) -> None:
        """update_containers returns False when no compose file."""
        from sibyl_cli.update import update_containers

        with patch("sibyl_cli.update.SIBYL_LOCAL_COMPOSE", tmp_path / "nonexistent.yml"):
            assert update_containers() is False

    def test_update_skills_delegates_to_setup(self) -> None:
        """update_skills calls setup_agent_integration."""
        from sibyl_cli.update import update_skills

        with patch("sibyl_cli.setup.setup_agent_integration", return_value=True) as mock:
            assert update_skills() is True
            mock.assert_called_once_with(verbose=False)
