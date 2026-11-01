"""Canonical registration for root-level memory and recall commands."""

import typer

from sibyl_cli import memory, recall


def register_commands(root: typer.Typer) -> None:
    """Register root commands in their established public order."""
    root.command("brief")(recall.brief_context)
    root.command("context")(recall.recall_context)
    root.command("remember")(memory.remember_memory)
    root.command("reflect")(memory.reflect_memory)
