"""Task management CLI commands.

Commands for the full task lifecycle: list, show, create, start, block,
unblock, review, complete, archive, update.

All commands communicate with the REST API to ensure proper event broadcasting.
All commands output table format by default. Use --json for JSON output.
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
    format_status,
    handle_client_error,
    info,
    pagination_hint,
    print_json,
    run_async,
    success,
)
from sibyl_cli.config_store import resolve_project_from_cwd

app = typer.Typer(
    name="task",
    help="Task lifecycle management",
    no_args_is_help=True,
)


# Use centralized handler from common.py
_handle_client_error = handle_client_error


def _output_response(response: dict, json_out: bool, success_msg: str | None = None) -> None:
    """Output response as JSON or table message."""
    if json_out:
        print_json(response)
    elif success_msg and response.get("success"):
        success(success_msg)
    elif not response.get("success"):
        error(f"Failed: {response.get('message', 'Unknown error')}")


def _validate_task_id(task_id: str) -> str:
    """Validate that a task ID has the expected format.

    Full task IDs are required - no prefix matching or guessing.
    Format: task_<12 hex chars> (17 chars total)

    Args:
        task_id: The task ID to validate.

    Returns:
        The task ID unchanged.

    Raises:
        SibylClientError: If the ID format is invalid.
    """
    if not task_id.startswith("task_"):
        raise SibylClientError(
            f"Invalid task ID format: {task_id}. Expected format: task_<12 hex chars>",
            status_code=400,
            detail=f"Invalid task ID: {task_id}",
        )
    if len(task_id) < 17:
        raise SibylClientError(
            f"Task ID too short: {task_id}. Full task ID required (17 chars).",
            status_code=400,
            detail=f"Full task ID required, got: {task_id}",
        )
    return task_id


def _apply_task_filters(
    entities: list[dict],
    status: str | None,
    priority: str | None,
    complexity: str | None,
    feature: str | None,
    tags: str | None,
    project: str | None,
    epic: str | None,
    no_epic: bool,
    assignee: str | None,
) -> list[dict]:
    """Apply client-side filters to task entities."""
    result = entities

    if status:
        # Support comma-separated statuses (e.g., "todo,doing")
        status_list = [s.strip() for s in status.split(",")]
        result = [e for e in result if e.get("metadata", {}).get("status") in status_list]

    if priority:
        # Support comma-separated priorities (e.g., "critical,high")
        priority_list = [p.strip().lower() for p in priority.split(",")]
        result = [
            e for e in result if e.get("metadata", {}).get("priority", "").lower() in priority_list
        ]

    if complexity:
        # Support comma-separated complexities (e.g., "simple,medium")
        complexity_list = [c.strip().lower() for c in complexity.split(",")]
        result = [
            e
            for e in result
            if e.get("metadata", {}).get("complexity", "").lower() in complexity_list
        ]

    if feature:
        result = [
            e
            for e in result
            if (e.get("metadata", {}).get("feature") or "").lower() == feature.lower()
        ]

    if tags:
        # Match if ANY tag matches
        tag_list = [t.strip().lower() for t in tags.split(",")]
        result = [
            e
            for e in result
            if any(t.lower() in tag_list for t in e.get("metadata", {}).get("tags", []))
        ]

    if project:
        result = [e for e in result if e.get("metadata", {}).get("project_id") == project]

    if epic:
        result = [e for e in result if e.get("metadata", {}).get("epic_id") == epic]

    if no_epic:
        result = [e for e in result if not e.get("metadata", {}).get("epic_id")]

    if assignee:
        result = [
            e
            for e in result
            if assignee.lower() in str(e.get("metadata", {}).get("assignees", [])).lower()
        ]

    return result


def _output_tasks_csv(entities: list[dict]) -> None:
    """Output tasks as CSV to stdout."""
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


def _output_tasks_table(
    entities: list[dict],
    effective_offset: int,
    effective_limit: int,
    has_more: bool,
    total: int,
) -> None:
    """Output tasks as a formatted table."""
    if not entities:
        info("No tasks found")
        return

    table = create_table("Tasks", "ID", "Title", "Status", "Priority", "Assignees")
    # ID, Status, Priority, Assignees are fixed-width; Title gets the rest
    table.columns[0].no_wrap = True  # ID
    table.columns[2].no_wrap = True  # Status
    table.columns[3].no_wrap = True  # Priority
    table.columns[4].no_wrap = True  # Assignees
    # Title column auto-sizes and can wrap if needed

    for e in entities:
        meta = e.get("metadata", {})
        table.add_row(
            e.get("id", ""),
            e.get("name", ""),  # Full title, no truncation
            format_status(meta.get("status", "unknown")),
            format_priority(meta.get("priority", "medium")),
            ", ".join(meta.get("assignees", []))[:20] or "-",
        )

    console.print(table)

    # Pagination info
    start = effective_offset + 1
    end = effective_offset + len(entities)
    if has_more:
        next_page = (effective_offset // effective_limit) + 2
        console.print(
            f"\n[dim]Showing {start}-{end} of {total}+ task(s) (--page {next_page} for more)[/dim]"
        )
    else:
        console.print(f"\n[dim]Showing {len(entities)} task(s)[/dim]")


@app.command("list")
def list_tasks(
    query: Annotated[
        str | None, typer.Option("-q", "--query", help="Search query (name/description)")
    ] = None,
    status: Annotated[
        str | None,
        typer.Option(
            "-s", "--status", help="Filter by status (comma-separated: todo,doing,blocked)"
        ),
    ] = None,
    priority: Annotated[
        str | None,
        typer.Option(
            "--priority",
            help="Filter by priority (comma-separated: critical,high,medium,low,someday)",
        ),
    ] = None,
    complexity: Annotated[
        str | None,
        typer.Option(
            "--complexity",
            help="Filter by complexity (comma-separated: trivial,simple,medium,complex,epic)",
        ),
    ] = None,
    feature: Annotated[
        str | None,
        typer.Option("-f", "--feature", help="Filter by feature area"),
    ] = None,
    tags: Annotated[
        str | None,
        typer.Option("--tags", help="Filter by tags (comma-separated, matches ANY)"),
    ] = None,
    project: Annotated[str | None, typer.Option("-p", "--project", help="Project ID")] = None,
    epic: Annotated[str | None, typer.Option("-e", "--epic", help="Epic ID to filter by")] = None,
    no_epic: Annotated[
        bool, typer.Option("--no-epic", help="Filter for tasks without an epic")
    ] = False,
    assignee: Annotated[str | None, typer.Option("-a", "--assignee", help="Assignee")] = None,
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max results (max: 200)")] = 50,
    offset: Annotated[int, typer.Option("--offset", help="Skip first N results")] = 0,
    page: Annotated[
        int | None, typer.Option("--page", help="Page number (1-based, uses limit)")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
    csv_out: Annotated[bool, typer.Option("--csv", help="CSV output")] = False,
    all_projects: Annotated[
        bool, typer.Option("--all", "-A", help="Ignore context, list from all projects")
    ] = False,
) -> None:
    """List tasks with optional filters. Use -q for semantic search. Default: table output.

    Auto-scopes to current project context unless --all is specified.

    Pagination: Use --limit (max 200) and --offset, or --page for convenience.
    """
    fmt = "json" if json_out else ("csv" if csv_out else "table")

    # Clamp limit to API maximum
    effective_limit = min(limit, 200)

    # Calculate offset from page if provided
    effective_offset = offset
    if page is not None:
        if page < 1:
            error("--page must be >= 1")
            raise typer.Exit(1)
        effective_offset = (page - 1) * effective_limit

    # Auto-resolve project from context if not explicitly set
    effective_project = project
    if not project and not all_projects:
        effective_project = resolve_project_from_cwd()

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            # Use semantic search if query provided, otherwise use explore
            if query:
                response = await client.search(
                    query=query,
                    types=["task"],
                    project=effective_project,
                    limit=effective_limit,
                    offset=effective_offset,
                )
                # Search returns results directly
                entities = response.get("results", [])
                has_more = response.get("has_more", False)
                total = response.get("total", len(entities))
            else:
                # All filtering handled by backend (supports comma-separated values)
                api_status = status
                api_priority = priority
                api_complexity = complexity
                api_tags = tags

                response = await client.explore(
                    mode="list",
                    types=["task"],
                    status=api_status,
                    priority=api_priority,
                    complexity=api_complexity,
                    feature=feature,
                    tags=api_tags,
                    project=effective_project,
                    epic=epic,
                    no_epic=no_epic,
                    limit=effective_limit,
                    offset=effective_offset,
                )
                entities = response.get("entities", [])
                has_more = response.get("has_more", False)
                total = response.get("actual_total") or response.get("total", len(entities))

            # Client-side filters (needed for search, or when API doesn't filter)
            entities = _apply_task_filters(
                entities,
                status,
                priority,
                complexity,
                feature,
                tags,
                effective_project,
                epic,
                no_epic,
                assignee,
            )

            if fmt == "json":
                print_json(entities)
                pagination_hint(
                    effective_offset, len(entities), total, has_more, effective_limit, "task"
                )
            elif fmt == "csv":
                _output_tasks_csv(entities)
            else:
                _output_tasks_table(entities, effective_offset, effective_limit, has_more, total)

        except SibylClientError as e:
            _handle_client_error(e)

    _list()


def _display_task_panel(entity: dict) -> None:
    """Display a task entity as a rich panel with related entities."""
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
            f"[{ELECTRIC_PURPLE}]Project:[/{ELECTRIC_PURPLE}] {meta['project_id']}",
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

    # Show related entities
    related_entities = entity.get("related", [])
    if related_entities:
        lines.append(f"\n[{NEON_CYAN}]Related:[/{NEON_CYAN}]")
        for rel in related_entities:
            direction = "→" if rel.get("direction") == "outgoing" else "←"
            lines.append(
                f"  [{CORAL}]{rel.get('relationship', '')}[/{CORAL}] {direction} "
                f"[{ELECTRIC_PURPLE}]{rel.get('entity_type', '')}[/{ELECTRIC_PURPLE}]: "
                f"{rel.get('name', '')} [{CORAL}]{rel.get('id', '')}[/{CORAL}]"
            )

    panel = create_panel("\n".join(lines), title=f"Task {entity.get('id', '')}")
    console.print(panel)


@app.command("show")
def show_task(
    task_id: Annotated[str, typer.Argument(help="Task ID (full ID required)")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show detailed task information with related context. Default: table output."""

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            # Validate full task ID (no prefix matching)
            resolved_id = _validate_task_id(task_id)

            entity = await client.get_entity(resolved_id)

            # JSON output (default)
            if json_out:
                print_json(entity)
                return

            _display_task_panel(entity)

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("start")
def start_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to start (full ID required)")],
    assignee: Annotated[str | None, typer.Option("--assignee", "-a", help="Assignee name")] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Start working on a task (moves to 'doing' status). Default: table output."""

    @run_async
    async def _start() -> None:
        client = get_client()

        try:
            # Validate full task ID (no prefix matching)
            resolved_id = _validate_task_id(task_id)

            response = await client.start_task(resolved_id, assignee)

            if json_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task started: {task_id}")
                branch = response.get("data", {}).get("branch_name")
                if branch:
                    info(f"Branch: {branch}")
                # Fetch and display the task with full context
                entity = await client.get_entity(resolved_id)
                console.print()
                _display_task_panel(entity)
            else:
                error(f"Failed to start task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _start()


@app.command("block")
def block_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to block (full ID required)")],
    reason: Annotated[str, typer.Option("--reason", "-r", help="Blocker reason (required)")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Mark a task as blocked with a reason. Default: table output."""

    @run_async
    async def _block() -> None:
        client = get_client()

        try:
            # Validate full task ID (no prefix matching)
            resolved_id = _validate_task_id(task_id)

            response = await client.block_task(resolved_id, reason)

            if json_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task blocked: {task_id}")
            else:
                error(f"Failed to block task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _block()


@app.command("unblock")
def unblock_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to unblock (full ID required)")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Resume a blocked task (moves back to 'doing'). Default: table output."""

    @run_async
    async def _unblock() -> None:
        client = get_client()

        try:
            # Validate full task ID (no prefix matching)
            resolved_id = _validate_task_id(task_id)

            response = await client.unblock_task(resolved_id)

            if json_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task unblocked: {task_id}")
            else:
                error(f"Failed to unblock task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _unblock()


@app.command("review")
def submit_review(
    task_id: Annotated[str, typer.Argument(help="Task ID to submit for review (full ID required)")],
    pr_url: Annotated[str | None, typer.Option("--pr", help="Pull request URL")] = None,
    commits: Annotated[
        str | None, typer.Option("--commits", "-c", help="Comma-separated commit SHAs")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Submit a task for review. Default: table output."""

    @run_async
    async def _review() -> None:
        client = get_client()

        try:
            # Validate full task ID (no prefix matching)
            resolved_id = _validate_task_id(task_id)
            commit_list = [c.strip() for c in commits.split(",")] if commits else None

            response = await client.submit_review(resolved_id, pr_url, commit_list)

            if json_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task submitted for review: {task_id}")
            else:
                error(f"Failed to submit for review: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _review()


@app.command("complete")
def complete_task(
    task_id: Annotated[str, typer.Argument(help="Task ID to complete (full ID required)")],
    hours: Annotated[float | None, typer.Option("--hours", "-h", help="Actual hours spent")] = None,
    learnings: Annotated[
        str | None, typer.Option("--learnings", "-l", help="Key learnings (creates episode)")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Complete a task and optionally capture learnings. Default: table output."""

    @run_async
    async def _complete() -> None:
        client = get_client()

        try:
            # Validate full task ID (no prefix matching)
            resolved_id = _validate_task_id(task_id)

            response = await client.complete_task(resolved_id, hours, learnings)

            if json_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task completed: {task_id}")
                if learnings:
                    info("Learning episode created from task")
            else:
                error(f"Failed to complete task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _complete()


@app.command("archive")
def archive_task(
    task_id: Annotated[str | None, typer.Argument(help="Task ID to archive")] = None,
    reason: Annotated[str | None, typer.Option("--reason", "-r", help="Archive reason")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    stdin: Annotated[
        bool, typer.Option("--stdin", help="Read task IDs from stdin (one per line)")
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Archive task(s). Supports --stdin for bulk operations.

    Examples:
        sibyl task archive task_xxx --yes
        sibyl task list -s todo -q "test" | jq -r '.[].id' | sibyl task archive --stdin --yes
    """
    import sys

    # Collect task IDs
    task_ids: list[str] = []

    if stdin:
        # Read from stdin
        for line in sys.stdin:
            line = line.strip()
            if line and line.startswith("task_"):
                task_ids.append(line)
        if not task_ids:
            error("No task IDs found on stdin")
            raise typer.Exit(1)
    elif task_id:
        task_ids = [task_id]
    else:
        error("Either task_id argument or --stdin is required")
        raise typer.Exit(1)

    # Require --yes for bulk operations (safety for multi-task archive)
    if len(task_ids) > 1 and not yes:
        error(f"Bulk archive requires --yes flag (found {len(task_ids)} tasks)")
        raise typer.Exit(1)

    @run_async
    async def _archive() -> None:
        client = get_client()
        results: list[dict] = []
        archived = 0
        failed = 0

        for tid in task_ids:
            try:
                resolved_id = _validate_task_id(tid)
                response = await client.archive_task(resolved_id, reason)
                results.append({"id": resolved_id, **response})
                if response.get("success"):
                    archived += 1
                else:
                    failed += 1
            except SibylClientError as e:
                results.append({"id": tid, "success": False, "error": str(e)})
                failed += 1

        if json_out:
            print_json(results if len(results) > 1 else results[0])
            return

        # Table output
        if len(task_ids) == 1:
            if results[0].get("success"):
                success(f"Task archived: {results[0]['id']}")
            else:
                error(f"Failed: {results[0].get('message', results[0].get('error', 'Unknown'))}")
        else:
            success(f"Archived {archived} task(s)")
            if failed:
                error(f"Failed: {failed} task(s)")

    _archive()


@app.command("create")
def create_task(
    title: Annotated[str, typer.Option("--title", help="Task title (required)")],
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project ID (auto-resolves from linked path)"),
    ] = None,
    description: Annotated[
        str | None, typer.Option("--description", "-d", help="Task description")
    ] = None,
    priority: Annotated[
        str, typer.Option("--priority", help="Priority: critical, high, medium, low, someday")
    ] = "medium",
    complexity: Annotated[
        str, typer.Option("--complexity", help="Complexity: trivial, simple, medium, complex, epic")
    ] = "medium",
    assignee: Annotated[
        str | None, typer.Option("--assignee", "-a", help="Initial assignee")
    ] = None,
    epic: Annotated[str | None, typer.Option("--epic", "-e", help="Epic ID to group under")] = None,
    feature: Annotated[str | None, typer.Option("--feature", "-f", help="Feature area")] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags")] = None,
    technologies: Annotated[
        str | None, typer.Option("--tech", help="Comma-separated technologies")
    ] = None,
    depends_on: Annotated[
        str | None, typer.Option("--depends-on", help="Comma-separated task IDs this depends on")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Create a new task in a project. Default: table output.

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
            tech_list = [t.strip() for t in technologies.split(",")] if technologies else None
            tag_list = [t.strip() for t in tags.split(",")] if tags else None
            assignee_list = [assignee] if assignee else None
            dep_list = [d.strip() for d in depends_on.split(",")] if depends_on else None

            response = await client.create_task(
                title=title,
                project_id=effective_project,
                description=description,
                priority=priority,
                complexity=complexity,
                assignees=assignee_list,
                epic_id=epic,
                feature=feature,
                tags=tag_list,
                technologies=tech_list,
                depends_on=dep_list,
            )

            if json_out:
                print_json(response)
                return

            task_id = response.get("task_id") or response.get("id")
            if response.get("success") or task_id:
                success(f"Task created: {task_id}")
                if assignee:
                    info(f"Assigned to: {assignee}")
                if dep_list:
                    info(f"Dependencies: {', '.join(dep_list)}")
            else:
                error(f"Failed to create task: {response.get('message', 'Unknown error')}")

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
    complexity: Annotated[
        str | None,
        typer.Option("--complexity", help="Complexity: trivial|simple|medium|complex|epic"),
    ] = None,
    title: Annotated[str | None, typer.Option("--title", help="Task title")] = None,
    description: Annotated[
        str | None, typer.Option("-d", "--description", help="Task description/content")
    ] = None,
    assignee: Annotated[str | None, typer.Option("-a", "--assignee", help="Assignee")] = None,
    epic: Annotated[str | None, typer.Option("-e", "--epic", help="Epic ID to group under")] = None,
    feature: Annotated[str | None, typer.Option("-f", "--feature", help="Feature area")] = None,
    tags: Annotated[
        str | None, typer.Option("--tags", help="Comma-separated tags (replaces existing)")
    ] = None,
    technologies: Annotated[
        str | None, typer.Option("--tech", help="Comma-separated technologies (replaces existing)")
    ] = None,
    add_dep: Annotated[
        str | None,
        typer.Option("--add-dep", help="Comma-separated task IDs to add as dependencies"),
    ] = None,
    remove_dep: Annotated[
        str | None,
        typer.Option("--remove-dep", help="Comma-separated task IDs to remove as dependencies"),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Update task fields directly. Default: table output."""

    @run_async
    async def _update() -> None:
        client = get_client()

        try:
            # Check we have something to update
            if not any(
                [
                    status,
                    priority,
                    complexity,
                    title,
                    description,
                    assignee,
                    epic,
                    feature,
                    tags,
                    technologies,
                    add_dep,
                    remove_dep,
                ]
            ):
                error(
                    "No fields to update. Use --status, --priority, --complexity, --title, "
                    "--description, --assignee, --epic, --feature, --tags, --tech, "
                    "--add-dep, or --remove-dep"
                )
                return

            resolved_id = _validate_task_id(task_id)
            assignees = [assignee] if assignee else None
            tag_list = [t.strip() for t in tags.split(",")] if tags else None
            tech_list = [t.strip() for t in technologies.split(",")] if technologies else None
            add_dep_list = [d.strip() for d in add_dep.split(",")] if add_dep else None
            remove_dep_list = [d.strip() for d in remove_dep.split(",")] if remove_dep else None

            response = await client.update_task(
                task_id=resolved_id,
                status=status,
                priority=priority,
                complexity=complexity,
                title=title,
                description=description,
                assignees=assignees,
                epic_id=epic,
                feature=feature,
                tags=tag_list,
                technologies=tech_list,
                add_depends_on=add_dep_list,
                remove_depends_on=remove_dep_list,
            )

            if json_out:
                print_json(response)
                return

            if response.get("success"):
                success(f"Task updated: {resolved_id}")
                info(f"Fields: {', '.join(response.get('data', {}).keys())}")
            else:
                error(f"Failed to update task: {response.get('message', 'Unknown error')}")

        except SibylClientError as e:
            _handle_client_error(e)

    _update()


# =============================================================================
# Task Notes Commands
# =============================================================================


@app.command("note")
def add_note(
    task_id: Annotated[str, typer.Argument(help="Task ID (full ID required)")],
    content: Annotated[str, typer.Argument(help="Note content")],
    agent: Annotated[
        bool, typer.Option("--agent", help="Mark as agent-authored (default: user)")
    ] = False,
    author: Annotated[
        str | None, typer.Option("--author", "-a", help="Author name/identifier")
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Add a note to a task.

    Examples:
        sibyl task note task_abc "Found the root cause"
        sibyl task note task_abc "Implementing fix" --agent --author claude
    """

    @run_async
    async def _note() -> None:
        client = get_client()

        try:
            resolved_id = _validate_task_id(task_id)
            author_type = "agent" if agent else "user"
            author_name = author or ""

            response = await client.create_note(resolved_id, content, author_type, author_name)

            if json_out:
                print_json(response)
                return

            if response.get("id"):
                success(f"Note added: {response['id']}")
            else:
                error("Failed to add note")

        except SibylClientError as e:
            _handle_client_error(e)

    _note()


@app.command("notes")
def list_notes(
    task_id: Annotated[str, typer.Argument(help="Task ID (full ID required)")],
    limit: Annotated[int, typer.Option("-n", "--limit", help="Max results")] = 20,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """List notes for a task.

    Example:
        sibyl task notes task_abc
    """

    @run_async
    async def _notes() -> None:
        client = get_client()

        try:
            resolved_id = _validate_task_id(task_id)

            response = await client.list_notes(resolved_id, limit)
            notes = response.get("notes", [])

            if json_out:
                print_json(notes)
                return

            if not notes:
                info("No notes for this task")
                return

            # Display notes in a readable format
            for note in notes:
                author_type = note.get("author_type", "user")
                author_name = note.get("author_name", "")
                created_at = note.get("created_at", "")[:19].replace("T", " ")

                # Icon based on author type
                icon = "🤖" if author_type == "agent" else "👤"
                author_display = f"{icon} {author_name}" if author_name else icon

                # Color based on author type
                color = NEON_CYAN if author_type == "agent" else ELECTRIC_PURPLE

                console.print(f"[{color}]{author_display}[/{color}] [dim]{created_at}[/dim]")
                console.print(f"  {note.get('content', '')}\n")

            console.print(f"[dim]{len(notes)} note(s)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _notes()
