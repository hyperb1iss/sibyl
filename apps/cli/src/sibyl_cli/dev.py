"""Devcontainer lifecycle management.

Provides shell access and lifecycle commands for the Sibyl devcontainer:
  sibyl dev shell    - Exec into devcontainer (or run one-off command)
  sibyl dev up       - Start devcontainer stack
  sibyl dev down     - Stop devcontainer stack
  sibyl dev status   - Show container status
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from pathlib import Path

import typer
from rich.table import Table

from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    error,
    info,
    success,
    warn,
)

app = typer.Typer(
    name="dev",
    help="Devcontainer shell & lifecycle commands",
    no_args_is_help=True,
)

# ============================================================================
# Helpers
# ============================================================================


def _find_devcontainer_compose() -> Path | None:
    """Walk up from cwd looking for .devcontainer/docker-compose.yml."""
    current = Path.cwd()
    for directory in [current, *current.parents]:
        candidate = directory / ".devcontainer" / "docker-compose.yml"
        if candidate.is_file():
            return candidate
    return None


def _require_compose() -> Path:
    """Return the compose file path or exit with an error."""
    compose_file = _find_devcontainer_compose()
    if compose_file is None:
        error("No .devcontainer/docker-compose.yml found (searched upward from cwd)")
        raise typer.Exit(1)
    return compose_file


def _require_docker() -> None:
    """Ensure docker is available and running, or exit."""
    if not shutil.which("docker"):
        error("Docker is not installed")
        console.print("\nInstall Docker from: https://docs.docker.com/get-docker/")
        raise typer.Exit(1)

    result = subprocess.run(
        ["docker", "info"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        error("Docker daemon is not running")
        raise typer.Exit(1)


def _run_compose(
    args: list[str],
    compose_file: Path,
    *,
    capture: bool = False,
    replace_process: bool = False,
) -> subprocess.CompletedProcess | None:
    """Run docker compose against the devcontainer compose file.

    If replace_process is True, exec replaces the current process (used for
    interactive shell).
    """
    cmd = ["docker", "compose", "-f", str(compose_file), *args]

    if replace_process:
        import os

        os.execvp(cmd[0], cmd)
        return None  # unreachable, keeps type checker happy

    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    return subprocess.run(cmd, check=False)


# ============================================================================
# Commands
# ============================================================================


@app.command()
def shell(
    service: str = typer.Option(
        "workspace", "--service", "-s", help="Container service to exec into"
    ),
    command: str | None = typer.Option(
        None, "--command", "-c", help="Run a one-off command instead of interactive shell"
    ),
) -> None:
    """Exec into the devcontainer (or run a one-off command)."""
    _require_docker()
    compose_file = _require_compose()

    if command:
        result = _run_compose(
            ["exec", service, "bash", "-c", command],
            compose_file,
        )
        raise typer.Exit(result.returncode if result else 1)

    # Interactive shell — hand off the tty
    _run_compose(
        ["exec", service, "bash"],
        compose_file,
        replace_process=True,
    )


@app.command()
def up(
    build: bool = typer.Option(
        False, "--build", "-b", help="Rebuild images before starting"
    ),
    detach: bool = typer.Option(
        True, "--detach/--no-detach", "-d", help="Run in background (default: true)"
    ),
) -> None:
    """Start the devcontainer stack."""
    _require_docker()
    compose_file = _require_compose()

    args = ["up"]
    if detach:
        args.append("-d")
    if build:
        args.append("--build")

    info("Starting devcontainer stack...")
    result = _run_compose(args, compose_file)
    if result and result.returncode == 0:
        success("Devcontainer stack is up")
        console.print(
            f"  [{NEON_CYAN}]Tip:[/{NEON_CYAN}] [bold]sibyl dev shell[/bold] to jump in"
        )
    else:
        error("Failed to start devcontainer stack")
        raise typer.Exit(1)


@app.command()
def down(
    volumes: bool = typer.Option(
        False, "--volumes", "-v", help="Also remove volumes (deletes data)"
    ),
) -> None:
    """Stop the devcontainer stack."""
    _require_docker()
    compose_file = _require_compose()

    args = ["down"]
    if volumes:
        args.append("--volumes")
        warn("Removing volumes — data will be deleted")

    info("Stopping devcontainer stack...")
    result = _run_compose(args, compose_file)
    if result and result.returncode == 0:
        success("Devcontainer stack stopped")
    else:
        error("Failed to stop devcontainer stack")
        raise typer.Exit(1)


@app.command()
def status() -> None:
    """Show devcontainer service status."""
    _require_docker()
    compose_file = _require_compose()

    result = _run_compose(["ps", "--format", "json"], compose_file, capture=True)
    if result is None or result.returncode != 0:
        error("Failed to get container status")
        raise typer.Exit(1)

    raw = result.stdout.strip()
    if not raw:
        info("No devcontainer services running")
        console.print(
            f"\nRun [{NEON_CYAN}]sibyl dev up[/{NEON_CYAN}] to start the stack."
        )
        return

    import json

    # docker compose ps --format json can return one JSON object per line
    containers: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if line:
            with contextlib.suppress(json.JSONDecodeError):
                containers.append(json.loads(line))

    if not containers:
        info("No devcontainer services running")
        return

    table = Table(
        title="Devcontainer Services", border_style=ELECTRIC_PURPLE
    )
    table.add_column("Service", style=NEON_CYAN)
    table.add_column("State", style=SUCCESS_GREEN)
    table.add_column("Status", style="dim")
    table.add_column("Ports", style=CORAL)

    for c in containers:
        name = c.get("Service", c.get("Name", "?"))
        state = c.get("State", "unknown")
        health = c.get("Status", "")
        # Build host→container port mappings from Publishers array
        publishers = c.get("Publishers") or []
        seen: set[str] = set()
        port_parts: list[str] = []
        for p in publishers:
            pub = p.get("PublishedPort", 0)
            tgt = p.get("TargetPort", 0)
            if pub and tgt:
                mapping = f"{pub}→{tgt}"
                if mapping not in seen:
                    seen.add(mapping)
                    port_parts.append(mapping)
        ports = ", ".join(port_parts)

        # Color the state
        if state == "running":
            state_display = f"[{SUCCESS_GREEN}]{state}[/{SUCCESS_GREEN}]"
        elif state == "exited":
            state_display = f"[dim]{state}[/dim]"
        else:
            state_display = state

        table.add_row(name, state_display, health, ports)

    console.print(table)
