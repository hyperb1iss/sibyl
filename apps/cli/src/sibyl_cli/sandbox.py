"""Sandbox lifecycle and terminal commands.

Provides CLI access to sandbox status, lifecycle operations, logs, and shell attach.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import termios
import tty
from typing import Annotated, Any

import typer

from sibyl_cli.auth_store import get_access_token
from sibyl_cli.client import SibylClient, SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    NEON_CYAN,
    console,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    run_async,
    success,
)

app = typer.Typer(
    name="sandbox",
    help="Manage execution sandboxes",
    no_args_is_help=True,
)


def _extract_items(data: Any) -> list[dict[str, Any]]:
    """Normalize list response shape across API variants."""
    if isinstance(data.get("sandboxes"), list):
        return [item for item in data["sandboxes"] if isinstance(item, dict)]
    if isinstance(data.get("items"), list):
        return [item for item in data["items"] if isinstance(item, dict)]
    if isinstance(data.get("data"), list):
        return [item for item in data["data"] if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _extract_sandbox(data: Any) -> dict[str, Any]:
    """Normalize detail response shape across API variants."""
    if isinstance(data.get("sandbox"), dict):
        return data["sandbox"]
    if isinstance(data.get("data"), dict):
        return data["data"]
    return data


def _sandbox_id(sandbox: dict[str, Any]) -> str:
    return str(sandbox.get("id") or sandbox.get("sandbox_id") or "-")


def _sandbox_status(sandbox: dict[str, Any]) -> str:
    return str(sandbox.get("status") or sandbox.get("phase") or "unknown")


def _sandbox_updated_at(sandbox: dict[str, Any]) -> str:
    return str(sandbox.get("updated_at") or sandbox.get("last_updated") or "-")


def _ws_urls(client: SibylClient, path_candidates: list[str]) -> list[str]:
    """Build websocket URLs with auth token for candidate paths."""
    base = client.base_url.removesuffix("/api")
    ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
    token = client.auth_token or get_access_token(client.base_url)

    urls: list[str] = []
    for path in path_candidates:
        url = f"{ws_base}{path}"
        if token:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}token={token}"
        urls.append(url)
    return urls


@contextlib.contextmanager
def _raw_stdin() -> Any:
    """Temporarily put stdin in raw mode for interactive shell passthrough."""
    if not sys.stdin.isatty():
        yield
        return

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, original)


async def _connect_websocket(urls: list[str]) -> Any:
    """Try websocket URLs in order until one connects."""
    import websockets

    last_error: Exception | None = None
    for url in urls:
        try:
            return await websockets.connect(url)
        except Exception as e:  # pragma: no cover - depends on server responses
            last_error = e
            continue

    if last_error:
        raise last_error
    raise RuntimeError("No websocket URLs configured")


@app.command("status")
def status(
    sandbox_id: Annotated[
        str | None,
        typer.Argument(help="Sandbox ID (omit to list all)", show_default=False),
    ] = None,
    project_id: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Filter by project ID"),
    ] = None,
    state: Annotated[
        str | None,
        typer.Option("--state", "-s", help="Filter by sandbox state"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Show sandbox status or list sandboxes."""

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                if sandbox_id:
                    data = await client.get_sandbox(sandbox_id)
                    sandbox = _extract_sandbox(data)

                    if json_output:
                        print_json(sandbox)
                        return

                    console.print("\n[bold]Sandbox Status[/bold]\n")
                    console.print(f"  ID:        [{NEON_CYAN}]{_sandbox_id(sandbox)}[/{NEON_CYAN}]")
                    console.print(
                        f"  Status:    [{CORAL}]{_sandbox_status(sandbox)}[/{CORAL}]"
                    )
                    console.print(
                        f"  Project:   [{NEON_CYAN}]{sandbox.get('project_id', '-')}[/{NEON_CYAN}]"
                    )
                    console.print(
                        f"  Task:      [{NEON_CYAN}]{sandbox.get('task_id', '-')}[/{NEON_CYAN}]"
                    )
                    console.print(f"  Updated:   {_sandbox_updated_at(sandbox)}")
                    console.print()
                    return

                data = await client.list_sandboxes(project_id=project_id, status=state)
                items = _extract_items(data)

                if json_output:
                    print_json(items)
                    return

                if not items:
                    info("No sandboxes found")
                    return

                table = create_table("Sandboxes", "ID", "Status", "Project", "Task", "Updated")
                for item in items:
                    table.add_row(
                        _sandbox_id(item),
                        _sandbox_status(item),
                        str(item.get("project_id") or "-"),
                        str(item.get("task_id") or "-"),
                        _sandbox_updated_at(item),
                    )
                console.print(table)

        except SibylClientError as e:
            handle_client_error(e)

    _run()


@app.command("start")
def start(
    task_id: Annotated[
        str | None,
        typer.Option("--task-id", help="Task ID to associate with sandbox"),
    ] = None,
    project_id: Annotated[
        str | None,
        typer.Option("--project-id", help="Project ID override"),
    ] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", help="Sandbox runtime image"),
    ] = None,
    ttl_seconds: Annotated[
        int | None,
        typer.Option("--ttl", help="Time-to-live in seconds"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Start a sandbox."""

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.start_sandbox(
                    task_id=task_id,
                    project_id=project_id,
                    image=image,
                    ttl_seconds=ttl_seconds,
                )
                sandbox = _extract_sandbox(data)

                if json_output:
                    print_json(data)
                    return

                success(
                    f"Started sandbox {_sandbox_id(sandbox)}"
                    f" ({_sandbox_status(sandbox)})"
                )

        except SibylClientError as e:
            handle_client_error(e)

    _run()


@app.command("suspend")
def suspend(
    sandbox_id: Annotated[str, typer.Argument(help="Sandbox ID")],
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Suspend a running sandbox."""

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.suspend_sandbox(sandbox_id)

                if json_output:
                    print_json(data)
                    return

                success(f"Suspended sandbox {sandbox_id}")

        except SibylClientError as e:
            handle_client_error(e)

    _run()


@app.command("resume")
def resume(
    sandbox_id: Annotated[str, typer.Argument(help="Sandbox ID")],
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Resume a suspended sandbox."""

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.resume_sandbox(sandbox_id)

                if json_output:
                    print_json(data)
                    return

                success(f"Resumed sandbox {sandbox_id}")

        except SibylClientError as e:
            handle_client_error(e)

    _run()


@app.command("destroy")
def destroy(
    sandbox_id: Annotated[str, typer.Argument(help="Sandbox ID")],
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Destroy a sandbox."""

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.destroy_sandbox(sandbox_id)

                if json_output:
                    print_json(data)
                    return

                success(f"Destroyed sandbox {sandbox_id}")

        except SibylClientError as e:
            handle_client_error(e)

    _run()


@app.command("logs")
def logs(
    sandbox_id: Annotated[str, typer.Argument(help="Sandbox ID")],
    tail: Annotated[
        int,
        typer.Option("--tail", "-n", help="Number of lines to fetch"),
    ] = 200,
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Stream logs in realtime"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """View sandbox logs."""

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                if not follow:
                    data = await client.sandbox_logs(sandbox_id, tail=tail)

                    if json_output:
                        print_json(data)
                        return

                    lines = data.get("lines") if isinstance(data, dict) else None
                    if isinstance(lines, list):
                        for line in lines:
                            console.print(str(line), end="")
                        return

                    text = data.get("log") if isinstance(data, dict) else None
                    if isinstance(text, str):
                        console.print(text, end="")
                        return

                    print_json(data)
                    return

                try:
                    urls = _ws_urls(
                        client,
                        [
                            f"/api/sandboxes/{sandbox_id}/logs/stream",
                            f"/api/sandbox/{sandbox_id}/logs/stream",
                            f"/api/sandboxes/{sandbox_id}/logs/ws",
                        ],
                    )

                    info("Connecting to sandbox log stream... (Ctrl+C to stop)")
                    ws = await _connect_websocket(urls)
                    async with ws:
                        success("Connected")
                        while True:
                            message = await ws.recv()
                            if isinstance(message, bytes):
                                sys.stdout.buffer.write(message)
                                sys.stdout.buffer.flush()
                            else:
                                print(message, end="", flush=True)
                except ImportError:
                    error("websockets package is required for --follow")
                    raise typer.Exit(1) from None

        except SibylClientError as e:
            handle_client_error(e)
        except KeyboardInterrupt:
            console.print("\n[dim]Log stream stopped[/dim]")

    _run()


@app.command("shell")
def shell(
    sandbox_id: Annotated[str, typer.Argument(help="Sandbox ID")],
    command: Annotated[
        str | None,
        typer.Option("--command", "-c", help="Run one command and exit"),
    ] = None,
) -> None:
    """Attach a terminal shell to a sandbox via WebSocket."""

    @run_async
    async def _run() -> None:
        async with get_client() as client:
            urls = _ws_urls(
                client,
                [
                    f"/api/sandboxes/{sandbox_id}/attach",
                    f"/api/sandbox/{sandbox_id}/attach",
                    f"/api/sandboxes/{sandbox_id}/shell/ws",
                ],
            )

            try:
                ws = await _connect_websocket(urls)
            except ImportError:
                error("websockets package is required for sandbox shell")
                raise typer.Exit(1) from None
            except Exception as e:
                error(f"Unable to attach shell: {e}")
                raise typer.Exit(1) from None

            async with ws:
                if command:
                    await ws.send(command + "\n")
                    while True:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=0.75)
                        except TimeoutError:
                            break
                        if isinstance(message, bytes):
                            sys.stdout.buffer.write(message)
                            sys.stdout.buffer.flush()
                        else:
                            print(message, end="", flush=True)
                    return

                success(f"Attached to sandbox {sandbox_id}. Press Ctrl+C to detach.")

                async def _read_stdin() -> None:
                    while True:
                        chunk = await asyncio.to_thread(sys.stdin.buffer.read, 1)
                        if not chunk:
                            break
                        await ws.send(chunk)

                async def _write_stdout() -> None:
                    while True:
                        message = await ws.recv()
                        if isinstance(message, bytes):
                            sys.stdout.buffer.write(message)
                            sys.stdout.buffer.flush()
                        else:
                            print(message, end="", flush=True)

                with _raw_stdin():
                    reader = asyncio.create_task(_read_stdin())
                    writer = asyncio.create_task(_write_stdout())
                    done, pending = await asyncio.wait(
                        {reader, writer}, return_when=asyncio.FIRST_COMPLETED
                    )

                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        exc = task.exception()
                        if exc is not None:
                            raise exc

    try:
        _run()
    except KeyboardInterrupt:
        console.print("\n[dim]Shell detached[/dim]")
    except SibylClientError as e:
        handle_client_error(e)
