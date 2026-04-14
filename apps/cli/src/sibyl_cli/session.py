"""Session context packaging CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_panel,
    format_priority,
    format_status,
    handle_client_error,
    print_json,
    run_async,
)
from sibyl_cli.config_store import (
    get_active_context,
    get_current_context,
    get_effective_project,
    get_effective_server_url,
)

app = typer.Typer(
    name="session",
    help="Package wake-up context for the current session",
)

SESSION_TASK_LIMIT = 5
SESSION_MEMORY_LIMIT = 3
SESSION_PREVIEW_CHARS = 180


def _format_preview(content: str, max_chars: int = SESSION_PREVIEW_CHARS) -> str:
    preview = content.strip()
    if preview.startswith("[") and "] " in preview:
        preview = preview.split("] ", 1)[1]
    preview = " ".join(preview.split())
    if len(preview) <= max_chars:
        return preview

    cutoff = preview.rfind(" ", 0, max_chars + 1)
    if cutoff < max_chars // 2:
        cutoff = max_chars
    return preview[:cutoff].rstrip() + "…"


def _summarize_task(task: dict[str, Any]) -> dict[str, Any]:
    meta = task.get("metadata", {})
    return {
        "id": task.get("id", ""),
        "name": task.get("name", ""),
        "status": meta.get("status", ""),
        "priority": meta.get("priority", ""),
        "feature": meta.get("feature"),
        "branch_name": meta.get("branch_name"),
    }


def _summarize_memory(entity: dict[str, Any]) -> dict[str, Any]:
    metadata = entity.get("metadata", {})
    return {
        "id": entity.get("id", ""),
        "name": entity.get("name", "Unknown"),
        "entity_type": entity.get("entity_type") or entity.get("type"),
        "source": entity.get("source"),
        "preview": _format_preview(entity.get("content", "")),
        "document_id": metadata.get("document_id"),
    }


def _derive_query(
    explicit_query: str | None,
    tasks: list[dict[str, Any]],
    project_name: str | None,
) -> str | None:
    if explicit_query:
        query = explicit_query.strip()
        return query or None

    task_titles = [task.get("name", "").strip() for task in tasks if task.get("name")]
    if task_titles:
        return " | ".join(task_titles[:2])[:140]

    if project_name:
        project = project_name.strip()
        return project or None

    return None


def _remember_next(
    tasks: list[dict[str, Any]],
    relevant_entities: list[dict[str, Any]],
    has_project: bool,
) -> str:
    blocked = next((task for task in tasks if task.get("status") == "blocked"), None)
    if blocked:
        return f"Unblock {blocked.get('name', 'the blocked task')} before you pick up new work."

    doing = next((task for task in tasks if task.get("status") == "doing"), None)
    if doing:
        return (
            f"Continue {doing.get('name', 'your active task')} and capture anything non-obvious "
            "with `sibyl capture`."
        )

    if relevant_entities:
        return (
            f"Review {relevant_entities[0].get('name', 'the top memory')} before you dive back in."
        )

    if has_project:
        return "No active tasks yet. Start one or capture the next useful learning."

    return "Link this directory to a project so session context stays scoped."


async def _build_session_bundle(
    query: str | None,
    task_limit: int,
    memory_limit: int,
    all_projects: bool,
) -> dict[str, Any]:
    active_context = get_active_context()
    linked_project, matched_path = get_current_context()
    effective_project = None if all_projects else get_effective_project()
    server_url = get_effective_server_url()

    context: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "server_url": server_url,
        "context_name": active_context.name if active_context else None,
        "org_slug": active_context.org_slug if active_context else None,
        "matched_path": matched_path,
        "project_id": effective_project,
        "project_name": None,
        "project_description": None,
        "scope": "all_projects" if all_projects else "project",
        "source": "path_mapping"
        if linked_project
        else ("active_context" if active_context else "legacy"),
    }

    async with get_client() as client:
        project_data: dict[str, Any] | None = None
        if effective_project:
            try:
                project_data = await client.get_entity(effective_project)
            except SibylClientError as exc:
                if exc.status_code != 404:
                    raise

        if project_data:
            context["project_name"] = project_data.get("name") or effective_project
            context["project_description"] = project_data.get("description")
        elif effective_project:
            context["project_name"] = effective_project

        tasks_response = await client.explore(
            mode="list",
            types=["task"],
            status="doing,blocked",
            project=effective_project,
            limit=task_limit,
        )
        tasks = [_summarize_task(task) for task in tasks_response.get("entities", [])][:task_limit]

        effective_query = _derive_query(query, tasks, context.get("project_name"))
        relevant_entities: list[dict[str, Any]] = []
        if effective_query and memory_limit > 0:
            search_response = await client.search(
                effective_query,
                project=None if all_projects else effective_project,
                limit=memory_limit + len(tasks),
            )
            task_ids = {task.get("id") for task in tasks}
            for result in search_response.get("results", []):
                if result.get("id") in task_ids:
                    continue
                entity_type = str(result.get("entity_type") or result.get("type") or "").lower()
                if entity_type in {"task", "project", "epic"}:
                    continue
                relevant_entities.append(_summarize_memory(result))
                if len(relevant_entities) >= memory_limit:
                    break

    remember_next = _remember_next(tasks, relevant_entities, bool(context.get("project_id")))
    return {
        "context": context,
        "query": effective_query,
        "tasks": tasks,
        "relevant_entities": relevant_entities,
        "remember_next": remember_next,
    }


def _render_bundle(bundle: dict[str, Any]) -> None:
    context = bundle.get("context", {})
    lines = [
        f"[{NEON_CYAN}]Server:[/{NEON_CYAN}] {context.get('server_url', 'unknown')}",
    ]

    if context.get("context_name"):
        lines.append(f"[{NEON_CYAN}]Context:[/{NEON_CYAN}] {context.get('context_name')}")
    if context.get("project_name"):
        lines.append(f"[{NEON_CYAN}]Project:[/{NEON_CYAN}] {context.get('project_name')}")
    elif context.get("project_id"):
        lines.append(f"[{NEON_CYAN}]Project:[/{NEON_CYAN}] {context.get('project_id')}")
    if context.get("matched_path"):
        lines.append(f"[{NEON_CYAN}]Path:[/{NEON_CYAN}] {context.get('matched_path')}")
    if bundle.get("query"):
        lines.append(f"[{NEON_CYAN}]Focus:[/{NEON_CYAN}] {bundle.get('query')}")

    tasks = bundle.get("tasks", [])
    if tasks:
        lines.append("")
        lines.append(f"[{ELECTRIC_PURPLE}]Active Tasks[/{ELECTRIC_PURPLE}]")
        for task in tasks:
            status = format_status(task.get("status", "unknown"))
            priority = format_priority(task.get("priority", "medium"))
            lines.append(
                f"  {status} {task.get('name', '')} {priority} [{CORAL}]{task.get('id', '')}[/{CORAL}]"
            )
    else:
        lines.append("")
        lines.append(f"[{ELECTRIC_PURPLE}]Active Tasks[/{ELECTRIC_PURPLE}]")
        lines.append("  [dim]No doing or blocked tasks.[/dim]")

    relevant_entities = bundle.get("relevant_entities", [])
    if relevant_entities:
        lines.append("")
        lines.append(f"[{ELECTRIC_PURPLE}]Relevant Memory[/{ELECTRIC_PURPLE}]")
        for entity in relevant_entities:
            lines.append(
                f"  [{NEON_CYAN}]{entity.get('name', 'Unknown')}[/{NEON_CYAN}] [{CORAL}]{entity.get('id', '')}[/{CORAL}]"
            )
            preview = entity.get("preview")
            if preview:
                lines.append(f"    [dim]{preview}[/dim]")

    lines.append("")
    lines.append(
        f"[{ELECTRIC_PURPLE}]Remember Next[/{ELECTRIC_PURPLE}] {bundle.get('remember_next', '')}"
    )

    console.print()
    console.print(create_panel("\n".join(lines), title="Session Bundle"))
    console.print()


@app.command("bundle")
def session_bundle(
    query: Annotated[
        str | None,
        typer.Argument(help="Optional focus query. Defaults to active task titles."),
    ] = None,
    task_limit: Annotated[
        int,
        typer.Option("--task-limit", min=1, max=20, help="Maximum active tasks to include"),
    ] = SESSION_TASK_LIMIT,
    memory_limit: Annotated[
        int,
        typer.Option(
            "--memory-limit",
            min=0,
            max=20,
            help="Maximum relevant memories to include",
        ),
    ] = SESSION_MEMORY_LIMIT,
    all_projects: Annotated[
        bool,
        typer.Option("--all", "-a", help="Search across all projects"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Package wake-up context for the current session."""

    @run_async
    async def _run() -> None:
        try:
            bundle = await _build_session_bundle(query, task_limit, memory_limit, all_projects)
        except SibylClientError as exc:
            handle_client_error(exc)
            return

        if json_output:
            print_json(bundle)
            return

        _render_bundle(bundle)

    _run()
