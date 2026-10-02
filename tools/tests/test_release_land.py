"""Landing a gated release on a branch that moved while the gates ran.

Every case runs real git against a bare "origin" repository, so the atomic
push, the merge and the refusals are what git actually does.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from tools.release.land_release import LandError, Landing, land_release, main

GIT = shutil.which("git") or "git"
GUARDS = ("VERSION", "charts/sibyl/Chart.yaml", "tools/release/")
TAG = "v1.2.4"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        [GIT, *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _clone(origin: Path, path: Path) -> Path:
    subprocess.run(  # noqa: S603
        [GIT, "clone", "-q", str(origin), str(path)], check=True
    )
    _git(path, "config", "user.name", "Release Bot")
    _git(path, "config", "user.email", "bot@example.invalid")
    return path


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path | str]:
    """An origin whose main is at the validated base, a runner clone holding
    the tagged release commit, and a second clone that merges other work."""
    origin = tmp_path / "origin.git"
    subprocess.run(  # noqa: S603
        [GIT, "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    seed = _clone(origin, tmp_path / "seed")
    _commit(seed, "VERSION", "1.2.3\n", "seed")
    _commit(seed, "charts/sibyl/Chart.yaml", "version: 1.2.3\n", "chart")
    base = _commit(seed, "app.py", "print('base')\n", "base")
    _git(seed, "push", "-q", "origin", "main")

    runner = _clone(origin, tmp_path / "runner")
    _commit(runner, "VERSION", "1.2.4\n", "chore(release): cut 1.2.4")
    _git(runner, "tag", "-a", TAG, "-m", f"Release {TAG}")
    return {"origin": origin, "runner": runner, "other": seed, "base": base}


def _land(
    world: dict[str, Path | str], before_push: Callable[[int], None] | None = None
) -> Landing:
    return land_release(
        Path(world["runner"]),
        remote="origin",
        branch="main",
        tag=TAG,
        base_sha=str(world["base"]),
        guards=GUARDS,
        before_push=before_push,
        log=lambda _message: None,
    )


def _origin(world: dict[str, Path | str], ref: str) -> str:
    return _git(Path(world["origin"]), "rev-parse", f"{ref}^{{commit}}")


def _origin_has(world: dict[str, Path | str], ref: str) -> bool:
    result = subprocess.run(  # noqa: S603
        [GIT, "rev-parse", "--verify", "--quiet", ref],
        cwd=Path(world["origin"]),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _merge_other_work(world: dict[str, Path | str], path: str = "docs.md") -> str:
    other = Path(world["other"])
    _git(other, "pull", "-q", "--ff-only", "origin", "main")
    sha = _commit(other, path, f"{path} landed meanwhile\n", f"feat: {path}")
    _git(other, "push", "-q", "origin", "main")
    return sha


def test_unmoved_branch_fast_forwards_to_the_tagged_commit(world: dict[str, Path | str]) -> None:
    release = _git(Path(world["runner"]), "rev-parse", "HEAD")

    landing = _land(world)

    assert landing.mode == "fast-forward"
    assert _origin(world, "main") == release
    assert _origin(world, TAG) == release


def test_moved_branch_gains_a_merge_and_the_tag_names_the_gated_commit(
    world: dict[str, Path | str],
) -> None:
    release = _git(Path(world["runner"]), "rev-parse", "HEAD")
    moved = _merge_other_work(world)

    landing = _land(world)

    assert landing.mode == "merge"
    head = _origin(world, "main")
    parents = _git(Path(world["origin"]), "rev-list", "--parents", "-n", "1", head).split()[1:]
    assert parents == [moved, release]
    assert _origin(world, TAG) == release
    # The merged tree is the moved branch plus the version pin.
    origin = Path(world["origin"])
    assert _git(origin, "show", f"{head}:VERSION") == "1.2.4"
    assert _git(origin, "show", f"{head}:docs.md") == "docs.md landed meanwhile"
    assert _git(origin, "diff", "--name-only", moved, head) == "VERSION"


@pytest.mark.parametrize("path", ["charts/sibyl/Chart.yaml", "tools/release/sync_versions.py"])
def test_refuses_when_the_moved_commits_touch_what_the_release_writes_or_runs(
    world: dict[str, Path | str], path: str
) -> None:
    moved = _merge_other_work(world, path)

    with pytest.raises(LandError, match=path):
        _land(world)

    assert _origin(world, "main") == moved
    assert not _origin_has(world, f"refs/tags/{TAG}")


def test_refuses_when_the_branch_no_longer_contains_the_base(world: dict[str, Path | str]) -> None:
    other = Path(world["other"])
    _git(other, "reset", "-q", "--hard", "HEAD~1")
    _commit(other, "rewritten.md", "rewritten\n", "rewritten history")
    _git(other, "push", "-q", "--force", "origin", "main")

    with pytest.raises(LandError, match="history was rewritten"):
        _land(world)

    assert not _origin_has(world, f"refs/tags/{TAG}")


def test_a_push_that_loses_a_race_merges_again(world: dict[str, Path | str]) -> None:
    release = _git(Path(world["runner"]), "rev-parse", "HEAD")
    first = _merge_other_work(world, "first.md")
    raced: list[str] = []

    def move_once(attempt: int) -> None:
        if attempt == 1:
            raced.append(_merge_other_work(world, "second.md"))

    landing = _land(world, before_push=move_once)

    assert landing.attempts == 1 + len(raced)
    head = _origin(world, "main")
    parents = _git(Path(world["origin"]), "rev-list", "--parents", "-n", "1", head).split()[1:]
    assert parents == [raced[0], release]
    assert first in _git(Path(world["origin"]), "rev-list", head).split()
    assert _origin(world, TAG) == release


def test_a_rejected_push_leaves_neither_branch_nor_tag_moved(world: dict[str, Path | str]) -> None:
    # A tag already on origin rejects the tag ref; the atomic push must then
    # leave main alone too, and a rejection without a race is not retried.
    other = Path(world["other"])
    _git(other, "tag", TAG)
    _git(other, "push", "-q", "origin", TAG)
    occupied = _origin(world, TAG)
    attempts: list[int] = []

    with pytest.raises(LandError, match="rejected"):
        _land(world, before_push=attempts.append)

    assert attempts == [1]
    assert _origin(world, "main") == str(world["base"])
    assert _origin(world, TAG) == occupied


def test_a_release_without_a_version_commit_pushes_only_the_tag(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(  # noqa: S603
        [GIT, "init", "-q", "--bare", "-b", "main", str(origin)], check=True
    )
    seed = _clone(origin, tmp_path / "seed")
    base = _commit(seed, "VERSION", "1.2.4\n", "hand-bumped")
    _git(seed, "push", "-q", "origin", "main")
    runner = _clone(origin, tmp_path / "runner")
    _git(runner, "tag", "-a", TAG, "-m", f"Release {TAG}")
    moved = _commit(seed, "docs.md", "later\n", "later")
    _git(seed, "push", "-q", "origin", "main")

    landing = land_release(
        runner,
        remote="origin",
        branch="main",
        tag=TAG,
        base_sha=base,
        guards=GUARDS,
        log=lambda _message: None,
    )

    assert landing.mode == "tag-only"
    assert _git(origin, "rev-parse", "main") == moved
    assert _git(origin, "rev-parse", f"{TAG}^{{commit}}") == base


def test_refuses_a_tag_that_is_not_the_base_or_its_child(world: dict[str, Path | str]) -> None:
    runner = Path(world["runner"])
    _commit(runner, "extra.md", "extra\n", "an unvalidated commit")
    _git(runner, "tag", "-f", "-a", TAG, "-m", f"Release {TAG}")

    with pytest.raises(LandError, match="neither the validated base"):
        _land(world)

    assert not _origin_has(world, f"refs/tags/{TAG}")


def test_cli_lands_and_records_the_outcome(
    world: dict[str, Path | str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard_file = tmp_path / "guards.txt"
    guard_file.write_text("VERSION\ncharts/sibyl/Chart.yaml\n")
    output = tmp_path / "output"
    _merge_other_work(world)
    monkeypatch.chdir(Path(world["runner"]))

    code = main(
        [
            "--branch",
            "main",
            "--tag",
            TAG,
            "--base-sha",
            str(world["base"]),
            "--guard-file",
            str(guard_file),
            "--guard",
            "tools/release/",
            "--github-output",
            str(output),
        ]
    )

    assert code == 0
    recorded = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert recorded["mode"] == "merge"
    assert recorded["branch_sha"] == _origin(world, "main")


def test_cli_refuses_an_empty_guard_file(tmp_path: Path) -> None:
    guard_file = tmp_path / "guards.txt"
    guard_file.write_text("\n")

    with pytest.raises(SystemExit):
        main(
            [
                "--branch",
                "main",
                "--tag",
                TAG,
                "--base-sha",
                "0" * 40,
                "--guard-file",
                str(guard_file),
            ]
        )
