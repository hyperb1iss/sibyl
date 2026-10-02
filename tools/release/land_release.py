"""Land a gated release commit on a branch that may have moved.

The Release workflow proves its gates on one commit and tags exactly that
commit. The gates take most of an hour, and other work can merge to the
branch meanwhile, so pushing the release commit as the branch head would be
rejected and the whole cut lost. Instead, when the branch has moved, the
release commit is merged into the branch's new head. The branch update and
the tag are pushed atomically, so either both land or neither does, and a
push that loses a race to another merge fetches and merges again.

The tag always names the gated commit, never the merge. The merge is refused,
before anything is pushed, when the commits that landed in the meantime touch
a file the version bump writes or the release tooling itself: then the merged
tree would be more than the moved branch plus the version pins, and the
release has to be dispatched again on the new head.

Like ``ci_evidence``, this module is stdlib only.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Pushes that keep losing races to other merges stop here. Each lost race
# means another merge landed in the seconds between fetch and push, so five
# in a row points at something other than ordinary traffic.
DEFAULT_ATTEMPTS = 5


class LandError(Exception):
    """The release could not be landed; nothing was pushed by the failing attempt."""


@dataclass(frozen=True)
class Landing:
    """How the branch received the release."""

    # "fast-forward": the branch had not moved and now points at the release.
    # "merge": the branch had moved and gained a merge of the release.
    # "tag-only": the branch already contains the release (no version commit).
    mode: str
    branch_sha: str
    attempts: int


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        raise LandError("git is not installed")
    result = subprocess.run(  # noqa: S603
        [git, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise LandError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _log(message: str) -> None:
    sys.stdout.write(f"{message}\n")
    sys.stdout.flush()


def _rev(repo: Path, ref: str) -> str:
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        _git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode == 0
    )


def _guarded(path: str, guards: Sequence[str]) -> bool:
    return any(
        path == guard or (guard.endswith("/") and path.startswith(guard)) for guard in guards
    )


def _fetch_branch(repo: Path, remote: str, branch: str) -> str:
    tracking = f"refs/remotes/{remote}/{branch}"
    _git(repo, "fetch", "--no-tags", remote, f"+refs/heads/{branch}:{tracking}")
    return _rev(repo, tracking)


def _merge_commit(
    repo: Path, *, base: str, moved: str, release: str, tag: str, branch: str, guards: Sequence[str]
) -> str:
    if not _is_ancestor(repo, base, moved):
        raise LandError(
            f"{branch} no longer contains the validated base {base} (its history was rewritten); "
            "dispatch the release again on the new head."
        )
    landed = _git(repo, "diff", "--name-only", f"{base}..{moved}").stdout.split()
    touched = sorted(path for path in landed if _guarded(path, guards))
    if touched:
        raise LandError(
            "commits that merged during the release touch files the release writes or "
            f"runs, so merging the release into {branch} would ship an untested combination: "
            f"{', '.join(touched)}. Dispatch the release again on the new head."
        )
    tree = _git(repo, "merge-tree", "--write-tree", moved, release, check=False)
    if tree.returncode != 0:
        raise LandError(f"the release does not merge cleanly into {branch}: {tree.stdout.strip()}")
    message = (
        f"chore(release): merge {tag} into {branch}\n\n"
        f"The release gates proved {release}, which {tag} names. Other work\n"
        f"merged to {branch} while they ran, so the version pins land here.\n"
    )
    return _git(
        repo,
        "commit-tree",
        tree.stdout.split()[0],
        "-p",
        moved,
        "-p",
        release,
        "-m",
        message,
    ).stdout.strip()


def land_release(
    repo: Path,
    *,
    remote: str,
    branch: str,
    tag: str,
    base_sha: str,
    guards: Sequence[str],
    attempts: int = DEFAULT_ATTEMPTS,
    before_push: Callable[[int], None] | None = None,
    log: Callable[[str], None] = _log,
) -> Landing:
    """Push ``tag`` and land its commit on ``branch``, merging when the branch moved.

    ``base_sha`` is the commit the gates validated; the tagged release commit
    is that commit or a version-pin commit on top of it. ``before_push`` runs
    after each fetch, which lets tests move the branch mid-attempt.
    """
    release = _rev(repo, tag)
    if release != base_sha and _git(repo, "rev-parse", f"{release}^").stdout.strip() != base_sha:
        raise LandError(f"{tag} names {release}, which is neither the validated base nor its child")

    for attempt in range(1, attempts + 1):
        moved = _fetch_branch(repo, remote, branch)
        if _is_ancestor(repo, release, moved):
            mode, target = "tag-only", moved
        elif _is_ancestor(repo, moved, release):
            mode, target = "fast-forward", release
        else:
            mode = "merge"
            target = _merge_commit(
                repo,
                base=base_sha,
                moved=moved,
                release=release,
                tag=tag,
                branch=branch,
                guards=guards,
            )
        if before_push is not None:
            before_push(attempt)

        refspecs = [f"refs/tags/{tag}:refs/tags/{tag}"]
        if mode != "tag-only":
            refspecs.insert(0, f"{target}:refs/heads/{branch}")
        pushed = _git(repo, "push", "--atomic", remote, *refspecs, check=False)
        if pushed.returncode == 0:
            log(f"Landed {tag} on {branch} by {mode} at {target} (attempt {attempt}).")
            return Landing(mode=mode, branch_sha=target, attempts=attempt)

        # A push rejected while the branch stayed put was not a lost race, so
        # fetching again would only repeat it.
        now = _fetch_branch(repo, remote, branch)
        if now == moved:
            raise LandError(f"push of {tag} to {branch} was rejected: {pushed.stderr.strip()}")
        log(f"::warning::{branch} moved to {now} during the push; merging the release again.")

    raise LandError(f"{branch} kept moving; gave up landing {tag} after {attempts} attempts")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Land a gated release on a branch that may have moved."
    )
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--branch", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--base-sha", required=True, help="the commit the gates validated")
    parser.add_argument(
        "--guard-file",
        type=Path,
        required=True,
        help="paths the version bump writes, one per line (sync_versions.py --list-targets)",
    )
    parser.add_argument(
        "--guard",
        action="append",
        default=[],
        help="another guarded path; a trailing slash guards a directory",
    )
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)

    guards = [line.strip() for line in args.guard_file.read_text().splitlines() if line.strip()]
    if not guards:
        parser.error(f"{args.guard_file} lists no guarded paths")
    try:
        landing = land_release(
            Path.cwd(),
            remote=args.remote,
            branch=args.branch,
            tag=args.tag,
            base_sha=args.base_sha,
            guards=[*guards, *args.guard],
            attempts=args.attempts,
        )
    except LandError as exc:
        _log(f"::error::{exc}")
        return 1
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"mode={landing.mode}\nbranch_sha={landing.branch_sha}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
