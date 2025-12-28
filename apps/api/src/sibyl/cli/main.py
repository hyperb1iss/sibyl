"""Main CLI application - ties all subcommands together.

This is the entry point for the sibyl CLI.
All commands communicate with the REST API to ensure proper event broadcasting.
"""

import asyncio
import contextlib
from pathlib import Path
from typing import Annotated

import typer

from sibyl.cli.auth import app as auth_app
from sibyl.cli.client import SibylClientError, get_client
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
    print_json,
    run_async,
    spinner,
    success,
)

# Import subcommand apps
from sibyl.cli.config_cmd import app as config_app
from sibyl.cli.context import app as context_app
from sibyl.cli.crawl import app as crawl_app
from sibyl.cli.db import app as db_app
from sibyl.cli.entity import app as entity_app
from sibyl.cli.epic import app as epic_app
from sibyl.cli.explore import app as explore_app
from sibyl.cli.export import app as export_app
from sibyl.cli.generate import app as generate_app
from sibyl.cli.org import app as org_app
from sibyl.cli.project import app as project_app
from sibyl.cli.source import app as source_app
from sibyl.cli.state import set_context_override
from sibyl.cli.task import app as task_app
from sibyl.cli.up_cmd import down, status as up_status, up

# Main app
app = typer.Typer(
    name="sibyl",
    help="Sibyl - Oracle of Development Wisdom",
    add_completion=False,
    no_args_is_help=False,  # We handle no-args ourselves for first-run UX
)


# Register subcommand groups
app.add_typer(task_app, name="task")
app.add_typer(epic_app, name="epic")
app.add_typer(project_app, name="project")
app.add_typer(entity_app, name="entity")
app.add_typer(explore_app, name="explore")
app.add_typer(source_app, name="source")
app.add_typer(crawl_app, name="crawl")
app.add_typer(export_app, name="export")
app.add_typer(db_app, name="db")
app.add_typer(generate_app, name="generate")
app.add_typer(auth_app, name="auth")
app.add_typer(org_app, name="org")
app.add_typer(config_app, name="config")
app.add_typer(context_app, name="context")

# Register top-level commands from up_cmd
app.command("up")(up)
app.command("down")(down)
app.command("status")(up_status)


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1."""
    if "Cannot connect" in str(e):
        console.print()
        console.print(f"  [{CORAL}]×[/{CORAL}] [bold]Cannot connect to Sibyl server[/bold]")
        console.print()
        console.print(
            f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl up[/bold {NEON_CYAN}]      [dim]Start services[/dim]"
        )
        console.print(
            f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl status[/bold {NEON_CYAN}]  [dim]Check status[/dim]"
        )
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
# Root-level commands (existing functionality)
# ============================================================================


@app.command()
def serve(
    host: str | None = typer.Option(
        None, "--host", "-h", help="Host to bind to (env: SIBYL_SERVER_HOST)"
    ),
    port: int | None = typer.Option(
        None, "--port", "-p", help="Port to listen on (env: SIBYL_SERVER_PORT)"
    ),
    transport: str = typer.Option(
        "streamable-http",
        "--transport",
        "-t",
        help="Transport type (streamable-http, sse, stdio)",
    ),
) -> None:
    """Start the Sibyl MCP server daemon.

    Examples:
        sibyl serve                    # Uses env vars or defaults
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
def dev(
    host: str = typer.Option("0.0.0.0", "--host", "-h", help="Host to bind to"),  # noqa: S104
    port: int = typer.Option(3334, "--port", "-p", help="Port to listen on"),
) -> None:
    """Start the server in development mode with hot reload.

    Watches for file changes and automatically restarts the server.
    Uses uvicorn's --reload flag for instant feedback during development.

    Examples:
        sibyl dev                      # Default: 0.0.0.0:3334
        sibyl dev -p 9000              # Custom port
    """
    import os
    import signal
    import subprocess
    import sys

    console.print(f"[{ELECTRIC_PURPLE}]Starting Sibyl in dev mode...[/{ELECTRIC_PURPLE}]")
    console.print(f"[{NEON_CYAN}]Hot reload enabled - watching for changes[/{NEON_CYAN}]")
    console.print(f"[dim]API: http://{host}:{port}/api[/dim]")
    console.print(f"[dim]MCP: http://{host}:{port}/mcp[/dim]")
    console.print(f"[dim]Docs: http://{host}:{port}/api/docs[/dim]\n")

    # preexec_fn=os.setsid makes uvicorn the leader of a new process group
    # This allows us to kill it AND all its children (reloader spawns workers)
    process = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "uvicorn",
            "sibyl.main:create_dev_app",
            "--factory",
            "--host",
            host,
            "--port",
            str(port),
            "--reload",
            "--reload-dir",
            "src",
            "--log-level",
            "warning",
        ],
        start_new_session=True,  # New process group (thread-safe)
    )

    def kill_process_group() -> None:
        """Kill uvicorn and ALL its children via process group."""
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
            process.wait(timeout=3)
        except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)

    try:
        process.wait()
    except KeyboardInterrupt:
        console.print(f"\n[{NEON_CYAN}]Shutting down...[/{NEON_CYAN}]")
        kill_process_group()


@app.command()
def health(
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Check server health status. Default: JSON output."""

    @run_async
    async def check_health() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Checking health...") as progress:
                    progress.add_task("Checking health...", total=None)
                    status = await client.health()
            else:
                status = await client.health()

            # JSON output (default)
            if not table_out:
                print_json(status)
                return

            # Table output
            table = create_table("Health Status", "Metric", "Value")
            status_str = status.get("status", "unknown")
            status_color = SUCCESS_GREEN if status_str == "healthy" else CORAL
            table.add_row("Status", f"[{status_color}]{status_str}[/{status_color}]")
            table.add_row("Server", status.get("server_name", "unknown"))
            table.add_row("Uptime", f"{status.get('uptime_seconds', 0):.1f}s")
            table.add_row("Graph Connected", "Yes" if status.get("graph_connected") else "No")

            if status.get("search_latency_ms"):
                table.add_row("Search Latency", f"{status['search_latency_ms']:.2f}ms")

            if entity_counts := status.get("entity_counts"):
                for entity_type, count in entity_counts.items():
                    table.add_row(f"Entities: {entity_type}", str(count))

            console.print(table)

            if status.get("errors"):
                console.print(f"\n[{CORAL}]Errors:[/{CORAL}]")
                for err in status["errors"]:
                    console.print(f"  [{CORAL}]•[/{CORAL}] {err}")

        except SibylClientError as e:
            _handle_client_error(e)

    check_health()


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-n", help="Max results"),
    entity_type: str = typer.Option(None, "--type", "-T", help="Filter by entity type"),
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Search the knowledge graph. Default: JSON output."""

    @run_async
    async def run_search() -> None:
        client = get_client()

        try:
            types = [entity_type] if entity_type else None

            if table_out:
                with spinner(f"Searching for '{query}'...") as progress:
                    progress.add_task(f"Searching for '{query}'...", total=None)
                    response = await client.search(query=query, types=types, limit=limit)
            else:
                response = await client.search(query=query, types=types, limit=limit)

            results = response.get("results", [])

            # JSON output (default)
            if not table_out:
                print_json(response)
                return

            # Table output
            total = response.get("total", len(results))
            console.print(
                f"\n[{ELECTRIC_PURPLE}]Found {total} results for[/{ELECTRIC_PURPLE}] "
                f"[{NEON_CYAN}]'{query}'[/{NEON_CYAN}]\n"
            )

            for i, result in enumerate(results, 1):
                title = f"{i}. {result.get('name', 'Unknown')}"
                content = []
                if result.get("content"):
                    display_content = (
                        result["content"][:200] + "..."
                        if len(result["content"]) > 200
                        else result["content"]
                    )
                    content.append(display_content)
                if result.get("source"):
                    content.append(f"[dim]Source: {result['source']}[/dim]")

                panel = create_panel(
                    "\n".join(content) if content else "[dim]No description[/dim]",
                    title=title,
                    subtitle=f"[{CORAL}]{result.get('type', '')}[/{CORAL}] [{ELECTRIC_YELLOW}]{result.get('score', 0):.2f}[/{ELECTRIC_YELLOW}]",
                )
                console.print(panel)

        except SibylClientError as e:
            _handle_client_error(e)

    run_search()


@app.command("add")
def add_knowledge(
    title: Annotated[str, typer.Argument(help="Title of the knowledge")],
    content: Annotated[str | None, typer.Argument(help="Content/description")] = None,
    entity_type: Annotated[
        str, typer.Option("--type", "-T", help="Entity type: episode, pattern, rule")
    ] = "episode",
    category: Annotated[str | None, typer.Option("--category", "-c", help="Category")] = None,
    languages: Annotated[
        str | None, typer.Option("--languages", "-l", help="Comma-separated languages")
    ] = None,
    tags: Annotated[str | None, typer.Option("--tags", help="Comma-separated tags")] = None,
    auto_link: Annotated[
        bool, typer.Option("--auto-link", "-a", help="Auto-link to related entities")
    ] = False,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Quickly add knowledge to the graph. Default: JSON output.

    Examples:
        sibyl add "Redis insight" "Connection pool sizing..."
        sibyl add "OAuth pattern" --type pattern -l python
        sibyl add "Debugging tip" -c debugging --auto-link
    """

    @run_async
    async def run_add() -> None:
        client = get_client()

        try:
            lang_list = [lang.strip() for lang in languages.split(",")] if languages else None
            tag_list = [tag.strip() for tag in tags.split(",")] if tags else None
            actual_content = content or title

            if table_out:
                with spinner("Adding knowledge...") as progress:
                    progress.add_task("Adding knowledge...", total=None)
                    response = await client.add_knowledge(
                        title=title,
                        content=actual_content,
                        entity_type=entity_type,
                        category=category,
                        languages=lang_list,
                        tags=tag_list,
                        auto_link=auto_link,
                    )
            else:
                response = await client.add_knowledge(
                    title=title,
                    content=actual_content,
                    entity_type=entity_type,
                    category=category,
                    languages=lang_list,
                    tags=tag_list,
                    auto_link=auto_link,
                )

            # JSON output (default)
            if not table_out:
                print_json(response)
                return

            # Table output
            if response.get("id"):
                success(f"Added: {response['id']}")
                if auto_link:
                    info("Auto-linked to related entities")
            else:
                error("Failed to add knowledge")

        except SibylClientError as e:
            _handle_client_error(e)

    run_add()


@app.command()
def stats(
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show knowledge graph statistics. Default: JSON output."""

    @run_async
    async def get_stats() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Loading statistics...") as progress:
                    progress.add_task("Loading statistics...", total=None)
                    stats_data = await client.stats()
            else:
                stats_data = await client.stats()

            # JSON output (default)
            if not table_out:
                print_json(stats_data)
                return

            # Table output
            console.print(
                create_panel(f"[{ELECTRIC_PURPLE}]Knowledge Graph Statistics[/{ELECTRIC_PURPLE}]")
            )

            if entities := stats_data.get("entities"):
                table = create_table("Entities by Type", "Type", "Count")
                for etype, count in entities.items():
                    table.add_row(etype, str(count))
                table.add_row("", "")
                table.add_row("Total", f"[bold]{stats_data.get('total_entities', 0)}[/bold]")
                console.print(table)

            if error_msg := stats_data.get("error"):
                error(f"Failed to get stats: {error_msg}")

        except SibylClientError as e:
            _handle_client_error(e)

    get_stats()


# Note: `sibyl config` command group is now in config_cmd.py


@app.command()
def setup() -> None:
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
        console.print(
            f"  [{NEON_CYAN}]Install Docker: https://docs.docker.com/get-docker/[/{NEON_CYAN}]"
        )
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


@app.command()
def context(
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show current project context based on directory.

    Displays which project is linked to the current working directory.
    Task commands will automatically scope to this project.

    Examples:
        sibyl context              # JSON: current project context
        sibyl context -t           # Table: human-readable display
    """
    from sibyl.cli.config_store import get_current_context

    project_id, matched_path = get_current_context()

    if not table_out:
        # JSON output
        import json

        result = {
            "project_id": project_id,
            "matched_path": matched_path,
            "cwd": str(Path.cwd()),
        }
        console.print(json.dumps(result, indent=2))
        return

    # Table output
    if project_id:

        @run_async
        async def _show_context() -> None:
            client = get_client()
            try:
                project = await client.get_entity(project_id)
                project_name = project.get("name", "Unknown")

                console.print(f"\n[{ELECTRIC_PURPLE}]Current Context[/{ELECTRIC_PURPLE}]\n")
                console.print(f"  Project: [{NEON_CYAN}]{project_name}[/{NEON_CYAN}]")
                console.print(f"  ID:      [{CORAL}]{project_id}[/{CORAL}]")
                console.print(f"  Path:    [{SUCCESS_GREEN}]{matched_path}[/{SUCCESS_GREEN}]")
                console.print("\n[dim]Task commands will auto-scope to this project[/dim]")

            except SibylClientError:
                # Project not found on server - still show the mapping
                console.print(f"\n[{ELECTRIC_PURPLE}]Current Context[/{ELECTRIC_PURPLE}]\n")
                console.print(
                    f"  Project: [{CORAL}]{project_id}[/{CORAL}] [dim](not found on server)[/dim]"
                )
                console.print(f"  Path:    {matched_path}")

        _show_context()
    else:
        info("No project context for current directory")
        info("Use 'sibyl project link' to link a directory to a project")


@app.command()
def worker(
    burst: Annotated[
        bool, typer.Option("--burst", "-b", help="Process jobs and exit (don't run continuously)")
    ] = False,
) -> None:
    """Start the background job worker.

    Processes crawl jobs, sync tasks, and other background work.
    Uses Redis (via FalkorDB) for job persistence and retries.

    Examples:
        sibyl worker           # Run continuously
        sibyl worker --burst   # Process pending jobs and exit
    """

    from arq import run_worker

    from sibyl.jobs.worker import WorkerSettings

    # Python 3.14+ requires an explicit event loop before arq's run_worker
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        run_worker(WorkerSettings, burst=burst)
    except KeyboardInterrupt:
        info("Worker stopped")
    finally:
        loop.close()


@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            "-C",  # Capital C to avoid conflict with subcommand -c options
            help="Use a specific context for this command (overrides active context)",
            envvar="SIBYL_CONTEXT",
        ),
    ] = None,
) -> None:
    """Sibyl - Oracle of Development Wisdom.

    Use --context/-C to override the active context for any command.
    You can also set SIBYL_CONTEXT environment variable.
    """
    # Set context override before any subcommand runs
    if context:
        set_context_override(context)

    # Handle no-args case with welcome message
    if ctx.invoked_subcommand is not None:
        return

    # No subcommand - show welcome or help
    from sibyl.cli.onboarding import needs_onboarding, show_first_run_message

    if needs_onboarding():
        # First-time user - show friendly welcome
        show_first_run_message()
    else:
        # Configured user - show quick status and help
        _show_quick_start()


def _show_quick_start() -> None:
    """Show quick start tips for configured users."""
    console.print()
    console.print(
        f"  [{ELECTRIC_PURPLE}]◈[/{ELECTRIC_PURPLE}] [bold {ELECTRIC_PURPLE}]Sibyl[/bold {ELECTRIC_PURPLE}] [dim]─ Oracle of Development Wisdom[/dim]"
    )
    console.print()
    console.print(
        f'    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl search[/bold {NEON_CYAN}] [white]"query"[/white]   [dim]Search knowledge[/dim]'
    )
    console.print(
        f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl task list[/bold {NEON_CYAN}]        [dim]View tasks[/dim]"
    )
    console.print(
        f'    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl add[/bold {NEON_CYAN}] [white]"title" "..."[/white] [dim]Capture learning[/dim]'
    )
    console.print(
        f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl up[/bold {NEON_CYAN}]               [dim]Start services[/dim]"
    )
    console.print()
    console.print(
        f"    [dim]Run[/dim] [bold {NEON_CYAN}]sibyl --help[/bold {NEON_CYAN}] [dim]for all commands[/dim]"
    )
    console.print()


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
