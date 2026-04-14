"""CLI config commands.

Commands for managing ~/.sibyl/config.toml settings.
"""

from typing import Annotated

import typer
from rich.table import Table

from sibyl_cli import config_store
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    error,
    info,
    success,
)
from sibyl_cli.onboarding import run_onboarding

app = typer.Typer(
    name="config",
    help="Manage CLI configuration",
    no_args_is_help=True,
)


@app.command("init")
def config_init(
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing config"),
    ] = False,
) -> None:
    """Setup wizard for first-time configuration."""
    if config_store.config_exists() and not force:
        error("Config already exists. Use --force to overwrite.")
        info("Use 'sibyl config show' to view current config.")
        raise typer.Exit(1)

    if run_onboarding():
        success("Configuration saved!")
    else:
        error("Setup was not completed.")
        raise typer.Exit(1)


@app.command("show")
def config_show() -> None:
    """Display current configuration."""
    config = config_store.load_config()

    table = Table(
        show_header=True,
        header_style=f"bold {NEON_CYAN}",
        border_style=NEON_CYAN,
        title=f"[{ELECTRIC_PURPLE}]Sibyl CLI Config[/{ELECTRIC_PURPLE}]",
        title_style="bold",
    )
    table.add_column("Setting", style=CORAL)
    table.add_column("Value", style="white")

    _sensitive_fragments = {"key", "secret", "password", "token"}

    def _is_sensitive(key: str) -> bool:
        lower = key.lower()
        return any(frag in lower for frag in _sensitive_fragments)

    # Flatten config for display
    def add_rows(d: dict, prefix: str = "") -> None:
        for key, value in d.items():
            full_key = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                add_rows(value, full_key)
            else:
                if not value:
                    display_value = "[dim]not set[/dim]"
                elif _is_sensitive(full_key) and isinstance(value, str) and len(value) > 8:
                    display_value = f"{value[:4]}{'•' * 8}{value[-4:]}"
                else:
                    display_value = str(value)
                table.add_row(full_key, display_value)

    add_rows(config)

    console.print()
    console.print(table)
    console.print()
    console.print(f"[dim]Config file: {config_store.config_path()}[/dim]")
    console.print()


@app.command("get")
def config_get(
    key: Annotated[str, typer.Argument(help="Config key (e.g., server.url)")],
) -> None:
    """Get a configuration value."""
    value = config_store.get(key)
    if value is None:
        error(f"Key '{key}' not found")
        raise typer.Exit(1)

    console.print(value)


@app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Config key (e.g., server.url)")],
    value: Annotated[str, typer.Argument(help="Value to set")],
) -> None:
    """Set a configuration value."""
    config_store.set_value(key, value)
    success(f"Set {key} = {value}")


@app.command("path")
def config_path() -> None:
    """Show config file path."""
    path = config_store.config_path()
    exists = "[green]exists[/green]" if path.exists() else "[dim]not created yet[/dim]"
    console.print(f"{path} ({exists})")


@app.command("reset")
def config_reset() -> None:
    """Reset configuration to defaults."""
    config_store.reset_config()
    success("Config reset to defaults.")


@app.command("edit")
def config_edit() -> None:
    """Open config file in default editor."""
    import os
    import subprocess

    path = config_store.config_path()

    # Ensure config exists
    if not path.exists():
        config_store.save_config(config_store.load_config())
        info(f"Created default config at {path}")

    # Get editor
    editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "nano"))

    try:
        subprocess.run([editor, str(path)], check=True)
        success(f"Config file saved: {path}")
    except FileNotFoundError:
        error(f"Editor '{editor}' not found. Set EDITOR environment variable.")
        raise typer.Exit(1) from None
    except subprocess.CalledProcessError:
        error("Editor exited with error.")
        raise typer.Exit(1) from None
