"""Local Sibyl instance management via Docker.

Provides the Docker-backed runtime used by the top-level `sibyl up` command.
The `sibyl local ...` namespace remains for lower-level lifecycle commands.
"""

from __future__ import annotations

import copy
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Annotated

import typer
import yaml
from packaging.version import InvalidVersion, Version
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
from sibyl_cli.docker_storage import (
    SURREAL_IMAGE,
    SURREAL_IMAGE_REFERENCE,
    surreal_data_mount,
    surreal_volume_initializer,
    upgraded_surreal_image,
)

try:
    import fcntl
except ImportError:  # Windows has no flock; the staged file name stays unique.
    fcntl = None

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

# A wedged Docker daemon accepts the socket and never answers, so every probe
# the CLI makes on its own behalf gives up after this long.
DOCKER_PROBE_TIMEOUT_SECONDS = 10


def _version_to_image_tag(version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+\.\d+)rc(\d+)", version)
    if match:
        return f"{match.group(1)}-rc.{match.group(2)}"
    return version


def release_image_tag(version: str) -> str | None:
    """The published image tag for a release or rc CLI version, else None.

    Images are only published from `vX.Y.Z` and `vX.Y.Z-rc.N` tags, so a dev
    build, a local version such as `1.4.0+g1a2b3c` (a `+` is not even legal in
    a Docker tag), an alpha or beta, or a post release has no image to move to.
    """
    try:
        parsed = Version(version)
    except InvalidVersion:
        return None
    if (
        len(parsed.release) != 3
        or parsed.epoch
        or parsed.dev is not None
        or parsed.post is not None
        or parsed.local
    ):
        return None
    if parsed.pre is None:
        return parsed.base_version
    kind, number = parsed.pre
    return f"{parsed.base_version}-rc.{number}" if kind == "rc" else None


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
                    "import httpx; httpx.get('http://localhost:3334/api/health/ready', trust_env=False).raise_for_status()",
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
        "surreal-init": surreal_volume_initializer(),
        "surrealdb": {
            "image": SURREAL_IMAGE_REFERENCE,
            "container_name": "sibyl-surrealdb",
            "depends_on": {"surreal-init": {"condition": "service_completed_successfully"}},
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
            "volumes": [surreal_data_mount()],
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


def compose_config_for(image_tag: str, current: dict | None = None) -> dict:
    """The local compose config with the Sibyl images moved to `image_tag`.

    SurrealDB follows the same rule as `sibyl docker upgrade`: a CLI-written
    default moves up to this release's server, and a newer or hand-written
    image in `current` stays, so the upgrade never takes SurrealDB backwards.
    """
    config = copy.deepcopy(COMPOSE_CONFIG)
    for name in ("api", "web"):
        service = config["services"][name]
        repository = service["image"].rpartition(":")[0]
        service["image"] = f"{repository}:{image_tag}"

    try:
        existing = (current or {})["services"]["surrealdb"]["image"]
    except (KeyError, TypeError):
        existing = None
    if isinstance(existing, str) and upgraded_surreal_image(existing) is None:
        warn(f"Leaving the SurrealDB image {existing} as written; this CLI runs {SURREAL_IMAGE}.")
        config["services"]["surrealdb"]["image"] = existing
    return config


def pinned_image(compose_text: str) -> str | None:
    """The API image reference a compose file pins."""
    try:
        image = (yaml.safe_load(compose_text) or {})["services"]["api"]["image"]
    except (yaml.YAMLError, KeyError, TypeError):
        return None
    return image if isinstance(image, str) else None


def pinned_image_tag(compose_text: str) -> str | None:
    """The API image tag a compose file pins."""
    image = pinned_image(compose_text)
    if image is None:
        return None
    repository, _, tag = image.rpartition(":")
    return tag if repository and "/" not in tag else None


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
            timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            error("Docker daemon is not running")
            console.print("\nStart Docker and try again.")
            return False
    except subprocess.TimeoutExpired:
        error(f"Docker did not answer within {DOCKER_PROBE_TIMEOUT_SECONDS}s")
        console.print("\nCheck that the Docker daemon is healthy and try again.")
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


def write_compose_file(config: dict | None = None, path: Path | None = None) -> None:
    """Write the compose config to disk."""
    SIBYL_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(path or SIBYL_LOCAL_COMPOSE, "w") as f:
        yaml.dump(config or COMPOSE_CONFIG, f, default_flow_style=False, sort_keys=False)


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


def run_compose(
    args: list[str],
    capture: bool = False,
    compose_file: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run docker compose with the local config."""
    cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file or SIBYL_LOCAL_COMPOSE),
        "--env-file",
        str(SIBYL_LOCAL_ENV),
        *args,
    ]
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
    return subprocess.run(cmd, check=False, timeout=timeout)


def container_owner(name: str = "sibyl-api") -> str | None:
    """Which CLI runtime, "local" or "docker", created the running `name` container.

    Both runtimes name their API container `sibyl-api`, so the name alone
    cannot say whose upgrade command applies.
    """
    label = '{{ index .Config.Labels "com.docker.compose.project.config_files" }}'
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", label, name],
            capture_output=True,
            text=True,
            check=False,
            timeout=DOCKER_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sibyl_home = SIBYL_LOCAL_DIR.parent.resolve()
    for config_file in result.stdout.strip().split(","):
        if not config_file:
            continue
        directory = Path(config_file).resolve().parent
        if directory == SIBYL_LOCAL_DIR.resolve():
            return "local"
        if directory == sibyl_home / "docker":
            return "docker"
    return None


def upgrade_command_for_running_server() -> str:
    """The upgrade command for whichever runtime owns the running API container."""
    if container_owner() == "docker":
        return "sibyl docker upgrade"
    return "sibyl local upgrade"


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
            response = httpx.get(
                "http://localhost:3334/api/health/ready", timeout=2, trust_env=False
            )
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
        warn("Existing Sibyl API container found; leaving server images unchanged.")
        console.print()
        console.print(f"  [{NEON_CYAN}]Web UI:[/{NEON_CYAN}]    http://localhost:3337")
        console.print(f"  [{NEON_CYAN}]API:[/{NEON_CYAN}]       http://localhost:3334")
        console.print()
        console.print(
            "To move to updated server images without leaving it down, run "
            f"[bold]{upgrade_command_for_running_server()}[/bold]."
        )
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
        if run_compose(["pull", "--quiet"]).returncode != 0:
            error("Failed to pull Docker images. Run 'sibyl up --pull' to retry.")
            raise typer.Exit(1)

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
        error("Sibyl did not become healthy. Run 'sibyl local logs' to inspect startup errors.")
        raise typer.Exit(1)

    # Show info
    console.print()
    console.print(f"[{SUCCESS_GREEN}][bold]Sibyl is ready![/bold][/{SUCCESS_GREEN}]")
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


def _compose_project_running() -> bool | None:
    """Whether this compose project has containers, or None when Compose cannot say.

    Unlike the name-based `is_running`, this cannot mistake the Docker
    runtime's `sibyl-api` container for this one.
    """
    try:
        result = run_compose(["ps", "-q"], capture=True, timeout=DOCKER_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return bool((result.stdout or "").strip())


@contextmanager
def _upgrade_lock() -> Iterator[None]:
    """Hold the local upgrade lock, or stop cleanly when another upgrade has it."""
    SIBYL_LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    with open(SIBYL_LOCAL_DIR / ".upgrade.lock", "w") as handle:
        if fcntl is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                error("Another sibyl local upgrade is running. Nothing changed.")
                raise typer.Exit(1) from None
        yield


def _staged_compose_file() -> Path:
    """A fresh file beside the compose file, so concurrent writers never share one."""
    handle, name = tempfile.mkstemp(
        dir=SIBYL_LOCAL_DIR, prefix="docker-compose.", suffix=".next.yml"
    )
    os.close(handle)
    return Path(name)


@app.command()
def upgrade(
    image_tag: Annotated[
        str | None,
        typer.Option("--tag", help="Server image tag to move to (default: this CLI's)"),
    ] = None,
) -> None:
    """Move a running local instance to new server images without leaving it down.

    Images are pulled while the current containers keep serving, so a failed
    pull changes nothing. Only then are the pins moved and the services
    recreated. There is no rollback after that point: the new SurrealDB may
    already have opened the data, and an older API must not start against a
    schema a newer one migrated.
    """
    if not SIBYL_LOCAL_COMPOSE.exists():
        if container_owner() == "docker":
            error("The running Sibyl belongs to the Docker runtime (`sibyl docker`).")
            info("Upgrade it with: sibyl docker upgrade")
        else:
            error("Sibyl is not configured. Run 'sibyl up' first.")
        raise typer.Exit(1)
    if not check_docker() or not check_docker_compose():
        raise typer.Exit(1)

    tag = image_tag or DEFAULT_IMAGE_TAG
    with _upgrade_lock():
        running = _compose_project_running()
        if running is None:
            error("Docker Compose could not report the local instance's state. Nothing changed.")
            info("Inspect it with: sibyl local status")
            raise typer.Exit(1)
        if not running:
            if container_owner() == "docker":
                info("The running Sibyl belongs to the Docker runtime (`sibyl docker`), not this one.")
                info("Upgrade it with: sibyl docker upgrade")
                return
            info("The local instance is not running, so there is nothing to upgrade in place.")
            prefix = "" if tag == DEFAULT_IMAGE_TAG else f"SIBYL_IMAGE_TAG={tag} "
            info(f"Start it on {tag} with: {prefix}sibyl up --pull")
            return

        current_text = SIBYL_LOCAL_COMPOSE.read_text()
        previous_tag = pinned_image_tag(current_text)
        if previous_tag is None:
            error(f"Could not read the image tag from {SIBYL_LOCAL_COMPOSE}. Nothing changed.")
            raise typer.Exit(1)
        target = compose_config_for(tag, yaml.safe_load(current_text))

        staged = _staged_compose_file()
        try:
            write_compose_file(target, staged)
            info(f"Pulling images for {tag} while {previous_tag} keeps serving...")
            if run_compose(["pull", "--quiet"], compose_file=staged).returncode != 0:
                error(
                    f"Could not pull the images for {tag}. Nothing changed; still on {previous_tag}."
                )
                raise typer.Exit(1)
            os.replace(staged, SIBYL_LOCAL_COMPOSE)
        finally:
            staged.unlink(missing_ok=True)

        info(f"Recreating services on {tag}...")
        started = run_compose(["up", "-d"]).returncode == 0
        if started and wait_for_healthy():
            console.print()
            success(f"Sibyl is running {tag}")
            return

    console.print()
    if started:
        error(f"Sibyl is running {tag} but did not report healthy within 2 minutes.")
        info("A long schema migration can take longer; check with: sibyl local status")
    else:
        error(f"Docker Compose could not start every service on {tag}.")
    info(
        f"The pins stay on {tag}. SurrealDB may already have opened the data on the new "
        "version, and an older API must not start against a newer schema."
    )
    info("Inspect the failure with: sibyl local logs")
    info(f"Retry the start with: sibyl local upgrade --tag {tag}")
    raise typer.Exit(1)


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
        raise typer.Exit(1)


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

    if not setup_agent_integration():
        raise typer.Exit(1)
    print_prompt_snippet()
