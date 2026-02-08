"""CLI entry point for Sibyl Runner daemon."""

import asyncio
import signal
from pathlib import Path

import typer
from rich.console import Console

from sibyl_runner.config import RunnerConfig, load_config, save_config
from sibyl_runner.daemon import RunnerDaemon

app = typer.Typer(
    name="sibyl-runner",
    help="Distributed agent execution daemon for Sibyl",
    no_args_is_help=True,
)
console = Console()

# SilkCircuit colors
PURPLE = "#e135ff"
CYAN = "#80ffea"
CORAL = "#ff6ac1"


@app.command()
def run(
    config_file: Path = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file (default: ~/.config/sibyl/runner.yaml)",
    ),
    server_url: str = typer.Option(
        None,
        "--server",
        "-s",
        help="Sibyl server URL (overrides config)",
    ),
    name: str = typer.Option(
        None,
        "--name",
        "-n",
        help="Runner display name (overrides config)",
    ),
    max_agents: int = typer.Option(
        None,
        "--max-agents",
        "-m",
        help="Max concurrent agents (overrides config)",
    ),
    runner_id: str = typer.Option(
        None,
        "--runner-id",
        help="Runner ID (overrides config/env; bypasses manual register flow when set)",
    ),
    sandbox_mode: bool | None = typer.Option(
        None,
        "--sandbox-mode/--no-sandbox-mode",
        help="Enable sandbox execution mode",
    ),
    foreground: bool = typer.Option(
        False,
        "--foreground",
        "-f",
        help="Run in foreground (don't daemonize)",
    ),
) -> None:
    """Start the runner daemon and connect to Sibyl server."""
    # Load config
    config = load_config(config_file)

    # Apply CLI overrides
    if server_url:
        config.server_url = server_url
    if name:
        config.name = name
    if max_agents:
        config.max_concurrent_agents = max_agents
    if runner_id:
        config.runner_id = runner_id
    if sandbox_mode is not None:
        config.sandbox_mode = sandbox_mode

    # Validate config
    if not config.server_url:
        console.print(f"[{CORAL}]Error:[/] No server URL configured")
        console.print("Use [cyan]--server[/] or set SIBYL_SERVER_URL / config file")
        raise typer.Exit(1)

    if not config.runner_id:
        console.print(f"[{CORAL}]Error:[/] Missing runner ID")
        console.print("Provide [cyan]--runner-id[/], set SIBYL_RUNNER_ID, or run register first")
        raise typer.Exit(1)

    console.print(f"[{PURPLE}]Sibyl Runner[/] starting...")
    mode_label = "sandbox" if config.sandbox_mode else "registered"
    console.print(f"  Server: [{CYAN}]{config.server_url}[/]")
    console.print(f"  Runner: [{CYAN}]{config.name or 'unnamed-runner'}[/] ({config.runner_id})")
    console.print(f"  Mode: [{CYAN}]{mode_label}[/]")
    if config.sandbox_mode and config.sandbox_id:
        console.print(f"  Sandbox: [{CYAN}]{config.sandbox_id}[/]")
    console.print(f"  Max agents: [{CYAN}]{config.max_concurrent_agents}[/]")

    # Run daemon
    try:
        asyncio.run(_run_daemon(config, foreground))
    except KeyboardInterrupt:
        console.print(f"\n[{PURPLE}]Shutting down...[/]")


async def _run_daemon(config: RunnerConfig, foreground: bool) -> None:
    """Run the daemon event loop."""
    daemon = RunnerDaemon(config)

    # Setup signal handlers
    loop = asyncio.get_running_loop()

    def shutdown() -> None:
        daemon.request_shutdown()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown)

    await daemon.run()


@app.command()
def register(
    server_url: str = typer.Option(
        ...,
        "--server",
        "-s",
        help="Sibyl server URL",
    ),
    name: str = typer.Option(
        None,
        "--name",
        "-n",
        help="Runner display name (default: hostname)",
    ),
    max_agents: int = typer.Option(
        3,
        "--max-agents",
        "-m",
        help="Max concurrent agents",
    ),
    config_file: Path = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to save config (default: ~/.config/sibyl/runner.yaml)",
    ),
) -> None:
    """Register this runner with a Sibyl server."""
    import socket

    # Get hostname for default name
    hostname = socket.gethostname()
    runner_name = name or hostname

    console.print(f"[{PURPLE}]Registering runner[/] with Sibyl server...")
    console.print(f"  Server: [{CYAN}]{server_url}[/]")
    console.print(f"  Name: [{CYAN}]{runner_name}[/]")

    try:
        result = asyncio.run(_register_runner(server_url, runner_name, hostname, max_agents))
    except Exception as e:
        console.print(f"[{CORAL}]Registration failed:[/] {e}")
        raise typer.Exit(1) from None

    # Save config
    config = RunnerConfig(
        server_url=server_url,
        runner_id=result["id"],
        graph_runner_id=result["graph_runner_id"],
        name=runner_name,
        max_concurrent_agents=max_agents,
    )
    save_config(config, config_file)

    console.print(f"\n[{PURPLE}]Registration successful![/]")
    console.print(f"  Runner ID: [{CYAN}]{result['id']}[/]")
    console.print(f"  Graph ID: [{CYAN}]{result['graph_runner_id']}[/]")
    console.print("\nRun [cyan]sibyl-runner run[/] to start the daemon")


async def _register_runner(
    server_url: str,
    name: str,
    hostname: str,
    max_agents: int,
) -> dict:
    """Register runner with server via REST API."""
    import httpx
    from sibyl_cli.auth_store import get_access_token, normalize_api_url

    url = f"{server_url.rstrip('/')}/api/runners/register"
    api_url = normalize_api_url(f"{server_url.rstrip('/')}/api")

    # Load auth token from CLI's auth store
    token = get_access_token(api_url)
    if not token:
        raise RuntimeError(
            "Not authenticated. Run 'sibyl auth login' first to authenticate with the server."
        )

    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json={
                "name": name,
                "hostname": hostname,
                "max_concurrent_agents": max_agents,
                "capabilities": ["docker"],  # TODO: detect capabilities
            },
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()


@app.command()
def status(
    config_file: Path = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to config file",
    ),
) -> None:
    """Show runner status."""
    config = load_config(config_file)

    if not config.runner_id:
        console.print(f"[{CORAL}]Runner not registered[/]")
        console.print("Run [cyan]sibyl-runner register[/] first")
        raise typer.Exit(1)

    console.print(f"[{PURPLE}]Runner Status[/]")
    console.print(f"  ID: [{CYAN}]{config.runner_id}[/]")
    console.print(f"  Name: [{CYAN}]{config.name}[/]")
    console.print(f"  Server: [{CYAN}]{config.server_url}[/]")
    console.print(f"  Max agents: [{CYAN}]{config.max_concurrent_agents}[/]")

    # TODO: Query server for live status


def main() -> None:
    """Entry point."""
    app()


if __name__ == "__main__":
    main()
