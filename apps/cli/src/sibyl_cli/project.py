"""Project management CLI commands.

Commands: list, show, create, update, progress, link, unlink.
All commands communicate with the REST API to ensure proper event broadcasting.
"""

import os
from typing import Annotated

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_panel,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    run_async,
    success,
    truncate,
    warn,
)
from sibyl_cli.config_store import (
    get_context,
    get_current_context,
    get_path_context_mappings,
    get_path_mappings,
    remove_path_mapping,
    set_path_mapping,
)
from sibyl_cli.project_refs import (
    PROJECT_RELINK_HINT,
    list_accessible_projects,
    matching_project_refs,
    project_matches_path_hint,
    resolve_project_reference,
)

app = typer.Typer(
    name="project",
    help="Project management",
    no_args_is_help=True,
)

_handle_client_error = handle_client_error


def _print_pinned_context(context_name: str | None) -> None:
    """Show the context (and server) a directory link now routes to."""
    if not context_name:
        return
    ctx = get_context(context_name)
    server = f" [dim]{ctx.server_url}[/dim]" if ctx else ""
    console.print(f"  → context [{NEON_CYAN}]{context_name}[/{NEON_CYAN}]{server}")


@app.command("list")
def list_projects(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
    csv_out: Annotated[bool, typer.Option("--csv", help="CSV output")] = False,
) -> None:
    """List all projects. Default: table output."""
    format_ = "json" if json_out else ("csv" if csv_out else "table")

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            response = await client.explore(
                mode="list",
                types=["project"],
                limit=limit,
            )
            entities = response.get("entities", [])

            if format_ == "json":
                print_json(entities)
                return

            if format_ == "csv":
                import csv
                import sys

                writer = csv.writer(sys.stdout)
                writer.writerow(["id", "name", "status", "description"])
                for e in entities:
                    meta = e.get("metadata", {})
                    writer.writerow(
                        [
                            e.get("id", ""),
                            e.get("name", ""),
                            meta.get("status", ""),
                            truncate(e.get("description") or "", 100),
                        ]
                    )
                return

            if not entities:
                info("No projects found")
                return

            table = create_table("Projects", "ID", "Name", "Status", "Description")
            for e in entities:
                meta = e.get("metadata", {})
                table.add_row(
                    e.get("id", ""),
                    truncate(e.get("name", ""), 30),
                    meta.get("status", "active"),
                    truncate(e.get("description") or "", 40),
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} project(s)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _list()


@app.command("show")
def show_project(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show project details with task summary. Default: table output."""

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            # Entity endpoint now includes enriched project summary
            entity = await client.get_entity(project_id)
            meta = entity.get("metadata", {})

            if json_out:
                print_json(entity)
                return

            # Use status_counts from enriched metadata (no extra API call needed)
            status_counts: dict[str, int] = meta.get("status_counts", {})
            total = meta.get("total_tasks", 0)
            pct = meta.get("progress_pct", 0.0)

            lines = [
                f"[{ELECTRIC_PURPLE}]Name:[/{ELECTRIC_PURPLE}] {entity.get('name', '')}",
                f"[{ELECTRIC_PURPLE}]Status:[/{ELECTRIC_PURPLE}] {meta.get('status', 'active')}",
                "",
                f"[{NEON_CYAN}]Description:[/{NEON_CYAN}]",
                entity.get("description") or "[dim]No description[/dim]",
                "",
                f"[{NEON_CYAN}]Task Summary:[/{NEON_CYAN}]",
            ]

            # Show status counts in priority order
            status_order = ["doing", "blocked", "review", "todo", "backlog", "done", "archived"]
            for status in status_order:
                count = status_counts.get(status, 0)
                if count > 0:
                    lines.append(f"  {status}: {count}")

            # Progress bar
            if total > 0:
                bar_filled = int(pct / 5)
                bar = f"[{SUCCESS_GREEN}]{'█' * bar_filled}[/{SUCCESS_GREEN}]{'░' * (20 - bar_filled)}"
                lines.append(f"\n[{ELECTRIC_PURPLE}]Progress:[/{ELECTRIC_PURPLE}] {bar} {pct:.0f}%")

            if meta.get("tech_stack"):
                lines.append(
                    f"\n[{NEON_CYAN}]Tech Stack:[/{NEON_CYAN}] {', '.join(meta['tech_stack'])}"
                )

            # Show critical tasks first (high priority / CRITICAL in name)
            critical_tasks = meta.get("critical_tasks", [])
            if critical_tasks:
                lines.append("\n[red]⚠ Critical:[/red]")
                for task in critical_tasks:
                    status = task.get("status", "")
                    lines.append(
                        f"  [red]●[/red] {task.get('name', '')} "
                        f"[dim]{status}[/dim] [{CORAL}]{task.get('id', '')}[/{CORAL}]"
                    )

            # Show epics with progress
            epics = meta.get("epics", [])
            if epics:
                lines.append(f"\n[{ELECTRIC_PURPLE}]Epics:[/{ELECTRIC_PURPLE}]")
                for epic in epics:
                    epic_pct = epic.get("progress_pct", 0)
                    epic_total = epic.get("total_tasks", 0)
                    mini_bar = "█" * int(epic_pct / 10) + "░" * (10 - int(epic_pct / 10))
                    lines.append(
                        f"  [{SUCCESS_GREEN}]{mini_bar}[/{SUCCESS_GREEN}] {epic_pct:.0f}% "
                        f"{epic.get('name', '')} ({epic_total} tasks)"
                    )

            # Show actionable tasks (doing, blocked, review) - now returned as "related"
            actionable = entity.get("related", [])
            if actionable:
                lines.append(f"\n[{NEON_CYAN}]Actionable:[/{NEON_CYAN}]")
                for task in actionable:
                    status = task.get("relationship", "")  # Status is in relationship field
                    status_color = {
                        "doing": SUCCESS_GREEN,
                        "blocked": "red",
                        "review": ELECTRIC_YELLOW,
                    }.get(status, CORAL)
                    lines.append(
                        f"  [{status_color}]{status}[/{status_color}] "
                        f"{task.get('name', '')} [{CORAL}]{task.get('id', '')}[/{CORAL}]"
                    )

            panel = create_panel("\n".join(lines), title=f"Project {entity.get('id', '')}")
            console.print(panel)

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("create")
def create_project(
    name: Annotated[str, typer.Option("--name", "-n", help="Project name (required)")],
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="Project description")
    ] = None,
    repo: Annotated[str | None, typer.Option("--repo", "-r", help="Repository URL")] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Create a new project. Default: table output."""

    @run_async
    async def _create() -> None:
        client = get_client()

        try:
            metadata = {}
            if repo:
                metadata["repository_url"] = repo

            response = await client.create_entity(
                name=name,
                content=description or f"Project: {name}",
                entity_type="project",
                metadata=metadata if metadata else None,
            )

            # JSON output (default)
            if json_out:
                print_json(response)
                return

            # Table output
            if response.get("id"):
                success(f"Project created: {response['id']}")
            else:
                error("Failed to create project")

        except SibylClientError as e:
            _handle_client_error(e)

    _create()


@app.command("progress")
def project_progress(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show project progress with visual breakdown. Default: table output."""

    @run_async
    async def _progress() -> None:
        client = get_client()

        try:
            response = await client.explore(
                mode="list",
                types=["task"],
                project=project_id,
                limit=200,
            )
            tasks = response.get("entities", [])

            status_counts: dict[str, int] = {}
            for t in tasks:
                status = t.get("metadata", {}).get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            total = len(tasks)
            done = status_counts.get("done", 0)
            pct = (done / total) * 100 if total > 0 else 0

            # JSON output (default)
            if json_out:
                output = {
                    "project_id": project_id,
                    "total_tasks": total,
                    "completed": done,
                    "progress_percent": round(pct, 1),
                    "by_status": status_counts,
                }
                print_json(output)
                return

            # Table output
            if not tasks:
                info("No tasks found for this project")
                return

            console.print(f"\n[{ELECTRIC_PURPLE}]Project Progress[/{ELECTRIC_PURPLE}]\n")

            # Progress bar
            bar_width = 40
            filled = int((pct / 100) * bar_width)
            bar = f"[{SUCCESS_GREEN}]{'█' * filled}[/{SUCCESS_GREEN}]{'░' * (bar_width - filled)}"
            console.print(f"  {bar} {pct:.1f}% ({done}/{total})")

            # Status breakdown
            console.print(f"\n[{NEON_CYAN}]Status Breakdown:[/{NEON_CYAN}]")
            order = ["backlog", "todo", "doing", "blocked", "review", "done", "archived"]
            for status in order:
                count = status_counts.get(status, 0)
                if count > 0:
                    status_bar = "█" * min(count, 30)
                    console.print(f"  {status:10} [{NEON_CYAN}]{status_bar}[/{NEON_CYAN}] {count}")

        except SibylClientError as e:
            _handle_client_error(e)

    _progress()


@app.command("link")
def link_project(
    project_id: Annotated[str, typer.Argument(help="Project ID to link (required)")],
    path: Annotated[
        str | None, typer.Option("--path", "-p", help="Directory path (defaults to cwd)")
    ] = None,
) -> None:
    """Link current directory to a project for automatic context.

    Once linked, task commands in this directory will auto-scope to the project.

    Examples:
        sibyl project link project_abc123     # Link cwd to specific project
        sibyl project link project_abc --path ~/dev/myproject
    """
    target_path = path or os.getcwd()

    @run_async
    async def _link() -> None:
        client = get_client()
        context_name = client.context_name

        # Verify project exists
        try:
            project = await client.get_entity(project_id)
            project_name = project.get("name", "Unknown")
        except SibylClientError:
            error(f"Project not found: {project_id}. {PROJECT_RELINK_HINT}.")
            return

        # Pin the project together with the context it lives on, so this directory
        # always routes to the right server regardless of the active context.
        assert project_id is not None
        set_path_mapping(target_path, project_id, context=context_name)

        success(f"Linked [{NEON_CYAN}]{target_path}[/{NEON_CYAN}]")
        console.print(f"  → [{ELECTRIC_PURPLE}]{project_name}[/{ELECTRIC_PURPLE}] ({project_id})")
        _print_pinned_context(context_name)
        info("Commands in this directory now route to this project and server")

    _link()


@app.command("relink")
def relink_project(
    project_id: Annotated[
        str | None,
        typer.Option("--id", help="Project ID, UUID, name, or slug to relink to"),
    ] = None,
    path: Annotated[
        str | None, typer.Option("--path", "-p", help="Directory path (defaults to cwd)")
    ] = None,
) -> None:
    """Repair the project link for the current directory."""
    target_path = path or os.getcwd()

    @run_async
    async def _relink() -> None:
        client = get_client()
        context_name = client.context_name
        try:
            projects = await list_accessible_projects(client)
            selected_id = None
            selected_name = None

            if project_id:
                selected_id = await resolve_project_reference(client, project_id)
                matches = matching_project_refs(projects, selected_id)
                if not matches:
                    raise ValueError(f"Project not found: {project_id}. {PROJECT_RELINK_HINT}.")
                selected_name = str(matches[0].get("name") or selected_id)
            else:
                matches = [
                    project
                    for project in projects
                    if project_matches_path_hint(project, target_path)
                ]
                if len(matches) == 1:
                    selected_id = str(matches[0]["id"])
                    selected_name = str(matches[0].get("name") or selected_id)
                elif not matches:
                    warn("No accessible project matched this directory.")
                else:
                    warn("Multiple accessible projects matched this directory.")

                if selected_id is None:
                    if projects:
                        console.print("  Candidates:")
                        for project in projects[:10]:
                            console.print(f"  - {project.get('id')}  {project.get('name', '')}")
                    info("Run: sibyl project relink --id <project>")
                    return

            set_path_mapping(target_path, selected_id, context=context_name)
            success(f"Relinked [{NEON_CYAN}]{target_path}[/{NEON_CYAN}]")
            console.print(
                f"  → [{ELECTRIC_PURPLE}]{selected_name}[/{ELECTRIC_PURPLE}] ({selected_id})"
            )
            _print_pinned_context(context_name)
        except (SibylClientError, ValueError) as exc:
            error(str(exc))
            info(PROJECT_RELINK_HINT)

    _relink()


@app.command("unlink")
def unlink_project(
    path: Annotated[
        str | None, typer.Option("--path", "-p", help="Directory path (defaults to cwd)")
    ] = None,
) -> None:
    """Remove project link from a directory.

    Examples:
        sibyl project unlink              # Unlink cwd
        sibyl project unlink --path ~/dev/myproject
    """
    target_path = path or os.getcwd()

    if remove_path_mapping(target_path):
        success(f"Unlinked [{NEON_CYAN}]{target_path}[/{NEON_CYAN}]")
    else:
        info(f"No link found for {target_path}")


@app.command("links")
def list_links() -> None:
    """List all directory links (project and/or context pins)."""
    project_map = get_path_mappings()
    context_map = get_path_context_mappings()

    if not project_map and not context_map:
        info("No directory links configured")
        info("Use 'sibyl project link' to pin a directory to a project and server")
        return

    # Get current context to highlight it
    _current_project, current_path = get_current_context()

    console.print(f"\n[{ELECTRIC_PURPLE}]Directory Links:[/{ELECTRIC_PURPLE}]\n")

    for mapped_path in sorted(project_map.keys() | context_map.keys()):
        is_current = mapped_path == current_path
        marker = f"[{SUCCESS_GREEN}]* [/{SUCCESS_GREEN}]" if is_current else "  "
        console.print(f"{marker}[{NEON_CYAN}]{mapped_path}[/{NEON_CYAN}]")
        if project_id := project_map.get(mapped_path):
            console.print(f"    → [{CORAL}]{project_id}[/{CORAL}]")
        if context_name := context_map.get(mapped_path):
            ctx = get_context(context_name)
            server = f" [dim]{ctx.server_url}[/dim]" if ctx else ""
            console.print(f"    → context [{NEON_CYAN}]{context_name}[/{NEON_CYAN}]{server}")

    if current_path:
        console.print("\n[dim]* = current directory[/dim]")
