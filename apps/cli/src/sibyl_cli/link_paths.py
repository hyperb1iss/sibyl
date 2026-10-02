"""Choose where a directory link is written, so worktrees follow their repository."""

from __future__ import annotations

import os

from sibyl_cli.common import info
from sibyl_cli.config_store import canonical_link_path, get_path_link


def resolve_link_target(path: str | None, *, this_worktree: bool) -> str:
    """The path a new link for ``path`` (default: cwd) should be stored under.

    Inside a git worktree that is the equivalent path in the main repository,
    unless the caller asked to pin only this worktree.
    """
    requested = path or os.getcwd()
    if this_worktree:
        return requested
    target, worktree_root = canonical_link_path(requested)
    if worktree_root is None:
        return requested
    info(
        f"{worktree_root} is a git worktree, so its repository {target} is linked "
        "and every worktree of it routes the same way (--this-worktree pins only this one)"
    )
    return target


def resolve_unlink_target(path: str | None) -> str:
    """The stored link that governs ``path``: its own entry if it has one, else the repository's."""
    requested = path or os.getcwd()
    if any(get_path_link(requested)):
        return requested
    target, worktree_root = canonical_link_path(requested)
    return target if worktree_root else requested
