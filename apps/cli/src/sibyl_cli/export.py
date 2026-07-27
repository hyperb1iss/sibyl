"""Materialize Sibyl memory into repo-local files.

`sibyl export memory` renders the graph into `.sibyl/memory/` so a
filesystem-native agent can grep the project's memory with no server in the
loop. The projection itself lives in `sibyl_core.export.memory_files`; this
module only decides what to fetch and where to put it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    NEON_CYAN,
    console,
    error,
    handle_client_error,
    info,
    run_async,
    success,
)
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.project_refs import resolve_project_reference
from sibyl_core.export import (
    MANIFEST_PATH,
    MemorySnapshot,
    build_memory_files_bundle,
    memory_record_from_entity,
    validate_memory_files_bundle,
    write_memory_files_bundle,
)

app = typer.Typer(
    name="export",
    help="Materialize memory into files agents can grep",
    no_args_is_help=True,
)

DEFAULT_OUTPUT = Path(".sibyl/memory")
DEFAULT_NOTE_TYPES = (
    "decision",
    "pattern",
    "rule",
    "guide",
    "procedure",
    "error_pattern",
    "plan",
    "preference",
    "claim",
    "template",
)
DEFAULT_TASK_STATUS = "doing,blocked"
EXPLORE_MAX_LIMIT = 200


async def _fetch_project(client: Any, project_id: str) -> dict[str, Any]:
    try:
        entity = await client.get_entity(project_id)
    except SibylClientError as exc:
        if exc.status_code == 404:
            return {}
        raise
    return entity if isinstance(entity, dict) else {}


async def _fetch_entities(client: Any, **filters: Any) -> list[dict[str, Any]]:
    response = await client.explore(mode="list", **filters)
    entities = response.get("entities") if isinstance(response, dict) else None
    return [entity for entity in entities or [] if isinstance(entity, dict)]


def _round_robin(pools: list[list[dict[str, Any]]], budget: int) -> list[dict[str, Any]]:
    """Interleave per-type pools so every type reaches the projection.

    A single multi-type query spends the whole budget on whichever type the
    backend happens to order first, which is how a 40-item export ends up as 40
    decisions and nothing else.
    """
    selected: list[dict[str, Any]] = []
    depth = 0
    while len(selected) < budget and any(len(pool) > depth for pool in pools):
        for pool in pools:
            if len(selected) >= budget:
                break
            if depth < len(pool):
                selected.append(pool[depth])
        depth += 1
    return selected


async def _hydrate(client: Any, entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Refetch each entity so the files carry its full body.

    List responses truncate the body at 500 characters; the whole point of the
    projection is that an agent never has to come back to the server for the
    rest, so an export without this ships previews pretending to be memories.
    """
    hydrated: list[dict[str, Any]] = []
    for entity in entities:
        entity_id = str(entity.get("id") or "")
        if not entity_id:
            hydrated.append(entity)
            continue
        try:
            full = await client.get_entity(entity_id)
        except SibylClientError:
            hydrated.append(entity)
            continue
        hydrated.append({**entity, **full} if isinstance(full, dict) else entity)
    return hydrated


async def _fetch_notes(
    client: Any,
    *,
    project_id: str,
    note_types: tuple[str, ...],
    note_limit: int,
    hydrate: bool,
) -> list[dict[str, Any]]:
    if not note_limit or not note_types:
        return []
    pools = [
        await _fetch_entities(
            client,
            types=[entity_type],
            project=project_id,
            limit=min(note_limit, EXPLORE_MAX_LIMIT),
        )
        for entity_type in note_types
    ]
    selected = _round_robin(pools, note_limit)
    return await _hydrate(client, selected) if hydrate else selected


async def _build_snapshot(
    *,
    project_id: str,
    note_types: tuple[str, ...],
    note_limit: int,
    task_limit: int,
    task_status: str,
    hydrate: bool,
) -> MemorySnapshot:
    async with get_client() as client:
        project = await _fetch_project(client, project_id)
        notes = await _fetch_notes(
            client,
            project_id=project_id,
            note_types=note_types,
            note_limit=note_limit,
            hydrate=hydrate,
        )
        tasks: list[dict[str, Any]] = []
        if task_limit:
            tasks = await _fetch_entities(
                client,
                types=["task"],
                project=project_id,
                status=task_status,
                limit=min(task_limit, EXPLORE_MAX_LIMIT),
            )
            if hydrate:
                tasks = await _hydrate(client, tasks)

    return MemorySnapshot(
        project_id=project_id,
        project_name=str(project.get("name")) if project.get("name") else None,
        project_description=str(project.get("description")) if project.get("description") else None,
        notes=tuple(memory_record_from_entity(entity) for entity in notes),
        tasks=tuple(memory_record_from_entity(entity) for entity in tasks),
    )


@app.command("memory")
def export_memory(
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Project id or name (defaults to the linked project)"),
    ] = None,
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Directory to materialize into"),
    ] = DEFAULT_OUTPUT,
    note_limit: Annotated[
        int,
        typer.Option("--notes", min=0, max=EXPLORE_MAX_LIMIT, help="Maximum memories to include"),
    ] = 60,
    task_limit: Annotated[
        int,
        typer.Option("--tasks", min=0, max=EXPLORE_MAX_LIMIT, help="Maximum open tasks to include"),
    ] = 25,
    task_status: Annotated[
        str,
        typer.Option("--task-status", help="Comma-separated task statuses to materialize"),
    ] = DEFAULT_TASK_STATUS,
    types: Annotated[
        list[str] | None,
        typer.Option("--type", "-T", help="Memory entity type to include (repeatable)"),
    ] = None,
    hydrate: Annotated[
        bool,
        typer.Option(
            "--hydrate/--no-hydrate",
            help="Refetch each entity for its full body instead of the 500-character preview",
        ),
    ] = True,
    writable: Annotated[
        bool,
        typer.Option("--writable", help="Leave files writable instead of read-only"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Write into a populated directory that is not an export"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Print the export receipt as JSON"),
    ] = False,
) -> None:
    """Materialize a project's memory into greppable files.

    Deterministic by construction: re-running against an unchanged graph
    rewrites byte-identical files, so the directory is safe to commit.
    """
    note_types = tuple(dict.fromkeys(types)) if types else DEFAULT_NOTE_TYPES

    @run_async
    async def run_export() -> None:
        try:
            project_id = await _resolve_project(project)
            snapshot = await _build_snapshot(
                project_id=project_id,
                note_types=note_types,
                note_limit=note_limit,
                task_limit=task_limit,
                task_status=task_status,
                hydrate=hydrate,
            )
        except SibylClientError as exc:
            handle_client_error(exc)
            return
        except ValueError as exc:
            error(str(exc))
            raise typer.Exit(code=1) from exc

        bundle = build_memory_files_bundle(snapshot)
        try:
            write_memory_files_bundle(bundle, output, read_only=not writable, force=force)
        except OSError as exc:
            error(str(exc))
            raise typer.Exit(code=1) from exc

        if validation_errors := validate_memory_files_bundle(output):
            error("Memory export validation failed")
            for validation_error in validation_errors:
                error(f"- {validation_error}")
            raise typer.Exit(code=1)

        receipt = {
            "project_id": project_id,
            "project_name": snapshot.project_name,
            "output": str(output),
            "notes": len(snapshot.notes),
            "tasks": len(snapshot.tasks),
            "handbook": snapshot.handbook is not None,
            "files": len(bundle.files),
            "content_digest": bundle.content_digest,
            "read_only": not writable,
            "hydrated": hydrate,
        }
        if json_output:
            console.print_json(json.dumps(receipt))
            return

        success(f"Materialized memory to {output}")
        info(
            f"{len(bundle.files)} files - {len(snapshot.notes)} memories, "
            f"{len(snapshot.tasks)} open tasks"
        )
        console.print(f"[{NEON_CYAN}]digest[/] {bundle.content_digest[:16]}")
        console.print(f"[{NEON_CYAN}]grep[/]   rg 'your term' {output}")
        console.print(f"[{NEON_CYAN}]verify[/] cat {output / MANIFEST_PATH}")

    run_export()


async def _resolve_project(reference: str | None) -> str:
    if reference:
        async with get_client() as client:
            return await resolve_project_reference(client, reference)

    linked = resolve_project_from_cwd()
    if not linked:
        msg = "No project linked to this directory. Pass --project or run: sibyl project link"
        raise ValueError(msg)
    return linked


__all__ = ["app", "export_memory"]
