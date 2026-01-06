"""Epic management CLI commands.

Commands for epic lifecycle: list, show, create, start, complete, archive.

Epics group related tasks within a project. They have a simpler lifecycle
than tasks: planning -> in_progress -> completed/archived.

All commands output JSON by default for LLM consumption. Use -t for table output.
"""

from typing import Annotated

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
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
    success,
    truncate,
)
from sibyl_cli.config_store import resolve_project_from_cwd

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


def _validate_epic_id(epic_id: str) -> str:
    """Validate that an epic ID has the expected format.

    Full epic IDs are required - no prefix matching or guessing.
    Format: epic_<12 hex chars> (17 chars total)

    Args:
        epic_id: The epic ID to validate.

    Returns:
        The epic ID unchanged.

    Raises:
        SibylClientError: If the ID format is invalid.
    """
    if not epic_id.startswith("epic_"):
        raise SibylClientError(
            f"Invalid epic ID format: {epic_id}. Expected format: epic_<12 hex chars>",
            status_code=400,
            detail=f"Invalid epic ID: {epic_id}",
        )
    if len(epic_id) < 17:
        raise SibylClientError(
            f"Epic ID too short: {epic_id}. Full epic ID required (17 chars).",
            status_code=400,
            detail=f"Full epic ID required, got: {epic_id}",
        )
    return epic_id


@app.command("list")
def list_epics(
    project: Annotated[str | None, typer.Option("-p", "--project", help="Project ID")] = None,
    status: Annotated[
        str | None, typer.Option("-s", "--status", help="planning|in_progress|blocked|completed")
    ] = None,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max results")] = 50,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
    all_projects: Annotated[
        bool, typer.Option("--all", "-A", help="Ignore context, list from all projects")
    ] = False,
) -> None:
    """List epics with optional filters. Default: table output.

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

            if json_out:
                print_json(entities)
                return

            # Table format
            if not entities:
                info("No epics found")
                return

            table = create_table("Epics", "ID", "Title", "Status", "Priority", "Progress")
            # ID, Status, Priority, Progress are fixed-width; Title gets the rest
            table.columns[0].no_wrap = True  # ID
            table.columns[2].no_wrap = True  # Status
            table.columns[3].no_wrap = True  # Priority
            table.columns[4].no_wrap = True  # Progress
            # Title column auto-sizes and can wrap if needed
            for e in entities:
                meta = e.get("metadata", {})
                total = meta.get("total_tasks", 0)
                completed = meta.get("completed_tasks", 0)
                progress_str = f"{completed}/{total}" if total > 0 else "-"
                table.add_row(
                    e.get("id", ""),
                    e.get("name", ""),  # Full title, no truncation
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
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show detailed epic information including progress. Default: table output."""

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            # Resolve short ID prefix to full ID
            resolved_id = _validate_epic_id(epic_id)

            entity = await client.get_entity(resolved_id)

            # JSON output (default)
            if json_out:
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
                    f"[{ELECTRIC_PURPLE}]Project:[/{ELECTRIC_PURPLE}] {meta['project_id']}",
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

            panel = create_panel("\n".join(lines), title=f"Epic {entity.get('id', '')}")
            console.print(panel)

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("create")
def create_epic(
    title: Annotated[str, typer.Option("--title", "-n", help="Epic title (required)")],
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project ID (auto-resolves from linked path)"),
    ] = None,
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
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Create a new epic in a project. Default: table output.

    Project is auto-resolved from linked directory if not specified.
    Use 'sibyl project link' to link a directory to a project.
    """
    # Auto-resolve project from linked path if not provided
    effective_project = project
    if not effective_project:
        effective_project = resolve_project_from_cwd()
    if not effective_project:
        error("No project specified and no linked project for current directory")
        info("Either use --project/-p or link this directory: sibyl project link <project_id>")
        raise typer.Exit(1)

    @run_async
    async def _create() -> None:
        client = get_client()

        try:
            tag_list = [t.strip() for t in tags.split(",")] if tags else None
            assignee_list = [assignee] if assignee else None

            # Build metadata
            metadata: dict = {
                "project_id": effective_project,
                "priority": priority,
                "status": "planning",
            }
            if assignee_list:
                metadata["assignees"] = assignee_list
            if tag_list:
                metadata["tags"] = tag_list

            response = await client.create_entity(
                name=title,
                content=description or title,
                entity_type="epic",
                metadata=metadata,
                sync=sync,
            )

            if json_out:
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
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Start working on an epic (moves to 'in_progress' status). Default: table output."""

    @run_async
    async def _start() -> None:
        client = get_client()

        try:
            resolved_id = _validate_epic_id(epic_id)

            # Build update data - status/assignees go in metadata
            metadata: dict = {"status": "in_progress"}
            if assignee:
                metadata["assignees"] = [assignee]

            response = await client.update_entity(resolved_id, metadata=metadata)

            if json_out:
                print_json(response)
                return

            if response.get("success") or response.get("id"):
                success(f"Epic started: {epic_id}")
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
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Complete an epic. Default: table output."""

    @run_async
    async def _complete() -> None:
        client = get_client()

        try:
            resolved_id = _validate_epic_id(epic_id)

            # Build update data - status/learnings go in metadata
            metadata: dict = {"status": "completed"}
            if learnings:
                metadata["learnings"] = learnings

            response = await client.update_entity(resolved_id, metadata=metadata)

            if json_out:
                print_json(response)
                return

            if response.get("success") or response.get("id"):
                success(f"Epic completed: {epic_id}")
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
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Archive an epic (terminal state). Default: table output."""

    @run_async
    async def _archive() -> None:
        client = get_client()

        try:
            resolved_id = _validate_epic_id(epic_id)

            # Build update data - status/learnings go in metadata
            metadata: dict = {"status": "archived"}
            if reason:
                metadata["learnings"] = f"Archived: {reason}"

            response = await client.update_entity(resolved_id, metadata=metadata)

            if json_out:
                print_json(response)
                return

            if response.get("success") or response.get("id"):
                success(f"Epic archived: {resolved_id}")
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
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Update epic fields directly. Default: table output."""

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

            resolved_id = _validate_epic_id(epic_id)

            # Build update data - status/priority/assignees go in metadata
            updates: dict = {}
            metadata: dict = {}

            if status:
                metadata["status"] = status
            if priority:
                metadata["priority"] = priority
            if assignee:
                metadata["assignees"] = [assignee]

            # Top-level fields supported by EntityUpdate schema
            if title:
                updates["name"] = title
            if tags:
                updates["tags"] = [t.strip() for t in tags.split(",")]
            if metadata:
                updates["metadata"] = metadata

            response = await client.update_entity(resolved_id, **updates)

            if json_out:
                print_json(response)
                return

            if response.get("success") or response.get("id"):
                success(f"Epic updated: {resolved_id}")
                info(f"Fields: {', '.join(updates.keys())}")
            else:
                error(f"Failed to update epic: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _update()


@app.command("roadmap")
def roadmap(
    project: Annotated[str | None, typer.Option("-p", "--project", help="Project ID")] = None,
    status: Annotated[
        str | None,
        typer.Option("-s", "--status", help="Filter epics: planning|in_progress|blocked|completed"),
    ] = None,
    include_done: Annotated[
        bool, typer.Option("--include-done", help="Include completed tasks in output")
    ] = False,
    output: Annotated[
        str | None, typer.Option("-o", "--output", help="Output file (default: stdout)")
    ] = None,
) -> None:
    """Generate a markdown roadmap document from epics and tasks.

    Creates a comprehensive summary including:
    - Project overview with epic counts
    - Each epic with progress bars and status
    - Tasks grouped by status (todo, doing, blocked, review, done)
    - Learnings extracted from completed tasks

    Example:
        sibyl epic roadmap                    # Current project roadmap
        sibyl epic roadmap -o roadmap.md      # Save to file
        sibyl epic roadmap --include-done     # Include completed tasks
    """
    from datetime import datetime

    # Auto-resolve project from context if not explicitly set
    effective_project = project
    if not project:
        effective_project = resolve_project_from_cwd()

    @run_async
    async def _roadmap() -> None:
        client = get_client()

        try:
            # Get project info if we have one
            project_name = effective_project or "All Projects"
            if effective_project:
                try:
                    proj = await client.get_entity(effective_project)
                    project_name = proj.get("name", effective_project)
                except Exception:
                    pass

            # Get all epics
            response = await client.explore(
                mode="list",
                types=["epic"],
                status=status,
                project=effective_project,
                limit=100,
            )
            epics = response.get("entities", [])

            # Filter by project if needed
            if effective_project:
                epics = [
                    e for e in epics if e.get("metadata", {}).get("project_id") == effective_project
                ]

            # Sort by priority: critical > high > medium > low > someday
            priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "someday": 4}
            epics.sort(
                key=lambda e: priority_order.get(e.get("metadata", {}).get("priority", "medium"), 2)
            )

            # Build markdown document
            lines: list[str] = []
            lines.append(f"# {project_name} Roadmap")
            lines.append("")
            lines.append(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*")
            lines.append("")

            # Summary stats
            total_epics = len(epics)
            planning = sum(1 for e in epics if e.get("metadata", {}).get("status") == "planning")
            in_progress = sum(
                1 for e in epics if e.get("metadata", {}).get("status") == "in_progress"
            )
            completed = sum(1 for e in epics if e.get("metadata", {}).get("status") == "completed")

            lines.append("## Overview")
            lines.append("")
            lines.append("| Status | Count |")
            lines.append("|--------|-------|")
            lines.append(f"| Planning | {planning} |")
            lines.append(f"| In Progress | {in_progress} |")
            lines.append(f"| Completed | {completed} |")
            lines.append(f"| **Total** | **{total_epics}** |")
            lines.append("")

            if not epics:
                lines.append("*No epics found.*")
            else:
                lines.append("## Epics")
                lines.append("")

                for epic in epics:
                    epic_id = epic.get("id", "")
                    epic_name = epic.get("name", "Untitled")
                    epic_desc = epic.get("description", "")
                    meta = epic.get("metadata", {})
                    epic_status = meta.get("status", "planning")
                    epic_priority = meta.get("priority", "medium")
                    total_tasks = meta.get("total_tasks", 0)
                    completed_tasks = meta.get("completed_tasks", 0)
                    pct = round((completed_tasks / total_tasks * 100) if total_tasks > 0 else 0)

                    # Epic header with status badge
                    status_emoji = {
                        "planning": "📋",
                        "in_progress": "🚧",
                        "blocked": "🚫",
                        "completed": "✅",
                        "archived": "📦",
                    }.get(epic_status, "📋")

                    priority_badge = {
                        "critical": "🔴",
                        "high": "🟠",
                        "medium": "🟡",
                        "low": "🟢",
                        "someday": "⚪",
                    }.get(epic_priority, "🟡")

                    lines.append(f"### {status_emoji} {epic_name}")
                    lines.append("")
                    lines.append(
                        f"**ID:** `{epic_id}` | **Priority:** {priority_badge} {epic_priority} | **Status:** {epic_status}"
                    )
                    lines.append("")

                    if epic_desc:
                        lines.append(f"> {epic_desc}")
                        lines.append("")

                    # Progress bar
                    if total_tasks > 0:
                        filled = int(pct / 5)  # 20 chars = 100%
                        bar = "█" * filled + "░" * (20 - filled)
                        lines.append(
                            f"**Progress:** `[{bar}]` {completed_tasks}/{total_tasks} ({pct}%)"
                        )
                        lines.append("")

                    # Get tasks for this epic
                    tasks_response = await client.explore(
                        mode="list",
                        types=["task"],
                        epic=epic_id,
                        limit=100,
                    )
                    tasks = tasks_response.get("entities", [])

                    if tasks:
                        # Group by status
                        by_status: dict[str, list] = {
                            "todo": [],
                            "doing": [],
                            "blocked": [],
                            "review": [],
                            "done": [],
                        }
                        for t in tasks:
                            t_status = t.get("metadata", {}).get("status", "todo")
                            if t_status in by_status:
                                by_status[t_status].append(t)
                            else:
                                by_status["todo"].append(t)

                        # Output tasks by status
                        for task_status, task_list in by_status.items():
                            if not task_list:
                                continue
                            if task_status == "done" and not include_done:
                                lines.append(
                                    f"**Done:** {len(task_list)} task(s) *(use --include-done to show)*"
                                )
                                continue

                            status_label = {
                                "todo": "📝 To Do",
                                "doing": "🔨 In Progress",
                                "blocked": "🚫 Blocked",
                                "review": "👀 In Review",
                                "done": "✅ Completed",
                            }.get(task_status, task_status)

                            lines.append(f"#### {status_label}")
                            lines.append("")

                            for t in task_list:
                                t_name = t.get("name", "Untitled")
                                t_id = t.get("id", "")
                                t_meta = t.get("metadata", {})
                                t_priority = t_meta.get("priority", "medium")

                                priority_marker = {
                                    "critical": "🔴",
                                    "high": "🟠",
                                    "medium": "",
                                    "low": "",
                                    "someday": "",
                                }.get(t_priority, "")

                                checkbox = "x" if task_status == "done" else " "
                                lines.append(f"- [{checkbox}] {priority_marker}{t_name} (`{t_id}`)")

                                # Include learnings for completed tasks
                                if task_status == "done" and t_meta.get("learnings"):
                                    learnings = t_meta["learnings"]
                                    # Truncate long learnings
                                    if len(learnings) > 200:
                                        learnings = learnings[:200] + "..."
                                    lines.append(f"  - 💡 *{learnings}*")

                            lines.append("")

                    lines.append("---")
                    lines.append("")

            # Aggregate learnings section
            lines.append("## Key Learnings")
            lines.append("")

            all_learnings: list[tuple[str, str, str]] = []  # (epic_name, task_name, learning)
            for epic in epics:
                epic_name = epic.get("name", "")
                tasks_response = await client.explore(
                    mode="list",
                    types=["task"],
                    epic=epic.get("id", ""),
                    limit=100,
                )
                for t in tasks_response.get("entities", []):
                    t_meta = t.get("metadata", {})
                    if t_meta.get("learnings") and t_meta.get("status") == "done":
                        all_learnings.append((epic_name, t.get("name", ""), t_meta["learnings"]))

            if all_learnings:
                for epic_name, task_name, learning in all_learnings[:20]:  # Limit to 20
                    lines.append(f"- **{task_name}** ({epic_name})")
                    lines.append(f"  > {learning}")
                    lines.append("")
            else:
                lines.append("*No learnings captured yet.*")
                lines.append("")

            # Join and output
            content = "\n".join(lines)

            if output:
                with open(output, "w") as f:
                    f.write(content)
                success(f"Roadmap written to {output}")
            else:
                console.print(content)

        except SibylClientError as e:
            _handle_client_error(e)

    _roadmap()


@app.command("tasks")
def list_epic_tasks(
    epic_id: Annotated[str, typer.Argument(help="Epic ID to list tasks for")],
    status: Annotated[
        str | None, typer.Option("-s", "--status", help="Filter by task status")
    ] = None,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max results")] = 50,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """List tasks belonging to an epic. Default: table output."""

    @run_async
    async def _list_tasks() -> None:
        client = get_client()

        try:
            resolved_id = _validate_epic_id(epic_id)

            # Use epic filter to get tasks with full metadata
            response = await client.explore(
                mode="list",
                types=["task"],
                epic=resolved_id,
                status=status,
                limit=limit,
            )

            entities = response.get("entities", [])

            if json_out:
                print_json(entities)
                return

            # Table format
            if not entities:
                info(f"No tasks found for epic {epic_id}")
                return

            from sibyl_cli.common import format_status

            table = create_table("Tasks", "ID", "Title", "Status", "Priority", "Assignees")
            for e in entities:
                meta = e.get("metadata", {})
                table.add_row(
                    e.get("id", ""),
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
