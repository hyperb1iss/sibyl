"""Self-hosted Docker runtime commands."""

from __future__ import annotations

import os
import secrets
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from sibyl_cli import config_store
from sibyl_cli.common import NEON_CYAN, console, error, info, success
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
                "${SIBYL_SURREAL_PASSWORD}",
                "rocksdb:///data/sibyl.db",
            ],
            "ports": [f"127.0.0.1:{surreal_port}:8000"],
            "volumes": ["sibyl_surreal:/data"],
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
            "command": ["sibyld", "worker"],
            "depends_on": {
                "api": {"condition": "service_started"},
                "valkey": {"condition": "service_started"},
            },
            "environment": {
                "SIBYL_STORE": "surreal",
                "SIBYL_AUTH_STORE": "surreal",
                "SIBYL_COORDINATION_BACKEND": "redis",
                "SIBYL_REDIS_URL": "redis://valkey:6379/0",
                "SIBYL_SURREAL_URL": "ws://surrealdb:8000/rpc",
                "SIBYL_SURREAL_USERNAME": "${SIBYL_SURREAL_USERNAME:-root}",
                "SIBYL_SURREAL_PASSWORD": "${SIBYL_SURREAL_PASSWORD}",
            },
            "restart": "unless-stopped",
        }
        services["api"]["depends_on"]["valkey"] = {"condition": "service_started"}
        services["api"]["environment"]["SIBYL_REDIS_URL"] = "redis://valkey:6379/0"

    return {
        "services": services,
        "volumes": {"sibyl_surreal": {"name": "sibyl_surreal"}},
        "networks": {"default": {"name": "sibyl"}},
    }


def write_env_file(*, image_tag: str, surreal_password: str, jwt_secret: str) -> None:
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
                "",
            ]
        )
    )
    os.chmod(SIBYL_DOCKER_ENV, 0o600)


def write_compose_file(config: dict[str, Any]) -> None:
    SIBYL_DOCKER_DIR.mkdir(parents=True, exist_ok=True)
    SIBYL_DOCKER_COMPOSE.write_text(yaml.dump(config, default_flow_style=False, sort_keys=False))


def _retag_managed_image(image: str, image_tag: str) -> str:
    for repository in MANAGED_IMAGE_REPOSITORIES:
        if image.startswith(f"{repository}:"):
            return f"{repository}:{image_tag}"
    return image


def update_configured_image_tag(image_tag: str) -> None:
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

    config = yaml.safe_load(SIBYL_DOCKER_COMPOSE.read_text()) or {}
    services = config.get("services", {})
    for service in services.values():
        if not isinstance(service, dict):
            continue
        image = service.get("image")
        if isinstance(image, str):
            service["image"] = _retag_managed_image(image, image_tag)
    write_compose_file(config)


def compose_command(args: list[str]) -> list[str]:
    return [
        "docker",
        "compose",
        "-f",
        str(SIBYL_DOCKER_COMPOSE),
        "--env-file",
        str(SIBYL_DOCKER_ENV),
        *args,
    ]


def run_compose(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(compose_command(args), text=True, check=False)


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
    if image_tag:
        update_configured_image_tag(image_tag)
    result = run_compose(["pull"])
    if result.returncode == 0:
        result = run_compose(["up", "-d"])
    if result.returncode != 0:
        error("Failed to upgrade Docker deployment.")
        raise typer.Exit(result.returncode)
    success("Docker deployment upgraded")
