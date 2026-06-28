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
from sibyl_core.session_bundle import (
    derive_query,
    memory_dedupe_keys,
    summarize_memory,
    summarize_raw_memory,
    summarize_task,
)
from sibyl_core.session_bundle import (
    remember_next as build_remember_next,
)

app = typer.Typer(
    name="session",
    help="Package wake-up context for the current session",
)

SESSION_TASK_LIMIT = 5
SESSION_MEMORY_LIMIT = 3


def _append_unique_memory(
    memories: list[dict[str, Any]], memory: dict[str, Any], limit: int
) -> None:
    if len(memories) >= limit:
        return
    candidate_keys = memory_dedupe_keys(memory)
    if candidate_keys and any(
        candidate_keys & memory_dedupe_keys(existing) for existing in memories
    ):
        return
    memories.append(memory)


async def _append_raw_memories(
    *,
    client: Any,
    memories: list[dict[str, Any]],
    query: str,
    project: str | None,
    limit: int,
) -> None:
    scope_requests: list[tuple[str, str | None]] = [("private", None)]
    if project:
        scope_requests.append(("project", project))

    for memory_scope, scope_key in scope_requests:
        remaining = limit - len(memories)
        if remaining <= 0:
            break
        try:
            response = await client.recall_raw_memory(
                query=query,
                memory_scope=memory_scope,
                scope_key=scope_key,
                limit=remaining,
            )
        except SibylClientError:
            continue

        for memory in response.get("memories", []):
            if isinstance(memory, dict):
                _append_unique_memory(memories, summarize_raw_memory(memory), limit)


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
        tasks = [summarize_task(task) for task in tasks_response.get("entities", [])][:task_limit]

        effective_query = derive_query(query, tasks, context.get("project_name"))
        relevant_entities: list[dict[str, Any]] = []
        if effective_query and memory_limit > 0:
            await _append_raw_memories(
                client=client,
                memories=relevant_entities,
                query=effective_query,
                project=None if all_projects else effective_project,
                limit=memory_limit,
            )
            if len(relevant_entities) < memory_limit:
                search_response = await client.search(
                    effective_query,
                    project=None if all_projects else effective_project,
                    limit=memory_limit + len(tasks),
                    include_documents=False,
                    include_graph=True,
                )
                task_ids = {task.get("id") for task in tasks}
                for result in search_response.get("results", []):
                    if result.get("id") in task_ids:
                        continue
                    entity_type = str(result.get("entity_type") or result.get("type") or "").lower()
                    if entity_type in {"task", "project", "epic"}:
                        continue
                    _append_unique_memory(relevant_entities, summarize_memory(result), memory_limit)

    remember_next = build_remember_next(tasks, relevant_entities, bool(context.get("project_id")))
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
