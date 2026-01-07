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
    info,
    print_json,
    run_async,
    success,
    truncate,
)
from sibyl_cli.config_store import (
    get_current_context,
    get_path_mappings,
    remove_path_mapping,
    set_path_mapping,
)

app = typer.Typer(
    name="project",
    help="Project management",
    no_args_is_help=True,
)


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1."""
    if "Cannot connect" in str(e):
        error(str(e))
    elif e.status_code == 404:
        error(f"Not found: {e.detail}")
    elif e.status_code == 400:
        error(f"Invalid request: {e.detail}")
    else:
        error(str(e))
    raise typer.Exit(1)


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

        # Verify project exists
        try:
            project = await client.get_entity(project_id)
            project_name = project.get("name", "Unknown")
        except SibylClientError:
            error(f"Project not found: {project_id}")
            return

        # Set the mapping (project_id is guaranteed non-None after verification above)
        assert project_id is not None
        set_path_mapping(target_path, project_id)

        success(f"Linked [{NEON_CYAN}]{target_path}[/{NEON_CYAN}]")
        console.print(f"  → [{ELECTRIC_PURPLE}]{project_name}[/{ELECTRIC_PURPLE}] ({project_id})")
        info("Task commands in this directory will now auto-scope to this project")

    _link()


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
    """List all directory-to-project links."""
    mappings = get_path_mappings()

    if not mappings:
        info("No project links configured")
        info("Use 'sibyl project link' to link a directory to a project")
        return

    # Get current context to highlight it
    _current_project, current_path = get_current_context()

    console.print(f"\n[{ELECTRIC_PURPLE}]Project Links:[/{ELECTRIC_PURPLE}]\n")

    for mapped_path, project_id in sorted(mappings.items()):
        is_current = mapped_path == current_path
        marker = f"[{SUCCESS_GREEN}]* [/{SUCCESS_GREEN}]" if is_current else "  "
        console.print(f"{marker}[{NEON_CYAN}]{mapped_path}[/{NEON_CYAN}]")
        console.print(f"    → [{CORAL}]{project_id}[/{CORAL}]")

    if current_path:
        console.print("\n[dim]* = current context[/dim]")
