"""Resolve which project a recall command reads from.

Recall used to widen to every accessible project whenever the working
directory had no link, so a command run from the wrong folder quietly
returned other projects' memories. Widening is now an explicit choice, and
every command that reads by project resolves it here so they agree.
"""

from __future__ import annotations

from collections.abc import Callable

import typer

from sibyl_cli.common import NEON_CYAN, err_console, error, info
from sibyl_cli.config_store import Context, get_default_project

ALL_PROJECTS_SCOPE = "all_projects"


def _notice(message: str) -> None:
    # stderr, like the command marker in main.py, so --json and brief output
    # stay machine-readable when a default project stands in for a link.
    err_console.print(f"[{NEON_CYAN}]→[/{NEON_CYAN}] {message}")


def resolve_recall_project(
    project: str | None,
    all_projects: bool,
    *,
    resolve_linked: Callable[[], str | None],
    resolve_context: Callable[[], Context | None],
    refuse: bool = True,
) -> str | None:
    """Pick the project a read scopes to, or None when nothing may be read.

    Order: an explicit --project, then --all, then the directory link, then the
    active context's default project, then the legacy defaults.project setting.
    A default stands in with a notice on stderr, since nothing in this
    directory chose it. With none of those the command refuses and names the
    ways to proceed. Callers that can degrade instead (the session bundle)
    pass refuse=False and get None, which they must treat as "read nothing",
    never as "read everything"; only --all returns None meaning everything.
    """
    if project:
        return project
    if all_projects:
        return None
    linked = resolve_linked()
    if linked:
        return linked
    context = resolve_context()
    default_project = context.default_project if context else None
    if default_project:
        _notice(f"No project is linked here; using the context default {default_project}")
        return default_project
    legacy_default = get_default_project()
    if legacy_default:
        _notice(f"No project is linked here; using the configured default {legacy_default}")
        return legacy_default
    if not refuse:
        return None
    error("No project for this directory, so nothing was read.")
    info("Find a project id with: sibyl project list")
    info("Scope it: --project <id>, or link the directory with: sibyl project link <id>")
    info("Or read every project you can access on purpose: --all")
    raise typer.Exit(1)
