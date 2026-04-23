"""Sibyld CLI - Server daemon commands.

This is the entry point for the sibyld daemon CLI.
Server-only commands: serve, worker, db, up/down/status, setup, generate.

For client commands (task, search, add, etc.), use the `sibyl` CLI.
"""

import asyncio
import contextlib
import shutil
import socket
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import typer

from sibyl.cli.common import (
    ELECTRIC_PURPLE,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_panel,
    info,
)

# Import server-only subcommand apps
from sibyl.cli.db import app as db_app
from sibyl.cli.export import app as export_app
from sibyl.cli.generate import app as generate_app
from sibyl.cli.migrate import app as migrate_app
from sibyl.cli.up_cmd import down, status as up_status, up

# Main app
app = typer.Typer(
    name="sibyld",
    help="Sibyld - Sibyl daemon for AI collective intelligence",
    add_completion=False,
    no_args_is_help=True,
)

# Register subcommand groups
app.add_typer(db_app, name="db")
app.add_typer(export_app, name="export")
app.add_typer(generate_app, name="generate")
app.add_typer(migrate_app, name="migrate")

# Register top-level commands from up_cmd
app.command("up")(up)
app.command("down")(down)
app.command("status")(up_status)


# ============================================================================
# Server commands
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
    reload: Annotated[
        bool, typer.Option("--reload", "-r", help="Enable hot reload (dev mode)")
    ] = False,
) -> None:
    """Start the Sibyl MCP server daemon.

    Examples:
        sibyld serve                   # Production mode
        sibyld serve --reload          # Dev mode with hot reload
        sibyld serve -p 9000           # Custom port
        sibyld serve -t stdio          # Legacy subprocess mode
    """
    from sibyl.config import settings

    # Use settings defaults if not specified
    host = host or settings.server_host
    port = port or settings.server_port

    if reload:
        _serve_with_reload(host, port)
    else:
        from sibyl.main import run_server

        try:
            run_server(host=host, port=port, transport=transport)
        except KeyboardInterrupt:
            console.print(f"\n[{NEON_CYAN}]Shutting down...[/{NEON_CYAN}]")


def _serve_with_reload(host: str, port: int) -> None:
    """Start server with hot reload using uvicorn."""
    import os
    import signal
    import subprocess
    import sys

    console.print(f"[{ELECTRIC_PURPLE}]Starting Sibyl in dev mode...[/{ELECTRIC_PURPLE}]")
    console.print(f"[{NEON_CYAN}]Hot reload enabled - watching for changes[/{NEON_CYAN}]")
    console.print(f"[dim]API: http://{host}:{port}/api[/dim]")
    console.print(f"[dim]MCP: http://{host}:{port}/mcp[/dim]")
    console.print(f"[dim]Docs: http://{host}:{port}/api/docs[/dim]")
    console.print("[dim]Debug stacks: kill -USR1 <api-child-pid>[/dim]\n")

    env = os.environ.copy()
    env.setdefault("PYTHONFAULTHANDLER", "1")
    env.setdefault("SIBYL_DEV_DIAGNOSTICS", "1")

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
        env=env,
        start_new_session=True,
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
def worker(
    burst: Annotated[
        bool, typer.Option("--burst", "-b", help="Process jobs and exit (don't run continuously)")
    ] = False,
) -> None:
    """Start the background job worker.

    Processes crawl jobs, sync tasks, and other background work.
    Uses Redis (via FalkorDB) for job persistence and retries.

    Examples:
        sibyld worker              # Run continuously (production)
        sibyld worker --burst      # Process pending jobs and exit

    For dev mode with hot reload, use arq directly:
        arq sibyl.jobs.worker.WorkerSettings --watch src
    """
    from sibyl.config import settings

    if settings.resolved_coordination_backend == "local":
        console.print(
            f"[{NEON_CYAN}]Local coordination runs background jobs in-process under "
            f"[{ELECTRIC_PURPLE}]sibyld serve[/{ELECTRIC_PURPLE}].[/{NEON_CYAN}]"
        )
        return

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


def _tcp_service_running(host: str, port: int) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False


def _check_env_file() -> bool:
    from sibyl.cli.common import error, success

    env_file = Path(".env")
    env_example = Path(".env.example")
    if env_file.exists():
        success(".env file exists")
        return True
    if env_example.exists():
        info("Creating .env from .env.example...")
        shutil.copy(env_example, env_file)
        success(".env file created - please update with your values")
        return False

    error(".env.example not found - are you in the project directory?")
    return False


def _check_openai_api_key_configured(settings: Any) -> bool:
    from sibyl.cli.common import error, success

    api_key = settings.openai_api_key.get_secret_value()
    if api_key and not api_key.startswith("sk-your"):
        success("OpenAI API key configured")
        return True

    error("OpenAI API key not set")
    console.print(f"  [{NEON_CYAN}]Set SIBYL_OPENAI_API_KEY in .env[/{NEON_CYAN}]")
    return False


def _check_docker_available() -> bool:
    from sibyl.cli.common import error, success

    if shutil.which("docker") is not None:
        success("Docker available")
        return True

    error("Docker not found")
    console.print(f"  [{NEON_CYAN}]Install Docker: https://docs.docker.com/get-docker/[/{NEON_CYAN}]")
    return False


def _check_surreal_services(settings: Any) -> bool:
    from sibyl.cli.common import error, success

    all_good = True
    surreal_url = settings.resolved_surreal_url
    parsed_surreal = urlparse(surreal_url)

    if parsed_surreal.scheme in {"ws", "wss", "http", "https"} and parsed_surreal.hostname:
        surreal_host = parsed_surreal.hostname
        surreal_port = parsed_surreal.port or 8000
        if _tcp_service_running(surreal_host, surreal_port):
            success(f"SurrealDB running on {surreal_host}:{surreal_port}")
        else:
            error(f"SurrealDB not running on {surreal_host}:{surreal_port}")
            console.print(f"  [{NEON_CYAN}]Start with: docker compose up -d[/{NEON_CYAN}]")
            all_good = False
    else:
        info(f"SurrealDB configured via {surreal_url}")

    redis_host = settings.redis_host or "127.0.0.1"
    redis_port = settings.redis_port or 6381
    if _tcp_service_running(redis_host, redis_port):
        success(f"Redis/Valkey running on {redis_host}:{redis_port}")
    else:
        error(f"Redis/Valkey not running on {redis_host}:{redis_port}")
        console.print(f"  [{NEON_CYAN}]Start with: docker compose up -d[/{NEON_CYAN}]")
        all_good = False

    return all_good


def _check_legacy_services(settings: Any) -> bool:
    from sibyl.cli.common import error, success

    if _tcp_service_running(settings.falkordb_host, settings.falkordb_port):
        success(f"FalkorDB running on {settings.falkordb_host}:{settings.falkordb_port}")
        return True

    error(f"FalkorDB not running on {settings.falkordb_host}:{settings.falkordb_port}")
    console.print(f"  [{NEON_CYAN}]Start with: docker compose up -d[/{NEON_CYAN}]")
    return False


@app.command()
def setup() -> None:
    """Check environment and guide first-time setup."""
    from sibyl.config import settings

    console.print(create_panel(f"[{ELECTRIC_PURPLE}]Sibyl Setup[/{ELECTRIC_PURPLE}]"))

    all_good = _check_env_file()
    all_good = _check_openai_api_key_configured(settings) and all_good
    all_good = _check_docker_available() and all_good
    all_good = (
        _check_surreal_services(settings)
        if settings.store == "surreal"
        else _check_legacy_services(settings)
    ) and all_good

    # Summary
    console.print()
    if all_good:
        console.print(
            create_panel(
                f"[{SUCCESS_GREEN}]All checks passed![/{SUCCESS_GREEN}]\n\n"
                f"[{NEON_CYAN}]Next steps:[/{NEON_CYAN}]\n"
                f"  1. Run [{ELECTRIC_PURPLE}]sibyld serve[/{ELECTRIC_PURPLE}] to start the daemon"
            )
        )
    else:
        console.print(
            create_panel(
                f"[{NEON_CYAN}]Setup incomplete[/{NEON_CYAN}]\n\n"
                "Please resolve the issues above, then run setup again."
            )
        )


@app.command()
def version() -> None:
    """Show version information."""
    console.print(
        create_panel(
            f"[{ELECTRIC_PURPLE}]Sibyld[/{ELECTRIC_PURPLE}] [{NEON_CYAN}]Sibyl Daemon[/{NEON_CYAN}]\n"
            f"Version 0.1.0\n"
            f"[dim]Knowledge graph and task workflow server[/dim]"
        )
    )


def main() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    main()
