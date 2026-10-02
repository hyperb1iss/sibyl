"""Choose where a directory link is written, so worktrees follow their repository."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sibyl_cli.common import info
from sibyl_cli.config_store import canonical_link_path, get_path_link

LinkField = Literal["project", "context"]


@dataclass(frozen=True)
class LinkTarget:
    """Where a new link is stored, and any worktree pin that would shadow it."""

    path: str
    # The requested path's own entry inside a worktree. Left in place it would
    # keep overriding the repository link just written for this directory.
    shadowing_pin: str | None = None


def resolve_link_target(path: str | None, *, this_worktree: bool) -> LinkTarget:
    """The path a new link for ``path`` (default: cwd) should be stored under.

    Inside a git worktree that is the equivalent path in the main repository,
    unless the caller asked to pin only this worktree.
    """
    requested = path or os.getcwd()
    if this_worktree:
        return LinkTarget(requested)
    target, worktree_root = canonical_link_path(requested)
    if worktree_root is None:
        return LinkTarget(requested)
    info(
        f"{worktree_root} is a git worktree, so the link is stored at {target} in its "
        "repository and applies to every worktree of it (--this-worktree pins only this one)"
    )
    own = str(Path(requested).expanduser().resolve())
    return LinkTarget(target, own if any(get_path_link(own)) else None)


def resolve_unlink_target(path: str | None, field: LinkField) -> str:
    """The stored link whose ``field`` governs ``path``.

    That is the path's own entry when it sets the field, else, inside a
    worktree, the equivalent path in its repository.
    """
    requested = path or os.getcwd()
    project, context = get_path_link(requested)
    if (project if field == "project" else context) is not None:
        return requested
    target, worktree_root = canonical_link_path(requested)
    if worktree_root is None:
        return requested
    info(f"{worktree_root} is a git worktree; this removes the link on {target} in its repository")
    return target
