"""Canonical registration for root-level memory and recall commands."""

import typer

from sibyl_cli import capture, memory, recall


def register_commands(root: typer.Typer) -> None:
    """Register root commands in their established public order."""
    root.command("graph-search", hidden=True)(recall.search)
    root.command("graph-add", hidden=True)(capture.add_knowledge)
    root.command("capture")(capture.capture_memory)
    root.command("note")(capture.note_alias)
    root.command("brief")(recall.brief_context)
    root.command("context")(recall.recall_context)
    root.command("recall", hidden=True)(recall.recall_context)
    root.command("search", hidden=True)(recall.recall_context)
    root.command("remember")(memory.remember_memory)
    root.command("add", hidden=True)(memory.remember_memory)
    root.command("reflect")(memory.reflect_memory)
