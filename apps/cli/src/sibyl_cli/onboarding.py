"""Beautiful onboarding wizard for Sibyl CLI.

Guides first-time users through setup with a polished experience.
Uses Rich for beautiful terminal output.
"""

from __future__ import annotations

import httpx
from rich.console import Console
from rich.prompt import Confirm, Prompt

from sibyl_cli import config_store
from sibyl_cli.common import (
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
)

console = Console()


def show_welcome() -> None:
    """Display welcome banner."""
    console.print()
    console.print(
        f"  [{ELECTRIC_PURPLE}]◈[/{ELECTRIC_PURPLE}] Welcome to [bold {ELECTRIC_PURPLE}]Sibyl[/bold {ELECTRIC_PURPLE}]"
    )
    console.print("    [dim]Your AI-powered knowledge oracle[/dim]")
    console.print()


def show_first_run_message() -> None:
    """Display message for first-time users (non-interactive)."""
    console.print()
    console.print(
        f"  [{ELECTRIC_PURPLE}]◈[/{ELECTRIC_PURPLE}] Welcome to [bold {ELECTRIC_PURPLE}]Sibyl[/bold {ELECTRIC_PURPLE}]"
    )
    console.print("    [dim]Your AI-powered knowledge oracle[/dim]")
    console.print()
    console.print("  [dim]Get started:[/dim]")
    console.print()
    console.print(
        f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl up[/bold {NEON_CYAN}]              [dim]Start local services[/dim]"
    )
    console.print(
        f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl setup[/bold {NEON_CYAN}]           [dim]Verify environment[/dim]"
    )
    console.print(
        f'    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl search[/bold {NEON_CYAN}] [white]"query"[/white]  [dim]Search knowledge[/dim]'
    )
    console.print()
    console.print(
        f"    [dim]Run[/dim] [bold {NEON_CYAN}]sibyl --help[/bold {NEON_CYAN}] [dim]for all commands[/dim]"
    )
    console.print()


def prompt_server_url() -> str:
    """Prompt user for server URL."""
    console.print()
    console.print(f"  [{ELECTRIC_PURPLE}]◈[/{ELECTRIC_PURPLE}] [bold]Server Connection[/bold]")
    console.print()
    console.print(
        f"    [{NEON_CYAN}]1[/{NEON_CYAN}] Local [dim]localhost:3334[/dim] [dim]← default[/dim]"
    )
    console.print(f"    [{NEON_CYAN}]2[/{NEON_CYAN}] Custom URL")
    console.print()

    choice = Prompt.ask(
        "  [dim]Enter choice[/dim]",
        choices=["1", "2"],
        default="1",
    )

    if choice == "1":
        url = "http://localhost:3334"
    else:
        url = Prompt.ask(
            "  [dim]Enter server URL[/dim]",
            default="http://localhost:3334",
        )
        # Ensure URL has protocol
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        # Remove trailing slash
        url = url.rstrip("/")

    console.print()
    console.print(f"  [{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] Server URL set to [bold]{url}[/bold]")
    return url


def test_connection(url: str) -> bool:
    """Test connection to server."""
    console.print()
    console.print(f"  [{ELECTRIC_PURPLE}]◈[/{ELECTRIC_PURPLE}] [bold]Quick Start[/bold]")
    console.print()
    console.print(
        f"    Run [bold {NEON_CYAN}]sibyl up[/bold {NEON_CYAN}] to start the server locally,"
    )
    console.print("    or connect to an existing server.")
    console.print()

    if not Confirm.ask("  [dim]Test connection now?[/dim]", default=True):
        return True  # Skip test, assume it's fine

    console.print()
    with console.status(f"[{NEON_CYAN}]Connecting to server...[/{NEON_CYAN}]"):
        try:
            response = httpx.get(f"{url}/api/health", timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                version = data.get("version", "unknown")
                console.print(
                    f"  [{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] Connected! "
                    f"Server version [bold]{version}[/bold]"
                )
                return True
            console.print(
                f"  [{ELECTRIC_YELLOW}]![/{ELECTRIC_YELLOW}] Server responded with status {response.status_code}"
            )
            return False
        except httpx.ConnectError:
            console.print(
                f"  [{ELECTRIC_YELLOW}]![/{ELECTRIC_YELLOW}] Could not connect to server at {url}"
            )
            console.print(
                f"    [dim]Run[/dim] [bold {NEON_CYAN}]sibyl up[/bold {NEON_CYAN}] [dim]to start locally[/dim]"
            )
            return False
        except Exception as e:
            console.print(f"  [{ELECTRIC_YELLOW}]![/{ELECTRIC_YELLOW}] Connection error: {e}")
            return False


def show_success() -> None:
    """Display success message with next steps."""
    console.print()
    console.print(f"  [{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] [bold]You're all set![/bold]")
    console.print()
    console.print("  [dim]Try these commands:[/dim]")
    console.print()
    console.print(
        f'    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl search[/bold {NEON_CYAN}] [white]"patterns"[/white]   [dim]Search knowledge[/dim]'
    )
    console.print(
        f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl task list[/bold {NEON_CYAN}]          [dim]View tasks[/dim]"
    )
    console.print(
        f'    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl add[/bold {NEON_CYAN}] [white]"Title" "..."[/white]   [dim]Capture knowledge[/dim]'
    )
    console.print()


def run_onboarding() -> bool:
    """Run the full onboarding wizard.

    Returns True if setup completed successfully.
    """
    try:
        # Welcome
        show_welcome()
        console.print("[dim]Let's get you set up. This takes about 30 seconds.[/dim]\n")

        # Server URL
        url = prompt_server_url()
        console.print()

        # Save config
        config = config_store.load_config()
        config["server"]["url"] = url
        config_store.save_config(config)

        # Test connection
        test_connection(url)

        # Success
        show_success()

        return True

    except KeyboardInterrupt:
        console.print("\n\n[dim]Setup cancelled.[/dim]")
        return False


def needs_onboarding() -> bool:
    """Check if user needs to go through onboarding.

    Returns True if:
    - Config file doesn't exist
    - Config file exists but has no server URL
    """
    if not config_store.config_exists():
        return True

    url = config_store.get_server_url()
    return not url or url == config_store.DEFAULT_CONFIG["server"]["url"]
