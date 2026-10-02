"""Landing a gated release on a branch that moved while the gates ran.

Every case runs real git against a bare "origin" repository, so the atomic
push, the merge and the refusals are what git actually does. The fixture's
pin check stands in for ``sync_versions.py --check``: every version-shaped
line of the chart must carry the current VERSION.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from tools.release import land_release as land_module
from tools.release.land_release import LandError, Landing, land_release, main

GIT = shutil.which("git") or "git"
GUARDS = ("tools/release/", ".github/workflows/publish.yml")
TAG = "v1.2.4"
CHART = "charts/sibyl/Chart.yaml"
CHART_AT = "name: sibyl\ndescription: memory\nkeywords: [memory]\nversion: {version}\n"
PIN_CHECK = """\
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
version = (root / "VERSION").read_text().strip()
chart = (root / "charts/sibyl/Chart.yaml").read_text().splitlines()
drift = [line for line in chart if re.search(r"\\d+\\.\\d+\\.\\d+", line) and version not in line]
if drift:
    print(f"pin drift: {drift}")
    sys.exit(1)
"""

World = dict[str, Path | str]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        [GIT, *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _clone(origin: Path, path: Path) -> Path:
    subprocess.run([GIT, "clone", "-q", str(origin), str(path)], check=True)  # noqa: S603
    _git(path, "config", "user.name", "Release Bot")
    _git(path, "config", "user.email", "bot@example.invalid")
    return path


@pytest.fixture
def world(tmp_path: Path) -> World:
    """An origin whose main is at the validated base, a runner clone holding
    the tagged version-bump commit, and a second clone that merges other work."""
    origin = tmp_path / "origin.git"
    subprocess.run([GIT, "init", "-q", "--bare", "-b", "main", str(origin)], check=True)  # noqa: S603
    other = _clone(origin, tmp_path / "other")
    base = _commit(
        other,
        {
            "VERSION": "1.2.3\n",
            CHART: CHART_AT.format(version="1.2.3"),
            "tools/release/sync_versions.py": PIN_CHECK,
            ".github/workflows/publish.yml": "name: Publish\n",
            "app.py": "print('base')\n",
        },
        "base",
    )
    _git(other, "push", "-q", "origin", "main")

    runner = _clone(origin, tmp_path / "runner")
    _commit(
        runner,
        {"VERSION": "1.2.4\n", CHART: CHART_AT.format(version="1.2.4")},
        "chore(release): cut 1.2.4",
    )
    _git(runner, "tag", "-a", TAG, "-m", f"Release {TAG}")
    return {"origin": origin, "runner": runner, "other": other, "base": base}


def _land(
    world: World,
    before_push: Callable[[int], None] | None = None,
    attempts: int = 5,
) -> Landing:
    return land_release(
        Path(world["runner"]),
        remote="origin",
        branch="main",
        tag=TAG,
        base_sha=str(world["base"]),
        guards=GUARDS,
        attempts=attempts,
        before_push=before_push,
        log=lambda _message: None,
    )


def _release(world: World) -> str:
    return _git(Path(world["runner"]), "rev-parse", "HEAD")


def _origin(world: World, ref: str) -> str:
    return _git(Path(world["origin"]), "rev-parse", f"{ref}^{{commit}}")


def _origin_show(world: World, ref: str, path: str) -> str:
    return _git(Path(world["origin"]), "show", f"{ref}:{path}")


def _origin_has_tag(world: World) -> bool:
    result = subprocess.run(  # noqa: S603
        [GIT, "rev-parse", "--verify", "--quiet", f"refs/tags/{TAG}"],
        cwd=Path(world["origin"]),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _parents(world: World, commit: str) -> list[str]:
    return _git(Path(world["origin"]), "rev-list", "--parents", "-n", "1", commit).split()[1:]


def _merge_other_work(world: World, files: dict[str, str] | None = None) -> str:
    other = Path(world["other"])
    _git(other, "pull", "-q", "--ff-only", "origin", "main")
    sha = _commit(other, files or {"docs.md": "landed meanwhile\n"}, "feat: other work")
    _git(other, "push", "-q", "origin", "main")
    return sha


def test_unmoved_branch_fast_forwards_to_the_tagged_commit(world: World) -> None:
    landing = _land(world)

    assert landing.mode == "fast-forward"
    assert _origin(world, "main") == _release(world)
    assert _origin(world, TAG) == _release(world)


def test_moved_branch_gains_a_merge_and_the_tag_names_the_gated_commit(world: World) -> None:
    moved = _merge_other_work(world)

    landing = _land(world)

    assert landing.mode == "merge"
    head = _origin(world, "main")
    assert landing.branch_sha == head
    assert _parents(world, head) == [moved, _release(world)]
    assert _origin(world, TAG) == _release(world)
    assert _origin_show(world, head, "docs.md") == "landed meanwhile"
    assert _origin_show(world, head, "VERSION") == "1.2.4"
    changed = _git(Path(world["origin"]), "diff", "--name-only", moved, head).split()
    assert sorted(changed) == sorted(["VERSION", CHART])


def test_moved_commits_may_edit_a_pin_file_away_from_its_pins(world: World) -> None:
    # A dependency bump edits the files that also carry the version pins.
    moved = _merge_other_work(
        world,
        {CHART: CHART_AT.format(version="1.2.3").replace("memory\n", "durable memory\n", 1)},
    )

    landing = _land(world)

    assert landing.mode == "merge"
    chart = _origin_show(world, "main", CHART)
    assert "description: durable memory" in chart
    assert "version: 1.2.4" in chart
    assert _parents(world, _origin(world, "main")) == [moved, _release(world)]


def test_refuses_when_the_moved_commits_edit_a_pin_line(world: World) -> None:
    moved = _merge_other_work(
        world, {CHART: CHART_AT.format(version="1.2.3").replace("1.2.3", "1.2.3-hold")}
    )

    with pytest.raises(LandError, match="does not merge cleanly"):
        _land(world)

    assert _origin(world, "main") == moved
    assert not _origin_has_tag(world)


def test_refuses_when_the_moved_commits_add_a_pin_the_bump_never_wrote(world: World) -> None:
    moved = _merge_other_work(
        world, {CHART: "appVersion: 1.2.3\n" + CHART_AT.format(version="1.2.3")}
    )

    with pytest.raises(LandError, match="pin check"):
        _land(world)

    assert _origin(world, "main") == moved
    assert not _origin_has_tag(world)


def test_refuses_when_the_merge_would_not_carry_the_bumps_edits(world: World) -> None:
    # Someone bumped the pins on main by hand: the merge is clean and passes
    # the pin check, but main would not gain the bump the gates proved.
    moved = _merge_other_work(
        world, {"VERSION": "1.2.4\n", CHART: CHART_AT.format(version="1.2.4")}
    )

    with pytest.raises(LandError, match="other lines than the version bump"):
        _land(world)

    assert _origin(world, "main") == moved
    assert not _origin_has_tag(world)


@pytest.mark.parametrize(
    "path", ["tools/release/sync_versions.py", ".github/workflows/publish.yml"]
)
def test_refuses_when_the_moved_commits_touch_a_guarded_path(world: World, path: str) -> None:
    moved = _merge_other_work(world, {path: "# changed meanwhile\n"})

    with pytest.raises(LandError, match=f"touch the release tooling.*{re.escape(path)}"):
        _land(world)

    assert _origin(world, "main") == moved
    assert not _origin_has_tag(world)


def test_a_rename_out_of_a_guarded_path_is_still_refused(world: World) -> None:
    # Rename detection would list only the new, unguarded name.
    other = Path(world["other"])
    _git(other, "pull", "-q", "--ff-only", "origin", "main")
    _git(other, "mv", ".github/workflows/publish.yml", ".github/workflows/ship.yml")
    _git(other, "commit", "-q", "-m", "rename the publish workflow")
    _git(other, "push", "-q", "origin", "main")

    with pytest.raises(LandError, match=r"touch the release tooling.*publish\.yml"):
        _land(world)

    assert not _origin_has_tag(world)


def test_refuses_a_branch_rewound_behind_the_base(world: World) -> None:
    other = Path(world["other"])
    _commit(other, {"older.md": "older\n"}, "older")
    _git(other, "push", "-q", "origin", "main")
    world["base"] = _git(other, "rev-parse", "HEAD")
    runner = Path(world["runner"])
    _git(runner, "fetch", "-q", "origin")
    _git(runner, "reset", "-q", "--hard", "origin/main")
    _commit(runner, {"VERSION": "1.2.4\n", CHART: CHART_AT.format(version="1.2.4")}, "cut")
    _git(runner, "tag", "-f", "-a", TAG, "-m", f"Release {TAG}")
    _git(other, "push", "-q", "--force", "origin", "HEAD~1:refs/heads/main")

    with pytest.raises(LandError, match="no longer contains the validated base"):
        _land(world)

    assert not _origin_has_tag(world)


def test_refuses_a_branch_whose_history_was_rewritten(world: World) -> None:
    other = Path(world["other"])
    _git(other, "commit", "-q", "--amend", "-m", "rewritten base")
    _git(other, "push", "-q", "--force", "origin", "main")

    with pytest.raises(LandError, match="no longer contains the validated base"):
        _land(world)

    assert not _origin_has_tag(world)


def test_a_push_that_loses_a_race_merges_again(world: World) -> None:
    first = _merge_other_work(world, {"first.md": "first\n"})
    raced: list[str] = []

    def move_once(attempt: int) -> None:
        if attempt == 1:
            raced.append(_merge_other_work(world, {"second.md": "second\n"}))

    landing = _land(world, before_push=move_once)

    assert landing.attempts == 1 + len(raced)
    head = _origin(world, "main")
    assert _parents(world, head) == [raced[0], _release(world)]
    assert first in _git(Path(world["origin"]), "rev-list", head).split()
    assert _origin(world, TAG) == _release(world)


def test_a_branch_that_keeps_moving_gives_up_without_pushing(world: World) -> None:
    attempts: list[int] = []

    def move_every_time(attempt: int) -> None:
        attempts.append(attempt)
        _merge_other_work(world, {f"race-{attempt}.md": "again\n"})

    with pytest.raises(LandError, match="kept moving"):
        _land(world, before_push=move_every_time, attempts=3)

    assert attempts == [1, 2, 3]
    assert not _origin_has_tag(world)


def test_a_rejection_without_a_race_fails_at_once(world: World) -> None:
    hook = Path(world["origin"]) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'pushes are frozen' >&2\nexit 1\n")
    hook.chmod(0o755)
    attempts: list[int] = []

    with pytest.raises(LandError, match=r"was rejected.*pushes are frozen"):
        _land(world, before_push=attempts.append)

    assert attempts == [1]
    assert _origin(world, "main") == str(world["base"])
    assert not _origin_has_tag(world)


@pytest.mark.parametrize("moved_first", [False, True], ids=["fast-forward", "merge"])
def test_an_occupied_remote_tag_fails_at_once_and_moves_nothing(
    world: World, moved_first: bool
) -> None:
    # The atomic push must leave main alone when the tag ref is refused.
    expected_main = _merge_other_work(world) if moved_first else str(world["base"])
    other = Path(world["other"])
    _git(other, "tag", TAG)
    _git(other, "push", "-q", "origin", TAG)
    occupied = _origin(world, TAG)
    attempts: list[int] = []

    with pytest.raises(LandError, match=f"{re.escape(TAG)} already exists on origin"):
        _land(world, before_push=attempts.append)

    assert attempts == [1]
    assert _origin(world, "main") == expected_main
    assert _origin(world, TAG) == occupied


def _lose_first_push_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first push really lands, but reports failure, as a dropped connection does."""
    real = land_module._git
    pushes: list[int] = []

    def flaky(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = real(repo, *args, check=check)
        if args[:1] == ("push",):
            pushes.append(result.returncode)
            if len(pushes) == 1:
                return subprocess.CompletedProcess(result.args, 128, "", "remote hung up")
        return result

    monkeypatch.setattr(land_module, "_git", flaky)


@pytest.mark.parametrize("bump", [True, False], ids=["merge", "tag-only"])
def test_a_push_whose_acknowledgement_was_lost_counts_as_landed(
    world: World, monkeypatch: pytest.MonkeyPatch, bump: bool
) -> None:
    if not bump:
        runner = Path(world["runner"])
        _git(runner, "reset", "-q", "--hard", str(world["base"]))
        _git(runner, "tag", "-f", "-a", TAG, "-m", f"Release {TAG}")
    _merge_other_work(world)
    _lose_first_push_ack(monkeypatch)

    landing = _land(world)

    assert landing.mode == ("merge" if bump else "tag-only")
    assert landing.attempts == 1
    assert _origin(world, "main") == landing.branch_sha
    assert _origin(world, TAG) == _release(world)


def test_a_landed_tag_on_a_rewound_branch_is_not_a_landing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Our tag object reached the remote, but main was forced back behind the
    # release before the re-fetch, so main does not carry the release.
    moved = _merge_other_work(world)
    real = land_module._git

    def push_then_rewind(
        repo: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = real(repo, *args, check=check)
        if args[:1] == ("push",) and result.returncode == 0:
            _git(
                Path(world["other"]), "push", "-q", "--force", "origin", f"{moved}:refs/heads/main"
            )
            return subprocess.CompletedProcess(result.args, 128, "", "remote hung up")
        return result

    monkeypatch.setattr(land_module, "_git", push_then_rewind)

    with pytest.raises(LandError, match="already exists"):
        _land(world)

    assert _origin(world, "main") == moved


def test_someone_elses_tag_on_the_same_commit_is_not_our_landing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A tag-only cut whose push fails while a same-named lightweight tag on
    # the base appears: the peeled commits match, the tag objects do not.
    runner = Path(world["runner"])
    _git(runner, "reset", "-q", "--hard", str(world["base"]))
    _git(runner, "tag", "-f", "-a", TAG, "-m", f"Release {TAG}")
    _merge_other_work(world)
    other = Path(world["other"])

    def squat(_attempt: int) -> None:
        _git(other, "tag", TAG, str(world["base"]))
        _git(other, "push", "-q", "origin", TAG)

    with pytest.raises(LandError, match=f"{re.escape(TAG)} already exists on origin"):
        _land(world, before_push=squat)

    assert _git(Path(world["origin"]), "cat-file", "-t", f"refs/tags/{TAG}") == "commit"


def test_a_release_without_a_version_commit_pushes_only_the_tag(world: World) -> None:
    runner = Path(world["runner"])
    _git(runner, "reset", "-q", "--hard", str(world["base"]))
    _git(runner, "tag", "-f", "-a", TAG, "-m", f"Release {TAG}")
    moved = _merge_other_work(world)

    landing = _land(world)

    assert landing.mode == "tag-only"
    assert _origin(world, "main") == moved
    assert _origin(world, TAG) == str(world["base"])


def test_refuses_a_tag_that_is_not_the_base_or_its_child(world: World) -> None:
    runner = Path(world["runner"])
    _commit(runner, {"extra.md": "extra\n"}, "an unvalidated commit")
    _git(runner, "tag", "-f", "-a", TAG, "-m", f"Release {TAG}")

    with pytest.raises(LandError, match="neither the validated base"):
        _land(world)

    assert not _origin_has_tag(world)


def test_cli_lands_and_records_the_outcome(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
            "--guard",
            "tools/release/",
            "--guard",
            ".github/workflows/publish.yml",
            "--github-output",
            str(output),
        ]
    )

    assert code == 0
    recorded = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert recorded == {"mode": "merge", "branch_sha": _origin(world, "main")}


def test_cli_requires_a_guard() -> None:
    with pytest.raises(SystemExit):
        main(["--branch", "main", "--tag", TAG, "--base-sha", "0" * 40])
