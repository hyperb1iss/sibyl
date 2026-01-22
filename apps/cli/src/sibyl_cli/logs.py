"""Log viewing commands for developer introspection.

Provides access to server logs for debugging and monitoring.
Requires organization OWNER role.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_YELLOW,
    ERROR_RED,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    error,
    handle_client_error,
    info,
    print_json,
    run_async,
)

app = typer.Typer(
    name="logs",
    help="View server logs (requires OWNER role)",
    no_args_is_help=True,
)


def _format_level(level: str) -> str:
    """Format log level with appropriate color."""
    level_colors = {
        "debug": "dim",
        "info": NEON_CYAN,
        "warning": ELECTRIC_YELLOW,
        "error": ERROR_RED,
        "critical": ERROR_RED,
    }
    color = level_colors.get(level.lower(), "white")
    return f"[{color}]{level:7}[/{color}]"


def _format_service(service: str) -> str:
    """Format service name with color."""
    service_colors = {
        "api": "#e135ff",  # Electric Purple
        "worker": "#f1fa8c",  # Electric Yellow
        "web": "#80ffea",  # Neon Cyan
        "cli": "#ff6ac1",  # Coral
    }
    color = service_colors.get(service.lower(), "white")
    return f"[{color}]{service:7}[/{color}]"


def _print_entry(entry: dict) -> None:
    """Print a single log entry with formatting."""
    timestamp = entry.get("timestamp", "")[:19]  # Trim to seconds
    service = entry.get("service", "unknown")
    level = entry.get("level", "info")
    event = entry.get("event", "")
    context = entry.get("context", {})

    # Format context as key=value pairs
    context_str = ""
    if context:
        pairs = [f"[dim]{k}=[/dim]{v}" for k, v in context.items()]
        context_str = " " + " ".join(pairs)

    console.print(
        f"[dim]{timestamp}[/dim] "
        f"{_format_service(service)} "
        f"{_format_level(level)} "
        f"{event}{context_str}"
    )


@app.command("tail")
def tail(
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Number of entries to show"),
    ] = 50,
    service: Annotated[
        str | None,
        typer.Option("--service", "-s", help="Filter by service (api, worker)"),
    ] = None,
    level: Annotated[
        str | None,
        typer.Option("--level", "-l", help="Filter by log level"),
    ] = None,
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Stream logs in real-time"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """View recent server logs.

    Shows the most recent log entries from the server's ring buffer.
    Use --follow to stream logs in real-time via WebSocket.

    Examples:
        sibyl logs tail                    # Last 50 entries
        sibyl logs tail -n 100             # Last 100 entries
        sibyl logs tail -s worker          # Worker logs only
        sibyl logs tail -l error           # Errors only
        sibyl logs tail -f                 # Stream in real-time
    """

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                if follow:
                    await _stream_logs(client, service, level)
                else:
                    params: dict = {"limit": limit}
                    if service:
                        params["service"] = service
                    if level:
                        params["level"] = level

                    response = await client.get("/logs", params=params)
                    entries: list[dict] = response if isinstance(response, list) else []

                    if json_output:
                        print_json(entries)
                        return

                    if not entries:
                        info("No log entries found")
                        return

                    console.print(f"\n[bold]Last {len(entries)} log entries:[/bold]\n")
                    for entry in entries:
                        if isinstance(entry, dict):
                            _print_entry(entry)
                    console.print()

        except SibylClientError as e:
            if e.status_code == 403:
                error("Access denied - OWNER role required for log access")
                raise typer.Exit(1) from None
            handle_client_error(e)

    _run()


async def _stream_logs(
    client: object,
    service: str | None,
    level: str | None,
) -> None:
    """Stream logs via WebSocket."""
    import websockets

    from sibyl_cli.auth_store import get_access_token
    from sibyl_cli.client import _get_default_api_url

    api_url = _get_default_api_url()
    token = get_access_token(api_url)

    if not token:
        error("Authentication required - run 'sibyl auth login' first")
        raise typer.Exit(1)

    # Build WebSocket URL (strip /api suffix for WS endpoint)
    base_url = api_url.removesuffix("/api")
    ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = f"{ws_url}/api/logs/stream?token={token}"

    info("Connecting to log stream... (Ctrl+C to stop)")
    console.print()

    try:
        async with websockets.connect(ws_url) as ws:
            console.print(f"[{SUCCESS_GREEN}]Connected[/{SUCCESS_GREEN}] - streaming logs\n")
            while True:
                try:
                    msg = await ws.recv()
                    import json

                    entry = json.loads(msg)

                    # Apply filters
                    if service and entry.get("service", "").lower() != service.lower():
                        continue
                    if level and entry.get("level", "").lower() != level.lower():
                        continue

                    _print_entry(entry)
                except websockets.ConnectionClosed:
                    error("Connection closed")
                    break
    except asyncio.CancelledError:
        console.print("\n[dim]Stream stopped[/dim]")
    except Exception as e:
        error(f"WebSocket error: {e}")
        raise typer.Exit(1) from None


@app.command("stats")
def stats(
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
) -> None:
    """Show log buffer statistics."""

    @run_async
    async def _run() -> None:
        try:
            async with get_client() as client:
                data = await client.get("/logs/stats")

                if json_output:
                    print_json(data)
                    return

                console.print("\n[bold]Log Buffer Statistics[/bold]\n")
                console.print(
                    f"  Buffer size:      [{CORAL}]{data.get('buffer_size', 0)}[/{CORAL}] entries"
                )
                console.print(
                    f"  Active streams:   [{CORAL}]{data.get('subscriber_count', 0)}[/{CORAL}]"
                )
                console.print()

        except SibylClientError as e:
            if e.status_code == 403:
                error("Access denied - OWNER role required")
                raise typer.Exit(1) from None
            handle_client_error(e)

    _run()
