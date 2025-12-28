"""Commands for running Sibyl locally.

`sibyl up` starts all services (FalkorDB, PostgreSQL, API server).
`sibyl down` stops everything.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated

import typer

from sibyl.cli.common import (
    ELECTRIC_PURPLE,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    error,
    info,
    success,
    warn,
)


# Find project root (where docker-compose.yml lives)
def _find_project_root() -> Path | None:
    """Find the Sibyl project root directory."""
    # Check common locations
    candidates = [
        Path.cwd(),  # Current directory
        Path(__file__).parent.parent.parent.parent,  # Relative to this file
        Path.home() / "dev" / "sibyl",  # Common dev location
    ]

    for path in candidates:
        if (path / "docker-compose.yml").exists():
            return path

    return None


def _run_docker_compose(args: list[str], project_root: Path) -> subprocess.CompletedProcess[str]:
    """Run docker compose command."""
    cmd = ["docker", "compose", *args]
    return subprocess.run(cmd, check=False, cwd=project_root, capture_output=True, text=True)  # noqa: S603


def _check_docker() -> bool:
    """Check if Docker is available and running."""
    try:
        result = subprocess.run(["docker", "info"], check=False, capture_output=True, text=True)  # noqa: S607
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _wait_for_services(project_root: Path, timeout: int = 60) -> bool:
    """Wait for services to be healthy."""
    start = time.time()
    while time.time() - start < timeout:
        result = _run_docker_compose(["ps", "--format", "json"], project_root)
        if result.returncode == 0:
            # Check if services are running
            if "running" in result.stdout.lower():
                return True
        time.sleep(2)
    return False


def up(
    detach: Annotated[
        bool,
        typer.Option("--detach", "-d", help="Run in background"),
    ] = False,
    with_worker: Annotated[
        bool,
        typer.Option("--with-worker", "-w", help="Also start job worker"),
    ] = False,
    skip_docker: Annotated[
        bool,
        typer.Option("--skip-docker", help="Skip Docker services (use existing)"),
    ] = False,
) -> None:
    """Start Sibyl services locally.

    Starts FalkorDB, PostgreSQL, and the API server.
    """
    project_root = _find_project_root()
    if not project_root:
        error("Could not find Sibyl project root (docker-compose.yml)")
        error("Run this command from the Sibyl project directory,")
        error("or install Sibyl in editable mode: uv tool install -e /path/to/sibyl")
        raise typer.Exit(1)

    console.print(
        f"\n[{ELECTRIC_PURPLE}]Starting Sibyl[/{ELECTRIC_PURPLE}] [dim]from {project_root}[/dim]\n"
    )

    # Check Docker
    if not skip_docker:
        if not _check_docker():
            error("Docker is not running. Please start Docker first.")
            raise typer.Exit(1)

        # Start Docker services
        with console.status(f"[{NEON_CYAN}]Starting Docker services...[/{NEON_CYAN}]"):
            result = _run_docker_compose(["up", "-d"], project_root)
            if result.returncode != 0:
                error("Failed to start Docker services")
                console.print(f"[dim]{result.stderr}[/dim]")
                raise typer.Exit(1)

        success("Docker services started (FalkorDB, PostgreSQL)")

        # Wait for services to be healthy
        with console.status(f"[{NEON_CYAN}]Waiting for services...[/{NEON_CYAN}]"):
            time.sleep(3)  # Give services a moment to initialize

    # Check for .env
    env_file = project_root / ".env"
    if not env_file.exists():
        warn(".env file not found. Using defaults.")
        info("Copy .env.example to .env and configure your API keys.")

    # Start API server
    if detach:
        _start_server_detached(project_root, with_worker)
    else:
        _start_server_foreground(project_root, with_worker)


def _start_server_foreground(project_root: Path, with_worker: bool) -> None:
    """Start server in foreground (blocking)."""
    console.print(f"\n[{SUCCESS_GREEN}]Starting API server...[/{SUCCESS_GREEN}]")
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(project_root / "src")

    # Load .env if it exists
    env_file = project_root / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "sibyl.main:app",
        "--host",
        "0.0.0.0",  # noqa: S104 - intentional for local dev
        "--port",
        "3334",
        "--reload",
    ]

    if with_worker:
        info("Worker mode: Running with in-process job worker")
        env["SIBYL_RUN_WORKER"] = "true"

    try:
        process = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=project_root,
            env=env,
        )

        # Handle Ctrl+C gracefully
        def signal_handler(_sig: int, _frame: object) -> None:
            console.print(f"\n[{NEON_CYAN}]Stopping server...[/{NEON_CYAN}]")
            process.terminate()
            process.wait(timeout=10)
            console.print(f"[{SUCCESS_GREEN}]Server stopped.[/{SUCCESS_GREEN}]")
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        process.wait()

    except KeyboardInterrupt:
        pass


def _start_server_detached(_project_root: Path, _with_worker: bool) -> None:
    """Start server in background."""
    # For detached mode, we'd need to use something like supervisord or systemd
    # For now, just inform the user
    warn("Detached mode not fully implemented yet.")
    info("For background running, use: nohup sibyl up &")
    info("Or run in a tmux/screen session.")


def down(
    volumes: Annotated[
        bool,
        typer.Option("--volumes", "-v", help="Also remove volumes (data loss!)"),
    ] = False,
) -> None:
    """Stop Sibyl services.

    Stops FalkorDB, PostgreSQL, and any running API server.
    """
    project_root = _find_project_root()
    if not project_root:
        error("Could not find Sibyl project root")
        raise typer.Exit(1)

    console.print(f"\n[{ELECTRIC_PURPLE}]Stopping Sibyl[/{ELECTRIC_PURPLE}]\n")

    # Stop Docker services
    with console.status(f"[{NEON_CYAN}]Stopping Docker services...[/{NEON_CYAN}]"):
        args = ["down"]
        if volumes:
            args.append("-v")
            warn("Removing volumes - all data will be lost!")

        result = _run_docker_compose(args, project_root)
        if result.returncode != 0:
            error("Failed to stop Docker services")
            console.print(f"[dim]{result.stderr}[/dim]")
        else:
            success("Docker services stopped")

    console.print()


def status() -> None:
    """Show status of Sibyl services."""
    project_root = _find_project_root()

    console.print(f"\n[{ELECTRIC_PURPLE}]Sibyl Status[/{ELECTRIC_PURPLE}]\n")

    # Check Docker services
    if project_root:
        result = _run_docker_compose(["ps"], project_root)
        if result.returncode == 0 and result.stdout.strip():
            console.print("[bold]Docker Services:[/bold]")
            console.print(result.stdout)
        else:
            console.print("[dim]No Docker services running[/dim]")
    else:
        warn("Could not find project root for Docker status")

    console.print()
