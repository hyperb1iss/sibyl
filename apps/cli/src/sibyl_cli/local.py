"""Local Sibyl instance management via Docker.

Provides the Docker-backed runtime used by the top-level `sibyl up` command.
The `sibyl local ...` namespace remains for lower-level lifecycle commands.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import time
import webbrowser
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.table import Table

from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    error,
    info,
    success,
    warn,
)

app = typer.Typer(
    name="local",
    help="Manage local Sibyl instance (Docker-based)",
    no_args_is_help=True,
)

# ============================================================================
# Configuration
# ============================================================================

SIBYL_LOCAL_DIR = Path.home() / ".sibyl" / "local"
SIBYL_LOCAL_ENV = SIBYL_LOCAL_DIR / ".env"
SIBYL_LOCAL_COMPOSE = SIBYL_LOCAL_DIR / "docker-compose.yml"


def _version_to_image_tag(version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)rc(\d+)", version)
    if match:
        return f"{match.group(1)}-rc.{match.group(2)}"
    return version


def _default_image_tag() -> str:
    override = os.getenv("SIBYL_IMAGE_TAG")
    if override:
        return override
    try:
        return _version_to_image_tag(pkg_version("sibyl-dev"))
    except PackageNotFoundError:
        return "1.0.0-rc.8"


DEFAULT_IMAGE_TAG = _default_image_tag()


# Docker Compose configuration embedded in the CLI
COMPOSE_CONFIG = {
    "services": {
        "api": {
            "image": f"ghcr.io/hyperb1iss/sibyl-api:{DEFAULT_IMAGE_TAG}",
            "container_name": "sibyl-api",
            "ports": ["127.0.0.1:3334:3334"],
            "depends_on": {
                "surrealdb": {"condition": "service_healthy"},
            },
            "environment": {
                "SIBYL_STORE": "surreal",
                "SIBYL_AUTH_STORE": "surreal",
                "SIBYL_COORDINATION_BACKEND": "local",
                "SIBYL_SURREAL_URL": "ws://surrealdb:8000/rpc",
                "SIBYL_SURREAL_USERNAME": "${SIBYL_SURREAL_USERNAME:-root}",
                "SIBYL_SURREAL_PASSWORD": "${SIBYL_SURREAL_PASSWORD:-sibyl_local}",
                "SIBYL_JWT_SECRET": "${SIBYL_JWT_SECRET}",
                "SIBYL_PUBLIC_URL": "http://localhost:3337",
                "SIBYL_OPENAI_API_KEY": "${SIBYL_OPENAI_API_KEY}",
                "SIBYL_ANTHROPIC_API_KEY": "${SIBYL_ANTHROPIC_API_KEY}",
                "SIBYL_LLM_PROVIDER": "anthropic",
                "SIBYL_LLM_MODEL": "claude-haiku-4-5",
                "SIBYL_SERVER_HOST": "0.0.0.0",
                "SIBYL_SERVER_PORT": "3334",
                "SIBYL_ENVIRONMENT": "production",
            },
            "healthcheck": {
                "test": [
                    "CMD",
                    "python",
                    "-c",
                    "import httpx; httpx.get('http://localhost:3334/api/health')",
                ],
                "interval": "10s",
                "timeout": "5s",
                "retries": 5,
                "start_period": "30s",
            },
            "restart": "unless-stopped",
        },
        "web": {
            "image": f"ghcr.io/hyperb1iss/sibyl-web:{DEFAULT_IMAGE_TAG}",
            "container_name": "sibyl-web",
            "ports": ["127.0.0.1:3337:3337"],
            "depends_on": {
                "api": {"condition": "service_healthy"},
            },
            "environment": {
                "SIBYL_API_URL": "http://api:3334/api",  # Server-side (SSR) fetches
                "NEXT_PUBLIC_API_URL": "http://localhost:3334",  # Client-side fetches
                "NODE_ENV": "production",
                # Next.js standalone binds 0.0.0.0 (IPv4 only) by default; :: makes
                # the server dual-stack so it answers on both ::1 and 127.0.0.1.
                "HOSTNAME": "::",
            },
            "healthcheck": {
                "test": ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:3337/"],
                "interval": "10s",
                "timeout": "5s",
                "retries": 3,
            },
            "restart": "unless-stopped",
        },
        "surrealdb": {
            "image": "${SIBYL_SURREAL_IMAGE:-surrealdb/surrealdb:v3.2.3}",
            "container_name": "sibyl-surrealdb",
            "command": [
                "start",
                "--log",
                "info",
                "--user",
                "${SIBYL_SURREAL_USERNAME:-root}",
                "--pass",
                "${SIBYL_SURREAL_PASSWORD:-sibyl_local}",
                "rocksdb:///data/sibyl.db",
            ],
            "ports": ["127.0.0.1:8000:8000"],
            "volumes": ["sibyl_surreal:/data"],
            "healthcheck": {
                "test": ["CMD", "/surreal", "is-ready", "--endpoint", "http://localhost:8000"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 5,
            },
            "restart": "unless-stopped",
        },
    },
    "volumes": {
        "sibyl_surreal": {"name": "sibyl_surreal"},
    },
    "networks": {
        "default": {"name": "sibyl"},
    },
}


# ============================================================================
# Helpers
# ============================================================================


def check_docker() -> bool:
    """Check if Docker is available and running."""
    if not shutil.which("docker"):
        error("Docker is not installed")
        console.print("\nInstall Docker from: https://docs.docker.com/get-docker/")
        return False

    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error("Docker daemon is not running")
            console.print("\nStart Docker and try again.")
            return False
    except Exception as e:
        error(f"Failed to check Docker: {e}")
        return False

    return True


def check_docker_compose() -> bool:
    """Check if Docker Compose is available."""
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.returncode == 0
    except Exception:
        return False


def is_running() -> bool:
    """Check if Sibyl containers are running."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=sibyl-api", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "sibyl-api" in result.stdout
    except Exception:
        return False


def write_compose_file() -> None:
    """Write the compose config to disk."""
    SIBYL_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(SIBYL_LOCAL_COMPOSE, "w") as f:
        yaml.dump(COMPOSE_CONFIG, f, default_flow_style=False, sort_keys=False)


def write_env_file(
    openai_key: str,
    anthropic_key: str,
    jwt_secret: str,
) -> None:
    """Write environment file with secrets."""
    SIBYL_LOCAL_DIR.mkdir(parents=True, exist_ok=True)

    env_content = f"""# Sibyl Local Configuration
# Generated by: sibyl up

# API Keys
SIBYL_OPENAI_API_KEY={openai_key}
SIBYL_ANTHROPIC_API_KEY={anthropic_key}

# Security
SIBYL_JWT_SECRET={jwt_secret}

# SurrealDB
SIBYL_SURREAL_USERNAME=root
SIBYL_SURREAL_PASSWORD={secrets.token_urlsafe(24)}
"""
    with open(SIBYL_LOCAL_ENV, "w") as f:
        f.write(env_content)

    # Secure the file
    os.chmod(SIBYL_LOCAL_ENV, 0o600)


def run_compose(args: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    """Run docker compose with the local config."""
    cmd = [
        "docker",
        "compose",
        "-f",
        str(SIBYL_LOCAL_COMPOSE),
        "--env-file",
        str(SIBYL_LOCAL_ENV),
        *args,
    ]
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, check=False)
    return subprocess.run(cmd, check=False)


def get_api_keys_from_env() -> tuple[str, str]:
    """Get API keys from environment variables."""
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    return openai_key, anthropic_key


def wait_for_healthy(timeout: int = 120) -> bool:
    """Wait for API to be healthy."""
    import httpx

    start = time.time()
    while time.time() - start < timeout:
        try:
            response = httpx.get("http://localhost:3334/api/health", timeout=2)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
        console.print(".", end="", style="dim")
    return False


# ============================================================================
# Commands
# ============================================================================


@app.command()
def start(
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Don't open browser after starting"),
    ] = False,
    pull: Annotated[
        bool,
        typer.Option("--pull", help="Pull latest images before starting"),
    ] = False,
) -> None:
    """Start local Sibyl instance.

    On first run, prompts for API keys and generates secrets.
    Subsequent runs use saved configuration.
    """
    console.print()
    console.print(f"[{ELECTRIC_PURPLE}][bold]Sibyl Local[/bold][/{ELECTRIC_PURPLE}]")
    console.print()

    # Check Docker
    if not check_docker():
        raise typer.Exit(1)

    if not check_docker_compose():
        error("Docker Compose is not available")
        raise typer.Exit(1)

    # Check if already running
    if is_running():
        warn("Sibyl is already running")
        console.print()
        console.print(f"  [{NEON_CYAN}]Web UI:[/{NEON_CYAN}]    http://localhost:3337")
        console.print(f"  [{NEON_CYAN}]API:[/{NEON_CYAN}]       http://localhost:3334")
        console.print()
        console.print("Run [bold]sibyl down[/bold] first if you want to restart.")
        return

    # First run setup
    if not SIBYL_LOCAL_ENV.exists():
        info("First run - configuring Sibyl...")

        openai_key, anthropic_key = get_api_keys_from_env()
        jwt_secret = secrets.token_hex(32)

        write_env_file(openai_key, anthropic_key, jwt_secret)
        success("Configuration saved")

        if not openai_key or not anthropic_key:
            warn("API keys not found in environment - configure via web UI")

    # Write compose file (always, in case of updates)
    write_compose_file()

    # Pull images if requested or first run
    if pull or not SIBYL_LOCAL_COMPOSE.exists():
        info("Pulling Docker images...")
        run_compose(["pull", "--quiet"])

    # Start services
    info("Starting services...")
    result = run_compose(["up", "-d"])
    if result.returncode != 0:
        error("Failed to start services")
        raise typer.Exit(1)

    # Wait for healthy
    console.print()
    info("Waiting for services to be healthy...")
    if wait_for_healthy():
        success("Sibyl is running!")
    else:
        warn("Services are starting (may take a moment)")

    # Show info
    console.print()
    console.print(f"[{SUCCESS_GREEN}][bold]🚀 Sibyl is ready![/bold][/{SUCCESS_GREEN}]")
    console.print()
    console.print(f"  [{NEON_CYAN}]Web UI:[/{NEON_CYAN}]    http://localhost:3337")
    console.print(f"  [{NEON_CYAN}]API:[/{NEON_CYAN}]       http://localhost:3334")
    console.print(f"  [{NEON_CYAN}]SurrealDB:[/{NEON_CYAN}] http://localhost:8000")
    console.print()

    # Open browser
    if not no_browser:
        webbrowser.open("http://localhost:3337")

    # Show next steps
    console.print(f"[{ELECTRIC_PURPLE}][bold]Next Steps[/bold][/{ELECTRIC_PURPLE}]")
    console.print()
    console.print("  1. Complete the setup wizard in your browser")
    console.print("  2. Open the Connect page for CLI and MCP setup")
    console.print()


@app.command()
def stop(
    destroy: Annotated[
        bool,
        typer.Option("--destroy", help="Also remove volumes (deletes all data)"),
    ] = False,
) -> None:
    """Stop local Sibyl instance."""
    if not SIBYL_LOCAL_COMPOSE.exists():
        error("Sibyl is not configured. Run 'sibyl up' first.")
        raise typer.Exit(1)

    if not is_running():
        info("Sibyl is not running")
        return

    info("Stopping Sibyl...")

    args = ["down"]
    if destroy:
        args.extend(["-v", "--remove-orphans"])
        warn("Removing volumes - all data will be deleted")

    result = run_compose(args)
    if result.returncode == 0:
        success("Sibyl stopped")
    else:
        error("Failed to stop Sibyl")


@app.command()
def status() -> None:
    """Show status of local Sibyl services."""
    if not SIBYL_LOCAL_COMPOSE.exists():
        error("Sibyl is not configured. Run 'sibyl up' first.")
        raise typer.Exit(1)

    result = subprocess.run(
        [
            "docker",
            "ps",
            "--filter",
            "name=sibyl-",
            "--format",
            "{{.Names}}\t{{.Status}}\t{{.Ports}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    if not result.stdout.strip():
        info("No Sibyl containers running")
        console.print("\nRun [bold]sibyl up[/bold] to start Sibyl.")
        return

    table = Table(title="Sibyl Services", border_style=ELECTRIC_PURPLE)
    table.add_column("Service", style=NEON_CYAN)
    table.add_column("Status", style=SUCCESS_GREEN)
    table.add_column("Ports", style=CORAL)

    for line in result.stdout.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) >= 2:
            name = parts[0].replace("sibyl-", "")
            status = parts[1]
            ports = parts[2] if len(parts) > 2 else ""
            # Simplify port display
            if ports:
                ports = ", ".join(
                    p.split("->")[0].split(":")[-1] for p in ports.split(", ") if "->" in p
                )
            table.add_row(name, status, ports)

    console.print(table)


@app.command()
def logs(
    service: Annotated[
        str | None,
        typer.Argument(help="Service to show logs for (api, web, worker, surrealdb)"),
    ] = None,
    follow: Annotated[
        bool,
        typer.Option("-f", "--follow", help="Follow log output"),
    ] = True,
    tail: Annotated[
        int,
        typer.Option("--tail", help="Number of lines to show"),
    ] = 100,
) -> None:
    """Show logs from Sibyl services."""
    if not SIBYL_LOCAL_COMPOSE.exists():
        error("Sibyl is not configured. Run 'sibyl up' first.")
        raise typer.Exit(1)

    args = ["logs", f"--tail={tail}"]
    if follow:
        args.append("-f")
    if service:
        args.append(service)

    run_compose(args)


@app.command()
def reset(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip confirmation"),
    ] = False,
) -> None:
    """Reset local Sibyl instance (removes all data)."""
    if not force:
        console.print()
        console.print(f"[{ELECTRIC_YELLOW}][bold]Warning:[/bold][/{ELECTRIC_YELLOW}] This will:")
        console.print("  • Stop all Sibyl containers")
        console.print("  • Delete all data (knowledge graph, users, etc.)")
        console.print("  • Remove saved configuration")
        console.print()
        if not typer.confirm("Are you sure?"):
            raise typer.Abort()

    info("Stopping containers...")
    if SIBYL_LOCAL_COMPOSE.exists():
        run_compose(["down", "-v", "--remove-orphans"])

    info("Removing configuration...")
    if SIBYL_LOCAL_DIR.exists():
        shutil.rmtree(SIBYL_LOCAL_DIR)

    success("Sibyl reset complete")
    console.print("\nRun [bold]sibyl up[/bold] to set up again.")


@app.command()
def setup(
    status_only: Annotated[
        bool,
        typer.Option("--status", "-s", help="Only show current installation status"),
    ] = False,
    show_snippet: Annotated[
        bool,
        typer.Option("--snippet", help="Show prompt snippet for Claude/Codex config"),
    ] = False,
) -> None:
    """Set up Claude/Codex integration (skills + hooks).

    Installs:
      • Skills for Claude Code (~/.claude/skills/sibyl/)
      • Skills for Codex CLI (~/.codex/skills/sibyl/)
      • Hooks for Claude Code (session-start, prompt injection)

    In development mode (run from Sibyl repo), creates symlinks.
    In package mode, copies embedded files.
    """
    from sibyl_cli.setup import (
        print_prompt_snippet,
        print_status,
        setup_agent_integration,
    )

    if status_only:
        print_status()
        return

    if show_snippet:
        print_prompt_snippet()
        return

    if setup_agent_integration():
        print_prompt_snippet()
