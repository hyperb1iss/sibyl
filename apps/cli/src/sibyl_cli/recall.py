"""Root commands for graph search and context recall."""

from __future__ import annotations

import sys
from typing import Annotated, Any

import typer

from sibyl_cli import capture_support, command_support, memory_views
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import CORAL, NEON_CYAN, console, error, info, print_json, run_async
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_core.models.context import ContextIntent

CONTEXT_INTENT_VALUES = [intent.value for intent in ContextIntent]
ADVERTISED_CONTEXT_INTENT_VALUES = ("build", "plan", "review", "debug", "general")
CONTEXT_INTENT_HELP = f"Agent intent: {', '.join(ADVERTISED_CONTEXT_INTENT_VALUES)}"


def normalize_context_intent(value: str) -> str:
    """Normalize a context intent for Typer option callbacks."""
    normalized = value.strip().lower()
    if normalized in CONTEXT_INTENT_VALUES:
        return normalized
    choices = ", ".join(CONTEXT_INTENT_VALUES)
    raise typer.BadParameter(f"{value!r} is not one of: {choices}")


def search(
    query: str = typer.Argument(..., help="Search query"),
    entity_type: str | None = typer.Option(None, "--type", "-t", help="Filter by entity type"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum results"),
    all_projects: bool = typer.Option(False, "--all", "-a", help="Search all projects"),
    graph_only: bool = typer.Option(False, "--graph-only", help="Search graph memory only"),
    docs_only: bool = typer.Option(False, "--docs-only", help="Search crawled docs only"),
    as_of: str | None = typer.Option(None, "--as-of", help="Filter graph memory as of a timestamp"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Search the knowledge graph."""
    if graph_only and docs_only:
        error("--graph-only and --docs-only cannot be combined")
        raise typer.Exit(1)

    normalized_type = entity_type.lower() if entity_type else None
    if graph_only and normalized_type == "document":
        error("--graph-only cannot be combined with --type document")
        raise typer.Exit(1)
    if docs_only and normalized_type and normalized_type != "document":
        error("--docs-only can only be combined with --type document")
        raise typer.Exit(1)

    # Auto-resolve project from context unless --all
    effective_project = None if all_projects else resolve_project_from_cwd()
    include_documents = not graph_only
    include_graph = not docs_only

    @run_async
    async def run_search() -> None:
        try:
            async with get_client() as client:
                types = [entity_type] if entity_type else None
                search_kwargs: dict[str, Any] = {}
                if as_of:
                    search_kwargs["as_of"] = as_of
                data = await client.search(
                    query,
                    types=types,
                    limit=limit,
                    project=effective_project,
                    include_documents=include_documents,
                    include_graph=include_graph,
                    **search_kwargs,
                )

                if json_output:
                    print_json(data)
                    return

                results = data.get("results", [])
                if not results:
                    info("No results found")
                    return

                console.print(f"\n[bold]Found {len(results)} results:[/bold]\n")
                for r in results:
                    entity_id = r.get("id", "")
                    name = r.get("name", "Unknown")
                    source = r.get("source")
                    content = r.get("content", "")
                    metadata = r.get("metadata", {})
                    heading_path = metadata.get("heading_path", [])
                    origin = str(
                        r.get("result_origin")
                        or ("document" if metadata.get("document_id") else "graph")
                    ).lower()
                    origin_label = {
                        "document": "docs",
                        "raw_memory": "memory",
                    }.get(origin, "graph")

                    # Header: Document name (source)
                    # Skip file paths - they're not useful. Show source name only.
                    display_source = source if source and not source.startswith("/") else None
                    source_info = f" ({display_source})" if display_source else ""
                    console.print(
                        f"  [dim]{origin_label}[/dim] "
                        f"[{NEON_CYAN}]{name}[/{NEON_CYAN}][dim]{source_info}[/dim]"
                    )

                    # Section path
                    if heading_path:
                        path_str = " > ".join(heading_path)
                        console.print(f"    [dim]{path_str}[/dim]")

                    # Content preview
                    if content:
                        metadata_snippet = metadata.get("snippet")
                        snippet = (
                            metadata_snippet
                            if isinstance(metadata_snippet, str)
                            else content
                            if "<mark>" in content
                            else None
                        )
                        console.print(
                            f"    {memory_views.format_highlight_preview(snippet, content)}",
                            soft_wrap=True,
                        )

                    # Show IDs for fetching
                    document_id = metadata.get("document_id")
                    if document_id:
                        # Crawled doc: show document_id for full doc retrieval
                        console.print(f"    [dim]doc:[/dim] [{CORAL}]{document_id}[/{CORAL}]")
                    else:
                        # Graph entity: show entity ID
                        console.print(f"    [{CORAL}]{entity_id}[/{CORAL}]")
                    console.print()

                # Hint for retrieval - check if any results are from crawled docs
                has_docs = any(r.get("metadata", {}).get("document_id") for r in results)
                has_entities = any(not r.get("metadata", {}).get("document_id") for r in results)

                hints = []
                if has_entities:
                    hints.append(f"[{NEON_CYAN}]sibyl show <id>[/{NEON_CYAN}]")
                if has_docs:
                    hints.append(f"[{NEON_CYAN}]sibyl crawl documents show <doc>[/{NEON_CYAN}]")

                if hints:
                    console.print(f"[dim]Full content:[/dim] {' [dim]or[/dim] '.join(hints)}")
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_search()


def brief_context(
    goal: str = typer.Argument(..., help="Subagent goal or task"),
    intent: str = typer.Option(
        "build",
        "--intent",
        "-i",
        callback=normalize_context_intent,
        help=CONTEXT_INTENT_HELP,
    ),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    all_projects: bool = typer.Option(False, "--all", "-a", help="Use all accessible projects"),
    budget: int = typer.Option(
        1500,
        "--budget",
        min=100,
        max=32_000,
        help="Token budget for the rendered brief",
    ),
) -> None:
    """One-shot lean context brief for injecting into a subagent prompt.

    Prints wake-layer markdown only: no skill ceremony, no related-graph
    expansion, no JSON envelope. Pipe or paste straight into a worker
    agent's prompt.
    """
    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_brief() -> None:
        try:
            async with get_client() as client:
                pack = await client.context_pack(
                    goal=goal,
                    intent=intent,
                    layer="wake",
                    project=effective_project,
                    limit=8,
                    include_related=False,
                    related_limit=0,
                    markdown_token_budget=budget,
                )
            sys.stdout.write((pack.get("markdown") or "") + "\n")
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_brief()


def recall_context(
    goal: str = typer.Argument(..., help="Agent goal or user task"),
    intent: str = typer.Option(
        "build",
        "--intent",
        "-i",
        callback=normalize_context_intent,
        help=CONTEXT_INTENT_HELP,
    ),
    layer: str = typer.Option(
        "recall",
        "--layer",
        help="Context depth: wake, recall, deep_search",
    ),
    domain: str | None = typer.Option(None, "--domain", "-d", help="Domain/category"),
    project: str | None = typer.Option(None, "--project", "-p", help="Project ID"),
    agent: str | None = typer.Option(None, "--agent", help="Agent diary identity to include"),
    all_projects: bool = typer.Option(False, "--all", "-a", help="Use all accessible projects"),
    limit: int = typer.Option(12, "--limit", "-l", min=1, max=50, help="Maximum context items"),
    related: bool = typer.Option(
        True,
        "--related/--no-related",
        help="Include one-hop related graph context",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output full JSON"),
    audit: bool = typer.Option(
        False,
        "--audit",
        help="Include full retrieval metadata per item (for auditing noisy packs)",
    ),
    budget: int | None = typer.Option(
        None,
        "--budget",
        min=100,
        max=32_000,
        help="Size rendered markdown to roughly this many tokens",
    ),
    raw: bool = typer.Option(False, "--raw", help="Recall verbatim raw memories"),
    diary: bool = typer.Option(False, "--diary", help="Recall a private agent diary"),
    memory_scope: str = typer.Option(
        "private",
        "--scope",
        callback=capture_support.normalize_memory_scope,
        help=capture_support.MEMORY_SCOPE_HELP,
    ),
    scope_key: str | None = typer.Option(None, "--scope-key", help="Project/team/shared scope key"),
    participant: Annotated[
        list[str] | None,
        typer.Option("--participant", help="Filter raw imports by participant"),
    ] = None,
    label: Annotated[
        list[str] | None,
        typer.Option("--label", help="Filter raw imports by adapter label"),
    ] = None,
    thread_id: str | None = typer.Option(None, "--thread", help="Filter raw imports by thread"),
    occurred_after: str | None = typer.Option(
        None,
        "--occurred-after",
        help="Filter raw imports after an ISO timestamp",
    ),
    occurred_before: str | None = typer.Option(
        None,
        "--occurred-before",
        help="Filter raw imports before an ISO timestamp",
    ),
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help="Filter raw memory by validity timestamp",
    ),
) -> None:
    """Recall a compact working context pack for an agent."""
    effective_project = project or (None if all_projects else resolve_project_from_cwd())

    @run_async
    async def run_recall() -> None:
        try:
            async with get_client() as client:
                if diary and not agent:
                    error("Provide --agent when using --diary.")
                    raise typer.Exit(code=1)
                if raw or diary:
                    recall_kwargs: dict[str, Any] = {}
                    if participant:
                        recall_kwargs["participants"] = participant
                    if label:
                        recall_kwargs["labels"] = label
                    if thread_id:
                        recall_kwargs["thread_id"] = thread_id
                    if occurred_after:
                        recall_kwargs["occurred_after"] = occurred_after
                    if occurred_before:
                        recall_kwargs["occurred_before"] = occurred_before
                    if as_of:
                        recall_kwargs["as_of"] = as_of
                    data = await client.recall_raw_memory(
                        query=goal,
                        memory_scope=memory_scope,
                        scope_key=scope_key,
                        diary=diary,
                        agent_id=agent if diary else None,
                        project_id=effective_project if diary else None,
                        limit=limit,
                        **recall_kwargs,
                    )
                    if json_output:
                        print_json(data)
                        return
                    memories = data.get("memories", [])
                    memory_views.print_raw_memory_results(
                        memories if isinstance(memories, list) else []
                    )
                    return

                pack = await client.context_pack(
                    goal=goal,
                    intent=intent,
                    layer=layer,
                    domain=domain,
                    project=effective_project,
                    agent_id=agent,
                    limit=limit,
                    include_related=related,
                    related_limit=3,
                    audit=audit,
                    markdown_token_budget=budget,
                )

            if json_output:
                print_json(pack)
                return
            console.print(pack.get("markdown") or "")
        except SibylClientError as e:
            command_support.handle_client_error(e)

    run_recall()
