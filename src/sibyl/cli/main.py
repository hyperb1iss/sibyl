"""Main CLI application - ties all subcommands together.

This is the entry point for the sibyl CLI.
"""

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from sibyl.cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_panel,
    create_table,
    error,
    info,
    print_db_hint,
    spinner,
    success,
)

# Import subcommand apps
from sibyl.cli.db import app as db_app
from sibyl.cli.entity import app as entity_app
from sibyl.cli.explore import app as explore_app
from sibyl.cli.export import app as export_app
from sibyl.cli.generate import app as generate_app
from sibyl.cli.project import app as project_app
from sibyl.cli.source import app as source_app
from sibyl.cli.task import app as task_app

# Main app
app = typer.Typer(
    name="sibyl",
    help="Sibyl - Oracle of Development Wisdom",
    add_completion=False,
    no_args_is_help=True,
)

# Register subcommand groups
app.add_typer(task_app, name="task")
app.add_typer(project_app, name="project")
app.add_typer(entity_app, name="entity")
app.add_typer(explore_app, name="explore")
app.add_typer(source_app, name="source")
app.add_typer(export_app, name="export")
app.add_typer(db_app, name="db")
app.add_typer(generate_app, name="generate")


# ============================================================================
# Root-level commands (existing functionality)
# ============================================================================


@app.command()
def serve(
    host: str = typer.Option("localhost", "--host", "-h", help="Host to bind to"),
    port: int = typer.Option(3334, "--port", "-p", help="Port to listen on"),
    transport: str = typer.Option(
        "streamable-http",
        "--transport",
        "-t",
        help="Transport type (streamable-http, sse, stdio)",
    ),
) -> None:
    """Start the Sibyl MCP server daemon.

    Examples:
        sibyl serve                    # Default: localhost:3334
        sibyl serve -p 9000            # Custom port
        sibyl serve -h 0.0.0.0         # Listen on all interfaces
        sibyl serve -t stdio           # Legacy subprocess mode
    """
    from sibyl.main import run_server

    try:
        run_server(host=host, port=port, transport=transport)
    except KeyboardInterrupt:
        console.print(f"\n[{NEON_CYAN}]Shutting down...[/{NEON_CYAN}]")


@app.command()
def health() -> None:
    """Check server health status."""

    async def check_health() -> None:
        from sibyl.tools.admin import health_check

        try:
            with spinner("Checking health...") as progress:
                progress.add_task("Checking health...", total=None)
                status = await health_check()

            table = create_table("Health Status", "Metric", "Value")
            status_color = SUCCESS_GREEN if status.status == "healthy" else CORAL
            table.add_row("Status", f"[{status_color}]{status.status}[/{status_color}]")
            table.add_row("Server", status.server_name)
            table.add_row("Uptime", f"{status.uptime_seconds:.1f}s")
            table.add_row("Graph Connected", "Yes" if status.graph_connected else "No")

            if status.search_latency_ms:
                table.add_row("Search Latency", f"{status.search_latency_ms:.2f}ms")

            if status.entity_counts:
                for entity_type, count in status.entity_counts.items():
                    table.add_row(f"Entities: {entity_type}", str(count))

            console.print(table)

            if status.errors:
                console.print(f"\n[{CORAL}]Errors:[/{CORAL}]")
                for err in status.errors:
                    console.print(f"  [{CORAL}]•[/{CORAL}] {err}")

        except Exception as e:
            error(f"Health check failed: {e}")
            print_db_hint()

    asyncio.run(check_health())


@app.command()
def ingest(
    path: Annotated[Path | None, typer.Argument(help="Path to ingest (default: entire repo)")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force re-ingestion")] = False,
) -> None:
    """Ingest wisdom documents into the knowledge graph."""

    async def run_ingest() -> None:
        from sibyl.tools.admin import sync_wisdom_docs

        console.print(create_panel(f"[{ELECTRIC_PURPLE}]Ingesting Knowledge[/{ELECTRIC_PURPLE}]"))

        path_str = str(path) if path else None

        try:
            with spinner("Ingesting documents...") as progress:
                progress.add_task("Ingesting documents...", total=None)
                result = await sync_wisdom_docs(path=path_str, force=force)

            if result.success:
                success("Ingestion complete!")
            else:
                error("Ingestion had errors")

            table = create_table(None, "Metric", "Value")
            table.add_row("Files Processed", str(result.files_processed))
            table.add_row("Entities Created", str(result.entities_created))
            table.add_row("Entities Updated", str(result.entities_updated))
            table.add_row("Duration", f"{result.duration_seconds:.2f}s")
            console.print(table)

            if result.errors:
                console.print(f"\n[{CORAL}]Errors:[/{CORAL}]")
                for err in result.errors[:10]:
                    console.print(f"  [{CORAL}]•[/{CORAL}] {err}")
                if len(result.errors) > 10:
                    console.print(f"  ... and {len(result.errors) - 10} more")

        except Exception as e:
            error(f"Ingestion failed: {e}")
            print_db_hint()

    asyncio.run(run_ingest())


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max results"),
    entity_type: str = typer.Option(None, "--type", "-t", help="Filter by entity type"),
) -> None:
    """Search the knowledge graph."""

    async def run_search() -> None:
        from sibyl.tools.core import search as unified_search

        try:
            with spinner(f"Searching for '{query}'...") as progress:
                progress.add_task(f"Searching for '{query}'...", total=None)
                types = [entity_type] if entity_type else None
                response = await unified_search(query=query, types=types, limit=limit)

            console.print(
                f"\n[{ELECTRIC_PURPLE}]Found {response.total} results for[/{ELECTRIC_PURPLE}] "
                f"[{NEON_CYAN}]'{query}'[/{NEON_CYAN}]\n"
            )

            for i, result in enumerate(response.results, 1):
                title = f"{i}. {result.name}"
                content = []
                if result.content:
                    display_content = result.content[:200] + "..." if len(result.content) > 200 else result.content
                    content.append(display_content)
                if result.source:
                    content.append(f"[dim]Source: {result.source}[/dim]")

                panel = create_panel(
                    "\n".join(content) if content else "[dim]No description[/dim]",
                    title=title,
                    subtitle=f"[{CORAL}]{result.type}[/{CORAL}] [{ELECTRIC_YELLOW}]{result.score:.2f}[/{ELECTRIC_YELLOW}]",
                )
                console.print(panel)

        except Exception as e:
            error(f"Search failed: {e}")
            print_db_hint()

    asyncio.run(run_search())


@app.command()
def stats() -> None:
    """Show knowledge graph statistics."""

    async def get_stats() -> None:
        from sibyl.tools.admin import get_stats as get_graph_stats

        try:
            with spinner("Loading statistics...") as progress:
                progress.add_task("Loading statistics...", total=None)
                stats_data = await get_graph_stats()

            console.print(create_panel(f"[{ELECTRIC_PURPLE}]Knowledge Graph Statistics[/{ELECTRIC_PURPLE}]"))

            if entities := stats_data.get("entities"):
                table = create_table("Entities by Type", "Type", "Count")
                for etype, count in entities.items():
                    table.add_row(etype, str(count))
                table.add_row("", "")
                table.add_row("Total", f"[bold]{stats_data.get('total_entities', 0)}[/bold]")
                console.print(table)

            if error_msg := stats_data.get("error"):
                error(f"Failed to get stats: {error_msg}")

        except Exception as e:
            error(f"Stats failed: {e}")
            print_db_hint()

    asyncio.run(get_stats())


@app.command("config")
def show_config() -> None:
    """Show current configuration."""
    from sibyl.config import settings

    console.print(create_panel(f"[{ELECTRIC_PURPLE}]Configuration[/{ELECTRIC_PURPLE}]"))

    table = create_table(None, "Setting", "Value")
    table.add_row("Server Name", settings.server_name)
    table.add_row("Repo Path", str(settings.conventions_repo_path))
    table.add_row("Log Level", settings.log_level)
    table.add_row("FalkorDB Host", settings.falkordb_host)
    table.add_row("FalkorDB Port", str(settings.falkordb_port))
    table.add_row("Graph Name", settings.falkordb_graph_name)
    table.add_row("Embedding Model", settings.embedding_model)
    console.print(table)


@app.command()
def setup() -> None:  # noqa: PLR0915
    """Check environment and guide first-time setup."""
    import shutil
    import socket

    from sibyl.config import settings

    console.print(create_panel(f"[{ELECTRIC_PURPLE}]Sibyl Setup[/{ELECTRIC_PURPLE}]"))

    all_good = True

    # Check 1: .env file exists
    env_file = Path(".env")
    env_example = Path(".env.example")
    if env_file.exists():
        success(".env file exists")
    elif env_example.exists():
        info("Creating .env from .env.example...")
        shutil.copy(env_example, env_file)
        success(".env file created - please update with your values")
        all_good = False
    else:
        error(".env.example not found - are you in the project directory?")
        all_good = False

    # Check 2: OpenAI API key
    api_key = settings.openai_api_key.get_secret_value()
    if api_key and not api_key.startswith("sk-your"):
        success("OpenAI API key configured")
    else:
        error("OpenAI API key not set")
        console.print(f"  [{NEON_CYAN}]Set SIBYL_OPENAI_API_KEY in .env[/{NEON_CYAN}]")
        all_good = False

    # Check 3: Docker available
    docker_available = shutil.which("docker") is not None
    if docker_available:
        success("Docker available")
    else:
        error("Docker not found")
        console.print(f"  [{NEON_CYAN}]Install Docker: https://docs.docker.com/get-docker/[/{NEON_CYAN}]")
        all_good = False

    # Check 4: FalkorDB connection
    falkor_running = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((settings.falkordb_host, settings.falkordb_port))
        sock.close()
        falkor_running = result == 0
    except Exception:  # noqa: S110
        pass  # Socket connection check - failure means not running

    if falkor_running:
        success(f"FalkorDB running on {settings.falkordb_host}:{settings.falkordb_port}")
    else:
        error(f"FalkorDB not running on {settings.falkordb_host}:{settings.falkordb_port}")
        console.print(f"  [{NEON_CYAN}]Start with: docker compose up -d[/{NEON_CYAN}]")
        all_good = False

    # Summary
    console.print()
    if all_good:
        console.print(
            create_panel(
                f"[{SUCCESS_GREEN}]All checks passed![/{SUCCESS_GREEN}]\n\n"
                f"[{NEON_CYAN}]Next steps:[/{NEON_CYAN}]\n"
                f"  1. Run [{ELECTRIC_PURPLE}]sibyl ingest[/{ELECTRIC_PURPLE}] to load docs\n"
                f"  2. Run [{ELECTRIC_PURPLE}]sibyl serve[/{ELECTRIC_PURPLE}] to start the daemon"
            )
        )
    else:
        console.print(
            create_panel(
                f"[{ELECTRIC_YELLOW}]Setup incomplete[/{ELECTRIC_YELLOW}]\n\n"
                "Please resolve the issues above, then run setup again."
            )
        )


@app.command()
def version() -> None:
    """Show version information."""
    console.print(
        create_panel(
            f"[{ELECTRIC_PURPLE}]Sibyl[/{ELECTRIC_PURPLE}] [{NEON_CYAN}]Oracle of Development Wisdom[/{NEON_CYAN}]\n"
            f"Version 0.1.0\n"
            f"[dim]Graphiti-powered knowledge graph for development conventions[/dim]"
        )
    )


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
