"""Shared CLI utilities - colors, console, helpers.

Sibyl Design Language for consistent terminal output.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from sibyl_core.logging.colors import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    ERROR_RED,
    NEON_CYAN,
    SUCCESS_GREEN,
)

if TYPE_CHECKING:
    from sibyl_cli.client import SibylClientError

# Shared console instance (for styled output only, NOT for JSON)
console = Console()


def _strip_embeddings(obj: object) -> object:
    """Recursively strip embedding arrays from data structures."""
    if isinstance(obj, dict):
        return {k: _strip_embeddings(v) for k, v in obj.items() if k != "embedding"}
    if isinstance(obj, list):
        return [_strip_embeddings(item) for item in obj]
    return obj


def print_json(data: object) -> None:
    """Print JSON to stdout without Rich formatting.

    IMPORTANT: Never use console.print() for JSON output - Rich wraps
    long lines at terminal width, inserting literal newlines that break
    JSON parsing.

    Also strips embedding arrays which are useless in CLI output and bloat
    the response (1536 floats per entity).
    """
    import json

    clean_data = _strip_embeddings(data)
    print(json.dumps(clean_data, indent=2, default=str, ensure_ascii=False))


def pagination_hint(
    offset: int, count: int, total: int, has_more: bool, limit: int, entity_type: str = "result"
) -> None:
    """Print pagination info to stderr (doesn't break JSON output).

    Shows something like:
        Showing 1-50 of 81 results (--page 2 for more)
    """
    import sys

    start = offset + 1
    end = offset + count
    plural = "s" if count != 1 else ""

    if has_more:
        next_page = (offset // limit) + 2
        msg = (
            f"Showing {start}-{end} of {total}+ {entity_type}{plural} (--page {next_page} for more)"
        )
    else:
        msg = f"Showing {count} {entity_type}{plural}"

    print(msg, file=sys.stderr)


def styled_header(text: str) -> Text:
    """Create a styled header with SilkCircuit colors."""
    return Text(text, style=f"bold {NEON_CYAN}")


def success(message: str) -> None:
    """Print a success message."""
    console.print(f"[{SUCCESS_GREEN}]✓[/{SUCCESS_GREEN}] {message}")


def error(message: str) -> None:
    """Print an error message."""
    console.print(f"[{ERROR_RED}]✗[/{ERROR_RED}] {message}")


def warn(message: str) -> None:
    """Print a warning message."""
    console.print(f"[{ELECTRIC_YELLOW}]![/{ELECTRIC_YELLOW}] {message}")


def info(message: str) -> None:
    """Print an info message."""
    console.print(f"[{NEON_CYAN}]→[/{NEON_CYAN}] {message}")


def hint(message: str) -> None:
    """Print a hint message."""
    console.print(f"[{ELECTRIC_YELLOW}]Hint:[/{ELECTRIC_YELLOW}] {message}")


def print_db_hint() -> None:
    """Print the common FalkorDB hint."""
    hint("Is FalkorDB running?")
    console.print(f"  [{NEON_CYAN}]docker compose up -d[/{NEON_CYAN}]")


def create_table(title: str | None = None, *columns: str, expand: bool = True) -> Table:
    """Create a styled table with SilkCircuit colors.

    Uses SIMPLE_HEAD box style - just a header underline, no heavy frames.
    Set expand=True (default) to use full terminal width.
    """
    table = Table(title=title, box=box.SIMPLE_HEAD, header_style=f"bold {NEON_CYAN}", expand=expand)
    for i, col in enumerate(columns):
        style = ELECTRIC_PURPLE if i == 0 else None
        justify = (
            "left" if i == 0 else "right" if col.lower() in ("count", "score", "value") else "left"
        )
        table.add_column(col, style=style, justify=justify)
    return table


def create_panel(content: str, title: str | None = None, subtitle: str | None = None) -> Panel:
    """Create a styled panel with SilkCircuit colors."""
    return Panel(
        content,
        title=f"[{ELECTRIC_PURPLE}]{title}[/{ELECTRIC_PURPLE}]" if title else None,
        subtitle=subtitle,
        border_style=NEON_CYAN,
    )


def create_tree(label: str) -> Tree:
    """Create a styled tree with SilkCircuit colors."""
    return Tree(f"[{ELECTRIC_PURPLE}]{label}[/{ELECTRIC_PURPLE}]")


def spinner(_description: str = "") -> Progress:
    """Create a spinner progress indicator.

    Args:
        _description: Unused - callers add their own task descriptions.
    """
    return Progress(
        SpinnerColumn(style=NEON_CYAN),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    )


def run_async[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, R]:
    """Decorator to run async functions in sync context (for Typer commands)."""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        async def coro() -> R:
            return await func(*args, **kwargs)

        return asyncio.run(coro())

    return wrapper


def format_status(status: str) -> str:
    """Format a task status with appropriate color."""
    status_colors = {
        "backlog": "dim",
        "todo": NEON_CYAN,
        "doing": ELECTRIC_PURPLE,
        "blocked": ERROR_RED,
        "review": ELECTRIC_YELLOW,
        "done": SUCCESS_GREEN,
        "archived": "dim",
    }
    color = status_colors.get(status.lower(), NEON_CYAN)
    return f"[{color}]{status}[/{color}]"


def format_priority(priority: str) -> str:
    """Format a task priority with appropriate color."""
    priority_colors = {
        "critical": ERROR_RED,
        "high": CORAL,
        "medium": ELECTRIC_YELLOW,
        "low": NEON_CYAN,
        "someday": "dim",
    }
    color = priority_colors.get(priority.lower(), NEON_CYAN)
    return f"[{color}]{priority}[/{color}]"


def truncate(text: str, max_length: int = 50) -> str:
    """Truncate text with ellipsis if too long."""
    if len(text) <= max_length:
        return text
    return text[: max_length - 3] + "..."


def handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1.

    This is the centralized error handler for all CLI commands.
    Import and use: `from sibyl_cli.common import handle_client_error`
    """
    if "Cannot connect" in str(e):
        error(str(e))
    elif e.status_code == 404:
        error(f"Not found: {e.detail}")
    elif e.status_code == 400:
        error(f"Invalid request: {e.detail}")
    else:
        error(str(e))
    raise typer.Exit(1)
