"""CLI for Sibyl - Oracle of Development Wisdom.

Snappy, async-first CLI with beautiful terminal output.
"""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

from sibyl.config import settings

# SilkCircuit color palette
ELECTRIC_PURPLE = "#e135ff"
NEON_CYAN = "#80ffea"
CORAL = "#ff6ac1"
ELECTRIC_YELLOW = "#f1fa8c"
SUCCESS_GREEN = "#50fa7b"
ERROR_RED = "#ff6363"

console = Console()
app = typer.Typer(
    name="sibyl",
    help="Sibyl - Oracle of Development Wisdom",
    add_completion=False,
    no_args_is_help=True,
)


def styled_header(text: str) -> Text:
    """Create a styled header with SilkCircuit colors."""
    return Text(text, style=f"bold {NEON_CYAN}")


def success(message: str) -> None:
    """Print a success message."""
    console.print(f"[{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] {message}")


def error(message: str) -> None:
    """Print an error message."""
    console.print(f"[{ERROR_RED}]✗[/{ERROR_RED}] {message}")


def info(message: str) -> None:
    """Print an info message."""
    console.print(f"[{NEON_CYAN}]→[/{NEON_CYAN}] {message}")


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

    The server runs as a persistent daemon that clients connect to via HTTP.

    Examples:
        sibyl serve                    # Default: localhost:3334
        sibyl serve -p 9000            # Custom port
        sibyl serve -h 0.0.0.0         # Listen on all interfaces
        sibyl serve -t stdio           # Legacy subprocess mode
    """
    transport_display = {
        "streamable-http": "Streamable HTTP",
        "sse": "Server-Sent Events",
        "stdio": "Standard I/O (subprocess)",
    }.get(transport, transport)

    console.print(
        Panel(
            f"[{ELECTRIC_PURPLE}]Sibyl[/{ELECTRIC_PURPLE}] [{NEON_CYAN}]Oracle of Development Wisdom[/{NEON_CYAN}]\n"
            f"[dim]{transport_display}[/dim]",
            subtitle="[dim]v0.1.0[/dim]",
            border_style=NEON_CYAN,
        )
    )

    if transport != "stdio":
        info(f"Starting daemon on [bold]{host}:{port}[/bold]")
        info(f"MCP endpoint: [bold]http://{host}:{port}/mcp[/bold]")
    else:
        info("Starting in stdio mode (subprocess)")

    from sibyl.main import run_server

    try:
        run_server(host=host, port=port, transport=transport)
    except KeyboardInterrupt:
        console.print(f"\n[{NEON_CYAN}]Server stopped[/{NEON_CYAN}]")


@app.command()
def health() -> None:
    """Check server health status."""

    async def check_health() -> None:
        from sibyl.tools.admin import health_check

        try:
            with Progress(
                SpinnerColumn(style=NEON_CYAN),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Checking health...", total=None)
                status = await health_check()

            # Display results
            table = Table(title="Health Status", border_style=NEON_CYAN)
            table.add_column("Metric", style=ELECTRIC_PURPLE)
            table.add_column("Value", style=NEON_CYAN)

            status_color = SUCCESS_GREEN if status.status == "healthy" else ERROR_RED
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
                console.print(f"\n[{ERROR_RED}]Errors:[/{ERROR_RED}]")
                for err in status.errors:
                    console.print(f"  [{CORAL}]•[/{CORAL}] {err}")

        except Exception as e:
            error(f"Health check failed: {e}")
            console.print(f"\n[{ELECTRIC_YELLOW}]Hint:[/{ELECTRIC_YELLOW}] Is FalkorDB running?")
            console.print(f"  [{NEON_CYAN}]docker compose up -d[/{NEON_CYAN}]")

    asyncio.run(check_health())


@app.command()
def ingest(
    path: Annotated[Path | None, typer.Argument(help="Path to ingest (default: entire repo)")] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Force re-ingestion")] = False,
) -> None:
    """Ingest wisdom documents into the knowledge graph."""

    async def run_ingest() -> None:
        from sibyl.tools.admin import sync_wisdom_docs

        console.print(
            Panel(
                f"[{ELECTRIC_PURPLE}]Ingesting Knowledge[/{ELECTRIC_PURPLE}]",
                border_style=NEON_CYAN,
            )
        )

        path_str = str(path) if path else None

        try:
            with Progress(
                SpinnerColumn(style=NEON_CYAN),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                task = progress.add_task("Ingesting documents...", total=None)

                result = await sync_wisdom_docs(path=path_str, force=force)

                progress.update(task, completed=True)

            # Display results
            if result.success:
                success("Ingestion complete!")
            else:
                error("Ingestion had errors")

            table = Table(border_style=NEON_CYAN)
            table.add_column("Metric", style=ELECTRIC_PURPLE)
            table.add_column("Value", style=NEON_CYAN)
            table.add_row("Files Processed", str(result.files_processed))
            table.add_row("Entities Created", str(result.entities_created))
            table.add_row("Entities Updated", str(result.entities_updated))
            table.add_row("Duration", f"{result.duration_seconds:.2f}s")

            console.print(table)

            if result.errors:
                console.print(f"\n[{ERROR_RED}]Errors:[/{ERROR_RED}]")
                for err in result.errors[:10]:  # Show first 10
                    console.print(f"  [{CORAL}]•[/{CORAL}] {err}")
                if len(result.errors) > 10:
                    console.print(f"  ... and {len(result.errors) - 10} more")

        except Exception as e:
            error(f"Ingestion failed: {e}")
            console.print(f"\n[{ELECTRIC_YELLOW}]Hint:[/{ELECTRIC_YELLOW}] Is FalkorDB running?")
            console.print(f"  [{NEON_CYAN}]docker compose up -d[/{NEON_CYAN}]")

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
            with Progress(
                SpinnerColumn(style=NEON_CYAN),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task(f"Searching for '{query}'...", total=None)

                # Use unified search with optional type filter
                types = [entity_type] if entity_type else None
                response = await unified_search(query=query, types=types, limit=limit)

            console.print(
                f"\n[{ELECTRIC_PURPLE}]Found {response.total} results for[/{ELECTRIC_PURPLE}] "
                f"[{NEON_CYAN}]'{query}'[/{NEON_CYAN}]\n"
            )

            for i, result in enumerate(response.results, 1):
                # Create result panel
                title = f"{i}. {result.name}"
                entity_type_str = result.type
                score = result.score

                content = []
                if result.content:
                    display_content = result.content[:200] + "..." if len(result.content) > 200 else result.content
                    content.append(display_content)
                if result.source:
                    content.append(f"[dim]Source: {result.source}[/dim]")

                panel = Panel(
                    "\n".join(content) if content else "[dim]No description[/dim]",
                    title=f"[{ELECTRIC_PURPLE}]{title}[/{ELECTRIC_PURPLE}]",
                    subtitle=f"[{CORAL}]{entity_type_str}[/{CORAL}] [{ELECTRIC_YELLOW}]{score:.2f}[/{ELECTRIC_YELLOW}]",
                    border_style=NEON_CYAN,
                )
                console.print(panel)

        except Exception as e:
            error(f"Search failed: {e}")
            console.print(f"\n[{ELECTRIC_YELLOW}]Hint:[/{ELECTRIC_YELLOW}] Is FalkorDB running?")
            console.print(f"  [{NEON_CYAN}]docker compose up -d[/{NEON_CYAN}]")

    asyncio.run(run_search())


@app.command()
def stats() -> None:
    """Show knowledge graph statistics."""

    async def get_stats() -> None:
        from sibyl.tools.admin import get_stats as get_graph_stats

        try:
            with Progress(
                SpinnerColumn(style=NEON_CYAN),
                TextColumn("[progress.description]{task.description}"),
                console=console,
            ) as progress:
                progress.add_task("Loading statistics...", total=None)
                stats_data = await get_graph_stats()

            console.print(
                Panel(
                    f"[{ELECTRIC_PURPLE}]Knowledge Graph Statistics[/{ELECTRIC_PURPLE}]",
                    border_style=NEON_CYAN,
                )
            )

            # Entity counts table
            if entities := stats_data.get("entities"):
                table = Table(title="Entities by Type", border_style=NEON_CYAN)
                table.add_column("Type", style=ELECTRIC_PURPLE)
                table.add_column("Count", style=NEON_CYAN, justify="right")

                for entity_type, count in entities.items():
                    table.add_row(entity_type, str(count))

                table.add_row("", "")
                table.add_row("Total", f"[bold]{stats_data.get('total_entities', 0)}[/bold]")

                console.print(table)

            if error_msg := stats_data.get("error"):
                error(f"Failed to get stats: {error_msg}")

        except Exception as e:
            error(f"Stats failed: {e}")
            console.print(f"\n[{ELECTRIC_YELLOW}]Hint:[/{ELECTRIC_YELLOW}] Is FalkorDB running?")
            console.print(f"  [{NEON_CYAN}]docker compose up -d[/{NEON_CYAN}]")

    asyncio.run(get_stats())


@app.command()
def config() -> None:
    """Show current configuration."""
    console.print(
        Panel(
            f"[{ELECTRIC_PURPLE}]Configuration[/{ELECTRIC_PURPLE}]",
            border_style=NEON_CYAN,
        )
    )

    table = Table(border_style=NEON_CYAN)
    table.add_column("Setting", style=ELECTRIC_PURPLE)
    table.add_column("Value", style=NEON_CYAN)

    table.add_row("Server Name", settings.server_name)
    table.add_row("Repo Path", str(settings.conventions_repo_path))
    table.add_row("Log Level", settings.log_level)
    table.add_row("FalkorDB Host", settings.falkordb_host)
    table.add_row("FalkorDB Port", str(settings.falkordb_port))
    table.add_row("Graph Name", settings.falkordb_graph_name)
    table.add_row("Embedding Model", settings.embedding_model)

    console.print(table)


@app.command()
def setup() -> None:
    """Check environment and guide first-time setup."""
    import shutil
    import socket

    console.print(
        Panel(
            f"[{ELECTRIC_PURPLE}]Sibyl Setup[/{ELECTRIC_PURPLE}]",
            border_style=NEON_CYAN,
        )
    )

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
        error(".env.example not found - are you in the mcp-server directory?")
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
        pass  # Connection check - failure means not running

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
            Panel(
                f"[{SUCCESS_GREEN}]All checks passed![/{SUCCESS_GREEN}]\n\n"
                f"[{NEON_CYAN}]Next steps:[/{NEON_CYAN}]\n"
                f"  1. Run [{ELECTRIC_PURPLE}]sibyl ingest[/{ELECTRIC_PURPLE}] to load wisdom docs\n"
                f"  2. Run [{ELECTRIC_PURPLE}]sibyl serve[/{ELECTRIC_PURPLE}] to start the daemon",
                border_style=SUCCESS_GREEN,
            )
        )
    else:
        console.print(
            Panel(
                f"[{ELECTRIC_YELLOW}]Setup incomplete[/{ELECTRIC_YELLOW}]\n\n"
                "Please resolve the issues above, then run setup again.",
                border_style=ELECTRIC_YELLOW,
            )
        )


@app.command()
def version() -> None:
    """Show version information."""
    console.print(
        Panel(
            f"[{ELECTRIC_PURPLE}]Sibyl[/{ELECTRIC_PURPLE}] [{NEON_CYAN}]Oracle of Development Wisdom[/{NEON_CYAN}]\n"
            f"Version 0.1.0\n"
            f"[dim]Graphiti-powered knowledge graph for development conventions[/dim]",
            border_style=NEON_CYAN,
        )
    )


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
