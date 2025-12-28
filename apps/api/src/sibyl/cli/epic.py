"""Epic management CLI commands.

Commands for epic lifecycle: list, show, create, start, complete, archive.

Epics group related tasks within a project. They have a simpler lifecycle
than tasks: planning -> in_progress -> completed/archived.

All commands output JSON by default for LLM consumption. Use -t for table output.
"""

from typing import TYPE_CHECKING, Annotated

import typer

from sibyl.cli.client import SibylClientError, get_client

if TYPE_CHECKING:
    from sibyl.cli.client import SibylClient

from sibyl.cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_panel,
    create_table,
    error,
    format_priority,
    handle_client_error,
    info,
    print_json,
    run_async,
    spinner,
    success,
    truncate,
)
from sibyl.cli.config_store import resolve_project_from_cwd

app = typer.Typer(
    name="epic",
    help="Epic lifecycle management (feature grouping for tasks)",
    no_args_is_help=True,
)


# Use centralized handler from common.py
_handle_client_error = handle_client_error


def format_epic_status(status: str) -> str:
    """Format epic status with colors."""
    colors = {
        "planning": NEON_CYAN,
        "in_progress": ELECTRIC_PURPLE,
        "blocked": "#ff6363",
        "completed": "#50fa7b",
        "archived": "#888888",
    }
    color = colors.get(status, "#888888")
    return f"[{color}]{status}[/{color}]"


async def _resolve_epic_id(client: "SibylClient", epic_id: str) -> str:
    """Resolve a short epic ID prefix to a full epic ID.

    If epic_id is already a full ID (17+ chars), returns it unchanged.
    Otherwise, searches for epics matching the prefix.

    Args:
        client: The Sibyl API client.
        epic_id: Full epic ID or short prefix (e.g., "epic_c24").

    Returns:
        The full epic ID if found.

    Raises:
        SibylClientError: If no match found or multiple matches.
    """
    # Already a full ID (epic_ + 12 hex chars = 17 chars minimum)
    if len(epic_id) >= 17:
        return epic_id

    # Search for matching epics
    try:
        result = await client.list_entities(entity_type="epic", page_size=100)
        entities = result.get("entities", [])

        # Find all epics matching the prefix
        matches = [e for e in entities if e.get("id", "").startswith(epic_id)]

        if len(matches) == 0:
            raise SibylClientError(
                f"No epic found matching prefix: {epic_id}",
                status_code=404,
                detail=f"No epic found matching prefix: {epic_id}",
            )
        if len(matches) == 1:
            return matches[0]["id"]
        # Multiple matches - show them
        match_ids = [m["id"] for m in matches[:5]]
        msg = f"Multiple epics match prefix '{epic_id}': {', '.join(match_ids)}"
        raise SibylClientError(msg, status_code=400, detail=msg)
    except SibylClientError:
        raise
    except Exception:
        # Fall back to using the ID as-is
        return epic_id


@app.command("list")
def list_epics(
    project: Annotated[str | None, typer.Option("-p", "--project", help="Project ID")] = None,
    status: Annotated[
        str | None, typer.Option("-s", "--status", help="planning|in_progress|blocked|completed")
    ] = None,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max results")] = 50,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
    all_projects: Annotated[
        bool, typer.Option("--all", "-A", help="Ignore context, list from all projects")
    ] = False,
) -> None:
    """List epics with optional filters. Default: JSON output.

    Auto-scopes to current project context unless --all is specified.
    """
    # Auto-resolve project from context if not explicitly set
    effective_project = project
    if not project and not all_projects:
        effective_project = resolve_project_from_cwd()

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Loading epics...") as progress:
                    progress.add_task("Loading epics...", total=None)
                    response = await client.explore(
                        mode="list",
                        types=["epic"],
                        status=status,
                        project=effective_project,
                        limit=limit,
                    )
            else:
                response = await client.explore(
                    mode="list",
                    types=["epic"],
                    status=status,
                    project=effective_project,
                    limit=limit,
                )

            entities = response.get("entities", [])

            # Client-side filters (when API doesn't filter)
            if status:
                entities = [e for e in entities if e.get("metadata", {}).get("status") == status]
            if effective_project:
                entities = [
                    e
                    for e in entities
                    if e.get("metadata", {}).get("project_id") == effective_project
                ]

            if not table_out:
                print_json(entities)
                return

            # Table format
            if not entities:
                info("No epics found")
                return

            table = create_table("Epics", "ID", "Title", "Status", "Priority", "Progress")
            for e in entities:
                meta = e.get("metadata", {})
                total = meta.get("total_tasks", 0)
                completed = meta.get("completed_tasks", 0)
                progress_str = f"{completed}/{total}" if total > 0 else "-"
                table.add_row(
                    e.get("id", "")[:12] + "...",
                    truncate(e.get("name", ""), 40),
                    format_epic_status(meta.get("status", "planning")),
                    format_priority(meta.get("priority", "medium")),
                    progress_str,
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} epic(s)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _list()


@app.command("show")
def show_epic(
    epic_id: Annotated[str, typer.Argument(help="Epic ID (full or prefix)")],
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show detailed epic information including progress. Default: JSON output."""

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            # Resolve short ID prefix to full ID
            resolved_id = await _resolve_epic_id(client, epic_id)

            if table_out:
                with spinner("Loading epic...") as progress:
                    progress.add_task("Loading epic...", total=None)
                    entity = await client.get_entity(resolved_id)
            else:
                entity = await client.get_entity(resolved_id)

            # JSON output (default)
            if not table_out:
                print_json(entity)
                return

            # Table output
            meta = entity.get("metadata", {})
            total = meta.get("total_tasks", 0)
            completed = meta.get("completed_tasks", 0)
            pct = round((completed / total * 100) if total > 0 else 0, 1)

            lines = [
                f"[{ELECTRIC_PURPLE}]Title:[/{ELECTRIC_PURPLE}] {entity.get('name', '')}",
                f"[{ELECTRIC_PURPLE}]Status:[/{ELECTRIC_PURPLE}] {format_epic_status(meta.get('status', 'planning'))}",
                f"[{ELECTRIC_PURPLE}]Priority:[/{ELECTRIC_PURPLE}] {format_priority(meta.get('priority', 'medium'))}",
                f"[{ELECTRIC_PURPLE}]Progress:[/{ELECTRIC_PURPLE}] {completed}/{total} tasks ({pct}%)",
                "",
                f"[{NEON_CYAN}]Description:[/{NEON_CYAN}]",
                entity.get("description") or "[dim]No description[/dim]",
            ]

            if meta.get("project_id"):
                lines.insert(
                    4,
                    f"[{ELECTRIC_PURPLE}]Project:[/{ELECTRIC_PURPLE}] {meta['project_id'][:12]}...",
                )

            if meta.get("assignees"):
                lines.insert(
                    5,
                    f"[{ELECTRIC_PURPLE}]Leads:[/{ELECTRIC_PURPLE}] {', '.join(meta['assignees'])}",
                )

            if meta.get("tags"):
                lines.append(f"\n[{CORAL}]Tags:[/{CORAL}] {', '.join(meta['tags'])}")

            if meta.get("learnings"):
                lines.append(f"\n[{CORAL}]Learnings:[/{CORAL}] {meta['learnings']}")

            panel = create_panel("\n".join(lines), title=f"Epic {entity.get('id', '')[:12]}")
            console.print(panel)

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("create")
def create_epic(
    title: Annotated[str, typer.Option("--title", "-n", help="Epic title", prompt=True)],
    project: Annotated[
        str, typer.Option("--project", "-p", help="Project ID (required)", prompt=True)
    ],
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="Epic description")
    ] = None,
    priority: Annotated[
        str, typer.Option("--priority", help="Priority: critical, high, medium, low, someday")
    ] = "medium",
    assignee: Annotated[
        str | None, typer.Option("--assignee", "-a", help="Epic lead/owner")
    ] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags")] = None,
    sync: Annotated[
        bool,
        typer.Option("--sync", help="Wait for epic creation (slower but immediately available)"),
    ] = False,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Create a new epic in a project. Default: JSON output."""

    @run_async
    async def _create() -> None:
        client = get_client()

        try:
            tag_list = [t.strip() for t in tags.split(",")] if tags else None
            assignee_list = [assignee] if assignee else None

            # Build metadata
            metadata: dict = {
                "project_id": project,
                "priority": priority,
                "status": "planning",
            }
            if assignee_list:
                metadata["assignees"] = assignee_list
            if tag_list:
                metadata["tags"] = tag_list

            if table_out:
                with spinner("Creating epic...") as progress:
                    progress.add_task("Creating epic...", total=None)
                    response = await client.create_entity(
                        name=title,
                        content=description or title,
                        entity_type="epic",
                        metadata=metadata,
                        sync=sync,
                    )
            else:
                response = await client.create_entity(
                    name=title,
                    content=description or title,
                    entity_type="epic",
                    metadata=metadata,
                    sync=sync,
                )

            if not table_out:
                print_json(response)
                return

            if response.get("id"):
                success(f"Epic created: {response['id']}")
                if assignee:
                    info(f"Lead: {assignee}")
            else:
                error("Failed to create epic")

        except SibylClientError as e:
            _handle_client_error(e)

    _create()


@app.command("start")
def start_epic(
    epic_id: Annotated[str, typer.Argument(help="Epic ID to start (full or prefix)")],
    assignee: Annotated[str | None, typer.Option("--assignee", "-a", help="Epic lead")] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Start working on an epic (moves to 'in_progress' status). Default: JSON output."""

    @run_async
    async def _start() -> None:
        client = get_client()

        try:
            resolved_id = await _resolve_epic_id(client, epic_id)

            # Build update data
            updates: dict = {"status": "in_progress"}
            if assignee:
                updates["assignees"] = [assignee]

            if table_out:
                with spinner("Starting epic...") as progress:
                    progress.add_task("Starting epic...", total=None)
                    response = await client.update_entity(resolved_id, **updates)
            else:
                response = await client.update_entity(resolved_id, **updates)

            if not table_out:
                print_json(response)
                return

            if response.get("success") or response.get("id"):
                success(f"Epic started: {epic_id[:12]}...")
            else:
                error(f"Failed to start epic: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _start()


@app.command("complete")
def complete_epic(
    epic_id: Annotated[str, typer.Argument(help="Epic ID to complete (full or prefix)")],
    learnings: Annotated[
        str | None, typer.Option("--learnings", "-l", help="Key learnings from the epic")
    ] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Complete an epic. Default: JSON output."""

    @run_async
    async def _complete() -> None:
        client = get_client()

        try:
            resolved_id = await _resolve_epic_id(client, epic_id)

            # Build update data
            updates: dict = {"status": "completed"}
            if learnings:
                updates["learnings"] = learnings

            if table_out:
                with spinner("Completing epic...") as progress:
                    progress.add_task("Completing epic...", total=None)
                    response = await client.update_entity(resolved_id, **updates)
            else:
                response = await client.update_entity(resolved_id, **updates)

            if not table_out:
                print_json(response)
                return

            if response.get("success") or response.get("id"):
                success(f"Epic completed: {epic_id[:12]}...")
                if learnings:
                    info("Learnings captured")
            else:
                error(f"Failed to complete epic: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _complete()


@app.command("archive")
def archive_epic(
    epic_id: Annotated[str, typer.Argument(help="Epic ID to archive")],
    reason: Annotated[str | None, typer.Option("--reason", "-r", help="Archive reason")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Archive an epic (terminal state). Default: JSON output."""
    if not yes:
        confirm = typer.confirm(f"Archive epic {epic_id[:12]}...? This cannot be undone.")
        if not confirm:
            info("Cancelled")
            return

    @run_async
    async def _archive() -> None:
        client = get_client()

        try:
            resolved_id = await _resolve_epic_id(client, epic_id)

            # Build update data
            updates: dict = {"status": "archived"}
            if reason:
                updates["learnings"] = f"Archived: {reason}"

            if table_out:
                with spinner("Archiving epic...") as progress:
                    progress.add_task("Archiving epic...", total=None)
                    response = await client.update_entity(resolved_id, **updates)
            else:
                response = await client.update_entity(resolved_id, **updates)

            if not table_out:
                print_json(response)
                return

            if response.get("success") or response.get("id"):
                success(f"Epic archived: {resolved_id[:16]}...")
            else:
                error(f"Failed to archive epic: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _archive()


@app.command("update")
def update_epic(
    epic_id: Annotated[str, typer.Argument(help="Epic ID to update")],
    status: Annotated[
        str | None,
        typer.Option("-s", "--status", help="Status: planning|in_progress|blocked|completed"),
    ] = None,
    priority: Annotated[
        str | None,
        typer.Option("-p", "--priority", help="Priority: critical|high|medium|low|someday"),
    ] = None,
    title: Annotated[str | None, typer.Option("--title", help="Epic title")] = None,
    assignee: Annotated[str | None, typer.Option("-a", "--assignee", help="Epic lead")] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags")] = None,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Update epic fields directly. Default: JSON output."""

    @run_async
    async def _update() -> None:
        client = get_client()

        try:
            # Check we have something to update
            if not any([status, priority, title, assignee, tags]):
                error(
                    "No fields to update. Use --status, --priority, --title, --assignee, or --tags"
                )
                return

            resolved_id = await _resolve_epic_id(client, epic_id)

            # Build update data
            updates: dict = {}
            if status:
                updates["status"] = status
            if priority:
                updates["priority"] = priority
            if title:
                updates["name"] = title
            if assignee:
                updates["assignees"] = [assignee]
            if tags:
                updates["tags"] = [t.strip() for t in tags.split(",")]

            if table_out:
                with spinner("Updating epic...") as progress:
                    progress.add_task("Updating epic...", total=None)
                    response = await client.update_entity(resolved_id, **updates)
            else:
                response = await client.update_entity(resolved_id, **updates)

            if not table_out:
                print_json(response)
                return

            if response.get("success") or response.get("id"):
                success(f"Epic updated: {resolved_id[:16]}...")
                info(f"Fields: {', '.join(updates.keys())}")
            else:
                error(f"Failed to update epic: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _update()


@app.command("tasks")
def list_epic_tasks(
    epic_id: Annotated[str, typer.Argument(help="Epic ID to list tasks for")],
    status: Annotated[
        str | None, typer.Option("-s", "--status", help="Filter by task status")
    ] = None,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max results")] = 50,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """List tasks belonging to an epic. Default: JSON output."""

    @run_async
    async def _list_tasks() -> None:
        client = get_client()

        try:
            resolved_id = await _resolve_epic_id(client, epic_id)

            # Get all tasks and filter by epic_id
            if table_out:
                with spinner("Loading tasks...") as progress:
                    progress.add_task("Loading tasks...", total=None)
                    response = await client.explore(
                        mode="list",
                        types=["task"],
                        limit=limit,
                    )
            else:
                response = await client.explore(
                    mode="list",
                    types=["task"],
                    limit=limit,
                )

            entities = response.get("entities", [])

            # Filter by epic_id
            entities = [e for e in entities if e.get("metadata", {}).get("epic_id") == resolved_id]

            # Filter by status if specified
            if status:
                entities = [e for e in entities if e.get("metadata", {}).get("status") == status]

            if not table_out:
                print_json(entities)
                return

            # Table format
            if not entities:
                info(f"No tasks found for epic {epic_id[:12]}...")
                return

            from sibyl.cli.common import format_status

            table = create_table("Tasks", "ID", "Title", "Status", "Priority", "Assignees")
            for e in entities:
                meta = e.get("metadata", {})
                table.add_row(
                    e.get("id", "")[:12] + "...",
                    truncate(e.get("name", ""), 40),
                    format_status(meta.get("status", "unknown")),
                    format_priority(meta.get("priority", "medium")),
                    ", ".join(meta.get("assignees", []))[:20] or "-",
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} task(s) for epic[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _list_tasks()
