"""Local Sibyl instance management via Docker.

Provides Supabase-style local development experience:
  sibyl local start   - Start local Sibyl instance
  sibyl local stop    - Stop containers
  sibyl local status  - Show running services
  sibyl local logs    - Follow container logs
  sibyl local reset   - Nuke and start fresh
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import time
import webbrowser
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

# Docker Compose configuration embedded in the CLI
COMPOSE_CONFIG = {
    "services": {
        "api": {
            "image": "ghcr.io/hyperb1iss/sibyl-api:latest",
            "container_name": "sibyl-api",
            "ports": ["3334:3334"],
            "depends_on": {
                "falkordb": {"condition": "service_healthy"},
                "postgres": {"condition": "service_healthy"},
            },
            "environment": {
                "SIBYL_POSTGRES_HOST": "postgres",
                "SIBYL_POSTGRES_PORT": "5432",
                "SIBYL_POSTGRES_USER": "sibyl",
                "SIBYL_POSTGRES_PASSWORD": "${SIBYL_POSTGRES_PASSWORD:-sibyl_local}",
                "SIBYL_POSTGRES_DB": "sibyl",
                "SIBYL_FALKORDB_HOST": "falkordb",
                "SIBYL_FALKORDB_PORT": "6379",
                "SIBYL_FALKORDB_PASSWORD": "${SIBYL_FALKORDB_PASSWORD:-sibyl_local}",
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
        "worker": {
            "image": "ghcr.io/hyperb1iss/sibyl-api:latest",
            "container_name": "sibyl-worker",
            "command": ["sibyld", "worker"],
            "depends_on": {
                "api": {"condition": "service_healthy"},
            },
            "environment": {
                "SIBYL_POSTGRES_HOST": "postgres",
                "SIBYL_POSTGRES_PORT": "5432",
                "SIBYL_POSTGRES_USER": "sibyl",
                "SIBYL_POSTGRES_PASSWORD": "${SIBYL_POSTGRES_PASSWORD:-sibyl_local}",
                "SIBYL_POSTGRES_DB": "sibyl",
                "SIBYL_FALKORDB_HOST": "falkordb",
                "SIBYL_FALKORDB_PORT": "6379",
                "SIBYL_FALKORDB_PASSWORD": "${SIBYL_FALKORDB_PASSWORD:-sibyl_local}",
                "SIBYL_OPENAI_API_KEY": "${SIBYL_OPENAI_API_KEY}",
                "SIBYL_ANTHROPIC_API_KEY": "${SIBYL_ANTHROPIC_API_KEY}",
                "SIBYL_LLM_PROVIDER": "anthropic",
                "SIBYL_LLM_MODEL": "claude-haiku-4-5",
            },
            "restart": "unless-stopped",
        },
        "web": {
            "image": "ghcr.io/hyperb1iss/sibyl-web:latest",
            "container_name": "sibyl-web",
            "ports": ["3337:3337"],
            "depends_on": {
                "api": {"condition": "service_healthy"},
            },
            "environment": {
                "SIBYL_API_URL": "http://api:3334",  # Server-side (SSR) fetches
                "NEXT_PUBLIC_API_URL": "http://localhost:3334",  # Client-side fetches
                "NODE_ENV": "production",
            },
            "healthcheck": {
                "test": ["CMD", "wget", "-q", "--spider", "http://localhost:3337/"],
                "interval": "10s",
                "timeout": "5s",
                "retries": 3,
            },
            "restart": "unless-stopped",
        },
        "falkordb": {
            "image": "falkordb/falkordb:latest",
            "container_name": "sibyl-falkordb",
            "ports": ["6380:6379", "3335:3000"],
            "volumes": ["sibyl_falkordb:/data"],
            "environment": {
                "FALKORDB_ARGS": "--requirepass ${SIBYL_FALKORDB_PASSWORD:-sibyl_local}",
            },
            "healthcheck": {
                "test": [
                    "CMD",
                    "redis-cli",
                    "-a",
                    "${SIBYL_FALKORDB_PASSWORD:-sibyl_local}",
                    "ping",
                ],
                "interval": "5s",
                "timeout": "3s",
                "retries": 5,
            },
            "restart": "unless-stopped",
        },
        "postgres": {
            "image": "pgvector/pgvector:pg18",
            "container_name": "sibyl-postgres",
            "ports": ["5433:5432"],
            "volumes": ["sibyl_postgres:/var/lib/postgresql"],
            "environment": {
                "POSTGRES_USER": "sibyl",
                "POSTGRES_PASSWORD": "${SIBYL_POSTGRES_PASSWORD:-sibyl_local}",
                "POSTGRES_DB": "sibyl",
            },
            "healthcheck": {
                "test": ["CMD-SHELL", "pg_isready -U sibyl -d sibyl"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 5,
            },
            "restart": "unless-stopped",
        },
    },
    "volumes": {
        "sibyl_falkordb": {"name": "sibyl_falkordb"},
        "sibyl_postgres": {"name": "sibyl_postgres"},
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
# Generated by: sibyl local start

# API Keys
SIBYL_OPENAI_API_KEY={openai_key}
SIBYL_ANTHROPIC_API_KEY={anthropic_key}

# Security
SIBYL_JWT_SECRET={jwt_secret}

# Database passwords
SIBYL_POSTGRES_PASSWORD=sibyl_local
SIBYL_FALKORDB_PASSWORD=sibyl_local
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
        console.print("Run [bold]sibyl local stop[/bold] first if you want to restart.")
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
    console.print(f"  [{NEON_CYAN}]Graph UI:[/{NEON_CYAN}]  http://localhost:3335")
    console.print()

    # Open browser
    if not no_browser:
        webbrowser.open("http://localhost:3337")

    # Show next steps
    console.print(f"[{ELECTRIC_PURPLE}][bold]Next Steps[/bold][/{ELECTRIC_PURPLE}]")
    console.print()
    console.print("  1. Complete the setup wizard in your browser")
    console.print("  2. Connect Claude Code:")
    console.print("     [dim]claude mcp add sibyl --transport http http://localhost:3334/mcp[/dim]")
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
        error("Sibyl is not configured. Run 'sibyl local start' first.")
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
        error("Sibyl is not configured. Run 'sibyl local start' first.")
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
        console.print("\nRun [bold]sibyl local start[/bold] to start Sibyl.")
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
        typer.Argument(help="Service to show logs for (api, web, worker, falkordb, postgres)"),
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
        error("Sibyl is not configured. Run 'sibyl local start' first.")
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
    console.print("\nRun [bold]sibyl local start[/bold] to set up again.")
