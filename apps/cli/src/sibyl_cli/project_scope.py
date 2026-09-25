"""Resolve which project a recall command reads from.

Recall used to widen to every accessible project whenever the working
directory had no link, so a command run from the wrong folder quietly
returned other projects' memories. Widening is now an explicit choice.
"""

from __future__ import annotations

from collections.abc import Callable

import typer

from sibyl_cli.common import error, info
from sibyl_cli.config_store import Context

ALL_PROJECTS_SCOPE = "all_projects"


def resolve_recall_project(
    project: str | None,
    all_projects: bool,
    *,
    resolve_linked: Callable[[], str | None],
    resolve_context: Callable[[], Context | None],
) -> str | None:
    """Pick the project a read scopes to, or None only when --all was passed.

    Order: an explicit --project, then --all, then the directory link, then the
    active context's default project (with a notice, since nothing in this
    directory chose it). With none of those the command refuses and names the
    three ways to proceed, instead of reading across every project.
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
        info(f"No project is linked here; using the context default {default_project}")
        return default_project
    error("No project for this directory, so nothing was read.")
    info("Scope it: --project <id>, or link the directory with: sibyl project link <id>")
    info("Or read every project you can access on purpose: --all")
    raise typer.Exit(1)
