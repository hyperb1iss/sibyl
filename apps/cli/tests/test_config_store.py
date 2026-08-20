"""Tests for CLI config store."""

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from sibyl_cli import config_store


class TestConfigStore:
    """Test config store operations."""

    def test_default_config_structure(self) -> None:
        """Default config has expected structure."""
        config = config_store.DEFAULT_CONFIG
        assert "server" in config
        assert "defaults" in config
        assert "paths" in config
        assert "active_context" in config
        assert "contexts" in config

    def test_default_server_url(self) -> None:
        """Default server URL is localhost:3334."""
        assert config_store.DEFAULT_CONFIG["server"]["url"] == "http://localhost:3334"

    def test_get_nested_value(self) -> None:
        """Can get nested values with dot notation."""
        test_config = {"level1": {"level2": {"value": "found"}}}
        result = config_store._get_nested(test_config, "level1.level2.value")
        assert result == "found"

    def test_get_nested_value_missing(self) -> None:
        """Returns default for missing nested values."""
        test_config = {"level1": {}}
        result = config_store._get_nested(test_config, "level1.level2.value", "default")
        assert result == "default"

    def test_set_nested_value(self) -> None:
        """Can set nested values with dot notation."""
        test_config: dict = {}
        config_store._set_nested(test_config, "level1.level2.value", "set")
        assert test_config["level1"]["level2"]["value"] == "set"

    def test_deep_copy(self) -> None:
        """Deep copy creates independent copy."""
        original = {"a": {"b": "value"}}
        copied = config_store._deep_copy(original)
        copied["a"]["b"] = "changed"
        assert original["a"]["b"] == "value"

    def test_deep_merge(self) -> None:
        """Deep merge combines dicts correctly."""
        base = {"a": {"b": 1, "c": 2}}
        override = {"a": {"b": 10, "d": 3}}
        config_store._deep_merge(base, override)
        assert base["a"]["b"] == 10
        assert base["a"]["c"] == 2
        assert base["a"]["d"] == 3

    def test_corrupt_config_is_a_typed_failure_without_overwrite(
        self,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.toml"
        corrupt = b"[server\nurl = 'https://broken.example.com'\n"
        config_path.write_bytes(corrupt)

        with (
            patch.object(config_store, "config_dir", return_value=tmp_path),
            pytest.raises(config_store.ConfigCorruptionError) as exc_info,
        ):
            config_store.set_value("server.url", "https://new.example.com")

        assert exc_info.value.path == config_path
        assert "sibyl config reset" in str(exc_info.value)
        assert config_path.read_bytes() == corrupt

    def test_non_utf8_config_is_a_typed_failure_without_overwrite(
        self,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.toml"
        corrupt = b"[server]\nurl = \"\xff\"\n"
        config_path.write_bytes(corrupt)

        with (
            patch.object(config_store, "config_dir", return_value=tmp_path),
            pytest.raises(config_store.ConfigCorruptionError) as exc_info,
        ):
            config_store.set_value("server.url", "https://new.example.com")

        assert isinstance(exc_info.value.cause, UnicodeDecodeError)
        assert config_path.read_bytes() == corrupt

    @pytest.mark.parametrize(
        "content,field",
        [
            ('server = "production"\n', "server"),
            ("[server]\nurl = 42\n", "server.url"),
            ("active_context = 42\n", "active_context"),
            ('paths = ["/repo"]\n', "paths"),
            ('[paths]\n"/repo" = 42\n', "paths./repo"),
            ('contexts = ["production"]\n', "contexts"),
            ("[contexts.production]\nserver_url = 42\n", "contexts.production.server_url"),
            (
                '[contexts.production]\nserver_url = "https://example.com"\ninsecure = "yes"\n',
                "contexts.production.insecure",
            ),
        ],
    )
    def test_valid_toml_with_wrong_known_shapes_never_defaults_or_retargets(
        self,
        tmp_path: Path,
        content: str,
        field: str,
    ) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(content, encoding="utf-8")
        original = config_path.read_bytes()

        with (
            patch.object(config_store, "config_dir", return_value=tmp_path),
            pytest.raises(config_store.ConfigCorruptionError) as exc_info,
        ):
            config_store.set_value("defaults.project", "project_safe")

        assert field in str(exc_info.value)
        assert "invalid schema" in str(exc_info.value)
        assert config_path.read_bytes() == original

    def test_mutation_cannot_write_an_invalid_known_shape(self, tmp_path: Path) -> None:
        with patch.object(config_store, "config_dir", return_value=tmp_path):
            config_store.set_value("defaults.project", "project_safe")
            original = config_store.config_path().read_bytes()

            with pytest.raises(config_store.ConfigCorruptionError, match="server"):
                config_store.set_value("server", "production")

            assert config_store.config_path().read_bytes() == original
            assert config_store.get_default_project() == "project_safe"

    def test_save_uses_same_directory_replace_and_fsync(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        replacements: list[tuple[Path, Path]] = []
        fsync_calls: list[int] = []
        original_replace = os.replace
        original_fsync = os.fsync

        def replace(source: str | Path, destination: str | Path) -> None:
            replacements.append((Path(source), Path(destination)))
            original_replace(source, destination)

        def fsync(fd: int) -> None:
            fsync_calls.append(fd)
            original_fsync(fd)

        monkeypatch.setattr(config_store.os, "replace", replace)
        monkeypatch.setattr(config_store.os, "fsync", fsync)

        with patch.object(config_store, "config_dir", return_value=tmp_path):
            config_store.save_config({"server": {"url": "https://safe.example.com"}})

        assert len(replacements) == 1
        temporary, destination = replacements[0]
        assert temporary.parent == destination.parent == tmp_path
        assert destination == tmp_path / "config.toml"
        assert len(fsync_calls) >= 1
        assert config_store._load_config_unlocked(destination)["server"]["url"] == (
            "https://safe.example.com"
        )

    def test_update_config_serializes_competing_processes(self, tmp_path: Path) -> None:
        script = """
import sys
import time
from pathlib import Path
from sibyl_cli import config_store

config_store.config_dir = lambda: Path(sys.argv[1])

def append(config):
    updates = config.setdefault("updates", [])
    time.sleep(0.2)
    updates.append(sys.argv[2])

config_store.update_config(append)
"""
        first = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), "first"])
        second = subprocess.Popen([sys.executable, "-c", script, str(tmp_path), "second"])

        assert first.wait(timeout=10) == 0
        assert second.wait(timeout=10) == 0
        with patch.object(config_store, "config_dir", return_value=tmp_path):
            assert sorted(config_store.load_config()["updates"]) == ["first", "second"]

    def test_ensure_config_file_preserves_an_existing_writer(self, tmp_path: Path) -> None:
        with patch.object(config_store, "config_dir", return_value=tmp_path):
            assert config_store.ensure_config_file() is True
            config_store.set_value("defaults.project", "project_concurrent")
            before = config_store.config_path().read_bytes()

            assert config_store.ensure_config_file() is False
            assert config_store.config_path().read_bytes() == before
            assert config_store.get_default_project() == "project_concurrent"

    def test_config_edit_uses_locked_file_creation(
        self,
        tmp_path: Path,
    ) -> None:
        from typer.testing import CliRunner

        from sibyl_cli import config_cmd

        path = tmp_path / "config.toml"
        with (
            patch.object(config_store, "config_path", return_value=path),
            patch.object(config_store, "ensure_config_file", return_value=True) as ensure,
            patch("subprocess.run") as run,
        ):
            result = CliRunner().invoke(config_cmd.app, ["edit"])

        assert result.exit_code == 0
        ensure.assert_called_once_with()
        run.assert_called_once()
        assert "Created default config" in result.stdout

    def test_cli_renders_corruption_without_a_traceback(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from sibyl_cli.main import main as cli_main

        config_home = tmp_path / ".sibyl"
        config_home.mkdir()
        (config_home / "config.toml").write_text("[broken", encoding="utf-8")
        monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(sys, "argv", ["sibyl", "config", "show"])

        with pytest.raises(SystemExit) as exc_info:
            cli_main()

        captured = capsys.readouterr()
        assert exc_info.value.code == 1
        assert "invalid" in captured.out
        assert "TOML" in captured.out
        assert "sibyl config reset" in captured.out
        assert "Traceback" not in captured.out + captured.err


class TestContext:
    """Test Context dataclass."""

    def test_context_defaults(self) -> None:
        """Context has sensible defaults."""
        ctx = config_store.Context(name="test")
        assert ctx.name == "test"
        assert ctx.server_url == "http://localhost:3334"
        assert ctx.org_slug is None
        assert ctx.default_project is None
        assert ctx.insecure is False

    def test_context_to_dict(self) -> None:
        """Context can be converted to dict."""
        ctx = config_store.Context(
            name="prod",
            server_url="https://sibyl.example.com",
            org_slug="myorg",
            default_project="project_abc",
            insecure=True,
        )
        d = ctx.to_dict()
        assert d["server_url"] == "https://sibyl.example.com"
        assert d["org_slug"] == "myorg"
        assert d["default_project"] == "project_abc"
        assert d["insecure"] is True

    def test_context_from_dict(self) -> None:
        """Context can be created from dict."""
        data = {
            "server_url": "https://api.example.com",
            "org_slug": "testorg",
            "default_project": "project_xyz",
            "insecure": False,
        }
        ctx = config_store.Context.from_dict("staging", data)
        assert ctx.name == "staging"
        assert ctx.server_url == "https://api.example.com"
        assert ctx.org_slug == "testorg"
        assert ctx.default_project == "project_xyz"

    def test_context_from_dict_missing_optional(self) -> None:
        """Context handles missing optional fields."""
        data = {"server_url": "http://localhost:3334"}
        ctx = config_store.Context.from_dict("local", data)
        assert ctx.org_slug is None
        assert ctx.default_project is None
        assert ctx.insecure is False


class TestPathMappings:
    """Test path-to-project mappings."""

    def test_resolve_project_no_mappings(self) -> None:
        """Returns None when no mappings exist."""
        with patch.object(config_store, "get_path_mappings", return_value={}):
            result = config_store.resolve_project_from_cwd()
            assert result is None

    def test_get_current_context_no_mappings(self) -> None:
        """Returns None tuple when no mappings exist."""
        with patch.object(config_store, "get_path_mappings", return_value={}):
            project, path = config_store.get_current_context()
            assert project is None
            assert path is None


class TestWorktreeResolution:
    """Test git worktree detection and resolution."""

    def test_resolve_worktree_regular_repo(self, tmp_path: Path) -> None:
        """Regular repo (directory .git) returns None."""
        # Create a regular git repo structure
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        result = config_store._resolve_worktree_main_repo(tmp_path)
        assert result is None

    def test_resolve_worktree_no_git(self, tmp_path: Path) -> None:
        """Directory without .git returns None."""
        result = config_store._resolve_worktree_main_repo(tmp_path)
        assert result is None

    def test_resolve_worktree_detects_worktree(self, tmp_path: Path) -> None:
        """Worktree with .git file resolves to main repo."""
        # Set up main repo structure
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        worktrees_dir = main_git / "worktrees" / "feature-branch"
        worktrees_dir.mkdir(parents=True)

        # Set up worktree
        worktree = tmp_path / "worktree-feature"
        worktree.mkdir()
        git_file = worktree / ".git"
        git_file.write_text(f"gitdir: {worktrees_dir}")

        result = config_store._resolve_worktree_main_repo(worktree)
        assert result == main_repo

    def test_resolve_worktree_nested_path(self, tmp_path: Path) -> None:
        """Worktree resolution works from nested subdirectory."""
        # Set up main repo structure
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        worktrees_dir = main_git / "worktrees" / "feature-branch"
        worktrees_dir.mkdir(parents=True)

        # Set up worktree with nested dir
        worktree = tmp_path / "worktree-feature"
        worktree.mkdir()
        git_file = worktree / ".git"
        git_file.write_text(f"gitdir: {worktrees_dir}")
        nested = worktree / "src" / "deep" / "path"
        nested.mkdir(parents=True)

        result = config_store._resolve_worktree_main_repo(nested)
        assert result == main_repo

    def test_resolve_project_from_worktree(self, tmp_path: Path) -> None:
        """resolve_project_from_cwd uses main repo link when in worktree."""
        # Set up main repo structure
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        worktrees_dir = main_git / "worktrees" / "feature-branch"
        worktrees_dir.mkdir(parents=True)

        # Set up worktree
        worktree = tmp_path / "worktree-feature"
        worktree.mkdir()
        git_file = worktree / ".git"
        git_file.write_text(f"gitdir: {worktrees_dir}")

        # Mock mappings - only main repo is linked
        mappings = {str(main_repo): "project_abc123"}

        with (
            patch.object(config_store, "get_path_mappings", return_value=mappings),
            patch("os.getcwd", return_value=str(worktree)),
        ):
            result = config_store.resolve_project_from_cwd()
            assert result == "project_abc123"

    def test_get_current_context_from_worktree(self, tmp_path: Path) -> None:
        """get_current_context returns main repo path when in worktree."""
        # Set up main repo structure
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        worktrees_dir = main_git / "worktrees" / "feature-branch"
        worktrees_dir.mkdir(parents=True)

        # Set up worktree
        worktree = tmp_path / "worktree-feature"
        worktree.mkdir()
        git_file = worktree / ".git"
        git_file.write_text(f"gitdir: {worktrees_dir}")

        # Mock mappings - only main repo is linked
        mappings = {str(main_repo): "project_abc123"}

        with (
            patch.object(config_store, "get_path_mappings", return_value=mappings),
            patch("os.getcwd", return_value=str(worktree)),
        ):
            project_id, matched_path = config_store.get_current_context()
            assert project_id == "project_abc123"
            assert matched_path == str(main_repo)

    def test_direct_match_preferred_over_worktree(self, tmp_path: Path) -> None:
        """Direct cwd match takes precedence if longer than worktree match."""
        # Set up main repo structure
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        worktrees_dir = main_git / "worktrees" / "feature-branch"
        worktrees_dir.mkdir(parents=True)

        # Set up worktree with specific subpath linked
        worktree = tmp_path / "worktree-feature"
        worktree.mkdir()
        git_file = worktree / ".git"
        git_file.write_text(f"gitdir: {worktrees_dir}")
        subdir = worktree / "specific-subdir"
        subdir.mkdir()

        # Both main repo AND the worktree subdir are linked
        # The more specific (longer) path should win
        mappings = {
            str(main_repo): "project_main",
            str(subdir): "project_subdir",
        }

        with (
            patch.object(config_store, "get_path_mappings", return_value=mappings),
            patch("os.getcwd", return_value=str(subdir)),
        ):
            result = config_store.resolve_project_from_cwd()
            # Direct match to subdir is longer, so it wins
            assert result == "project_subdir"


class TestPathEntryParsing:
    """Test parsing/serialization of [paths] entries (legacy string vs table)."""

    def test_legacy_string_is_project_only(self) -> None:
        assert config_store._path_entry_fields("project_abc") == ("project_abc", None)

    def test_empty_string_is_empty(self) -> None:
        assert config_store._path_entry_fields("") == (None, None)

    def test_table_with_both_fields(self) -> None:
        value = {"project": "project_abc", "context": "work"}
        assert config_store._path_entry_fields(value) == ("project_abc", "work")

    def test_table_context_only(self) -> None:
        assert config_store._path_entry_fields({"context": "work"}) == (None, "work")

    def test_make_entry_project_only_stays_string(self) -> None:
        """Project-only links keep the legacy bare-string form to avoid churn."""
        assert config_store._make_path_entry("project_abc", None) == "project_abc"

    def test_make_entry_with_context_is_table(self) -> None:
        entry = config_store._make_path_entry("project_abc", "work")
        assert entry == {"project": "project_abc", "context": "work"}

    def test_make_entry_context_only(self) -> None:
        assert config_store._make_path_entry(None, "work") == {"context": "work"}


class TestPathLinkStorage:
    """Test round-trip storage of project + context pins in an isolated config."""

    def test_pin_project_and_context(self, tmp_path: Path) -> None:
        with patch.object(config_store, "config_dir", return_value=tmp_path):
            config_store.set_path_mapping("/repo/work", "project_w", context="work")
            norm = str(Path("/repo/work").resolve())
            assert config_store.get_path_mappings()[norm] == "project_w"
            assert config_store.get_path_context_mappings()[norm] == "work"
            assert config_store.get_path_link("/repo/work") == ("project_w", "work")

    def test_legacy_string_entry_is_readable(self, tmp_path: Path) -> None:
        """A bare-string path value (pre-feature config) reads as project-only."""
        norm = str(Path("/repo/legacy").resolve())
        with patch.object(config_store, "config_dir", return_value=tmp_path):
            config_store.save_config({"paths": {norm: "project_legacy"}})
            assert config_store.get_path_mappings()[norm] == "project_legacy"
            assert config_store.get_path_context_mappings() == {}
            assert config_store.get_path_link("/repo/legacy") == ("project_legacy", None)

    def test_set_mapping_preserves_existing_context(self, tmp_path: Path) -> None:
        with patch.object(config_store, "config_dir", return_value=tmp_path):
            config_store.set_path_mapping("/repo/work", "project_w", context="work")
            # Re-pin the project without specifying context (default keeps it).
            config_store.set_path_mapping("/repo/work", "project_w2")
            assert config_store.get_path_link("/repo/work") == ("project_w2", "work")

    def test_set_context_preserves_project(self, tmp_path: Path) -> None:
        with patch.object(config_store, "config_dir", return_value=tmp_path):
            config_store.set_path_mapping("/repo/work", "project_w")
            config_store.set_path_context("/repo/work", "work")
            assert config_store.get_path_link("/repo/work") == ("project_w", "work")

    def test_remove_context_keeps_project(self, tmp_path: Path) -> None:
        with patch.object(config_store, "config_dir", return_value=tmp_path):
            config_store.set_path_mapping("/repo/work", "project_w", context="work")
            assert config_store.remove_path_context("/repo/work") is True
            assert config_store.get_path_link("/repo/work") == ("project_w", None)
            # No context left to remove.
            assert config_store.remove_path_context("/repo/work") is False

    def test_remove_mapping_clears_everything(self, tmp_path: Path) -> None:
        with patch.object(config_store, "config_dir", return_value=tmp_path):
            config_store.set_path_mapping("/repo/work", "project_w", context="work")
            assert config_store.remove_path_mapping("/repo/work") is True
            assert config_store.get_path_link("/repo/work") == (None, None)


class TestContextFromCwd:
    """Test cwd -> context resolution (mirrors project resolution, worktree-aware)."""

    def test_no_mappings(self) -> None:
        with patch.object(config_store, "get_path_context_mappings", return_value={}):
            assert config_store.resolve_context_from_cwd() is None

    def test_direct_match(self, tmp_path: Path) -> None:
        repo = tmp_path / "work-repo"
        repo.mkdir()
        mappings = {str(repo): "work"}
        with (
            patch.object(config_store, "get_path_context_mappings", return_value=mappings),
            patch("os.getcwd", return_value=str(repo)),
        ):
            assert config_store.resolve_context_from_cwd() == "work"

    def test_longest_prefix_wins(self, tmp_path: Path) -> None:
        parent = tmp_path / "work"
        child = parent / "secret-project"
        child.mkdir(parents=True)
        mappings = {str(parent): "work", str(child): "work-secret"}
        with (
            patch.object(config_store, "get_path_context_mappings", return_value=mappings),
            patch("os.getcwd", return_value=str(child)),
        ):
            assert config_store.resolve_context_from_cwd() == "work-secret"

    def test_resolves_from_worktree_main_repo(self, tmp_path: Path) -> None:
        main_repo = tmp_path / "main-repo"
        main_repo.mkdir()
        main_git = main_repo / ".git"
        main_git.mkdir()
        worktrees_dir = main_git / "worktrees" / "feature"
        worktrees_dir.mkdir(parents=True)
        worktree = tmp_path / "worktree-feature"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {worktrees_dir}")

        mappings = {str(main_repo): "work"}
        with (
            patch.object(config_store, "get_path_context_mappings", return_value=mappings),
            patch("os.getcwd", return_value=str(worktree)),
        ):
            assert config_store.resolve_context_from_cwd() == "work"


class TestResolveContextName:
    """Test effective context precedence: override > directory pin > active."""

    def test_override_wins(self) -> None:
        with (
            patch("sibyl_cli.state.get_context_override", return_value="forced"),
            patch.object(config_store, "resolve_context_from_cwd", return_value="pinned"),
            patch.object(config_store, "get_active_context_name", return_value="active"),
        ):
            assert config_store.resolve_context_name() == "forced"

    def test_pin_beats_active(self) -> None:
        with (
            patch("sibyl_cli.state.get_context_override", return_value=None),
            patch.object(config_store, "resolve_context_from_cwd", return_value="pinned"),
            patch.object(config_store, "get_active_context_name", return_value="active"),
        ):
            assert config_store.resolve_context_name() == "pinned"

    def test_falls_back_to_active(self) -> None:
        with (
            patch("sibyl_cli.state.get_context_override", return_value=None),
            patch.object(config_store, "resolve_context_from_cwd", return_value=None),
            patch.object(config_store, "get_active_context_name", return_value="active"),
        ):
            assert config_store.resolve_context_name() == "active"

    def test_none_when_nothing_set(self) -> None:
        with (
            patch("sibyl_cli.state.get_context_override", return_value=None),
            patch.object(config_store, "resolve_context_from_cwd", return_value=None),
            patch.object(config_store, "get_active_context_name", return_value=None),
        ):
            assert config_store.resolve_context_name() is None
