"""Beautiful onboarding wizard for Sibyl CLI.

Guides first-time users through setup with a polished experience.
Uses Rich for beautiful terminal output.
"""

from __future__ import annotations

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

from sibyl.cli import config_store
from sibyl.cli.common import (
    ELECTRIC_PURPLE,
    NEON_CYAN,
    SUCCESS_GREEN,
)

console = Console()


def show_welcome() -> None:
    """Display welcome banner."""
    welcome_text = Text()
    welcome_text.append("Welcome to ", style="white")
    welcome_text.append("Sibyl", style=f"bold {ELECTRIC_PURPLE}")
    welcome_text.append("\n")
    welcome_text.append("Your AI-powered knowledge oracle", style="dim")

    console.print()
    console.print(
        Panel(
            welcome_text,
            border_style=NEON_CYAN,
            padding=(1, 4),
        )
    )
    console.print()


def show_first_run_message() -> None:
    """Display message for first-time users (non-interactive)."""
    text = Text()
    text.append("Welcome to ", style="white")
    text.append("Sibyl", style=f"bold {ELECTRIC_PURPLE}")
    text.append("!\n\n", style="white")
    text.append("Looks like this is your first time.\n", style="dim")
    text.append("Run ", style="dim")
    text.append("sibyl config init", style=f"bold {NEON_CYAN}")
    text.append(" to get started.", style="dim")

    console.print()
    console.print(
        Panel(
            text,
            border_style=NEON_CYAN,
            padding=(1, 4),
        )
    )
    console.print()


def prompt_server_url() -> str:
    """Prompt user for server URL."""
    console.print(
        Panel(
            "[white]Where is your Sibyl server?[/white]\n\n"
            f"[{NEON_CYAN}]1[/{NEON_CYAN}] Local ([dim]localhost:3334[/dim]) [dim]← default[/dim]\n"
            f"[{NEON_CYAN}]2[/{NEON_CYAN}] Custom URL",
            title="[white]Server Connection[/white]",
            border_style=NEON_CYAN,
            padding=(1, 2),
        )
    )

    choice = Prompt.ask(
        "\n[dim]Enter choice[/dim]",
        choices=["1", "2"],
        default="1",
    )

    if choice == "1":
        url = "http://localhost:3334"
    else:
        url = Prompt.ask(
            "[dim]Enter server URL[/dim]",
            default="http://localhost:3334",
        )
        # Ensure URL has protocol
        if not url.startswith(("http://", "https://")):
            url = f"http://{url}"
        # Remove trailing slash
        url = url.rstrip("/")

    console.print(f"\n[{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] Server URL set to [bold]{url}[/bold]")
    return url


def test_connection(url: str) -> bool:
    """Test connection to server."""
    console.print(
        Panel(
            f"[white]Run [bold]{NEON_CYAN}]sibyl up[/{NEON_CYAN}][/bold] to start the server locally,\n"
            "or connect to an existing server.\n\n"
            "[dim]Test connection now?[/dim]",
            title="[white]Quick Start[/white]",
            border_style=NEON_CYAN,
            padding=(1, 2),
        )
    )

    if not Confirm.ask("\n[dim]Test connection[/dim]", default=True):
        return True  # Skip test, assume it's fine

    console.print()
    with console.status(f"[{NEON_CYAN}]Connecting to server...[/{NEON_CYAN}]"):
        try:
            response = httpx.get(f"{url}/api/health", timeout=5.0)
            if response.status_code == 200:
                data = response.json()
                version = data.get("version", "unknown")
                console.print(
                    f"[{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] Connected! "
                    f"Server version [bold]{version}[/bold]"
                )
                return True
            console.print(
                f"[yellow]⚠[/yellow] Server responded with status {response.status_code}"
            )
            return False
        except httpx.ConnectError:
            console.print(
                f"[yellow]⚠[/yellow] Could not connect to server at {url}\n"
                f"  [dim]Run [bold]sibyl up[/bold] to start the server locally[/dim]"
            )
            return False
        except Exception as e:
            console.print(f"[yellow]⚠[/yellow] Connection error: {e}")
            return False


def show_success() -> None:
    """Display success message with next steps."""
    text = Text()
    text.append("✓ ", style=SUCCESS_GREEN)
    text.append("You're all set!\n\n", style="bold white")
    text.append("Try these commands:\n\n", style="dim")
    text.append("  sibyl search ", style=NEON_CYAN)
    text.append('"patterns"', style="white")
    text.append("   Search knowledge\n", style="dim")
    text.append("  sibyl task list", style=NEON_CYAN)
    text.append("          View tasks\n", style="dim")
    text.append("  sibyl add ", style=NEON_CYAN)
    text.append('"Title" "..."', style="white")
    text.append("   Capture knowledge\n", style="dim")

    console.print()
    console.print(
        Panel(
            text,
            border_style=SUCCESS_GREEN,
            padding=(1, 4),
        )
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
