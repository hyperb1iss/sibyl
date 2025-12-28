"""Project management CLI commands.

Commands: list, show, create, update, progress, link, unlink.
All commands communicate with the REST API to ensure proper event broadcasting.
"""

import os
from typing import Annotated

import typer

from sibyl.cli.client import SibylClientError, get_client
from sibyl.cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_panel,
    create_table,
    error,
    info,
    print_json,
    run_async,
    spinner,
    success,
    truncate,
)
from sibyl.cli.config_store import (
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
        info("Start the server with: sibyl serve")
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
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
    csv_out: Annotated[bool, typer.Option("--csv", help="CSV output")] = False,
) -> None:
    """List all projects. Default: JSON output."""
    format_ = "table" if table_out else ("csv" if csv_out else "json")

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            if format_ in ("json", "csv"):
                response = await client.explore(
                    mode="list",
                    types=["project"],
                    limit=limit,
                )
            else:
                with spinner("Loading projects...") as progress:
                    progress.add_task("Loading projects...", total=None)
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
                    e.get("id", "")[:8] + "...",
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
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show project details with task summary. Default: JSON output."""

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Loading project...") as progress:
                    progress.add_task("Loading project...", total=None)
                    entity = await client.get_entity(project_id)
                    tasks_response = await client.explore(
                        mode="list",
                        types=["task"],
                        project=project_id,
                        limit=500,
                    )
            else:
                entity = await client.get_entity(project_id)
                tasks_response = await client.explore(
                    mode="list",
                    types=["task"],
                    project=project_id,
                    limit=500,
                )

            # JSON output (default)
            if not table_out:
                # Include task summary in output
                tasks = tasks_response.get("entities", [])
                status_counts: dict[str, int] = {}
                for t in tasks:
                    status = t.get("metadata", {}).get("status", "unknown")
                    status_counts[status] = status_counts.get(status, 0) + 1

                output = {
                    **entity,
                    "task_summary": {
                        "total": len(tasks),
                        "by_status": status_counts,
                    },
                }
                print_json(output)
                return

            # Table output
            tasks = tasks_response.get("entities", [])
            status_counts: dict[str, int] = {}
            for t in tasks:
                status = t.get("metadata", {}).get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            meta = entity.get("metadata", {})
            lines = [
                f"[{ELECTRIC_PURPLE}]Name:[/{ELECTRIC_PURPLE}] {entity.get('name', '')}",
                f"[{ELECTRIC_PURPLE}]Status:[/{ELECTRIC_PURPLE}] {meta.get('status', 'active')}",
                "",
                f"[{NEON_CYAN}]Description:[/{NEON_CYAN}]",
                entity.get("description") or "[dim]No description[/dim]",
                "",
                f"[{NEON_CYAN}]Task Summary:[/{NEON_CYAN}]",
            ]

            for status, count in sorted(status_counts.items()):
                lines.append(f"  {status}: {count}")

            total = len(tasks)
            done = status_counts.get("done", 0)
            if total > 0:
                pct = (done / total) * 100
                bar_filled = int(pct / 5)
                bar = f"[{SUCCESS_GREEN}]{'█' * bar_filled}[/{SUCCESS_GREEN}]{'░' * (20 - bar_filled)}"
                lines.append(f"\n[{ELECTRIC_PURPLE}]Progress:[/{ELECTRIC_PURPLE}] {bar} {pct:.0f}%")

            if meta.get("tech_stack"):
                lines.append(
                    f"\n[{NEON_CYAN}]Tech Stack:[/{NEON_CYAN}] {', '.join(meta['tech_stack'])}"
                )

            panel = create_panel("\n".join(lines), title=f"Project {entity.get('id', '')[:8]}")
            console.print(panel)

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("create")
def create_project(
    name: Annotated[str, typer.Option("--name", "-n", help="Project name", prompt=True)],
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="Project description")
    ] = None,
    repo: Annotated[str | None, typer.Option("--repo", "-r", help="Repository URL")] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Create a new project. Default: JSON output."""

    @run_async
    async def _create() -> None:
        client = get_client()

        try:
            metadata = {}
            if repo:
                metadata["repository_url"] = repo

            if table_out:
                with spinner("Creating project...") as progress:
                    progress.add_task("Creating project...", total=None)
                    response = await client.create_entity(
                        name=name,
                        content=description or f"Project: {name}",
                        entity_type="project",
                        metadata=metadata if metadata else None,
                    )
            else:
                response = await client.create_entity(
                    name=name,
                    content=description or f"Project: {name}",
                    entity_type="project",
                    metadata=metadata if metadata else None,
                )

            # JSON output (default)
            if not table_out:
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
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show project progress with visual breakdown. Default: JSON output."""

    @run_async
    async def _progress() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Loading progress...") as progress:
                    progress.add_task("Loading progress...", total=None)
                    response = await client.explore(
                        mode="list",
                        types=["task"],
                        project=project_id,
                        limit=500,
                    )
            else:
                response = await client.explore(
                    mode="list",
                    types=["task"],
                    project=project_id,
                    limit=500,
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
            if not table_out:
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
    project_id: Annotated[
        str | None, typer.Argument(help="Project ID to link (interactive if omitted)")
    ] = None,
    path: Annotated[
        str | None, typer.Option("--path", "-p", help="Directory path (defaults to cwd)")
    ] = None,
) -> None:
    """Link current directory to a project for automatic context.

    Once linked, task commands in this directory will auto-scope to the project.

    Examples:
        sibyl project link                    # Interactive - pick from list
        sibyl project link project_abc123     # Link cwd to specific project
        sibyl project link project_abc --path ~/dev/myproject
    """
    target_path = path or os.getcwd()

    @run_async
    async def _link() -> None:
        nonlocal project_id

        client = get_client()

        # If no project ID, show interactive picker
        if not project_id:
            try:
                with spinner("Loading projects...") as progress:
                    progress.add_task("Loading projects...", total=None)
                    response = await client.explore(
                        mode="list",
                        types=["project"],
                        limit=50,
                    )

                entities = response.get("entities", [])
                if not entities:
                    error("No projects found. Create one first with: sibyl project create")
                    return

                # Show projects and prompt for selection
                console.print(f"\n[{ELECTRIC_PURPLE}]Available Projects:[/{ELECTRIC_PURPLE}]\n")
                for i, e in enumerate(entities, 1):
                    console.print(f"  [{NEON_CYAN}]{i}[/{NEON_CYAN}] {e.get('name', 'Unnamed')}")
                    console.print(f"      [{CORAL}]{e.get('id', '')[:16]}...[/{CORAL}]")

                console.print()
                choice = typer.prompt("Select project number", type=int)

                if choice < 1 or choice > len(entities):
                    error(f"Invalid choice: {choice}")
                    return

                project_id = entities[choice - 1].get("id")

            except SibylClientError as e:
                _handle_client_error(e)
                return

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
        console.print(
            f"  → [{ELECTRIC_PURPLE}]{project_name}[/{ELECTRIC_PURPLE}] ({project_id[:16]}...)"
        )
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
