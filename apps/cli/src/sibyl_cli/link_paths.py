"""Choose where a directory link is written, so worktrees follow their repository."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sibyl_cli.common import info
from sibyl_cli.config_store import (
    canonical_link_path,
    get_path_context_mappings,
    get_path_link,
    get_path_mappings,
)

LinkField = Literal["project", "context"]


@dataclass(frozen=True)
class LinkTarget:
    """Where a new link is stored, and any worktree pin that would shadow it."""

    path: str
    # The requested path's own entry inside a worktree. Left in place it would
    # keep overriding the repository link just written for this directory.
    shadowing_pin: str | None = None


def _worktree_pin_above(
    own: Path, worktree_root: Path, fields: tuple[LinkField, ...]
) -> str | None:
    """The nearest pin inside the worktree, above ``own``, that sets one of ``fields``."""
    maps = []
    if "project" in fields:
        maps.append(get_path_mappings())
    if "context" in fields:
        maps.append(get_path_context_mappings())
    best: Path | None = None
    for mapping in maps:
        for mapped in mapping:
            pinned = Path(mapped)
            if (
                pinned != own
                and own.is_relative_to(pinned)
                and pinned.is_relative_to(worktree_root)
                and (best is None or len(pinned.parts) > len(best.parts))
            ):
                best = pinned
    return str(best) if best else None


def resolve_link_target(
    path: str | None, *, this_worktree: bool, fields: tuple[LinkField, ...]
) -> LinkTarget:
    """The path a new link for ``path`` (default: cwd) should be stored under.

    Inside a git worktree that is the equivalent path in the main repository,
    unless the caller asked to pin only this worktree, or a pin higher up in
    the same worktree would outrank a repository link for the ``fields``
    being written; then the link goes on the requested path so it applies.
    """
    requested = path or os.getcwd()
    if this_worktree:
        return LinkTarget(requested)
    target, worktree_root = canonical_link_path(requested)
    if worktree_root is None:
        return LinkTarget(requested)
    own_path = Path(requested).expanduser().resolve()
    outranking = _worktree_pin_above(own_path, Path(worktree_root), fields)
    if outranking:
        info(
            f"{outranking} pins this worktree and outranks repository links, so the link is "
            f"stored on {own_path} itself (sibyl project links --prune reviews worktree pins)"
        )
        return LinkTarget(str(own_path))
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
