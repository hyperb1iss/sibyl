"""Self-updater for Sibyl easy install deployments.

Updates the CLI, the container runtime `sibyl up` or `sibyl docker` set up,
and skills/hooks. Only works for uv tool installs, not development/source
installs.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from dataclasses import dataclass
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import typer
from packaging.version import Version
from rich.panel import Panel
from rich.table import Table

from sibyl_cli import docker as docker_runtime
from sibyl_cli import local as local_runtime
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
from sibyl_core.version_contract import server_is_ahead

app = typer.Typer(help="Update Sibyl components")

# ============================================================================
# Constants
# ============================================================================

PYPI_URL = "https://pypi.org/pypi/sibyl-dev/json"

# `update --check` must finish even when a wedged daemon never answers.
DOCKER_PROBE_TIMEOUT_SECONDS = local_runtime.DOCKER_PROBE_TIMEOUT_SECONDS


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


def get_server_version() -> str | None:
    """Ask the configured server what it is running.

    PyPI is the wrong target on its own: against a remote server, matching
    the newest release proves nothing if that server is pinned elsewhere.
    Returns None when there is no configured server or it cannot be reached.
    """
    try:
        from sibyl_cli import config_store

        base_url = config_store.get_effective_server_url()
    except Exception:
        return None
    if not base_url:
        return None

    try:
        import httpx

        response = httpx.get(f"{base_url.rstrip('/')}/health", timeout=5.0)
        if response.status_code != 200:
            return None
        version = response.json().get("version")
    except Exception:
        return None
    return str(version) if version else None


# ============================================================================
# Container Runtimes
# ============================================================================


_parse_tag = local_runtime.image_tag_version


@dataclass(frozen=True)
class ContainerRuntime:
    """A compose runtime that `sibyl up` or `sibyl docker init` wrote to disk."""

    name: str
    compose_file: Path
    env_file: Path

    def image_tag(self) -> str | None:
        """The tag of the API image the compose file pins."""
        try:
            return local_runtime.pinned_image_tag(self.compose_file.read_text())
        except OSError:
            return None

    def is_running(self) -> bool | None:
        """Whether this runtime has running containers, or None when Docker did not answer.

        Containers are matched on the compose-file label, so another project
        in a directory named `docker` or `local` never reads as this one.
        """
        containers = local_runtime.containers_for(self.compose_file)
        return None if containers is None else bool(containers)

    def running_api_tag(self) -> str | None:
        """The image tag of this runtime's running API container, if one runs.

        An interrupted upgrade can leave the pin ahead of the containers, so
        what runs is the version that counts.
        """
        return local_runtime.running_api_tag(self.compose_file)

    def upgrade_command(self, image_tag: str) -> list[str]:
        """The runtime's own upgrade command, run through the `sibyl` on PATH.

        Both pull before they restart anything, so a missing image leaves the
        running server alone.
        """
        return ["sibyl", self.name, "upgrade", "--tag", image_tag]


def installed_container_runtimes() -> list[ContainerRuntime]:
    """Runtimes with a compose file at the path their own commands write."""
    runtimes = [
        ContainerRuntime(
            "docker", docker_runtime.SIBYL_DOCKER_COMPOSE, docker_runtime.SIBYL_DOCKER_ENV
        ),
        ContainerRuntime("local", local_runtime.SIBYL_LOCAL_COMPOSE, local_runtime.SIBYL_LOCAL_ENV),
    ]
    return [runtime for runtime in runtimes if runtime.compose_file.exists()]


@dataclass(frozen=True)
class ContainerTarget:
    """The server image tag the containers should run, or why there is none."""

    tag: str | None
    source: str = "CLI"
    reason: str | None = None


def container_target(cli_version: str | None) -> ContainerTarget:
    """The tag a CLI at `cli_version` runs, following the rule `sibyl up` uses."""
    if override := os.getenv("SIBYL_IMAGE_TAG"):
        return ContainerTarget(override, source="SIBYL_IMAGE_TAG")
    if not cli_version:
        return ContainerTarget(None, reason="CLI version unknown")
    if tag := local_runtime.release_image_tag(cli_version):
        return ContainerTarget(tag)
    return ContainerTarget(None, reason=f"CLI {cli_version} is not a release with an image")


@dataclass(frozen=True)
class ContainerPlan:
    """Where one runtime stands against the server image its CLI expects."""

    runtime: ContainerRuntime
    current_tag: str | None
    target: ContainerTarget
    running: bool | None
    pinned_tag: str | None = None

    @property
    def api_missing(self) -> bool:
        """The runtime has containers up but no API, e.g. after an interrupted start."""
        return self.running is True and self.current_tag is None and self.pinned_tag is not None

    @property
    def target_tag(self) -> str | None:
        return self.target.tag

    @property
    def behind(self) -> bool:
        target = _parse_tag(self.target_tag)
        if self.api_missing:
            # Bringing the API back on the target is the fix, unless the pin
            # names something newer than this CLI, which must not go backwards.
            pinned = _parse_tag(self.pinned_tag)
            return pinned is not None and target is not None and pinned <= target
        current = _parse_tag(self.current_tag)
        return current is not None and target is not None and current < target

    @property
    def applies(self) -> bool:
        """Only a runtime known to be running is upgraded; `update` never starts one."""
        return self.behind and self.running is True

    def status(self) -> str:
        status = self._status()
        if (
            not self.api_missing
            and self.pinned_tag
            and self.current_tag
            and self.pinned_tag != self.current_tag
        ):
            status += f" [dim](pin says {self.pinned_tag})[/dim]"
        return status

    def _status(self) -> str:
        current, target, source = self.current_tag, self.target_tag, self.target.source
        if self.api_missing:
            missing = f"no API container running (pin says {self.pinned_tag})"
            if self.applies:
                return f"{missing} → [{SUCCESS_GREEN}]{target}[/{SUCCESS_GREEN}]"
            return f"{missing} [dim](left alone)[/dim]"
        if current is None:
            return "[dim]Unknown image tag[/dim]"
        if target is None:
            return f"{current} [dim]({self.target.reason}, left alone)[/dim]"
        if self.applies:
            return f"{current} → [{SUCCESS_GREEN}]{target}[/{SUCCESS_GREEN}]"
        if self.behind and self.running is None:
            return f"{current} [dim](Docker did not answer, state unknown; left alone)[/dim]"
        if self.behind and self.runtime.name == "local":
            return f"{current} [dim](stopped; the next `sibyl up` runs {target})[/dim]"
        if self.behind:
            return (
                f"{current} [dim](stopped; `sibyl docker upgrade --tag {target}` "
                "moves it and starts it)[/dim]"
            )
        if _parse_tag(current) is None:
            return f"{current} [dim](custom tag, left alone)[/dim]"
        if _parse_tag(target) is None:
            return f"{current} [dim]({source} {target} is not a release, left alone)[/dim]"
        if _parse_tag(current) == _parse_tag(target):
            return f"[{SUCCESS_GREEN}]{current}[/{SUCCESS_GREEN}] (matches {source})"
        return f"{current} [dim](newer than {source} {target}, left alone)[/dim]"


def plan_container_upgrades(cli_version: str | None) -> list[ContainerPlan]:
    """Compare each installed runtime with the image tag `cli_version` runs."""
    target = container_target(cli_version)
    plans = []
    for runtime in installed_container_runtimes():
        running = runtime.is_running()
        pinned = runtime.image_tag()
        # What runs is the current version. The pin only speaks for a runtime
        # with nothing up; a runtime that runs without its API is not current.
        current = runtime.running_api_tag() if running else pinned
        plans.append(ContainerPlan(runtime, current, target, running, pinned))
    return plans


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
        # The binary landed but its skills did not, so the command as a whole
        # did not do what it said it would.
        return sync_skills_after_cli_update()
    else:
        error("Failed to update CLI")
        if result.stderr:
            console.print(f"[dim]{result.stderr.strip()}[/dim]")
        return False


def sync_skills_after_cli_update() -> bool:
    """Refresh skills by invoking the upgraded CLI entrypoint."""
    result = subprocess.run(
        ["sibyl", "skill", "--install", "--quiet"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True

    warn("CLI updated, but skill refresh failed")
    if result.stderr:
        console.print(f"[dim]{result.stderr.strip()}[/dim]")
    return False


def upgrade_container_runtime(plan: ContainerPlan) -> bool:
    """Hand a running runtime to its own upgrade command, then check the pin it left."""
    target = plan.target_tag
    if target is None:
        return False
    command = plan.runtime.upgrade_command(target)
    shown = " ".join(command)
    info(f"Running {shown}")
    # The `sibyl` on PATH may be a different build than this process, so the
    # tag travels explicitly instead of being derived from its version.
    env = {**os.environ, "SIBYL_IMAGE_TAG": target}
    try:
        result = subprocess.run(command, check=False, env=env)
    except OSError as exc:
        error(f"Could not run {shown}: {exc}")
        return False

    pinned = plan.runtime.image_tag()
    running_tag = plan.runtime.running_api_tag()
    name = plan.runtime.name
    if result.returncode == 0 and pinned == target and running_tag == target:
        success(f"The {name} runtime now runs {target}")
        return True
    if result.returncode == 0 and running_tag is None:
        # `docker upgrade` does not wait for health, so an API that exits right
        # after `up -d` only shows up here.
        error(f"{shown} finished, but no {name} API container is running {target}.")
        info(f"Inspect it with: sibyl {name} logs")
        return False
    if result.returncode == 0:
        found = running_tag if running_tag != target else pinned
        error(f"{shown} finished, but the {name} runtime is on {found}, not {target}.")
        return False

    error(f"{shown} failed")
    if name == "local":
        # `local upgrade` already said which it was: nothing changed, or the
        # new version is pinned and did not come up healthy.
        return False
    # `docker upgrade` pulls before it touches the pin, so an unchanged pin
    # means the pull failed and a moved one means the start did.
    if pinned != target:
        info(f"Nothing changed; the {name} runtime still pins {pinned}.")
        return False
    warn(f"The {target} images are pulled and pinned, but the {name} runtime did not start.")
    info("Start it with: sibyl docker up")
    info("Inspect the failure with: sibyl docker logs")
    return False


def update_skills() -> bool:
    """Update Claude/Codex skills and hooks."""
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
        typer.Option("--containers", help="Only upgrade the running container runtime"),
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

    Updates the CLI, the running `sibyl up` or `sibyl docker` runtime, and
    Claude/Codex skills/hooks. Only works for easy install deployments
    (uv tool install).
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

    # Containers follow the CLI this run leaves installed, so the server never
    # lands ahead of the client that talks to it.
    container_plans: list[ContainerPlan] = []
    if do_containers:
        installed = cli_current if do_cli else get_current_cli_version()
        container_plans = plan_container_upgrades(cli_latest if cli_has_update else installed)
    # A runtime that is behind but not upgraded here, or whose tag cannot be
    # read, is not "up to date" even when nothing is left to apply.
    needs_attention = any(
        (plan.behind and not plan.applies) or plan.current_tag is None for plan in container_plans
    )

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

        # The server you are actually talking to is the version that
        # matters; PyPI only says what is available to install.
        server_version = get_server_version()
        if server_version is None:
            table.add_row("Server", "[dim]Not reachable[/dim]")
        elif server_version == "0.0.0":
            table.add_row("Server", f"[{ELECTRIC_YELLOW}]no version stamped[/{ELECTRIC_YELLOW}]")
        elif server_is_ahead(client=cli_current, server=server_version):
            table.add_row(
                "Server",
                f"[{ELECTRIC_YELLOW}]{server_version}[/{ELECTRIC_YELLOW}] (ahead of this CLI)",
            )
            has_updates = True
        else:
            table.add_row("Server", f"[{SUCCESS_GREEN}]{server_version}[/{SUCCESS_GREEN}]")

    if do_containers and not container_plans:
        table.add_row("Containers", "[dim]Not installed[/dim]")
    for plan in container_plans:
        table.add_row(f"Containers ({plan.runtime.name})", plan.status())
        has_updates = has_updates or plan.applies

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
        elif not needs_attention:
            success("Everything is up to date!")
        return

    # If no updates and not forcing skills refresh
    if not has_updates and not do_skills:
        if not needs_attention:
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

    cli_failed = do_cli and cli_has_update and not update_cli()
    if cli_failed:
        all_success = False
    elif do_containers and cli_has_update:
        # A constrained tool install can "upgrade" without moving, so the
        # containers follow the version that actually landed, not PyPI.
        landed = get_current_cli_version()
        if landed is None:
            warn("Could not read the CLI version after the upgrade.")
        elif landed != cli_latest:
            warn(f"The CLI is on {landed}, not {cli_latest}; containers follow {landed}.")
        container_plans = plan_container_upgrades(landed)

    to_upgrade = [plan for plan in container_plans if plan.applies]
    if to_upgrade and cli_failed:
        warn("Skipped the container upgrade because the CLI upgrade did not finish.")
        info("Re-run it once the CLI is current: sibyl update --containers")
    else:
        for plan in to_upgrade:
            if not upgrade_container_runtime(plan):
                all_success = False

    if do_skills:
        info("Refreshing skills and hooks...")
        if update_skills():
            success("Skills refreshed")
        else:
            warn("Skills refresh had issues")
            all_success = False

    console.print()
    if all_success:
        success("Update complete!")
    else:
        warn("Update completed with some issues")
        raise typer.Exit(1)
