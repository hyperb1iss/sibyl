"""Git worktrees route like their repository.

Worktrees are disposable checkouts of one repository, so a team repository and
all its worktrees must reach the same Sibyl server and project. Two traps broke
that: links made inside a worktree were stored on the worktree and pinned
whatever context was active at the time, and resolution compared raw path
lengths across trees, so a worktree path (always longer than its repository's)
or any long ancestor pin silently won.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from sibyl_cli import config_store
from sibyl_cli.main import app


def _repo_with_worktree(root: Path, *, worktree_parent: Path | None = None) -> tuple[Path, Path]:
    """A main repository and one linked worktree, laid out the way git does."""
    main_repo = root / "repo"
    worktree_git = main_repo / ".git" / "worktrees" / "feature"
    worktree_git.mkdir(parents=True)
    worktree = (worktree_parent or root / "worktrees") / "feature"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {worktree_git}\n")
    return main_repo, worktree


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config_store.Path, "home", lambda: tmp_path / "home")
    (tmp_path / "home").mkdir()
    return tmp_path


def _resolve_context(cwd: Path, mappings: dict[str, str]) -> str | None:
    with (
        patch.object(config_store, "get_path_context_mappings", return_value=mappings),
        patch("os.getcwd", return_value=str(cwd)),
    ):
        return config_store.resolve_context_from_cwd()


def test_a_repository_link_beats_a_longer_ancestor_pin_of_the_worktree(tmp_path: Path) -> None:
    # ~/dev/worktrees/<repo> style parents are longer than ~/dev/<repo>; the
    # repository's own link must still decide where its worktrees route.
    deep_parent = tmp_path / "a" / "much" / "longer" / "worktree" / "parent"
    main_repo, worktree = _repo_with_worktree(tmp_path, worktree_parent=deep_parent)
    mappings = {str(deep_parent): "personal", str(main_repo): "team"}

    assert _resolve_context(worktree, mappings) == "team"


def test_an_ancestor_pin_still_covers_worktrees_of_unlinked_repositories(tmp_path: Path) -> None:
    deep_parent = tmp_path / "worktrees-root"
    _main_repo, worktree = _repo_with_worktree(tmp_path, worktree_parent=deep_parent)

    assert _resolve_context(worktree, {str(deep_parent): "personal"}) == "personal"


def test_a_repository_subdirectory_link_applies_inside_every_worktree(tmp_path: Path) -> None:
    main_repo, worktree = _repo_with_worktree(tmp_path)
    (worktree / "apps" / "web").mkdir(parents=True)
    mappings = {str(main_repo): "team", str(main_repo / "apps" / "web"): "web-team"}

    assert _resolve_context(worktree / "apps" / "web", mappings) == "web-team"
    assert _resolve_context(worktree, mappings) == "team"


def test_an_explicit_worktree_pin_still_wins(tmp_path: Path) -> None:
    main_repo, worktree = _repo_with_worktree(tmp_path)

    assert _resolve_context(worktree, {str(main_repo): "team", str(worktree): "mine"}) == "mine"


def test_a_project_only_worktree_link_inherits_the_repository_context(tmp_path: Path) -> None:
    main_repo, worktree = _repo_with_worktree(tmp_path)
    with (
        patch.object(config_store, "get_path_mappings", return_value={str(worktree): "project_wt"}),
        patch.object(
            config_store, "get_path_context_mappings", return_value={str(main_repo): "team"}
        ),
        patch("os.getcwd", return_value=str(worktree)),
    ):
        assert config_store.resolve_project_from_cwd() == "project_wt"
        assert config_store.resolve_context_from_cwd() == "team"


def test_canonical_link_path_maps_a_worktree_onto_its_repository(tmp_path: Path) -> None:
    main_repo, worktree = _repo_with_worktree(tmp_path)
    (worktree / "pkg").mkdir()

    assert config_store.canonical_link_path(str(worktree / "pkg")) == (
        str((main_repo / "pkg").resolve()),
        str(worktree.resolve()),
    )
    plain = tmp_path / "plain"
    plain.mkdir()
    assert config_store.canonical_link_path(str(plain)) == (str(plain.resolve()), None)


def _flat(text: str) -> str:
    """Rich wraps long paths; compare output with its whitespace collapsed."""
    return " ".join(text.split())


def _paths() -> dict[str, object]:
    return config_store.load_config().get("paths", {})


def test_project_link_inside_a_worktree_links_the_repository(home: Path) -> None:
    main_repo, worktree = _repo_with_worktree(home)
    client = MagicMock()
    client.context_name = "team"
    client.get_entity = AsyncMock(return_value={"id": "project_v2", "name": "V2"})

    with (
        patch("sibyl_cli.project.get_client", return_value=client),
        patch("os.getcwd", return_value=str(worktree)),
    ):
        result = CliRunner().invoke(app, ["project", "link", "project_v2"])

    assert result.exit_code == 0, result.stdout
    paths = _paths()
    assert str(main_repo.resolve()) in paths
    assert str(worktree.resolve()) not in paths
    assert "is a git worktree" in _flat(result.stdout)


def test_this_worktree_pins_only_the_worktree(home: Path) -> None:
    main_repo, worktree = _repo_with_worktree(home)
    client = MagicMock()
    client.context_name = "team"
    client.get_entity = AsyncMock(return_value={"id": "project_v2", "name": "V2"})

    with (
        patch("sibyl_cli.project.get_client", return_value=client),
        patch("os.getcwd", return_value=str(worktree)),
    ):
        result = CliRunner().invoke(app, ["project", "link", "project_v2", "--this-worktree"])

    assert result.exit_code == 0, result.stdout
    paths = _paths()
    assert str(worktree.resolve()) in paths
    assert str(main_repo.resolve()) not in paths


def test_context_link_and_unlink_inside_a_worktree_act_on_the_repository(home: Path) -> None:
    main_repo, worktree = _repo_with_worktree(home)
    config_store.create_context("team", "https://sibyl.team.example")

    with patch("os.getcwd", return_value=str(worktree)):
        linked = CliRunner().invoke(app, ["config", "context", "link", "team"])
        assert linked.exit_code == 0, linked.stdout
        assert config_store.get_path_link(str(main_repo)) == (None, "team")

        unlinked = CliRunner().invoke(app, ["config", "context", "unlink"])
        assert unlinked.exit_code == 0, unlinked.stdout
        assert config_store.get_path_link(str(main_repo)) == (None, None)


def test_prune_plans_then_applies_worktree_and_dead_link_cleanup(home: Path) -> None:
    main_repo, worktree = _repo_with_worktree(home)
    other_repo, other_worktree = _repo_with_worktree(home / "other")
    gone = home / "removed-worktree"
    config_store.set_path_mapping(str(main_repo), "project_v2")
    config_store.set_path_mapping(str(worktree), "project_v2", context="local")
    config_store.set_path_mapping(str(other_worktree), "project_other", context="team")
    gone.mkdir()
    config_store.set_path_mapping(str(gone), "project_old")
    gone.rmdir()
    before = dict(_paths())

    dry = CliRunner().invoke(app, ["project", "links", "--prune"])

    assert dry.exit_code == 0, dry.stdout
    assert _paths() == before
    assert "this worktree pinned project project_v2, context local" in _flat(dry.stdout)
    assert "directory is gone" in _flat(dry.stdout)
    assert "Dry run" in _flat(dry.stdout)

    applied = CliRunner().invoke(app, ["project", "links", "--prune", "--apply"])

    assert applied.exit_code == 0, applied.stdout
    paths = _paths()
    assert str(worktree.resolve()) not in paths
    assert str(gone.resolve()) not in paths
    assert str(other_worktree.resolve()) not in paths
    assert config_store.get_path_link(str(main_repo)) == ("project_v2", None)
    assert config_store.get_path_link(str(other_repo)) == ("project_other", "team")

    tidy = CliRunner().invoke(app, ["project", "links", "--prune"])
    assert "already tidy" in _flat(tidy.stdout)


def test_apply_without_prune_is_refused(home: Path) -> None:
    result = CliRunner().invoke(app, ["project", "links", "--apply"])

    assert result.exit_code == 1
    assert "--apply only applies a --prune plan" in _flat(result.stdout)
