"""Task management CLI commands.

Commands for the full task lifecycle: list, show, create, start, block,
unblock, review, complete, archive, update.
"""

from typing import Annotated

import typer

from sibyl.cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_panel,
    create_table,
    error,
    format_priority,
    format_status,
    info,
    print_db_hint,
    run_async,
    spinner,
    success,
    truncate,
)

app = typer.Typer(
    name="task",
    help="Task lifecycle management",
    no_args_is_help=True,
)


@app.command("list")
def list_tasks(
    status: Annotated[str | None, typer.Option("--status", "-s", help="Filter by status")] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Filter by project ID")
    ] = None,
    assignee: Annotated[
        str | None, typer.Option("--assignee", "-a", help="Filter by assignee")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 50,
    format_: Annotated[
        str, typer.Option("--format", "-f", help="Output format: table, json, csv")
    ] = "table",
) -> None:
    """List tasks with optional filters."""

    @run_async
    async def _list() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner("Loading tasks...") as progress:
                progress.add_task("Loading tasks...", total=None)
                response = await explore(
                    mode="list",
                    types=["task"],
                    status=status,
                    project=project,
                    limit=limit,
                )

            entities = response.entities or []

            # Filter by assignee if specified (done client-side)
            if assignee:
                entities = [
                    e
                    for e in entities
                    if assignee.lower() in str(e.metadata.get("assignees", [])).lower()
                ]

            if format_ == "json":
                import json

                console.print(json.dumps([e.model_dump() for e in entities], indent=2, default=str))
                return

            if format_ == "csv":
                import csv
                import sys

                writer = csv.writer(sys.stdout)
                writer.writerow(["id", "title", "status", "priority", "project", "assignees"])
                for e in entities:
                    writer.writerow(
                        [
                            e.id,
                            e.name,
                            e.metadata.get("status", ""),
                            e.metadata.get("priority", ""),
                            e.metadata.get("project_id", ""),
                            ",".join(e.metadata.get("assignees", [])),
                        ]
                    )
                return

            # Table format (default)
            if not entities:
                info("No tasks found")
                return

            table = create_table("Tasks", "ID", "Title", "Status", "Priority", "Assignees")
            for e in entities:
                table.add_row(
                    e.id[:8] + "...",
                    truncate(e.name, 40),
                    format_status(e.metadata.get("status", "unknown")),
                    format_priority(e.metadata.get("priority", "medium")),
                    ", ".join(e.metadata.get("assignees", []))[:20] or "-",
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} task(s)[/dim]")

        except Exception as e:
            error(f"Failed to list tasks: {e}")
            print_db_hint()

    _list()


@app.command("show")
def show_task(
    task_id: Annotated[str, typer.Argument(help="Task ID (full or prefix)")],
) -> None:
    """Show detailed task information."""

    @run_async
    async def _show() -> None:
        from sibyl.graph.entities import EntityManager

        try:
            with spinner("Loading task...") as progress:
                progress.add_task("Loading task...", total=None)
                manager = EntityManager()
                entity = await manager.get(task_id)

            if not entity:
                error(f"Task not found: {task_id}")
                return

            # Build detail panel
            meta = entity.metadata or {}
            lines = [
                f"[{ELECTRIC_PURPLE}]Title:[/{ELECTRIC_PURPLE}] {entity.name}",
                f"[{ELECTRIC_PURPLE}]Status:[/{ELECTRIC_PURPLE}] {format_status(meta.get('status', 'unknown'))}",
                f"[{ELECTRIC_PURPLE}]Priority:[/{ELECTRIC_PURPLE}] {format_priority(meta.get('priority', 'medium'))}",
                "",
                f"[{NEON_CYAN}]Description:[/{NEON_CYAN}]",
                entity.description or "[dim]No description[/dim]",
            ]

            if meta.get("project_id"):
                lines.insert(
                    3,
                    f"[{ELECTRIC_PURPLE}]Project:[/{ELECTRIC_PURPLE}] {meta['project_id'][:8]}...",
                )

            if meta.get("assignees"):
                lines.insert(
                    4,
                    f"[{ELECTRIC_PURPLE}]Assignees:[/{ELECTRIC_PURPLE}] {', '.join(meta['assignees'])}",
                )

            if meta.get("feature"):
                lines.append(f"\n[{CORAL}]Feature:[/{CORAL}] {meta['feature']}")

            if meta.get("branch_name"):
                lines.append(f"[{CORAL}]Branch:[/{CORAL}] {meta['branch_name']}")

            if meta.get("technologies"):
                lines.append(f"[{CORAL}]Tech:[/{CORAL}] {', '.join(meta['technologies'])}")

            panel = create_panel("\n".join(lines), title=f"Task {entity.id[:8]}")
            console.print(panel)

        except Exception as e:
            error(f"Failed to show task: {e}")
            print_db_hint()

    _show()


@app.command("start")
def start_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to start")],
    assignee: Annotated[str | None, typer.Option("--assignee", "-a", help="Assignee name")] = None,
) -> None:
    """Start working on a task (moves to 'doing' status)."""

    @run_async
    async def _start() -> None:
        from sibyl.tools.core import manage

        try:
            with spinner("Starting task...") as progress:
                progress.add_task("Starting task...", total=None)
                response = await manage(
                    action="start_task",
                    entity_id=task_id,
                    assignee=assignee,
                )

            if response.success:
                success(f"Task started: {task_id[:8]}...")
                if response.data and response.data.get("branch_name"):
                    info(f"Branch: {response.data['branch_name']}")
            else:
                error(f"Failed to start task: {response.message}")

        except Exception as e:
            error(f"Failed to start task: {e}")
            print_db_hint()

    _start()


@app.command("block")
def block_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to block")],
    reason: Annotated[str, typer.Option("--reason", "-r", help="Blocker reason", prompt=True)],
) -> None:
    """Mark a task as blocked with a reason."""

    @run_async
    async def _block() -> None:
        from sibyl.tools.core import manage

        try:
            with spinner("Blocking task...") as progress:
                progress.add_task("Blocking task...", total=None)
                response = await manage(
                    action="block_task",
                    entity_id=task_id,
                    blocker=reason,
                )

            if response.success:
                success(f"Task blocked: {task_id[:8]}...")
            else:
                error(f"Failed to block task: {response.message}")

        except Exception as e:
            error(f"Failed to block task: {e}")
            print_db_hint()

    _block()


@app.command("unblock")
def unblock_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to unblock")],
) -> None:
    """Resume a blocked task (moves back to 'doing')."""

    @run_async
    async def _unblock() -> None:
        from sibyl.tools.core import manage

        try:
            with spinner("Unblocking task...") as progress:
                progress.add_task("Unblocking task...", total=None)
                response = await manage(
                    action="unblock_task",
                    entity_id=task_id,
                )

            if response.success:
                success(f"Task unblocked: {task_id[:8]}...")
            else:
                error(f"Failed to unblock task: {response.message}")

        except Exception as e:
            error(f"Failed to unblock task: {e}")
            print_db_hint()

    _unblock()


@app.command("review")
def submit_review(
    task_id: Annotated[str, typer.Argument(help="Task ID to submit for review")],
    pr_url: Annotated[str | None, typer.Option("--pr", help="Pull request URL")] = None,
    commits: Annotated[
        str | None, typer.Option("--commits", "-c", help="Comma-separated commit SHAs")
    ] = None,
) -> None:
    """Submit a task for review."""

    @run_async
    async def _review() -> None:
        from sibyl.tools.core import manage

        try:
            commit_list = [c.strip() for c in commits.split(",")] if commits else None

            with spinner("Submitting for review...") as progress:
                progress.add_task("Submitting for review...", total=None)
                response = await manage(
                    action="submit_review",
                    entity_id=task_id,
                    pr_url=pr_url,
                    commit_shas=commit_list,
                )

            if response.success:
                success(f"Task submitted for review: {task_id[:8]}...")
            else:
                error(f"Failed to submit for review: {response.message}")

        except Exception as e:
            error(f"Failed to submit for review: {e}")
            print_db_hint()

    _review()


@app.command("complete")
def complete_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to complete")],
    hours: Annotated[float | None, typer.Option("--hours", "-h", help="Actual hours spent")] = None,
    learnings: Annotated[
        str | None, typer.Option("--learnings", "-l", help="Key learnings (creates episode)")
    ] = None,
) -> None:
    """Complete a task and optionally capture learnings."""

    @run_async
    async def _complete() -> None:
        from sibyl.tools.core import manage

        try:
            with spinner("Completing task...") as progress:
                progress.add_task("Completing task...", total=None)
                response = await manage(
                    action="complete_task",
                    entity_id=task_id,
                    actual_hours=hours,
                    learnings=learnings,
                )

            if response.success:
                success(f"Task completed: {task_id[:8]}...")
                if learnings:
                    info("Learning episode created from task")
            else:
                error(f"Failed to complete task: {response.message}")

        except Exception as e:
            error(f"Failed to complete task: {e}")
            print_db_hint()

    _complete()


@app.command("archive")
def archive_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to archive")],
    reason: Annotated[str | None, typer.Option("--reason", "-r", help="Archive reason")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
) -> None:
    """Archive a task (terminal state)."""
    if not yes:
        confirm = typer.confirm(f"Archive task {task_id[:8]}...? This cannot be undone.")
        if not confirm:
            info("Cancelled")
            return

    @run_async
    async def _archive() -> None:
        from sibyl.tools.core import manage

        try:
            with spinner("Archiving task...") as progress:
                progress.add_task("Archiving task...", total=None)
                # Note: archive_reason captured in learnings if provided
                response = await manage(
                    action="archive",
                    entity_id=task_id,
                    learnings=reason if reason else None,
                )

            if response.success:
                success(f"Task archived: {task_id[:8]}...")
            else:
                error(f"Failed to archive task: {response.message}")

        except Exception as e:
            error(f"Failed to archive task: {e}")
            print_db_hint()

    _archive()
