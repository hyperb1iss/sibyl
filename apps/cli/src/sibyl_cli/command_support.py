"""Shared parsing and error handling for CLI command modules."""

from __future__ import annotations

import typer

from sibyl_cli.client import SibylClientError
from sibyl_cli.common import CORAL, NEON_CYAN, console
from sibyl_cli.common import handle_client_error as render_client_error


def parse_csv_ids(value: str | None) -> list[str]:
    """Parse a comma-separated identifier option."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def append_unique_ids(existing: list[str], additions: list[str]) -> list[str]:
    """Append identifiers while preserving their first-seen order."""
    seen = set(existing)
    combined = list(existing)
    for item in additions:
        if item not in seen:
            combined.append(item)
            seen.add(item)
    return combined


def parse_id_args(values: list[str]) -> list[str]:
    """Parse positional identifier arguments that may each contain CSV values."""
    ids: list[str] = []
    for value in values:
        ids = append_unique_ids(ids, parse_csv_ids(value))
    return ids


def handle_client_error(error: SibylClientError) -> None:
    """Render a client error consistently, then exit with failure."""
    if error.error_code or error.request_id or error.remediation:
        render_client_error(error)
    elif "Cannot connect" in str(error):
        console.print()
        console.print(f"  [{CORAL}]×[/{CORAL}] [bold]Cannot connect to Sibyl server[/bold]")
        console.print()
        console.print(f"    [{NEON_CYAN}]›[/{NEON_CYAN}] Check that the Sibyl server is running")
    elif error.status_code == 401:
        console.print()
        console.print(f"  [{CORAL}]×[/{CORAL}] [bold]Authentication required[/bold]")
        console.print()
        console.print(
            f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl auth login[/bold {NEON_CYAN}]   [dim]Log in[/dim]"
        )
        console.print(
            f"    [{NEON_CYAN}]›[/{NEON_CYAN}] [bold {NEON_CYAN}]sibyl auth local-signup[/bold {NEON_CYAN}]  [dim]Create a local account[/dim]"
        )
        console.print()
    elif error.status_code == 403:
        console.print()
        console.print(f"  [{CORAL}]×[/{CORAL}] [bold]Access denied[/bold]")
        if error.detail:
            console.print()
            console.print(f"    [{NEON_CYAN}]›[/{NEON_CYAN}] {error.detail}")
        console.print()
    else:
        render_client_error(error)
    raise typer.Exit(1)
