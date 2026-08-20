"""Root commands for quick capture, graph addition, and task notes."""

from __future__ import annotations

import typer

from sibyl_cli import capture_support, command_support, memory_views
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    error,
    print_json,
    print_json_result,
    resolve_content_input,
    run_async,
    success,
)
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.id_resolution import resolve_id_prefix
from sibyl_core.models.entities import EntityType


def add_knowledge(
    title: str | None = typer.Argument(None, help="Title/name of the knowledge"),
    content: str | None = typer.Argument(None, help="Content/description"),
    title_option: str | None = typer.Option(None, "--title", help="Title/name of the knowledge"),
    content_option: str | None = typer.Option(None, "--content", help="Content/description"),
    content_file: str | None = typer.Option(None, "--content-file", help="Read content from file"),
    max_size: int = typer.Option(
        1_048_576,
        "--max-size",
        min=1,
        help="Maximum content file size in bytes",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Allow --content-file to read through symlinks",
    ),
    entity_type: str = typer.Option(
        "episode",
        "--type",
        "-t",
        callback=capture_support.normalize_add_type,
        help=capture_support.ENTITY_TYPE_HELP,
    ),
    category: str | None = typer.Option(None, "--category", "-c", help="Category"),
    language: str | None = typer.Option(None, "--language", "-l", help="Language"),
    tags: str | None = typer.Option(
        None,
        "--tags",
        help="Comma-separated browse-only metadata tags",
    ),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help=(
            "Comma-separated entity IDs. A bare ID links untyped; prefix an ID "
            "with supersedes:, contradicts:, requires:, supports:, or decides: "
            "to declare what this memory does to that one (this memory is the "
            "subject), which is what retrieval weights when it walks the graph"
        ),
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to connect with RELATED_TO edges",
    ),
    active_task: bool = typer.Option(
        True,
        "--active-task/--no-active-task",
        help="Auto-link to the single active task in the current project",
    ),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new entity is persisted and ready for direct retrieval",
    ),
    skip_conflicts: bool = typer.Option(
        False,
        "--skip-conflicts",
        "--no-conflict-check",
        help="Skip semantic duplicate/conflict detection",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Add knowledge to the graph."""
    resolved_title = (title_option or title or "").strip()
    try:
        resolved_content = (
            resolve_content_input(
                content_option if content_option is not None else content,
                content_file=content_file,
                max_size=max_size,
                follow_symlinks=follow_symlinks,
            )
            or ""
        ).strip()
    except ValueError as e:
        error(str(e))
        raise typer.Exit(code=1) from e
    if not resolved_title:
        error("Provide a title as an argument or with --title.")
        raise typer.Exit(code=1)
    if not resolved_content:
        error("Provide content as an argument, via stdin, or with --content-file.")
        raise typer.Exit(code=1)
    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    related_ids = command_support.parse_csv_ids(related_to)
    task_ids = command_support.parse_csv_ids(task)
    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_add() -> None:
        try:
            async with get_client() as client:
                data = await capture_support.write_memory_capture(
                    client,
                    title=resolved_title,
                    content=resolved_content,
                    kind=entity_type,
                    domain=category,
                    tags=parsed_tags,
                    related_ids=related_ids,
                    task_ids=task_ids,
                    active_task=active_task,
                    effective_project=effective_project,
                    capture_mode="add",
                    surface="cli",
                    wait_searchable=wait_searchable,
                    skip_conflicts=skip_conflicts,
                    languages=[language] if language else None,
                )

                if json_output:
                    print_json(data)
                    return

                memory_views.print_memory_capture_result(
                    title=resolved_title,
                    kind=entity_type,
                    data=data,
                    wait_searchable=wait_searchable,
                )
        except SibylClientError as e:
            command_support.handle_client_error(e)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(code=1) from e

    run_add()


def capture_memory(
    content: str | None = typer.Argument(
        None,
        help="What to capture. Reads stdin if omitted.",
    ),
    title: str | None = typer.Option(
        None,
        "--title",
        "-t",
        help="Optional title. Derived from content when omitted.",
    ),
    entity_type: str = typer.Option(
        "episode",
        "--type",
        callback=capture_support.normalize_add_type,
        help=capture_support.ENTITY_TYPE_HELP,
    ),
    tags: str | None = typer.Option(
        None,
        "--tags",
        help="Comma-separated browse-only metadata tags",
    ),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help=(
            "Comma-separated entity IDs. A bare ID links untyped; prefix an ID "
            "with supersedes:, contradicts:, requires:, supports:, or decides: "
            "to declare what this memory does to that one (this memory is the "
            "subject), which is what retrieval weights when it walks the graph"
        ),
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to connect with RELATED_TO edges",
    ),
    active_task: bool = typer.Option(
        True,
        "--active-task/--no-active-task",
        help="Auto-link to the single active task in the current project",
    ),
    content_file: str | None = typer.Option(None, "--content-file", help="Read content from file"),
    max_size: int = typer.Option(
        1_048_576,
        "--max-size",
        min=1,
        help="Maximum content file size in bytes",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Allow --content-file to read through symlinks",
    ),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new entity is persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Capture a quick memory without separate title and content fields."""

    try:
        resolved_content = (
            resolve_content_input(
                content,
                content_file=content_file,
                max_size=max_size,
                follow_symlinks=follow_symlinks,
            )
            or ""
        ).strip()
    except ValueError as e:
        error(str(e))
        raise typer.Exit(code=1) from e
    if not resolved_content:
        error("Provide capture content as an argument, via stdin, or with --content-file.")
        raise typer.Exit(code=1)

    resolved_title = (title or "").strip() or capture_support.derive_capture_title(resolved_content)
    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    related_ids = command_support.parse_csv_ids(related_to)
    task_ids = command_support.parse_csv_ids(task)
    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_capture() -> None:
        try:
            async with get_client() as client:
                data = await capture_support.write_memory_capture(
                    client,
                    title=resolved_title,
                    content=resolved_content,
                    kind=entity_type,
                    domain=None,
                    tags=parsed_tags,
                    related_ids=related_ids,
                    task_ids=task_ids,
                    active_task=active_task,
                    effective_project=effective_project,
                    capture_mode="quick",
                    surface="cli",
                    wait_searchable=wait_searchable,
                )

                if json_output:
                    print_json(data)
                    return

                memory_views.print_memory_capture_result(
                    title=resolved_title,
                    kind=entity_type,
                    data=data,
                    wait_searchable=wait_searchable,
                )
        except SibylClientError as e:
            command_support.handle_client_error(e)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(code=1) from e

    run_capture()


def note_alias(
    subject: str = typer.Argument(..., help="Task ID for task notes, or free note content"),
    content: str | None = typer.Argument(None, help="Note body or '-' for stdin"),
    content_file: str | None = typer.Option(
        None, "--content-file", help="Read note content from file"
    ),
    max_size: int = typer.Option(
        1_048_576,
        "--max-size",
        min=1,
        help="Maximum content file size in bytes",
    ),
    follow_symlinks: bool = typer.Option(
        False,
        "--follow-symlinks",
        help="Allow --content-file to read through symlinks",
    ),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    tags: str | None = typer.Option(
        None,
        "--tags",
        help="Comma-separated browse-only metadata tags",
    ),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help=(
            "Comma-separated entity IDs. A bare ID links untyped; prefix an ID "
            "with supersedes:, contradicts:, requires:, supports:, or decides: "
            "to declare what this memory does to that one (this memory is the "
            "subject), which is what retrieval weights when it walks the graph"
        ),
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to connect with RELATED_TO edges",
    ),
    active_task: bool = typer.Option(
        True,
        "--active-task/--no-active-task",
        help="Auto-link free notes to the single active task in the current project",
    ),
    assistant: bool = typer.Option(
        False,
        "--assistant",
        "--agent",
        help="Mark task note as assistant-authored",
    ),
    author: str | None = typer.Option(None, "--author", "-a", help="Task note author"),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until free notes are persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Add a task note or capture a free note memory."""
    task_note = capture_support.looks_like_task_id(subject)

    try:
        resolved_content = (
            resolve_content_input(
                content if content is not None else (None if task_note else subject),
                content_file=content_file,
                max_size=max_size,
                follow_symlinks=follow_symlinks,
            )
            or ""
        ).strip()
    except ValueError as e:
        error(str(e))
        raise typer.Exit(code=1) from e

    if not resolved_content:
        error("Provide note content as an argument, via stdin, or with --content-file.")
        raise typer.Exit(code=1)

    @run_async
    async def run_note() -> None:
        try:
            async with get_client() as client:
                if task_note:
                    resolved_id = await resolve_id_prefix(client, subject, entity_type="task")
                    response = await client.create_note(
                        resolved_id,
                        resolved_content,
                        "agent" if assistant else "user",
                        author or "",
                    )
                    if json_output:
                        print_json_result(
                            response,
                            succeeded=bool(response.get("id") or response.get("success")),
                        )
                        return
                    note_id = response.get("id")
                    if note_id:
                        success(f"Note added: {note_id}")
                    elif response.get("success"):
                        success(f"Note added to task: {resolved_id}")
                    else:
                        error("Failed to add note")
                        raise typer.Exit(1)
                    return

                parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
                data = await capture_support.write_memory_capture(
                    client,
                    title=subject
                    if content is not None
                    else capture_support.derive_capture_title(resolved_content),
                    content=resolved_content,
                    kind=EntityType.NOTE.value,
                    domain=None,
                    tags=parsed_tags,
                    related_ids=command_support.parse_csv_ids(related_to),
                    task_ids=command_support.parse_csv_ids(task),
                    active_task=active_task,
                    effective_project=project
                    or (None if all_projects else resolve_project_from_cwd()),
                    capture_mode="remember",
                    surface="cli",
                    wait_searchable=wait_searchable,
                )
                if json_output:
                    print_json(data)
                    return
                memory_views.print_memory_capture_result(
                    title=subject
                    if content is not None
                    else capture_support.derive_capture_title(resolved_content),
                    kind=EntityType.NOTE.value,
                    data=data,
                    wait_searchable=wait_searchable,
                )
        except SibylClientError as e:
            command_support.handle_client_error(e)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(code=1) from e

    run_note()
