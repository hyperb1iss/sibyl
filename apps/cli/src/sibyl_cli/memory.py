"""Root commands for deliberate memory creation and reflection."""

from __future__ import annotations

import sys
from typing import Annotated, Any

import typer

from sibyl_cli import capture_support, command_support, memory_views, recall
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    console,
    error,
    info,
    print_json,
    print_mutation_receipt,
    resolve_content_input,
    run_async,
    success,
)
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.project_refs import resolve_project_reference


def remember_memory(
    title: str = typer.Argument(..., help="Title/name of the memory"),
    content: str | None = typer.Argument(
        None,
        help="Memory body. Reads stdin if omitted.",
    ),
    content_option: str | None = typer.Option(None, "--content", help="Memory body"),
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
    kind: str | None = typer.Option(
        None,
        "--kind",
        "-k",
        callback=capture_support.normalize_memory_kind,
        help=capture_support.ENTITY_TYPE_HELP,
        show_default="episode",
    ),
    legacy_kind: str | None = typer.Option(
        None,
        "--type",
        "-t",
        callback=capture_support.normalize_legacy_memory_kind,
        hidden=True,
    ),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
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
    keys: Annotated[
        list[str] | None,
        typer.Option(
            "--key",
            help=(
                "Exact-match retrieval key this memory answers to (error string, symbol, "
                "config flag, alias). Repeatable, up to 16."
            ),
        ),
    ] = None,
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
    surface: str = typer.Option("cli", "--surface", help="Capture surface metadata"),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new memory is persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
    raw: bool = typer.Option(False, "--raw", help="Store verbatim raw memory only"),
    diary: bool = typer.Option(False, "--diary", help="Store a private agent diary entry"),
    agent: str | None = typer.Option(None, "--agent", help="Agent identity for diary entries"),
    source_id: str | None = typer.Option(None, "--source-id", help="Raw memory source ID"),
    memory_scope: str = typer.Option(
        "private",
        "--scope",
        callback=capture_support.normalize_memory_scope,
        help=capture_support.MEMORY_SCOPE_HELP,
    ),
    scope_key: str | None = typer.Option(None, "--scope-key", help="Project/team/shared scope key"),
    pin: bool = typer.Option(False, "--pin", help="Exempt this memory from ordinary decay"),
    basis: str | None = typer.Option(
        None,
        "--basis",
        callback=capture_support.normalize_memory_basis,
        help="Epistemic basis: observed, inferred, told, or assumed",
    ),
    propose_scope: str | None = typer.Option(
        None,
        "--propose-scope",
        callback=capture_support.normalize_memory_proposal_scope,
        help="Nominate this memory for audited promotion to team scope",
    ),
    spans_json: str | None = typer.Option(
        None,
        "--spans-json",
        help=(
            'Cut plan as JSON: \'[{"start":0,"end":812,"label":"Root cause"},...]\'. '
            "Half-open character offsets into the stored body, tiling it exactly."
        ),
    ),
    atomic: bool = typer.Option(
        False,
        "--atomic",
        help="Declare this memory one retrievable unit that must not be cut into passages",
    ),
    probe: list[str] | None = capture_support.PROBE_OPTION,
) -> None:
    """Remember a decision, plan, idea, claim, artifact, session, or learning."""

    if kind and legacy_kind and kind != legacy_kind:
        error("--kind and the legacy --type alias must match")
        raise typer.Exit(code=1)
    kind = kind or legacy_kind or "episode"

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
    if not resolved_content:
        error("Provide memory content as an argument, via stdin, or with --content-file.")
        raise typer.Exit(code=1)

    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    # Not comma-split: a retrieval key can legitimately contain a comma (an error
    # string, a formatted tuple), so repeating --key is the only unambiguous way
    # to declare one.
    parsed_keys = [key.strip() for key in keys or () if key.strip()] or None
    related_ids = command_support.parse_csv_ids(related_to)
    task_ids = command_support.parse_csv_ids(task)
    probes = [entry.strip() for entry in (probe or []) if entry.strip()]
    parsed_spans = capture_support.parse_spans_json(spans_json)
    if parsed_spans is not None and atomic:
        error("--atomic and --spans-json describe opposite things; pick one.")
        raise typer.Exit(code=1)
    if (raw or diary) and (parsed_spans is not None or atomic or probes):
        # A raw-only write stores the verbatim record and no graph row, so there
        # is nothing for spans to cut and nothing for a probe to retrieve.
        error("--spans-json, --atomic, and --probe need a graph memory; drop --raw/--diary.")
        raise typer.Exit(code=1)
    metadata = {
        "capture_mode": "remember",
        "capture_surface": surface,
        "remember_kind": kind,
    }
    capture_metadata: dict[str, Any] = {}
    if pin:
        capture_metadata["pinned"] = True
    if basis:
        capture_metadata["basis"] = basis
    if propose_scope:
        capture_metadata["suggested_memory_scope"] = propose_scope
    metadata.update(capture_metadata)
    if domain:
        metadata["domain"] = domain

    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_remember() -> None:
        try:
            async with get_client() as client:
                if diary and not agent:
                    error("Provide --agent when using --diary.")
                    raise typer.Exit(code=1)
                if raw or diary:
                    resolved_project = (
                        await resolve_project_reference(client, effective_project)
                        if effective_project
                        else None
                    )
                    if resolved_project:
                        metadata["project_id"] = resolved_project
                    data = await client.remember_raw_memory(
                        title=title,
                        raw_content=resolved_content,
                        source_id=source_id,
                        memory_scope=memory_scope,
                        scope_key=scope_key,
                        diary=diary,
                        agent_id=agent,
                        project_id=resolved_project if diary else None,
                        tags=parsed_tags,
                        metadata=metadata,
                        provenance={"remember_kind": kind},
                        capture_surface=surface,
                    )

                    memory_id = data.get("id", "unknown")
                    if json_output:
                        print_json(data)
                        return

                    label = f"diary entry for {agent}" if diary else "raw memory"
                    success(f"Remembered {label}: {title}")
                    print_mutation_receipt(data)
                    console.print(f"  [dim]ID: {memory_id}[/dim]")
                    if policy_reason := data.get("policy_reason"):
                        console.print(f"  [dim]Policy: {policy_reason}[/dim]")
                    return

                data = await capture_support.write_memory_capture(
                    client,
                    title=title,
                    content=resolved_content,
                    kind=kind,
                    domain=domain,
                    tags=parsed_tags,
                    related_ids=related_ids,
                    task_ids=task_ids,
                    active_task=active_task,
                    effective_project=effective_project,
                    capture_mode="remember",
                    surface=surface,
                    wait_searchable=wait_searchable,
                    memory_scope=memory_scope,
                    scope_key=scope_key,
                    source_id=source_id,
                    retrieval_keys=parsed_keys,
                    capture_metadata=capture_metadata,
                    spans=parsed_spans,
                    atomic=atomic,
                    probes=probes or None,
                )

                if json_output:
                    print_json(data)
                    return

                memory_views.print_memory_capture_result(
                    title=title,
                    kind=kind,
                    data=data,
                    wait_searchable=wait_searchable,
                )
        except SibylClientError as e:
            command_support.handle_client_error(e)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(code=1) from e

    run_remember()


def reflect_memory(
    content: str | None = typer.Argument(
        None,
        help="Raw notes to reflect. Reads stdin if omitted.",
    ),
    title: str = typer.Option("Session reflection", "--title", "-t", help="Source/session title"),
    intent: str = typer.Option(
        "general",
        "--intent",
        "-i",
        callback=recall.normalize_context_intent,
        help=f"Intent: {', '.join(recall.CONTEXT_INTENT_VALUES)}",
    ),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(
        False,
        "--all-projects",
        help="Do not auto-scope to the linked project",
    ),
    related_to: str | None = typer.Option(
        None,
        "--related-to",
        help="Comma-separated entity IDs to link persisted candidates to",
    ),
    task: str | None = typer.Option(
        None,
        "--task",
        help="Comma-separated task IDs to link persisted output to",
    ),
    active_task: bool = typer.Option(
        True,
        "--active-task/--no-active-task",
        help="When persisting, auto-link to the single active task in the current project",
    ),
    persist: bool = typer.Option(False, "--persist", help="Persist candidates into the graph"),
    persist_source: bool = typer.Option(
        True,
        "--source/--no-source",
        help="When persisting, also store the raw notes as a session memory",
    ),
    persist_review: bool = typer.Option(
        False,
        "--review",
        help="Store persisted output in the raw review queue instead of graph promotion",
    ),
    cited: str | None = typer.Option(
        None,
        "--cited",
        help="Comma-separated context/search IDs that informed this reflection",
    ),
    limit: int = typer.Option(12, "--limit", "-l", min=1, max=25, help="Maximum candidates"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Reflect raw notes into memory candidates, optionally persisting them."""

    resolved_content = content
    if resolved_content is None and not sys.stdin.isatty():
        resolved_content = sys.stdin.read()

    resolved_content = (resolved_content or "").strip()
    if not resolved_content:
        error("Provide notes as an argument or via stdin.")
        raise typer.Exit(code=1)

    effective_project = project or (None if all_projects else resolve_project_from_cwd())
    related_ids = command_support.parse_csv_ids(related_to)
    task_ids = command_support.parse_csv_ids(task)
    cited_ids = command_support.parse_csv_ids(cited)

    @run_async
    async def run_reflect() -> None:
        try:
            async with get_client() as client:
                resolved_links = await capture_support.resolve_capture_links(
                    client=client,
                    project=effective_project,
                    related_ids=related_ids,
                    task_ids=task_ids,
                    active_task=active_task and persist,
                )
                data = await client.reflect(
                    content=resolved_content,
                    source_title=title,
                    intent=intent,
                    domain=domain,
                    project=effective_project,
                    related_to=resolved_links,
                    persist=persist,
                    persist_source=persist_source,
                    persist_review=persist_review,
                    cited_ids=cited_ids or None,
                    limit=limit,
                )

            if json_output:
                print_json(data)
                return

            console.print(data.get("markdown") or "")
            memory_views.print_reflection_persistence_summary(
                data,
                persist=persist,
                persist_source=persist_source,
            )
            citation_usage = data.get("citation_usage", {})
            if citation_usage:
                info(
                    "Citations recorded: "
                    f"{citation_usage.get('stamped_count', 0)}/"
                    f"{citation_usage.get('cited_count', len(cited_ids))}"
                )
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_reflect()
