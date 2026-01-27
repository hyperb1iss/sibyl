"""Self-updater for Sibyl easy install deployments.

Updates CLI, Docker containers, and skills/hooks.
Only works for uv tool installs, not development/source installs.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import typer
from packaging.version import Version
from rich.panel import Panel
from rich.table import Table

from sibyl_cli.common import (
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

app = typer.Typer(help="Update Sibyl components")

# ============================================================================
# Constants
# ============================================================================

PYPI_URL = "https://pypi.org/pypi/sibyl-dev/json"
DOCKER_HUB_API = "https://hub.docker.com/v2/repositories"

# Images we manage
SIBYL_IMAGES = [
    "falkordb/falkordb",
    # Add sibyl images when published to Docker Hub
    # "hyperbliss/sibyld",
    # "hyperbliss/sibyl-web",
]

SIBYL_LOCAL_DIR = Path.home() / ".sibyl"
SIBYL_LOCAL_COMPOSE = SIBYL_LOCAL_DIR / "docker-compose.yml"


# ============================================================================
# Dev Mode Detection
# ============================================================================


def is_dev_mode() -> bool:
    """Check if running from source vs easy install."""
    # Check if skills are symlinks (dev mode symlinks to repo)
    skill_path = Path.home() / ".claude" / "skills" / "sibyl"
    if skill_path.is_symlink():
        return True

    # Check if current directory looks like the sibyl repo
    cwd = Path.cwd()
    if (cwd / "moon.yml").exists() and (cwd / "apps" / "cli").exists():
        return True

    # Check parent directories too
    for parent in cwd.parents:
        if (
            (parent / "moon.yml").exists()
            and (parent / "apps" / "cli").exists()
            and str(cwd).startswith(str(parent))
        ):
            return True
        # Don't go above home
        if parent == Path.home():
            break

    return False


# ============================================================================
# Version Checking
# ============================================================================


def get_current_cli_version() -> str | None:
    """Get currently installed CLI version."""
    try:
        return pkg_version("sibyl-dev")
    except Exception:
        return None


def get_latest_cli_version() -> str | None:
    """Get latest CLI version from PyPI."""
    try:
        req = urllib.request.Request(
            PYPI_URL,
            headers={"Accept": "application/json", "User-Agent": "sibyl-updater/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            return data.get("info", {}).get("version")
    except Exception:
        return None


def cli_update_available() -> tuple[str | None, str | None, bool]:
    """Check if CLI update is available.

    Returns (current_version, latest_version, update_available)
    """
    current = get_current_cli_version()
    latest = get_latest_cli_version()

    if current is None or latest is None:
        return current, latest, False

    try:
        update_available = Version(latest) > Version(current)
    except Exception:
        update_available = False

    return current, latest, update_available


def get_local_image_digest(image: str) -> str | None:
    """Get digest of locally pulled image."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{index .RepoDigests 0}}", image],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Extract just the digest part
            digest = result.stdout.strip()
            if "@" in digest:
                return digest.split("@")[1]
            return digest
    except Exception:
        pass
    return None


def get_remote_image_digest(image: str, tag: str = "latest") -> str | None:
    """Get latest digest from Docker Hub."""
    try:
        # Parse image name
        if "/" in image:
            namespace, repo = image.split("/", 1)
        else:
            namespace = "library"
            repo = image

        url = f"{DOCKER_HUB_API}/{namespace}/{repo}/tags/{tag}"
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "sibyl-updater/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
            # Get the digest from the response
            digest = data.get("digest")
            if digest:
                return digest
            # Try images array
            images = data.get("images", [])
            if images:
                return images[0].get("digest")
    except Exception:
        pass
    return None


def check_container_updates() -> tuple[int, int, list[str]]:
    """Check for container image updates.

    Returns (total_images, updates_available, list_of_updatable_images)
    """
    if not SIBYL_LOCAL_COMPOSE.exists():
        return 0, 0, []

    updates = []
    total = 0

    for image in SIBYL_IMAGES:
        total += 1
        local_digest = get_local_image_digest(image)
        remote_digest = get_remote_image_digest(image)

        if local_digest and remote_digest and local_digest != remote_digest:
            updates.append(image)

    return total, len(updates), updates


# ============================================================================
# Update Functions
# ============================================================================


def update_cli() -> bool:
    """Update CLI via uv tool upgrade."""
    info("Upgrading CLI...")

    result = subprocess.run(
        ["uv", "tool", "upgrade", "sibyl-dev"],
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode == 0:
        # Get new version
        new_version = get_current_cli_version()
        success(f"CLI updated to {new_version}")
        return True
    else:
        error("Failed to update CLI")
        if result.stderr:
            console.print(f"[dim]{result.stderr.strip()}[/dim]")
        return False


def update_containers(restart: bool = True) -> bool:
    """Update Docker containers."""
    if not SIBYL_LOCAL_COMPOSE.exists():
        warn("No local Sibyl installation found")
        return False

    info("Pulling container images...")

    # Check if containers are running
    ps_result = subprocess.run(
        ["docker", "compose", "-f", str(SIBYL_LOCAL_COMPOSE), "ps", "-q"],
        capture_output=True,
        text=True,
        check=False,
    )
    was_running = bool(ps_result.stdout.strip())

    # Pull new images
    result = subprocess.run(
        ["docker", "compose", "-f", str(SIBYL_LOCAL_COMPOSE), "pull"],
        capture_output=False,
        check=False,
    )

    if result.returncode != 0:
        error("Failed to pull container images")
        return False

    # Restart if was running
    if was_running and restart:
        info("Restarting containers with new images...")
        result = subprocess.run(
            ["docker", "compose", "-f", str(SIBYL_LOCAL_COMPOSE), "up", "-d"],
            capture_output=False,
            check=False,
        )
        if result.returncode != 0:
            error("Failed to restart containers")
            return False

    success("Containers updated")
    return True


def update_skills() -> bool:
    """Update skills and hooks."""
    from sibyl_cli.setup import setup_agent_integration

    return setup_agent_integration(verbose=False)


# ============================================================================
# Main Command
# ============================================================================


@app.callback(invoke_without_command=True)
def update(
    ctx: typer.Context,
    check_only: Annotated[
        bool,
        typer.Option("--check", "-c", help="Only check for updates, don't apply"),
    ] = False,
    cli_only: Annotated[
        bool,
        typer.Option("--cli", help="Only update CLI"),
    ] = False,
    containers_only: Annotated[
        bool,
        typer.Option("--containers", help="Only update Docker containers"),
    ] = False,
    skills_only: Annotated[
        bool,
        typer.Option("--skills", help="Only update skills and hooks"),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Skip confirmation prompt"),
    ] = False,
) -> None:
    """Check for and apply Sibyl updates.

    Updates the CLI, Docker containers, and agent skills/hooks.
    Only works for easy install deployments (uv tool install).
    """
    # Check for dev mode
    if is_dev_mode():
        console.print()
        console.print(f"[{ELECTRIC_YELLOW}]You're running Sibyl from source.[/{ELECTRIC_YELLOW}]")
        console.print()
        console.print("To update, use:")
        console.print(f"  [{NEON_CYAN}]git pull[/{NEON_CYAN}]")
        console.print(f"  [{NEON_CYAN}]moon run install-dev[/{NEON_CYAN}]")
        console.print()
        raise typer.Exit(0)

    # Determine what to update
    update_all = not (cli_only or containers_only or skills_only)
    do_cli = update_all or cli_only
    do_containers = update_all or containers_only
    do_skills = update_all or skills_only

    console.print()
    info("Checking for updates...")
    console.print()

    # Check CLI version
    cli_current, cli_latest, cli_has_update = None, None, False
    if do_cli:
        cli_current, cli_latest, cli_has_update = cli_update_available()

    # Check container updates
    container_total, container_updates, _container_list = 0, 0, []
    if do_containers:
        container_total, container_updates, _container_list = check_container_updates()

    # Skills are always "updateable" (we just re-copy)

    # Build status table
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Component", style=NEON_CYAN)
    table.add_column("Status")

    has_updates = False

    if do_cli:
        if cli_current is None:
            table.add_row("CLI", "[dim]Not installed via uv tool[/dim]")
        elif cli_latest is None:
            table.add_row("CLI", f"[dim]{cli_current} (couldn't check PyPI)[/dim]")
        elif cli_has_update:
            table.add_row("CLI", f"{cli_current} → [{SUCCESS_GREEN}]{cli_latest}[/{SUCCESS_GREEN}]")
            has_updates = True
        else:
            table.add_row("CLI", f"[{SUCCESS_GREEN}]{cli_current}[/{SUCCESS_GREEN}] (latest)")

    if do_containers:
        if container_total == 0:
            table.add_row("Containers", "[dim]Not installed[/dim]")
        elif container_updates > 0:
            table.add_row(
                "Containers",
                f"[{SUCCESS_GREEN}]{container_updates} image(s) to update[/{SUCCESS_GREEN}]",
            )
            has_updates = True
        else:
            table.add_row("Containers", f"[{SUCCESS_GREEN}]Up to date[/{SUCCESS_GREEN}]")

    if do_skills:
        table.add_row("Skills", "[dim]Will refresh[/dim]")

    # Display results
    if has_updates:
        panel = Panel(
            table,
            title=f"[{ELECTRIC_PURPLE}][bold]Updates Available[/bold][/{ELECTRIC_PURPLE}]",
            border_style=ELECTRIC_PURPLE,
            padding=(1, 2),
        )
    else:
        panel = Panel(
            table,
            title=f"[{SUCCESS_GREEN}][bold]Status[/bold][/{SUCCESS_GREEN}]",
            border_style=SUCCESS_GREEN,
            padding=(1, 2),
        )

    console.print(panel)
    console.print()

    # If check only, stop here
    if check_only:
        if has_updates:
            console.print(f"Run [{NEON_CYAN}]sibyl update[/{NEON_CYAN}] to apply updates.")
        else:
            success("Everything is up to date!")
        return

    # If no updates and not forcing skills refresh
    if not has_updates and not do_skills:
        success("Everything is up to date!")
        return

    # Confirm
    if not yes:
        proceed = typer.confirm("Apply updates?", default=True)
        if not proceed:
            raise typer.Abort()

    console.print()

    # Apply updates
    all_success = True

    if do_cli and cli_has_update and not update_cli():
        all_success = False

    if do_containers and container_updates > 0 and not update_containers():
        all_success = False

    if do_skills:
        info("Refreshing skills and hooks...")
        if update_skills():
            success("Skills refreshed")
        else:
            warn("Skills refresh had issues")

    console.print()
    if all_success:
        success("Update complete!")
    else:
        warn("Update completed with some issues")
