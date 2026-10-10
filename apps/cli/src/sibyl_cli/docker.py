"""Self-hosted Docker runtime commands."""

from __future__ import annotations

import io
import os
import secrets
import subprocess
import tarfile
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from sibyl_cli import config_store
from sibyl_cli.common import NEON_CYAN, console, error, info, success, warn
from sibyl_cli.docker_storage import (
    MANAGED_SURREAL_IMAGE,
    SURREAL_IMAGE,
    SURREAL_IMAGE_REFERENCE,
    surreal_data_mount,
    surreal_volume_initializer,
    upgraded_surreal_image,
)
from sibyl_cli.local import DEFAULT_IMAGE_TAG, check_docker, check_docker_compose

app = typer.Typer(
    name="docker",
    help="Manage a self-hosted Sibyl Docker deployment",
    no_args_is_help=True,
)

SIBYL_DOCKER_DIR = Path.home() / ".sibyl" / "docker"
SIBYL_DOCKER_ENV = SIBYL_DOCKER_DIR / ".env"
SIBYL_DOCKER_COMPOSE = SIBYL_DOCKER_DIR / "docker-compose.yml"
MANAGED_IMAGE_REPOSITORIES = (
    "ghcr.io/hyperb1iss/sibyl-api",
    "ghcr.io/hyperb1iss/sibyl-api-crawler",
    "ghcr.io/hyperb1iss/sibyl-web",
)
# The shape `init` writes for the SurrealDB image. Anything else in that slot
# was edited by hand, and upgrade leaves it alone.


# Where the API and worker find the bundled Valkey. Settings read the host and
# port; there is no URL setting, so a URL here would leave both on localhost.
VALKEY_ADDRESS = {"SIBYL_REDIS_HOST": "valkey", "SIBYL_REDIS_PORT": "6379"}
# The URL setting bundles before 1.5 wrote in its place, which Sibyl never read.
LEGACY_VALKEY_URL = "redis://valkey:6379/0"
SETTINGS_KEY_ENV = {"SIBYL_SETTINGS_KEY": "${SIBYL_SETTINGS_KEY:-}"}
WORKER_SECRETS_ENV = {
    "SIBYL_JWT_SECRET": "${SIBYL_JWT_SECRET}",
    **SETTINGS_KEY_ENV,
    "SIBYL_ENVIRONMENT": "production",
}
WORKER_COMMAND = ["sibyld", "worker", "--require-redis"]
# The image's own healthcheck asks the API port, which a worker does not serve.
WORKER_HEALTHCHECK = {
    "test": ["CMD", "sibyld", "worker", "--check"],
    "interval": "30s",
    "timeout": "10s",
    "retries": 3,
    "start_period": "30s",
}


def compose_config(
    *,
    image_tag: str,
    api_port: int,
    web_port: int,
    surreal_port: int,
    with_worker: bool,
    with_crawler: bool,
) -> dict[str, Any]:
    api_image = (
        f"ghcr.io/hyperb1iss/sibyl-api-crawler:{image_tag}"
        if with_crawler
        else f"ghcr.io/hyperb1iss/sibyl-api:{image_tag}"
    )
    services: dict[str, Any] = {
        "api": {
            "image": api_image,
            "container_name": "sibyl-api",
            "ports": [f"127.0.0.1:{api_port}:3334"],
            "depends_on": {"surrealdb": {"condition": "service_healthy"}},
            "environment": {
                "SIBYL_STORE": "surreal",
                "SIBYL_AUTH_STORE": "surreal",
                "SIBYL_COORDINATION_BACKEND": "redis" if with_worker else "local",
                "SIBYL_SURREAL_URL": "ws://surrealdb:8000/rpc",
                "SIBYL_SURREAL_USERNAME": "${SIBYL_SURREAL_USERNAME:-root}",
                "SIBYL_SURREAL_PASSWORD": "${SIBYL_SURREAL_PASSWORD}",
                "SIBYL_JWT_SECRET": "${SIBYL_JWT_SECRET}",
                # From the env file, so stored secrets survive the container
                # being recreated and every process decrypts them alike.
                **SETTINGS_KEY_ENV,
                "SIBYL_PUBLIC_URL": f"http://localhost:{web_port}",
                "SIBYL_SERVER_HOST": "0.0.0.0",
                "SIBYL_SERVER_PORT": "3334",
                "SIBYL_ENVIRONMENT": "production",
            },
            "restart": "unless-stopped",
        },
        "web": {
            "image": f"ghcr.io/hyperb1iss/sibyl-web:{image_tag}",
            "container_name": "sibyl-web",
            "ports": [f"127.0.0.1:{web_port}:3337"],
            "depends_on": {"api": {"condition": "service_started"}},
            "environment": {
                "SIBYL_API_URL": "http://api:3334/api",
                "NEXT_PUBLIC_API_URL": f"http://localhost:{api_port}",
                "NODE_ENV": "production",
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
                "${SIBYL_SURREAL_PASSWORD}",
                "rocksdb:///data/sibyl.db",
            ],
            "ports": [f"127.0.0.1:{surreal_port}:8000"],
            "volumes": [surreal_data_mount()],
            "healthcheck": {
                "test": ["CMD", "/surreal", "is-ready", "--endpoint", "http://localhost:8000"],
                "interval": "5s",
                "timeout": "3s",
                "retries": 5,
            },
            "restart": "unless-stopped",
        },
    }

    if with_worker:
        services["valkey"] = {
            "image": "${SIBYL_VALKEY_IMAGE:-valkey/valkey:8-alpine}",
            "container_name": "sibyl-valkey",
            "restart": "unless-stopped",
        }
        services["worker"] = {
            "image": deepcopy(api_image),
            "container_name": "sibyl-worker",
            "command": list(WORKER_COMMAND),
            "depends_on": {
                "api": {"condition": "service_started"},
                "valkey": {"condition": "service_started"},
            },
            "environment": {
                "SIBYL_STORE": "surreal",
                "SIBYL_AUTH_STORE": "surreal",
                "SIBYL_COORDINATION_BACKEND": "redis",
                **VALKEY_ADDRESS,
                "SIBYL_SURREAL_URL": "ws://surrealdb:8000/rpc",
                "SIBYL_SURREAL_USERNAME": "${SIBYL_SURREAL_USERNAME:-root}",
                "SIBYL_SURREAL_PASSWORD": "${SIBYL_SURREAL_PASSWORD}",
                # The worker verifies the API's tokens and decrypts the
                # settings it saved, so it carries the same two keys.
                **WORKER_SECRETS_ENV,
            },
            "healthcheck": deepcopy(WORKER_HEALTHCHECK),
            "restart": "unless-stopped",
        }
        services["api"]["depends_on"]["valkey"] = {"condition": "service_started"}
        services["api"]["environment"].update(VALKEY_ADDRESS)

    return {
        "services": services,
        "volumes": {"sibyl_surreal": {"name": "sibyl_surreal"}},
        "networks": {"default": {"name": "sibyl"}},
    }


def write_env_file(
    *,
    image_tag: str,
    surreal_password: str,
    jwt_secret: str,
    settings_key: str | None = None,
) -> None:
    SIBYL_DOCKER_DIR.mkdir(parents=True, exist_ok=True)
    SIBYL_DOCKER_ENV.write_text(
        "\n".join(
            [
                "# Sibyl Docker Configuration",
                "# Generated by: sibyl docker init",
                f"SIBYL_IMAGE_TAG={image_tag}",
                "SIBYL_SURREAL_USERNAME=root",
                f"SIBYL_SURREAL_PASSWORD={surreal_password}",
                f"SIBYL_JWT_SECRET={jwt_secret}",
                f"SIBYL_SETTINGS_KEY={settings_key or secrets.token_hex(32)}",
                "",
            ]
        )
    )
    os.chmod(SIBYL_DOCKER_ENV, 0o600)


def write_compose_file(config: dict[str, Any], path: Path | None = None) -> None:
    SIBYL_DOCKER_DIR.mkdir(parents=True, exist_ok=True)
    target = path or SIBYL_DOCKER_COMPOSE
    target.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _retag_managed_image(image: str, image_tag: str) -> str:
    for repository in MANAGED_IMAGE_REPOSITORIES:
        if image.startswith(f"{repository}:"):
            return f"{repository}:{image_tag}"
    return image


def write_env_image_tag(image_tag: str) -> None:
    content = SIBYL_DOCKER_ENV.read_text()
    lines = []
    wrote_tag = False
    for line in content.splitlines():
        if line.startswith("SIBYL_IMAGE_TAG="):
            lines.append(f"SIBYL_IMAGE_TAG={image_tag}")
            wrote_tag = True
        else:
            lines.append(line)
    if not wrote_tag:
        lines.append(f"SIBYL_IMAGE_TAG={image_tag}")
    SIBYL_DOCKER_ENV.write_text("\n".join(lines) + "\n")


def _surreal_image_override() -> str | None:
    """The SIBYL_SURREAL_IMAGE value Compose interpolates, if one is set.

    The shell environment wins over the env file, as it does in Compose.
    """
    override = os.environ.get("SIBYL_SURREAL_IMAGE")
    if override:
        return override
    for line in SIBYL_DOCKER_ENV.read_text().splitlines():
        key, sep, value = line.removeprefix("export ").partition("=")
        value = value.strip().strip("\"'")
        if sep and key.strip() == "SIBYL_SURREAL_IMAGE" and value:
            return value
    return None


def _plan_surreal_image(service: dict[str, Any]) -> None:
    """Move an older CLI-written SurrealDB default up to the server this CLI runs.

    Only the default inside `${SIBYL_SURREAL_IMAGE:-...}` moves, and never
    backwards, so SIBYL_SURREAL_IMAGE still wins and a hand-written image stays.
    """
    image = service.get("image")
    if not isinstance(image, str):
        return

    if image != SURREAL_IMAGE_REFERENCE:
        moved = upgraded_surreal_image(image)
        if moved is None:
            warn(f"Leaving the SurrealDB image {image} as written; this CLI runs {SURREAL_IMAGE}.")
            return
        service["image"] = moved
        match = MANAGED_SURREAL_IMAGE.fullmatch(image)
        shipped_tag = SURREAL_IMAGE.rpartition(":")[2]
        info(f"Upgrading SurrealDB from {match['tag'] if match else image} to {shipped_tag}")

    override = _surreal_image_override()
    if override and override != SURREAL_IMAGE:
        warn(f"SIBYL_SURREAL_IMAGE keeps SurrealDB on {override}; this CLI runs {SURREAL_IMAGE}.")


def _migrate_runtime_services(services: dict[str, Any]) -> None:
    """Bring an older bundle's API and worker up to what init writes now.

    Older bundles pointed both at Valkey through a URL setting Sibyl never
    read, gave the worker no keys, and kept the settings key inside the API
    container. Only what init itself wrote is rewritten; a value edited by
    hand stays.
    """
    for name in ("api", "worker"):
        service = services.get(name)
        if not isinstance(service, dict) or not isinstance(service.get("environment"), dict):
            continue
        environment = service["environment"]
        if environment.get("SIBYL_REDIS_URL") == LEGACY_VALKEY_URL:
            del environment["SIBYL_REDIS_URL"]
            for key, value in VALKEY_ADDRESS.items():
                environment.setdefault(key, value)
        for key, value in (WORKER_SECRETS_ENV if name == "worker" else SETTINGS_KEY_ENV).items():
            environment.setdefault(key, value)
        if name == "worker":
            if service.get("command") == ["sibyld", "worker"]:
                service["command"] = list(WORKER_COMMAND)
            service.setdefault("healthcheck", deepcopy(WORKER_HEALTHCHECK))


def upgraded_compose_config(config: dict[str, Any], image_tag: str | None) -> dict[str, Any]:
    """The compose config an upgrade moves to, leaving `config` as it is."""
    target = deepcopy(config)
    services = target.get("services") or {}
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        if image_tag and isinstance(image, str):
            service["image"] = _retag_managed_image(image, image_tag)
        if name == "surrealdb":
            _plan_surreal_image(service)
    _migrate_runtime_services(services)
    return target


def _env_settings_key() -> str | None:
    for line in SIBYL_DOCKER_ENV.read_text().splitlines():
        key, sep, value = line.removeprefix("export ").partition("=")
        if sep and key.strip() == "SIBYL_SETTINGS_KEY":
            return value.strip().strip("\"'") or None
    return None


# Where an older bundle's API kept the settings key it generated for itself.
API_SETTINGS_KEY_PATH = "/home/sibyl/.sibyl/settings.key"
_MISSING_PATH_MARKERS = ("could not find the file", "no such container:path")


class SettingsKeyUnreadableError(Exception):
    """An API container exists, but the settings key in it could not be read."""


def _api_container_ids() -> list[str]:
    """The bundle's API containers, running or stopped."""
    result = subprocess.run(
        compose_command(["ps", "--all", "--quiet", "api"]),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SettingsKeyUnreadableError(result.stderr.strip() or "docker compose ps failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _container_settings_key(container_id: str) -> str | None:
    """The key file inside a container, or None if it has none.

    ``docker cp`` reads a stopped container as well as a running one, which
    ``docker compose exec`` cannot.
    """
    result = subprocess.run(
        ["docker", "cp", f"{container_id}:{API_SETTINGS_KEY_PATH}", "-"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        if any(marker in detail.lower() for marker in _MISSING_PATH_MARKERS):
            return None
        raise SettingsKeyUnreadableError(detail or f"docker cp exited {result.returncode}")
    try:
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            member = next(entry for entry in archive.getmembers() if entry.isfile())
            content = archive.extractfile(member)
            key = content.read().decode().strip() if content is not None else ""
    except (tarfile.TarError, StopIteration, UnicodeDecodeError) as exc:
        raise SettingsKeyUnreadableError(f"unreadable key file: {exc}") from exc
    if len(key) != 44 or not key.endswith("="):
        raise SettingsKeyUnreadableError("the key file does not hold a settings key")
    return key


def _existing_api_settings_key() -> tuple[str | None, bool]:
    """The key an older bundle's API generated, and whether an API container exists."""
    container_ids = _api_container_ids()
    for container_id in container_ids:
        key = _container_settings_key(container_id)
        if key is not None:
            return key, True
    return None, bool(container_ids)


def ensure_env_settings_key() -> None:
    """Give the bundle's env file a settings key if it has none yet.

    An older bundle's API generated its key inside its container. That key,
    read from the container whether it is running or stopped, goes into the
    env file so the secrets it encrypted stay readable. A key that exists
    but cannot be read stops the upgrade instead of being replaced.
    """
    if _env_settings_key() is not None:
        return
    try:
        key, has_container = _existing_api_settings_key()
    except SettingsKeyUnreadableError as exc:
        error(f"Could not read the settings key the API container holds: {exc}")
        info(
            "Nothing was changed. Copy it into the env file yourself, as "
            f"SIBYL_SETTINGS_KEY=<contents of {API_SETTINGS_KEY_PATH} in the API "
            f"container> in {SIBYL_DOCKER_ENV}, then run the upgrade again."
        )
        raise typer.Exit(1) from exc
    if key is not None:
        info("Keeping the settings key the API container generated, in the env file")
    elif has_container:
        key = secrets.token_hex(32)
        info("The API never stored an encrypted setting; writing a new settings key")
    else:
        key = secrets.token_hex(32)
        warn(
            "No API container is left to read an earlier settings key from, so a new "
            "one is written. Provider keys saved in the web app before must be "
            "entered again."
        )
    content = SIBYL_DOCKER_ENV.read_text()
    separator = "" if not content or content.endswith("\n") else "\n"
    SIBYL_DOCKER_ENV.write_text(f"{content}{separator}SIBYL_SETTINGS_KEY={key}\n")
    os.chmod(SIBYL_DOCKER_ENV, 0o600)


def compose_command(args: list[str], compose_file: Path | None = None) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(compose_file or SIBYL_DOCKER_COMPOSE),
        "--env-file",
        str(SIBYL_DOCKER_ENV),
        *args,
    ]


def run_compose(
    args: list[str], compose_file: Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(compose_command(args, compose_file), text=True, check=False)


def require_configured() -> None:
    if not SIBYL_DOCKER_COMPOSE.exists() or not SIBYL_DOCKER_ENV.exists():
        error("Docker runtime is not initialized.")
        info("Run 'sibyl docker init' first.")
        raise typer.Exit(1)


def require_docker() -> None:
    if not check_docker() or not check_docker_compose():
        raise typer.Exit(1)


@app.command("init")
def init_docker(
    api_port: Annotated[int, typer.Option("--api-port", help="Host API port")] = 3334,
    web_port: Annotated[int, typer.Option("--web-port", help="Host web port")] = 3337,
    surreal_port: Annotated[int, typer.Option("--surreal-port", help="Host SurrealDB port")] = 8000,
    image_tag: Annotated[str, typer.Option("--tag", help="Sibyl image tag")] = DEFAULT_IMAGE_TAG,
    with_worker: Annotated[
        bool, typer.Option("--with-worker", help="Add Valkey and worker")
    ] = False,
    with_crawler: Annotated[
        bool,
        typer.Option("--with-crawler", help="Use the crawler-enabled API image"),
    ] = False,
    context_name: Annotated[str, typer.Option("--context", help="Context name to create")] = (
        "docker"
    ),
    activate: Annotated[
        bool, typer.Option("--activate/--no-activate", help="Set context active")
    ] = (True),
    force: Annotated[bool, typer.Option("--force", "-f", help="Overwrite existing files")] = False,
) -> None:
    """Generate pinned Docker compose files under ~/.sibyl/docker."""
    if (SIBYL_DOCKER_ENV.exists() or SIBYL_DOCKER_COMPOSE.exists()) and not force:
        error("Docker runtime already exists. Use --force to overwrite it.")
        raise typer.Exit(1)

    config = compose_config(
        image_tag=image_tag,
        api_port=api_port,
        web_port=web_port,
        surreal_port=surreal_port,
        with_worker=with_worker,
        with_crawler=with_crawler,
    )
    write_env_file(
        image_tag=image_tag,
        surreal_password=secrets.token_urlsafe(32),
        jwt_secret=secrets.token_hex(32),
        settings_key=secrets.token_hex(32),
    )
    write_compose_file(config)

    server_url = f"http://localhost:{api_port}"
    existing = config_store.get_context(context_name)
    if existing:
        config_store.update_context(context_name, server_url=server_url)
        if activate:
            config_store.set_active_context(context_name)
    else:
        config_store.create_context(context_name, server_url=server_url, set_active=activate)

    success("Docker runtime initialized")
    console.print(f"  [{NEON_CYAN}]Directory:[/{NEON_CYAN}] {SIBYL_DOCKER_DIR}")
    console.print(f"  [{NEON_CYAN}]API:[/{NEON_CYAN}]       {server_url}")
    console.print(f"  [{NEON_CYAN}]Web:[/{NEON_CYAN}]       http://localhost:{web_port}")
    info("Next: sibyl docker up")


@app.command("up")
def up(
    pull: Annotated[bool, typer.Option("--pull", help="Pull images before starting")] = False,
) -> None:
    """Start the Docker deployment."""
    require_configured()
    require_docker()
    if pull:
        run_compose(["pull"])
    result = run_compose(["up", "-d"])
    if result.returncode != 0:
        error("Failed to start Docker deployment.")
        raise typer.Exit(result.returncode)
    success("Docker deployment is running")


@app.command("logs")
def logs(
    service: Annotated[str | None, typer.Argument(help="Optional service name")] = None,
    follow: Annotated[bool, typer.Option("-f", "--follow/--no-follow", help="Follow logs")] = True,
    tail: Annotated[int, typer.Option("--tail", help="Number of lines to show")] = 100,
) -> None:
    """Show Docker deployment logs."""
    require_configured()
    args = ["logs", f"--tail={tail}"]
    if follow:
        args.append("-f")
    if service:
        args.append(service)
    run_compose(args)


@app.command("down")
def down(
    volumes: Annotated[bool, typer.Option("-v", "--volumes", help="Also remove volumes")] = False,
) -> None:
    """Stop the Docker deployment."""
    require_configured()
    args = ["down"]
    if volumes:
        args.append("-v")
    result = run_compose(args)
    if result.returncode != 0:
        error("Failed to stop Docker deployment.")
        raise typer.Exit(result.returncode)
    success("Docker deployment stopped")


@app.command("upgrade")
def upgrade(
    image_tag: Annotated[str | None, typer.Option("--tag", help="Write a new image tag")] = None,
) -> None:
    """Pull current images and recreate containers."""
    require_configured()
    require_docker()
    # Before anything is recreated: an older bundle's API holds its only copy.
    ensure_env_settings_key()
    current = yaml.safe_load(SIBYL_DOCKER_COMPOSE.read_text()) or {}
    target = upgraded_compose_config(current, image_tag)

    # Pull the target images from a staged copy first. A failed pull leaves the
    # compose file describing the containers that are still running, so the
    # pins never claim an upgrade that did not happen.
    staged = SIBYL_DOCKER_DIR / "docker-compose.upgrade.yml"
    try:
        write_compose_file(target, staged)
        pulled = run_compose(["pull"], staged)
        if pulled.returncode != 0:
            error("Failed to pull the upgrade images; the deployment and its pins are unchanged.")
            raise typer.Exit(pulled.returncode)
        if target != current:
            staged.replace(SIBYL_DOCKER_COMPOSE)
    finally:
        staged.unlink(missing_ok=True)
    if image_tag:
        write_env_image_tag(image_tag)

    # The api waits on `surrealdb: service_healthy`, so Compose recreates
    # SurrealDB on its new image and waits for it before the new API starts.
    result = run_compose(["up", "-d"])
    if result.returncode != 0:
        error("Failed to upgrade Docker deployment.")
        raise typer.Exit(result.returncode)
    success("Docker deployment upgraded")
