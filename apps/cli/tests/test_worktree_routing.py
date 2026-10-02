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


def _resolved_link(cwd: Path) -> tuple[str | None, str | None]:
    with patch("os.getcwd", return_value=str(cwd)):
        return config_store.resolve_project_from_cwd(), config_store.resolve_context_from_cwd()


def test_prune_drops_dead_and_redundant_links_and_keeps_differing_pins(home: Path) -> None:
    main_repo, worktree = _repo_with_worktree(home)
    other_repo, other_worktree = _repo_with_worktree(home / "other")
    _third_repo, redundant_worktree = _repo_with_worktree(home / "third")
    gone = home / "removed-worktree"
    config_store.set_path_mapping(str(main_repo), "project_v2")
    config_store.set_path_mapping(str(worktree), "project_v2", context="local")
    config_store.set_path_mapping(str(other_worktree), "project_other", context="team")
    config_store.set_path_mapping(str(_third_repo), "project_third", context="team")
    config_store.set_path_mapping(str(redundant_worktree), "project_third")
    gone.mkdir()
    config_store.set_path_mapping(str(gone), "project_old")
    gone.rmdir()
    before = dict(_paths())
    routes_before = {cwd: _resolved_link(cwd) for cwd in (worktree, other_worktree, main_repo)}

    dry = CliRunner().invoke(app, ["project", "links", "--prune"])

    assert dry.exit_code == 0, dry.stdout
    assert _paths() == before
    flat = _flat(dry.stdout)
    assert "its checkout is gone" in flat
    assert "unlike" in flat and "sibyl project unlink --path" in flat
    assert "already provides project project_third, context team" in flat
    assert "writes these 3 changes" in flat

    applied = CliRunner().invoke(app, ["project", "links", "--prune", "--apply"])

    assert applied.exit_code == 0, applied.stdout
    paths = _paths()
    assert str(gone.resolve()) not in paths
    assert str(redundant_worktree.resolve()) not in paths
    # The differing pin stays, so that worktree still routes where it did.
    assert str(worktree.resolve()) in paths
    assert config_store.get_path_link(str(other_repo)) == ("project_other", "team")
    assert {cwd: _resolved_link(cwd) for cwd in (worktree, main_repo)} == {
        cwd: routes_before[cwd] for cwd in (worktree, main_repo)
    }
    assert _resolved_link(other_worktree) == routes_before[other_worktree]

    again = CliRunner().invoke(app, ["project", "links", "--prune"])
    assert "already tidy" in _flat(again.stdout)


def test_prune_keeps_a_link_on_a_branch_only_directory(home: Path) -> None:
    # project link inside a worktree stores this at the repository's
    # equivalent path, which does not exist in the main checkout.
    main_repo, worktree = _repo_with_worktree(home)
    (worktree / "apps" / "newthing").mkdir(parents=True)
    config_store.set_path_mapping(
        str(main_repo / "apps" / "newthing"), "project_new", context="team"
    )

    result = CliRunner().invoke(app, ["project", "links", "--prune", "--apply"])

    assert result.exit_code == 0, result.stdout
    assert config_store.get_path_link(str(main_repo / "apps" / "newthing")) == (
        "project_new",
        "team",
    )
    assert _resolved_link(worktree / "apps" / "newthing") == ("project_new", "team")


def test_prune_lifts_an_unlinked_repository_s_unanimous_worktree_link(home: Path) -> None:
    main_repo, worktree = _repo_with_worktree(home)
    config_store.set_path_mapping(str(worktree), "project_v2", context="team")

    result = CliRunner().invoke(app, ["project", "links", "--prune", "--apply"])

    assert result.exit_code == 0, result.stdout
    assert config_store.get_path_link(str(main_repo)) == ("project_v2", "team")
    assert config_store.get_path_link(str(worktree)) == (None, None)


def test_prune_refuses_a_plan_made_stale_by_another_writer(home: Path) -> None:
    _main_repo, worktree = _repo_with_worktree(home)
    config_store.set_path_mapping(str(worktree), "project_v2", context="team")
    plan = config_store.plan_link_cleanup()
    config_store.set_path_mapping(str(home / "elsewhere"), "project_new")

    with pytest.raises(config_store.LinkCleanupConflictError):
        config_store.apply_link_cleanup(plan)
    assert config_store.get_path_link(str(worktree)) == ("project_v2", "team")


def test_relative_and_submodule_gitdirs_resolve_against_their_own_directory(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main_repo = home / "repo"
    worktree_git = main_repo / ".git" / "worktrees" / "feature"
    worktree_git.mkdir(parents=True)
    (worktree_git / "commondir").write_text("../..\n")
    worktree = home / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text("gitdir: ../../repo/.git/worktrees/feature\n")
    submodule = worktree / "libs" / "sub"
    submodule.mkdir(parents=True)
    (worktree_git / "modules" / "libs" / "sub").mkdir(parents=True)
    (submodule / ".git").write_text(
        "gitdir: ../../../../repo/.git/worktrees/feature/modules/libs/sub\n"
    )
    # Resolution must not depend on where the CLI happens to run.
    monkeypatch.chdir(home / "worktrees")

    assert config_store.canonical_link_path(str(worktree)) == (
        str(main_repo.resolve()),
        str(worktree.resolve()),
    )
    assert config_store.canonical_link_path(str(submodule)) == (
        str((main_repo / "libs" / "sub").resolve()),
        str(worktree.resolve()),
    )


def test_a_real_git_worktree_routes_like_its_repository(home: Path) -> None:
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    main_repo = home / "real"
    main_repo.mkdir()
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "HOME": str(home)}

    def git(*argv: str) -> None:
        subprocess.run(
            ["git", "-C", str(main_repo), *argv], check=True, capture_output=True, env=env
        )

    git("init", "-q", "-b", "main")
    git(
        "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"
    )
    worktree = home / "worktrees" / "real" / "feature"
    git("worktree", "add", "-q", "-b", "feature", str(worktree))
    config_store.set_path_mapping(str(main_repo), "project_real", context="team")

    assert _resolved_link(worktree) == ("project_real", "team")
    assert config_store.canonical_link_path(str(worktree))[0] == str(main_repo.resolve())


def test_linking_from_a_pinned_worktree_retires_the_shadowing_pin(home: Path) -> None:
    main_repo, worktree = _repo_with_worktree(home)
    config_store.create_context("personal", "https://sibyl.personal.example")
    config_store.create_context("team", "https://sibyl.team.example")
    config_store.set_path_mapping(str(worktree), "project_mine", context="personal")
    client = MagicMock()
    client.context_name = "team"
    client.get_entity = AsyncMock(return_value={"id": "project_v2", "name": "V2"})

    with (
        patch("sibyl_cli.project.get_client", return_value=client),
        patch("os.getcwd", return_value=str(worktree)),
    ):
        result = CliRunner().invoke(app, ["project", "link", "project_v2"])

    assert result.exit_code == 0, result.stdout
    assert config_store.get_path_link(str(main_repo)) == ("project_v2", "team")
    assert config_store.get_path_link(str(worktree)) == (None, None)
    assert _resolved_link(worktree) == ("project_v2", "team")


def test_unlink_removes_the_field_that_actually_governs_the_directory(home: Path) -> None:
    main_repo, worktree = _repo_with_worktree(home)
    config_store.create_context("team", "https://sibyl.team.example")
    config_store.set_path_mapping(str(main_repo), "project_v2", context="team")
    config_store.set_path_mapping(str(worktree), "project_mine")

    with patch("os.getcwd", return_value=str(worktree)):
        unpinned = CliRunner().invoke(app, ["config", "context", "unlink"])

    assert unpinned.exit_code == 0, unpinned.stdout
    # The worktree's own entry has no context, so the repository's pin was the one to remove.
    assert config_store.get_path_link(str(main_repo)) == ("project_v2", None)
    assert config_store.get_path_link(str(worktree)) == ("project_mine", None)
    assert "removes the link on" in _flat(unpinned.stdout)


def test_apply_without_prune_is_refused(home: Path) -> None:
    result = CliRunner().invoke(app, ["project", "links", "--apply"])

    assert result.exit_code == 1
    assert "--apply only applies a --prune plan" in _flat(result.stdout)
