"""Shared CLI utilities - colors, console, helpers.

SilkCircuit Design Language for consistent terminal output.
"""

import asyncio
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

# SilkCircuit color palette
ELECTRIC_PURPLE = "#e135ff"
NEON_CYAN = "#80ffea"
CORAL = "#ff6ac1"
ELECTRIC_YELLOW = "#f1fa8c"
SUCCESS_GREEN = "#50fa7b"
ERROR_RED = "#ff6363"

# Shared console instance
console = Console()

# Type vars for async decorator
P = ParamSpec("P")
R = TypeVar("R")


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


def create_table(title: str | None = None, *columns: str) -> Table:
    """Create a styled table with SilkCircuit colors."""
    table = Table(title=title, border_style=NEON_CYAN)
    for i, col in enumerate(columns):
        style = ELECTRIC_PURPLE if i == 0 else NEON_CYAN
        justify = "left" if i == 0 else "right" if col.lower() in ("count", "score", "value") else "left"
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


def run_async(func: Callable[P, Awaitable[R]]) -> Callable[P, R]:
    """Decorator to run async functions in sync context (for Typer commands)."""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return asyncio.run(func(*args, **kwargs))

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
