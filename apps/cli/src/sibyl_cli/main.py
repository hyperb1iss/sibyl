"""Main CLI application - client-side commands for Sibyl.

This is the entry point for the sibyl-dev package.
All commands communicate with the REST API.

Server commands (serve, dev, db, generate, etc.) are in sibyl-server.
"""

from typing import Annotated

import typer

# Import subcommand apps
from sibyl_cli.agent import app as agent_app
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
    success,
)
from sibyl_cli.config_cmd import app as config_app
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.context import app as context_app
from sibyl_cli.crawl import app as crawl_app
from sibyl_cli.document import app as document_app
from sibyl_cli.entity import app as entity_app
from sibyl_cli.epic import app as epic_app
from sibyl_cli.explore import app as explore_app
from sibyl_cli.local import app as local_app
from sibyl_cli.org import app as org_app
from sibyl_cli.project import app as project_app
from sibyl_cli.runner import app as runner_app
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
app.add_typer(agent_app, name="agent")
app.add_typer(task_app, name="task")
app.add_typer(epic_app, name="epic")
app.add_typer(project_app, name="project")
app.add_typer(runner_app, name="runner")
app.add_typer(entity_app, name="entity")
app.add_typer(explore_app, name="explore")
app.add_typer(source_app, name="source")
app.add_typer(crawl_app, name="crawl")
app.add_typer(document_app, name="document")
app.add_typer(auth_app, name="auth")
app.add_typer(org_app, name="org")
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="context")
app.add_typer(local_app, name="local")


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

                    # Content preview (first 100 chars)
                    if content:
                        # Strip heading prefix if present
                        preview = content
                        if preview.startswith("[") and "] " in preview:
                            preview = preview.split("] ", 1)[1]
                        preview = " ".join(preview.split())[:100]
                        if len(content) > 100:
                            preview += "…"
                        console.print(f"    {preview}")

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
                    hints.append(f"[{NEON_CYAN}]sibyl document show <doc>[/{NEON_CYAN}]")

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
                )

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

    console.print(f"sibyl-dev version {__version__}")


@app.command()
def upgrade(
    pull_images: Annotated[
        bool,
        typer.Option("--pull", "-p", help="Also pull latest Docker images"),
    ] = False,
    check_only: Annotated[
        bool,
        typer.Option("--check", "-c", help="Check for updates without installing"),
    ] = False,
) -> None:
    """Upgrade sibyl-dev to the latest version.

    Automatically detects installation method (uv, pipx, or pip) and uses
    the appropriate upgrade command.
    """
    import shutil
    import subprocess

    from sibyl_cli import __version__

    info(f"Current version: {__version__}")

    # Detect installation method
    install_method = None
    upgrade_cmd = None

    if shutil.which("uv"):
        # Check if installed via uv tool
        result = subprocess.run(
            ["uv", "tool", "list"],
            capture_output=True,
            text=True,
            check=False,
        )
        if "sibyl-dev" in result.stdout:
            install_method = "uv"
            upgrade_cmd = ["uv", "tool", "upgrade", "sibyl-dev"]

    if not install_method and shutil.which("pipx"):
        # Check if installed via pipx
        result = subprocess.run(
            ["pipx", "list", "--short"],
            capture_output=True,
            text=True,
            check=False,
        )
        if "sibyl-dev" in result.stdout:
            install_method = "pipx"
            upgrade_cmd = ["pipx", "upgrade", "sibyl-dev"]

    if not install_method:
        # Fall back to pip
        install_method = "pip"
        upgrade_cmd = ["pip", "install", "--upgrade", "sibyl-dev"]

    console.print(f"[dim]Detected installation method: {install_method}[/dim]")

    if check_only:
        # Just check PyPI for latest version
        import httpx

        try:
            resp = httpx.get("https://pypi.org/pypi/sibyl-dev/json", timeout=10)
            if resp.status_code == 200:
                latest = resp.json().get("info", {}).get("version", "unknown")
                if latest != __version__:
                    info(f"Update available: {__version__} → {latest}")
                    console.print("\nRun [bold]sibyl upgrade[/bold] to install")
                else:
                    success("Already up to date!")
            else:
                error("Could not check PyPI for updates")
        except Exception as e:
            error(f"Failed to check for updates: {e}")
        return

    # Run upgrade
    info(f"Upgrading via {install_method}...")
    assert upgrade_cmd is not None  # Always set by this point
    result = subprocess.run(upgrade_cmd, check=False)

    if result.returncode == 0:
        success("CLI upgraded successfully!")
    else:
        error("Upgrade failed")
        raise typer.Exit(1)

    # Optionally pull Docker images
    if pull_images:
        console.print()
        info("Pulling latest Docker images...")
        from sibyl_cli.local import SIBYL_LOCAL_COMPOSE, run_compose

        if SIBYL_LOCAL_COMPOSE.exists():
            run_compose(["pull"])
            success("Docker images updated!")
        else:
            console.print("[dim]No local instance configured. Skipping image pull.[/dim]")


def main() -> None:
    """CLI entry point."""
    app()


if __name__ == "__main__":
    main()
