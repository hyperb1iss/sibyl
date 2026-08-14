"""Host-managed embedded daemon commands."""

from __future__ import annotations

import os
import platform
import plistlib
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import typer

from sibyl_cli import config_store
from sibyl_cli.common import NEON_CYAN, console, error, info, success
from sibyl_cli.state import get_context_override

SIBYL_RUN_DIR = Path.home() / ".sibyl" / "run"
SIBYLD_PID_FILE = SIBYL_RUN_DIR / "sibyld.pid"
SIBYLD_LOG_FILE = SIBYL_RUN_DIR / "sibyld.log"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
SERVICE_LABEL = "tech.hyperbliss.sibyl"

service_app = typer.Typer(help="Install local daemon service files")


@dataclass(frozen=True)
class HostContext:
    name: str
    server_url: str


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def resolve_host_context() -> HostContext | None:
    context_name = get_context_override()
    ctx = (
        config_store.get_context(context_name)
        if context_name
        else config_store.resolve_effective_context()
    )
    if ctx is None:
        return None
    return HostContext(name=ctx.name, server_url=ctx.server_url)


def is_local_server(server_url: str) -> bool:
    parsed = urlparse(server_url)
    return (parsed.hostname or "").lower() in LOCAL_HOSTS


def require_local_context() -> HostContext:
    ctx = resolve_host_context()
    if ctx is None:
        error("No active context is configured.")
        info("Run 'sibyl init --local' before starting the local daemon.")
        raise typer.Exit(1)
    if not is_local_server(ctx.server_url):
        error(f"Context '{ctx.name}' points to {ctx.server_url}.")
        info("Switch to a local context with 'sibyl context use local'.")
        raise typer.Exit(1)
    return ctx


def read_pid(path: Path | None = None) -> int | None:
    path = path or SIBYLD_PID_FILE
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def daemon_command(
    host: str,
    port: int,
    transport: str,
    *,
    executable: str = "sibyld",
) -> list[str]:
    return [
        executable,
        "serve",
        "--embedded",
        "--host",
        host,
        "--port",
        str(port),
        "--transport",
        transport,
    ]


def resolve_sibyld_executable() -> str:
    return shutil.which("sibyld") or "sibyld"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"


def systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "sibyl.service"


def service_log_file() -> Path:
    return SIBYL_RUN_DIR / "sibyld.service.log"


def render_launchd_plist(command: list[str]) -> str:
    log_path = service_log_file()
    payload = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": True,
        "WorkingDirectory": str(Path.home()),
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }
    return plistlib.dumps(payload, sort_keys=True).decode("utf-8")


def render_systemd_unit(command: list[str]) -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=Sibyl local embedded daemon",
            "After=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={shlex.join(command)}",
            "Restart=on-failure",
            "RestartSec=5",
            "WorkingDirectory=%h",
            f"StandardOutput=append:{service_log_file()}",
            f"StandardError=append:{service_log_file()}",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def serve(
    host: Annotated[str, typer.Option("--host", help="Host to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to listen on")] = 3334,
    transport: Annotated[str, typer.Option("--transport", "-t", help="MCP transport")] = (
        "streamable-http"
    ),
    background: Annotated[
        bool, typer.Option("--background", "-d", help="Run in the background")
    ] = (False),
    web: Annotated[bool, typer.Option("--web", help="Also start the web UI when bundled")] = False,
) -> None:
    """Start the local embedded Sibyl daemon for the active local context."""
    ctx = require_local_context()
    if web:
        error("The web UI is not bundled into the host install yet.")
        info("Use 'sibyl docker up' for the full API + web stack.")
        raise typer.Exit(1)

    cmd = daemon_command(host, port, transport)
    if not background:
        console.print(f"[{NEON_CYAN}]Context:[/{NEON_CYAN}] {ctx.name} -> {ctx.server_url}")
        raise typer.Exit(subprocess.run(cmd, check=False).returncode)

    existing_pid = read_pid()
    if existing_pid and pid_alive(existing_pid):
        error(f"sibyld is already running as pid {existing_pid}.")
        raise typer.Exit(1)

    SIBYL_RUN_DIR.mkdir(parents=True, exist_ok=True)
    log = SIBYLD_LOG_FILE.open("a", encoding="utf-8")
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log.close()
    SIBYLD_PID_FILE.write_text(f"{process.pid}\n")
    success(f"Started sibyld as pid {process.pid}")
    console.print(f"  [{NEON_CYAN}]Context:[/{NEON_CYAN}] {ctx.name} -> {ctx.server_url}")
    console.print(f"  [{NEON_CYAN}]Logs:[/{NEON_CYAN}]    {SIBYLD_LOG_FILE}")


def start(
    host: Annotated[str, typer.Option("--host", help="Host to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to listen on")] = 3334,
    transport: Annotated[str, typer.Option("--transport", "-t", help="MCP transport")] = (
        "streamable-http"
    ),
    web: Annotated[bool, typer.Option("--web", help="Also start the web UI when bundled")] = False,
) -> None:
    """Start the local embedded Sibyl daemon in the background."""
    serve(host=host, port=port, transport=transport, background=True, web=web)


def stop(
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait before failing")
    ] = 5.0,
) -> None:
    """Stop the background local embedded daemon."""
    pid = read_pid()
    if pid is None:
        info("No sibyld pid file found.")
        return
    if not pid_alive(pid):
        SIBYLD_PID_FILE.unlink(missing_ok=True)
        info(f"Removed stale sibyld pid file for pid {pid}.")
        return

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            SIBYLD_PID_FILE.unlink(missing_ok=True)
            success("sibyld stopped")
            return
        time.sleep(0.1)

    error(f"sibyld pid {pid} did not stop within {timeout:g}s.")
    raise typer.Exit(1)


@service_app.command("install")
def install_service(
    host: Annotated[str, typer.Option("--host", help="Host to bind")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Port to listen on")] = 3334,
    transport: Annotated[str, typer.Option("--transport", "-t", help="MCP transport")] = (
        "streamable-http"
    ),
    force: Annotated[
        bool, typer.Option("--force", "-f", help="Overwrite an existing service file")
    ] = (False),
) -> None:
    """Write a native user-service file for the active local context."""
    ctx = require_local_context()
    system = platform.system()
    command = daemon_command(
        host,
        port,
        transport,
        executable=resolve_sibyld_executable(),
    )

    if system == "Darwin":
        path = launchd_plist_path()
        content = render_launchd_plist(command)
        start_command = f"launchctl bootstrap gui/$(id -u) {shlex.quote(str(path))}"
    elif system == "Linux":
        path = systemd_unit_path()
        content = render_systemd_unit(command)
        start_command = "systemctl --user enable --now sibyl.service"
    else:
        error(f"Native service install is not supported on {system or 'this platform'}.")
        raise typer.Exit(1)

    if path.exists() and not force:
        error(f"{path} already exists. Use --force to overwrite it.")
        raise typer.Exit(1)

    path.parent.mkdir(parents=True, exist_ok=True)
    SIBYL_RUN_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    success(f"Installed Sibyl service file for context '{ctx.name}'")
    console.print(f"  [{NEON_CYAN}]File:[/{NEON_CYAN}]  {path}")
    console.print(f"  [{NEON_CYAN}]Logs:[/{NEON_CYAN}]  {service_log_file()}")
    info("The service was not started automatically.")
    info(f"Start it with: {start_command}")


@service_app.command("path")
def service_path() -> None:
    """Print the native service file path for this platform."""
    system = platform.system()
    if system == "Darwin":
        console.print(str(launchd_plist_path()))
    elif system == "Linux":
        console.print(str(systemd_unit_path()))
    else:
        error(f"Native service files are not supported on {system or 'this platform'}.")
        raise typer.Exit(1)
