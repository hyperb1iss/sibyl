"""Land a gated release commit on a branch that may have moved.

The Release workflow proves its gates on one commit and tags exactly that
commit. The gates take most of an hour, and other work can merge to the
branch meanwhile, so pushing the release commit as the branch head would be
rejected and the whole cut lost. Instead, when the branch has moved, the
release commit is merged into the branch's new head. The branch update and
the tag are pushed atomically, so either both land or neither does, and a
push that loses a race to another merge fetches and merges again.

The tag always names the gated commit, never the merge. Before anything is
pushed, the merge has to show it is the moved branch plus the version bump
and nothing else: its line edits on top of the moved branch must be exactly
the bump's line edits on top of the validated base, and the merged tree must
pass the pin check (``sync_versions.py --check``), so a pin the moved commits
added is caught too. Commits that merged meanwhile may touch the pin files
freely, as dependency bumps do. They may not touch a guarded path: the
release tooling, which decides what a pin is, or a workflow the release
still runs from the branch head rather than the tag. Then the release is
dispatched again on the new head.

Like ``ci_evidence``, this module is stdlib only.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

# Pushes that keep losing races to other merges stop here. Each lost race
# means another merge landed in the seconds between fetch and push, so five
# in a row points at something other than ordinary traffic.
DEFAULT_ATTEMPTS = 5
DEFAULT_PIN_CHECK = "tools/release/sync_versions.py"


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
    result = _git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    return result.returncode == 0


def _guarded(path: str, guards: Sequence[str]) -> bool:
    return any(
        path == guard or (guard.endswith("/") and path.startswith(guard)) for guard in guards
    )


def _changed_paths(repo: Path, old: str, new: str) -> list[str]:
    # NUL-separated and without rename detection, so a renamed file lists both
    # names and an unusual name is never quoted or split.
    out = _git(repo, "diff", "--name-only", "--no-renames", "-z", old, new).stdout
    return [path for path in out.split("\0") if path]


def _line_edits(repo: Path, old: str, new: str) -> str:
    """The removed and added lines per file, without context or positions."""
    diff = _git(repo, "diff", "-U0", "--no-renames", "--no-color", "--no-ext-diff", old, new)
    kept = (line for line in diff.stdout.splitlines() if line.startswith(("diff --git ", "-", "+")))
    return "\n".join(kept)


def _remote_tag(repo: Path, remote: str, tag: str) -> str | None:
    """The commit ``tag`` names on the remote, or None when it is absent."""
    out = _git(repo, "ls-remote", remote, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}").stdout
    refs: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        refs[ref] = sha
    # An annotated tag lists its own object and, peeled, the commit it names.
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def _fetch_branch(repo: Path, remote: str, branch: str) -> str:
    tracking = f"refs/remotes/{remote}/{branch}"
    _git(repo, "fetch", "--no-tags", remote, f"+refs/heads/{branch}:{tracking}")
    return _rev(repo, tracking)


def _check_pins(repo: Path, commit: str, pin_check: str) -> None:
    with tempfile.TemporaryDirectory(prefix="land-release-") as scratch:
        tree = Path(scratch) / "tree"
        _git(repo, "worktree", "add", "--detach", "--quiet", str(tree), commit)
        try:
            result = subprocess.run(  # noqa: S603
                [sys.executable, str(tree / pin_check), "--check"],
                cwd=tree,
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            _git(repo, "worktree", "remove", "--force", str(tree), check=False)
            _git(repo, "worktree", "prune", check=False)
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        raise LandError(
            "the release merged into the moved branch fails the pin check, so a commit that "
            f"merged meanwhile changed what the version bump has to write: {detail} "
            "Dispatch the release again on the new head."
        )


def _merge_commit(
    repo: Path,
    *,
    base: str,
    moved: str,
    release: str,
    tag: str,
    branch: str,
    guards: Sequence[str],
    pin_check: str,
) -> str:
    touched = sorted(path for path in _changed_paths(repo, base, moved) if _guarded(path, guards))
    if touched:
        raise LandError(
            "commits that merged during the release touch the release tooling or a workflow "
            f"the release runs from {branch}, so the merge would ship an untested "
            f"combination: {', '.join(touched)}. Dispatch the release again on the new head."
        )
    tree = _git(repo, "merge-tree", "--write-tree", moved, release, check=False)
    if tree.returncode != 0:
        raise LandError(f"the release does not merge cleanly into {branch}: {tree.stdout.strip()}")
    message = (
        f"chore(release): merge {tag} into {branch}\n\n"
        f"The release gates proved {release}, which {tag} names. Other work\n"
        f"merged to {branch} while they ran, so the version pins land here.\n"
    )
    merge = _git(
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
    if _line_edits(repo, moved, merge) != _line_edits(repo, base, release):
        raise LandError(
            f"merging the release into {branch} edits other lines than the version bump did, "
            "so a commit that merged meanwhile overlaps the pins. Dispatch the release again "
            "on the new head."
        )
    _check_pins(repo, merge, pin_check)
    return merge


def land_release(
    repo: Path,
    *,
    remote: str,
    branch: str,
    tag: str,
    base_sha: str,
    guards: Sequence[str],
    pin_check: str = DEFAULT_PIN_CHECK,
    attempts: int = DEFAULT_ATTEMPTS,
    before_push: Callable[[int], None] | None = None,
    log: Callable[[str], None] = _log,
) -> Landing:
    """Push ``tag`` and land its commit on ``branch``, merging when the branch moved.

    ``base_sha`` is the commit the gates validated; the tagged release commit
    is that commit or a version-pin commit on top of it. ``pin_check`` is the
    repository script run with ``--check`` on a merged tree. ``before_push``
    runs after each fetch, which lets tests move the branch mid-attempt.
    """
    release = _rev(repo, tag)
    if release != base_sha and _git(repo, "rev-parse", f"{release}^").stdout.strip() != base_sha:
        raise LandError(f"{tag} names {release}, which is neither the validated base nor its child")

    for attempt in range(1, attempts + 1):
        moved = _fetch_branch(repo, remote, branch)
        if not _is_ancestor(repo, base_sha, moved):
            raise LandError(
                f"{branch} no longer contains the validated base {base_sha} (it was rewound "
                "or rewritten); dispatch the release again on the new head."
            )
        if _is_ancestor(repo, release, moved):
            mode, target = "tag-only", moved
        elif moved == base_sha:
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
                pin_check=pin_check,
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

        now = _fetch_branch(repo, remote, branch)
        remote_tag = _remote_tag(repo, remote, tag)
        if remote_tag == release and _is_ancestor(repo, target, now):
            # The push landed and only its acknowledgement was lost.
            log(f"Landed {tag} on {branch} by {mode} at {target} (attempt {attempt}).")
            return Landing(mode=mode, branch_sha=target, attempts=attempt)
        if remote_tag is not None:
            raise LandError(f"{tag} already exists on {remote} at {remote_tag}, not {release}")
        # A push rejected while the branch stayed put was not a lost race, so
        # fetching again would only repeat it.
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
        "--guard",
        action="append",
        required=True,
        help="a path no commit that merged meanwhile may touch; a trailing slash guards a directory",
    )
    parser.add_argument(
        "--pin-check",
        default=DEFAULT_PIN_CHECK,
        help="script run with --check on a merged tree (default: %(default)s)",
    )
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)

    try:
        landing = land_release(
            Path.cwd(),
            remote=args.remote,
            branch=args.branch,
            tag=args.tag,
            base_sha=args.base_sha,
            guards=args.guard,
            pin_check=args.pin_check,
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
