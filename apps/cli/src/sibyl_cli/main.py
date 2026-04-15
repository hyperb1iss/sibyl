"""Main CLI application - client-side commands for Sibyl.

This is the entry point for the sibyl-dev package.
All commands communicate with the REST API.

Server commands (serve, dev, db, generate, etc.) are in sibyl-server.
"""

import re
import sys
from importlib.metadata import version as pkg_version
from typing import Annotated

import typer

from sibyl_cli.archive import app as archive_app
from sibyl_cli.auth import app as auth_app
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    NEON_CYAN,
    console,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    run_async,
    success,
)
from sibyl_cli.config_cmd import app as config_app
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.context import app as context_app
from sibyl_cli.crawl import app as crawl_app
from sibyl_cli.debug import app as debug_app
from sibyl_cli.dev import app as dev_app
from sibyl_cli.entity import app as entity_app
from sibyl_cli.epic import app as epic_app
from sibyl_cli.explore import app as explore_app
from sibyl_cli.local import app as local_app
from sibyl_cli.logs import app as logs_app
from sibyl_cli.org import app as org_app
from sibyl_cli.project import app as project_app
from sibyl_cli.session import app as session_app
from sibyl_cli.state import set_context_override
from sibyl_cli.task import app as task_app
from sibyl_cli.update import app as update_app


def get_version() -> str:
    """Get the installed package version."""
    try:
        return pkg_version("sibyl-dev")
    except Exception:
        return "unknown"


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        print(f"sibyl {get_version()}")
        raise typer.Exit()


# Main app
app = typer.Typer(
    name="sibyl",
    help="Sibyl - Oracle of Development Wisdom (CLI Client)",
    add_completion=False,
    no_args_is_help=False,
)


# Register subcommand groups
app.add_typer(task_app, name="task")
app.add_typer(epic_app, name="epic")
app.add_typer(project_app, name="project")
app.add_typer(archive_app, name="archive")
app.add_typer(session_app, name="session")
app.add_typer(entity_app, name="entity")
app.add_typer(explore_app, name="explore")
app.add_typer(crawl_app, name="crawl")
app.add_typer(debug_app, name="debug")
app.add_typer(dev_app, name="dev")
app.add_typer(auth_app, name="auth")
app.add_typer(org_app, name="org")
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="context")
app.add_typer(local_app, name="local")
app.add_typer(logs_app, name="logs")
app.add_typer(update_app, name="update")


SEARCH_PREVIEW_CHARS = 220
CAPTURE_TITLE_CHARS = 72


def _format_search_preview(content: str, max_chars: int = SEARCH_PREVIEW_CHARS) -> str:
    """Format search result previews for terminal display."""
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


def _derive_capture_title(content: str) -> str:
    """Create a compact default title for quick captures."""
    compact = re.sub(r"\s+", " ", content).strip()
    if not compact:
        return "Untitled capture"
    if len(compact) <= CAPTURE_TITLE_CHARS:
        return compact
    return compact[: CAPTURE_TITLE_CHARS - 1].rstrip(" ,;:-") + "…"


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1."""
    if "Cannot connect" in str(e):
        console.print()
        console.print(f"  [{CORAL}]×[/{CORAL}] [bold]Cannot connect to Sibyl server[/bold]")
        console.print()
        console.print(f"    [{NEON_CYAN}]›[/{NEON_CYAN}] Check that the Sibyl server is running")
        console.print()
    elif e.status_code in {401, 403}:
        console.print()
        console.print(f"  [{CORAL}]×[/{CORAL}] [bold]Authentication required[/bold]")
        console.print()
        console.print(
            f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl auth login[/bold {NEON_CYAN}]   [dim]Log in[/dim]"
        )
        console.print(
            f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl auth signup[/bold {NEON_CYAN}]  [dim]Create account[/dim]"
        )
        console.print()
    else:
        handle_client_error(e)
    raise typer.Exit(1)


# ============================================================================
# Global callback for context override
# ============================================================================


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            "-C",
            help="Override project context for this command (project ID or name)",
            envvar="SIBYL_CONTEXT",
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show version and exit",
            callback=version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Sibyl CLI - interact with your knowledge graph."""
    if context:
        set_context_override(context)

    # Show help if no command
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())


# ============================================================================
# Root-level commands
# ============================================================================


@app.command()
def health(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Check Sibyl server health."""

    @run_async
    async def check_health() -> None:
        try:
            async with get_client() as client:
                data = await client.get("/health")

                if json_output:
                    print_json(data)
                    return
                status = data.get("status", "unknown")
                server = data.get("server_name", "sibyl")

                if status == "healthy":
                    success(f"{server} is healthy")
                    if counts := data.get("counts"):
                        console.print(f"  [dim]Entities: {counts.get('entities', 0)}[/dim]")
                        console.print(
                            f"  [dim]Relationships: {counts.get('relationships', 0)}[/dim]"
                        )
                else:
                    error(f"{server} is unhealthy: {status}")
                    raise typer.Exit(1)
        except SibylClientError as e:
            _handle_client_error(e)

    check_health()


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    entity_type: str | None = typer.Option(None, "--type", "-t", help="Filter by entity type"),
    limit: int = typer.Option(10, "--limit", "-l", help="Maximum results"),
    all_projects: bool = typer.Option(False, "--all", "-a", help="Search all projects"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Search the knowledge graph."""
    # Auto-resolve project from context unless --all
    effective_project = None if all_projects else resolve_project_from_cwd()

    @run_async
    async def run_search() -> None:
        try:
            async with get_client() as client:
                types = [entity_type] if entity_type else None
                data = await client.search(
                    query, types=types, limit=limit, project=effective_project
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

                    # Header: Document name (source)
                    # Skip file paths - they're not useful. Show source name only.
                    display_source = source if source and not source.startswith("/") else None
                    source_info = f" ({display_source})" if display_source else ""
                    console.print(f"  [{NEON_CYAN}]{name}[/{NEON_CYAN}][dim]{source_info}[/dim]")

                    # Section path
                    if heading_path:
                        path_str = " > ".join(heading_path)
                        console.print(f"    [dim]{path_str}[/dim]")

                    # Content preview
                    if content:
                        console.print(f"    {_format_search_preview(content)}", soft_wrap=True)

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
                    hints.append(f"[{NEON_CYAN}]sibyl entity show <id>[/{NEON_CYAN}]")
                if has_docs:
                    hints.append(f"[{NEON_CYAN}]sibyl crawl documents show <doc>[/{NEON_CYAN}]")

                if hints:
                    console.print(f"[dim]Full content:[/dim] {' [dim]or[/dim] '.join(hints)}")
        except SibylClientError as e:
            _handle_client_error(e)

    run_search()


@app.command("add")
def add_knowledge(
    title: str = typer.Argument(..., help="Title/name of the knowledge"),
    content: str = typer.Argument(..., help="Content/description"),
    entity_type: str = typer.Option("episode", "--type", "-t", help="Entity type"),
    category: str | None = typer.Option(None, "--category", "-c", help="Category"),
    language: str | None = typer.Option(None, "--language", "-l", help="Language"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new entity is persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Add knowledge to the graph."""

    @run_async
    async def run_add() -> None:
        try:
            async with get_client() as client:
                data = await client.create_entity(
                    name=title,
                    content=content,
                    entity_type=entity_type,
                    category=category,
                    languages=[language] if language else None,
                    tags=[t.strip() for t in tags.split(",")] if tags else None,
                    sync=wait_searchable,
                )

                entity_id = data.get("id", "unknown")

                if json_output:
                    print_json(data)
                    return

                if wait_searchable:
                    success(f"Added {entity_type}: {title}")
                else:
                    info(f"Queued {entity_type}: {title}")
                console.print(f"  [dim]ID: {entity_id}[/dim]")
        except SibylClientError as e:
            _handle_client_error(e)

    run_add()


@app.command("capture")
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
    entity_type: str = typer.Option("episode", "--type", help="Entity type for the capture"),
    tags: str | None = typer.Option(None, "--tags", help="Comma-separated tags"),
    wait_searchable: bool = typer.Option(
        False,
        "--wait-searchable",
        help="Wait until the new entity is persisted and ready for direct retrieval",
    ),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Capture a quick memory without separate title and content fields."""

    resolved_content = content
    if resolved_content is None and not sys.stdin.isatty():
        resolved_content = sys.stdin.read()

    resolved_content = (resolved_content or "").strip()
    if not resolved_content:
        error("Provide capture content as an argument or via stdin.")
        raise typer.Exit(code=1)

    resolved_title = (title or "").strip() or _derive_capture_title(resolved_content)

    @run_async
    async def run_capture() -> None:
        try:
            async with get_client() as client:
                data = await client.create_entity(
                    name=resolved_title,
                    content=resolved_content,
                    entity_type=entity_type,
                    tags=[t.strip() for t in tags.split(",") if t.strip()] if tags else None,
                    metadata={"capture_mode": "quick", "capture_surface": "cli"},
                    sync=wait_searchable,
                )

                entity_id = data.get("id", "unknown")

                if json_output:
                    print_json(data)
                    return

                if wait_searchable:
                    success(f"Captured {entity_type}: {resolved_title}")
                else:
                    info(f"Queued {entity_type}: {resolved_title}")
                console.print(f"  [dim]ID: {entity_id}[/dim]")
        except SibylClientError as e:
            _handle_client_error(e)

    run_capture()


@app.command()
def stats(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Show knowledge graph statistics."""

    @run_async
    async def get_stats() -> None:
        try:
            async with get_client() as client:
                data = await client.get("/admin/stats")

                if json_output:
                    print_json(data)
                    return

                console.print("\n[bold]Knowledge Graph Statistics[/bold]\n")

                if counts := data.get("entity_counts"):
                    table = create_table("Entity Type", "Count")
                    for etype, count in sorted(counts.items()):
                        table.add_row(etype, str(count))
                    console.print(table)
                    console.print()

                if rel_counts := data.get("relationship_counts"):
                    table = create_table("Relationship Type", "Count")
                    for rtype, count in sorted(rel_counts.items()):
                        table.add_row(rtype, str(count))
                    console.print(table)
                console.print()
        except SibylClientError as e:
            _handle_client_error(e)

    get_stats()


@app.command()
def version() -> None:
    """Show version information."""
    console.print(f"sibyl {get_version()}")


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
