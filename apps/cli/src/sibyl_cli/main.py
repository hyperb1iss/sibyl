"""Main CLI application - client-side commands for Sibyl.

This is the entry point for the sibyl-cli package.
All commands communicate with the REST API.

Server commands (serve, dev, db, generate, etc.) are in sibyl-server.
"""

from typing import Annotated, Any

import typer

from sibyl_cli.auth import app as auth_app
from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    NEON_CYAN,
    console,
    create_table,
    error,
    info,
    print_json,
    run_async,
    spinner,
    success,
)

# Import subcommand apps
from sibyl_cli.config_cmd import app as config_app
from sibyl_cli.context import app as context_app
from sibyl_cli.crawl import app as crawl_app
from sibyl_cli.entity import app as entity_app
from sibyl_cli.epic import app as epic_app
from sibyl_cli.explore import app as explore_app
from sibyl_cli.org import app as org_app
from sibyl_cli.project import app as project_app
from sibyl_cli.source import app as source_app
from sibyl_cli.state import set_context_override
from sibyl_cli.task import app as task_app

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
app.add_typer(entity_app, name="entity")
app.add_typer(explore_app, name="explore")
app.add_typer(source_app, name="source")
app.add_typer(crawl_app, name="crawl")
app.add_typer(auth_app, name="auth")
app.add_typer(org_app, name="org")
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="context")


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
    elif e.status_code == 404:
        error(f"Not found: {e.detail}")
    elif e.status_code == 400:
        error(f"Invalid request: {e.detail}")
    else:
        error(str(e))
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
                with spinner("Checking health..."):
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
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Search the knowledge graph."""

    @run_async
    async def run_search() -> None:
        try:
            async with get_client() as client:
                with spinner(f"Searching for '{query}'..."):
                    params: dict[str, Any] = {"q": query, "limit": limit}
                    if entity_type:
                        params["type"] = entity_type
                    data = await client.get("/search", params=params)

                if json_output:
                    print_json(data)
                    return

                results = data.get("results", [])
                if not results:
                    info("No results found")
                    return

                console.print(f"\n[bold]Found {len(results)} results:[/bold]\n")
                for r in results:
                    entity = r.get("entity", {})
                    name = entity.get("name", "Unknown")
                    etype = entity.get("entity_type", "unknown")
                    score = r.get("score", 0)
                    console.print(
                        f"  [{NEON_CYAN}]{name}[/{NEON_CYAN}] "
                        f"[dim]({etype})[/dim] "
                        f"[{CORAL}]{score:.2f}[/{CORAL}]"
                    )
                console.print()
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
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Add knowledge to the graph."""

    @run_async
    async def run_add() -> None:
        try:
            async with get_client() as client:
                with spinner("Adding knowledge..."):
                    payload: dict[str, Any] = {
                        "title": title,
                        "content": content,
                        "entity_type": entity_type,
                    }
                    if category:
                        payload["category"] = category
                    if language:
                        payload["languages"] = [language]
                    if tags:
                        payload["tags"] = [t.strip() for t in tags.split(",")]

                    data = await client.post("/entities", json=payload)

                if json_output:
                    print_json(data)
                    return

                entity_id = data.get("id", "unknown")
                success(f"Added {entity_type}: {title}")
                console.print(f"  [dim]ID: {entity_id}[/dim]")
        except SibylClientError as e:
            _handle_client_error(e)

    run_add()


@app.command()
def stats(
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
) -> None:
    """Show knowledge graph statistics."""

    @run_async
    async def get_stats() -> None:
        try:
            async with get_client() as client:
                with spinner("Getting stats..."):
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
    from sibyl_cli import __version__

    console.print(f"sibyl-cli version {__version__}")


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
