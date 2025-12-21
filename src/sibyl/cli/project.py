"""Project management CLI commands.

Commands: list, show, create, update, progress.
"""

from typing import Annotated

import typer

from sibyl.cli.common import (
    ELECTRIC_PURPLE,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_panel,
    create_table,
    error,
    info,
    print_db_hint,
    run_async,
    spinner,
    success,
    truncate,
)

app = typer.Typer(
    name="project",
    help="Project management",
    no_args_is_help=True,
)


@app.command("list")
def list_projects(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    format_: Annotated[
        str, typer.Option("--format", "-f", help="Output format: table, json, csv")
    ] = "table",
) -> None:
    """List all projects."""

    @run_async
    async def _list() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner("Loading projects...") as progress:
                progress.add_task("Loading projects...", total=None)
                response = await explore(
                    mode="list",
                    types=["project"],
                    limit=limit,
                )

            entities = response.entities or []

            if format_ == "json":
                import json

                console.print(json.dumps([e.model_dump() for e in entities], indent=2, default=str))
                return

            if format_ == "csv":
                import csv
                import sys

                writer = csv.writer(sys.stdout)
                writer.writerow(["id", "name", "status", "description"])
                for e in entities:
                    writer.writerow(
                        [
                            e.id,
                            e.name,
                            e.metadata.get("status", ""),
                            truncate(e.description or "", 100),
                        ]
                    )
                return

            if not entities:
                info("No projects found")
                return

            table = create_table("Projects", "ID", "Name", "Status", "Description")
            for e in entities:
                table.add_row(
                    e.id[:8] + "...",
                    truncate(e.name, 30),
                    e.metadata.get("status", "active"),
                    truncate(e.description or "", 40),
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} project(s)[/dim]")

        except Exception as e:
            error(f"Failed to list projects: {e}")
            print_db_hint()

    _list()


@app.command("show")
def show_project(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
) -> None:
    """Show project details with task summary."""

    @run_async
    async def _show() -> None:
        from sibyl.graph.client import get_graph_client
        from sibyl.graph.entities import EntityManager
        from sibyl.tools.core import explore

        try:
            with spinner("Loading project...") as progress:
                progress.add_task("Loading project...", total=None)
                client = await get_graph_client()
                manager = EntityManager(client)
                entity = await manager.get(project_id)

                # Get task counts
                tasks_response = await explore(
                    mode="list",
                    types=["task"],
                    project=project_id,
                    limit=500,
                )

            if not entity:
                error(f"Project not found: {project_id}")
                return

            tasks = tasks_response.entities or []
            status_counts: dict[str, int] = {}
            for t in tasks:
                status = t.metadata.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            meta = entity.metadata or {}
            lines = [
                f"[{ELECTRIC_PURPLE}]Name:[/{ELECTRIC_PURPLE}] {entity.name}",
                f"[{ELECTRIC_PURPLE}]Status:[/{ELECTRIC_PURPLE}] {meta.get('status', 'active')}",
                "",
                f"[{NEON_CYAN}]Description:[/{NEON_CYAN}]",
                entity.description or "[dim]No description[/dim]",
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

            panel = create_panel("\n".join(lines), title=f"Project {entity.id[:8]}")
            console.print(panel)

        except Exception as e:
            error(f"Failed to show project: {e}")
            print_db_hint()

    _show()


@app.command("create")
def create_project(
    name: Annotated[str, typer.Option("--name", "-n", help="Project name", prompt=True)],
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="Project description")
    ] = None,
    repo: Annotated[str | None, typer.Option("--repo", "-r", help="Repository URL")] = None,
) -> None:
    """Create a new project."""

    @run_async
    async def _create() -> None:
        from sibyl.tools.core import add

        try:
            with spinner("Creating project...") as progress:
                progress.add_task("Creating project...", total=None)
                response = await add(
                    title=name,
                    content=description or f"Project: {name}",
                    entity_type="project",
                    metadata={"repository_url": repo} if repo else None,
                )

            if response.success:
                success(f"Project created: {response.id}")
            else:
                error(f"Failed to create project: {response.message}")

        except Exception as e:
            error(f"Failed to create project: {e}")
            print_db_hint()

    _create()


@app.command("progress")
def project_progress(
    project_id: Annotated[str, typer.Argument(help="Project ID")],
) -> None:
    """Show project progress with visual breakdown."""

    @run_async
    async def _progress() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner("Loading progress...") as progress:
                progress.add_task("Loading progress...", total=None)
                response = await explore(
                    mode="list",
                    types=["task"],
                    project=project_id,
                    limit=500,
                )

            tasks = response.entities or []
            if not tasks:
                info("No tasks found for this project")
                return

            status_counts: dict[str, int] = {}
            for t in tasks:
                status = t.metadata.get("status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1

            total = len(tasks)
            done = status_counts.get("done", 0)
            pct = (done / total) * 100 if total > 0 else 0

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

        except Exception as e:
            error(f"Failed to get progress: {e}")
            print_db_hint()

    _progress()
