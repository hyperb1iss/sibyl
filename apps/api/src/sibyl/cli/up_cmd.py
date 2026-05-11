"""Commands for running Sibyl locally.

`sibyl up` starts local data services and the API server.
`sibyl down` stops everything.
"""

from __future__ import annotations

import contextlib
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
from sibyl.runtime_shape import (
    default_auth_store,
    requires_surreal_support,
    resolve_coordination_backend,
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


def _default_local_surreal_url(env: dict[str, str]) -> str:
    port = env.get("SIBYL_SURREAL_PORT", "8000")
    return f"ws://127.0.0.1:{port}/rpc"


def _resolve_coordination_backend(env: dict[str, str]) -> str:
    return resolve_coordination_backend(
        store=env.get("SIBYL_STORE", "surreal"),
        coordination_backend=env.get("SIBYL_COORDINATION_BACKEND", "auto"),
    )


def _apply_surreal_dev_defaults(env: dict[str, str]) -> None:
    env.setdefault("SIBYL_STORE", "surreal")
    env["SIBYL_AUTH_STORE"] = default_auth_store(store=env["SIBYL_STORE"])
    env.setdefault("SIBYL_COORDINATION_BACKEND", "auto")

    if requires_surreal_support(
        store=env["SIBYL_STORE"],
        auth_store=env["SIBYL_AUTH_STORE"],
    ):
        env.setdefault("SIBYL_SURREAL_URL", _default_local_surreal_url(env))
        env.pop("SIBYL_SURREAL_DATA_DIR", None)

        surreal_url = env["SIBYL_SURREAL_URL"]
        if surreal_url.startswith(
            (
                "ws://127.0.0.1",
                "ws://localhost",
                "http://127.0.0.1",
                "http://localhost",
            )
        ):
            env.setdefault("SIBYL_SURREAL_USERNAME", "root")
            env.setdefault("SIBYL_SURREAL_PASSWORD", "root")

    if env["SIBYL_STORE"] != "surreal":
        env.setdefault("SIBYL_REDIS_HOST", "127.0.0.1")
        env.setdefault("SIBYL_REDIS_PORT", "6381")
        env.setdefault("SIBYL_REDIS_PASSWORD", "")
        return

    if _resolve_coordination_backend(env) == "redis":
        env.setdefault("SIBYL_REDIS_HOST", "127.0.0.1")
        env.setdefault("SIBYL_REDIS_PORT", "6381")
        env.setdefault("SIBYL_REDIS_PASSWORD", "")
    else:
        env.pop("SIBYL_REDIS_HOST", None)
        env.pop("SIBYL_REDIS_PORT", None)
        env.pop("SIBYL_REDIS_PASSWORD", None)


def _load_runtime_env(project_root: Path) -> dict[str, str]:
    env = os.environ.copy()
    python_paths = [
        str(project_root / "apps" / "api" / "src"),
        str(project_root / "packages" / "python" / "sibyl-core" / "src"),
    ]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(python_paths)

    env_file = project_root / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip().strip('"').strip("'")

    _apply_surreal_dev_defaults(env)
    return env


def _compose_services_for_env(env: dict[str, str]) -> list[str]:
    store = env.get("SIBYL_STORE", "surreal")
    auth_store = env.get("SIBYL_AUTH_STORE", default_auth_store(store=store))
    services: list[str] = []

    if store == "legacy":
        services.append("falkordb")
    if requires_surreal_support(store=store, auth_store=auth_store):
        services.append("surrealdb")
    if _resolve_coordination_backend(env) == "redis":
        services.append("redis")
    return services


def _configure_requested_worker_mode(env: dict[str, str], *, with_worker: bool) -> None:
    if not with_worker:
        return

    coordination_backend = _resolve_coordination_backend(env)
    if coordination_backend == "local":
        info("Local coordination already runs jobs and schedules in-process")
        return

    if env.get("SIBYL_STORE", "surreal") == "legacy":
        info("Worker mode: running embedded arq worker in the API process")
        env["SIBYL_RUN_WORKER"] = "true"
        return

    warn("`--with-worker` is only supported in legacy mode")
    info("Run `moon run api:worker` or `uv run sibyld worker` in another shell.")


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

    Starts the configured local data services and the API server.
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

    env = _load_runtime_env(project_root)

    # Check Docker
    if not skip_docker:
        if not _check_docker():
            error("Docker is not running. Please start Docker first.")
            raise typer.Exit(1)

        # Start Docker services
        with console.status(f"[{NEON_CYAN}]Starting Docker services...[/{NEON_CYAN}]"):
            result = _run_docker_compose(
                ["up", "-d", *_compose_services_for_env(env)], project_root
            )
            if result.returncode != 0:
                error("Failed to start Docker services")
                console.print(f"[dim]{result.stderr}[/dim]")
                raise typer.Exit(1)

        success("Docker services started")

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
        _start_server_foreground(project_root, with_worker, env)


def _start_server_foreground(project_root: Path, with_worker: bool, env: dict[str, str]) -> None:
    """Start server in foreground (blocking)."""
    api_root = project_root / "apps" / "api"
    reload_root = api_root / "src"

    console.print(f"\n[{SUCCESS_GREEN}]Starting API server...[/{SUCCESS_GREEN}]")
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "sibyl.main:create_dev_app",
        "--factory",
        "--host",
        "0.0.0.0",  # noqa: S104 - intentional for local dev
        "--port",
        "3334",
        "--reload",
        "--reload-dir",
        str(reload_root),
        "--timeout-graceful-shutdown",
        "5",
    ]

    _configure_requested_worker_mode(env, with_worker=with_worker)
    env.setdefault("PYTHONFAULTHANDLER", "1")
    env.setdefault("SIBYL_DEV_DIAGNOSTICS", "1")

    try:
        process = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=api_root,
            env=env,
            start_new_session=True,
        )

        def stop_process() -> None:
            with contextlib.suppress(ProcessLookupError, OSError):
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, OSError):
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.wait(timeout=5)

        def signal_handler(_sig: int, _frame: object) -> None:
            console.print(f"\n[{NEON_CYAN}]Stopping server...[/{NEON_CYAN}]")
            stop_process()
            console.print(f"[{SUCCESS_GREEN}]Server stopped.[/{SUCCESS_GREEN}]")
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        process.wait()

    except KeyboardInterrupt:
        stop_process()


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

    Stops local data services and any running API server.
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
