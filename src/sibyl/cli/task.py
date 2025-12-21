"""Task management CLI commands.

Commands for the full task lifecycle: list, show, create, start, block,
unblock, review, complete, archive, update.

All commands communicate with the REST API to ensure proper event broadcasting.
All commands output JSON by default for LLM consumption. Use -t for table output.
"""

from typing import Annotated

import typer

from sibyl.cli.client import SibylClientError, get_client
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
    print_json,
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


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages."""
    if "Cannot connect" in str(e):
        error(str(e))
        info("Start the server with: sibyl serve")
    elif e.status_code == 404:
        error(f"Not found: {e.detail}")
    elif e.status_code == 400:
        error(f"Invalid request: {e.detail}")
    else:
        error(str(e))


def _output_response(response: dict, table_out: bool, success_msg: str | None = None) -> None:
    """Output response as JSON or table message."""
    if not table_out:
        print_json(response)
    elif success_msg and response.get("success"):
        success(success_msg)
    elif not response.get("success"):
        error(f"Failed: {response.get('message', 'Unknown error')}")


@app.command("list")
def list_tasks(
    status: Annotated[
        str | None, typer.Option("-s", "--status", help="todo|doing|blocked|review|done")
    ] = None,
    project: Annotated[str | None, typer.Option("-p", "--project", help="Project ID")] = None,
    assignee: Annotated[str | None, typer.Option("-a", "--assignee", help="Assignee")] = None,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max results")] = 50,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
    csv_out: Annotated[bool, typer.Option("--csv", help="CSV output")] = False,
) -> None:
    """List tasks with optional filters. Default: JSON output."""
    fmt = "table" if table_out else ("csv" if csv_out else "json")

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            if fmt in ("json", "csv"):
                response = await client.explore(
                    mode="list",
                    types=["task"],
                    status=status,
                    project=project,
                    limit=limit,
                )
            else:
                with spinner("Loading tasks...") as progress:
                    progress.add_task("Loading tasks...", total=None)
                    response = await client.explore(
                        mode="list",
                        types=["task"],
                        status=status,
                        project=project,
                        limit=limit,
                    )

            entities = response.get("entities", [])

            # Filter by assignee if specified (done client-side)
            if assignee:
                entities = [
                    e
                    for e in entities
                    if assignee.lower() in str(e.get("metadata", {}).get("assignees", [])).lower()
                ]

            if fmt == "json":
                print_json(entities)
                return

            if fmt == "csv":
                import csv
                import sys

                writer = csv.writer(sys.stdout)
                writer.writerow(["id", "title", "status", "priority", "project", "assignees"])
                for e in entities:
                    meta = e.get("metadata", {})
                    writer.writerow(
                        [
                            e.get("id", ""),
                            e.get("name", ""),
                            meta.get("status", ""),
                            meta.get("priority", ""),
                            meta.get("project_id", ""),
                            ",".join(meta.get("assignees", [])),
                        ]
                    )
                return

            # Table format
            if not entities:
                info("No tasks found")
                return

            table = create_table("Tasks", "ID", "Title", "Status", "Priority", "Assignees")
            for e in entities:
                meta = e.get("metadata", {})
                table.add_row(
                    e.get("id", "")[:8] + "...",
                    truncate(e.get("name", ""), 40),
                    format_status(meta.get("status", "unknown")),
                    format_priority(meta.get("priority", "medium")),
                    ", ".join(meta.get("assignees", []))[:20] or "-",
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} task(s)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _list()


@app.command("show")
def show_task(
    task_id: Annotated[str, typer.Argument(help="Task ID (full or prefix)")],
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show detailed task information. Default: JSON output."""

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Loading task...") as progress:
                    progress.add_task("Loading task...", total=None)
                    entity = await client.get_entity(task_id)
            else:
                entity = await client.get_entity(task_id)

            # JSON output (default)
            if not table_out:
                print_json(entity)
                return

            # Table output
            meta = entity.get("metadata", {})
            lines = [
                f"[{ELECTRIC_PURPLE}]Title:[/{ELECTRIC_PURPLE}] {entity.get('name', '')}",
                f"[{ELECTRIC_PURPLE}]Status:[/{ELECTRIC_PURPLE}] {format_status(meta.get('status', 'unknown'))}",
                f"[{ELECTRIC_PURPLE}]Priority:[/{ELECTRIC_PURPLE}] {format_priority(meta.get('priority', 'medium'))}",
                "",
                f"[{NEON_CYAN}]Description:[/{NEON_CYAN}]",
                entity.get("description") or "[dim]No description[/dim]",
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

            panel = create_panel("\n".join(lines), title=f"Task {entity.get('id', '')[:8]}")
            console.print(panel)

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("start")
def start_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to start")],
    assignee: Annotated[str | None, typer.Option("--assignee", "-a", help="Assignee name")] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Start working on a task (moves to 'doing' status). Default: JSON output."""

    @run_async
    async def _start() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Starting task...") as progress:
                    progress.add_task("Starting task...", total=None)
                    response = await client.start_task(task_id, assignee)
            else:
                response = await client.start_task(task_id, assignee)

            if not table_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task started: {task_id[:8]}...")
                if response.get("data", {}).get("branch_name"):
                    info(f"Branch: {response['data']['branch_name']}")
            else:
                error(f"Failed to start task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _start()


@app.command("block")
def block_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to block")],
    reason: Annotated[str, typer.Option("--reason", "-r", help="Blocker reason", prompt=True)],
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Mark a task as blocked with a reason. Default: JSON output."""

    @run_async
    async def _block() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Blocking task...") as progress:
                    progress.add_task("Blocking task...", total=None)
                    response = await client.block_task(task_id, reason)
            else:
                response = await client.block_task(task_id, reason)

            if not table_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task blocked: {task_id[:8]}...")
            else:
                error(f"Failed to block task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _block()


@app.command("unblock")
def unblock_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to unblock")],
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Resume a blocked task (moves back to 'doing'). Default: JSON output."""

    @run_async
    async def _unblock() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Unblocking task...") as progress:
                    progress.add_task("Unblocking task...", total=None)
                    response = await client.unblock_task(task_id)
            else:
                response = await client.unblock_task(task_id)

            if not table_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task unblocked: {task_id[:8]}...")
            else:
                error(f"Failed to unblock task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _unblock()


@app.command("review")
def submit_review(
    task_id: Annotated[str, typer.Argument(help="Task ID to submit for review")],
    pr_url: Annotated[str | None, typer.Option("--pr", help="Pull request URL")] = None,
    commits: Annotated[
        str | None, typer.Option("--commits", "-c", help="Comma-separated commit SHAs")
    ] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Submit a task for review. Default: JSON output."""

    @run_async
    async def _review() -> None:
        client = get_client()

        try:
            commit_list = [c.strip() for c in commits.split(",")] if commits else None

            if table_out:
                with spinner("Submitting for review...") as progress:
                    progress.add_task("Submitting for review...", total=None)
                    response = await client.submit_review(task_id, pr_url, commit_list)
            else:
                response = await client.submit_review(task_id, pr_url, commit_list)

            if not table_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task submitted for review: {task_id[:8]}...")
            else:
                error(f"Failed to submit for review: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _review()


@app.command("complete")
def complete_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to complete")],
    hours: Annotated[float | None, typer.Option("--hours", "-h", help="Actual hours spent")] = None,
    learnings: Annotated[
        str | None, typer.Option("--learnings", "-l", help="Key learnings (creates episode)")
    ] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Complete a task and optionally capture learnings. Default: JSON output."""

    @run_async
    async def _complete() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Completing task...") as progress:
                    progress.add_task("Completing task...", total=None)
                    response = await client.complete_task(task_id, hours, learnings)
            else:
                response = await client.complete_task(task_id, hours, learnings)

            if not table_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task completed: {task_id[:8]}...")
                if learnings:
                    info("Learning episode created from task")
            else:
                error(f"Failed to complete task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _complete()


@app.command("archive")
def archive_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to archive")],
    reason: Annotated[str | None, typer.Option("--reason", "-r", help="Archive reason")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Archive a task (terminal state). Default: JSON output."""
    if not yes and table_out:
        confirm = typer.confirm(f"Archive task {task_id[:8]}...? This cannot be undone.")
        if not confirm:
            info("Cancelled")
            return

    @run_async
    async def _archive() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Archiving task...") as progress:
                    progress.add_task("Archiving task...", total=None)
                    response = await client.archive_task(task_id, reason)
            else:
                response = await client.archive_task(task_id, reason)

            if not table_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task archived: {task_id[:8]}...")
            else:
                error(f"Failed to archive task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _archive()


@app.command("create")
def create_task(
    title: Annotated[str, typer.Option("--title", "-T", help="Task title", prompt=True)],
    project: Annotated[
        str, typer.Option("--project", "-p", help="Project ID (required)", prompt=True)
    ],
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="Task description")
    ] = None,
    priority: Annotated[
        str, typer.Option("--priority", help="Priority: critical, high, medium, low, someday")
    ] = "medium",
    assignee: Annotated[
        str | None, typer.Option("--assignee", "-a", help="Initial assignee")
    ] = None,
    feature: Annotated[str | None, typer.Option("--feature", "-f", help="Feature area")] = None,
    technologies: Annotated[
        str | None, typer.Option("--tech", help="Comma-separated technologies")
    ] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Create a new task in a project. Default: JSON output."""

    @run_async
    async def _create() -> None:
        client = get_client()

        try:
            tech_list = [t.strip() for t in technologies.split(",")] if technologies else None
            assignee_list = [assignee] if assignee else None

            # Build metadata
            metadata: dict = {
                "project_id": project,
                "priority": priority,
                "status": "todo",
            }
            if assignee_list:
                metadata["assignees"] = assignee_list
            if tech_list:
                metadata["technologies"] = tech_list
            if feature:
                metadata["feature"] = feature

            if table_out:
                with spinner("Creating task...") as progress:
                    progress.add_task("Creating task...", total=None)
                    response = await client.create_entity(
                        name=title,
                        content=description or title,
                        entity_type="task",
                        metadata=metadata,
                    )
            else:
                response = await client.create_entity(
                    name=title,
                    content=description or title,
                    entity_type="task",
                    metadata=metadata,
                )

            if not table_out:
                print_json(response)
                return

            if response.get("id"):
                success(f"Task created: {response['id']}")
                if assignee:
                    info(f"Assigned to: {assignee}")
            else:
                error("Failed to create task")

        except SibylClientError as e:
            _handle_client_error(e)

    _create()


@app.command("update")
def update_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to update")],
    status: Annotated[
        str | None, typer.Option("-s", "--status", help="Status: todo|doing|blocked|review|done")
    ] = None,
    priority: Annotated[
        str | None,
        typer.Option("-p", "--priority", help="Priority: critical|high|medium|low|someday"),
    ] = None,
    title: Annotated[str | None, typer.Option("-T", "--title", help="Task title")] = None,
    assignee: Annotated[str | None, typer.Option("-a", "--assignee", help="Assignee")] = None,
    feature: Annotated[str | None, typer.Option("-f", "--feature", help="Feature area")] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Update task fields directly. Default: JSON output."""

    @run_async
    async def _update() -> None:
        client = get_client()

        try:
            # Check we have something to update
            if not any([status, priority, title, assignee, feature]):
                error(
                    "No fields to update. Use --status, --priority, --title, --assignee, or --feature"
                )
                return

            assignees = [assignee] if assignee else None

            if table_out:
                with spinner("Updating task...") as progress:
                    progress.add_task("Updating task...", total=None)
                    response = await client.update_task(
                        task_id=task_id,
                        status=status,
                        priority=priority,
                        title=title,
                        assignees=assignees,
                        feature=feature,
                    )
            else:
                response = await client.update_task(
                    task_id=task_id,
                    status=status,
                    priority=priority,
                    title=title,
                    assignees=assignees,
                    feature=feature,
                )

            if not table_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task updated: {task_id[:8]}...")
                info(f"Fields: {', '.join(response.get('data', {}).keys())}")
            else:
                error(f"Failed to update task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _update()
