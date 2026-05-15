"""Pending write buffer CLI commands."""

from __future__ import annotations

from typing import Annotated, Any

import typer

from sibyl_cli.client import SibylClient, SibylClientError
from sibyl_cli.common import console, create_table, error, print_json, run_async, success
from sibyl_cli.pending_writes import (
    delete_pending_write,
    increment_attempts,
    list_pending_writes,
    pending_write_label,
    read_pending_write,
)

app = typer.Typer(help="Inspect and replay locally buffered writes")


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    title, kind = pending_write_label(item)
    return {
        "id": item.get("id"),
        "created_at": item.get("created_at"),
        "method": item.get("method"),
        "path": item.get("path"),
        "title": title,
        "kind": kind,
        "attempts": item.get("attempts", 0),
        "base_url": item.get("base_url"),
    }


def _selected_writes(write_ids: list[str]) -> list[dict[str, Any]]:
    if not write_ids:
        return list_pending_writes()
    return [read_pending_write(write_id) for write_id in write_ids]


@app.command("list")
def list_writes(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output JSON")] = False,
) -> None:
    """List buffered writes without printing sensitive payload bodies."""
    summaries = [_summary(item) for item in list_pending_writes()]
    if json_output:
        print_json({"pending_writes": summaries})
        return
    if not summaries:
        success("No pending writes")
        return
    table = create_table("Pending Writes")
    table.add_column("ID", style="cyan")
    table.add_column("Method")
    table.add_column("Path")
    table.add_column("Kind")
    table.add_column("Title")
    table.add_column("Attempts", justify="right")
    for item in summaries:
        table.add_row(
            str(item["id"])[:12],
            str(item["method"]),
            str(item["path"]),
            str(item["kind"]),
            str(item["title"]),
            str(item["attempts"]),
        )
    console.print(table)


@app.command("discard")
def discard_writes(
    write_ids: Annotated[list[str], typer.Argument(help="Pending write IDs or prefixes")],
) -> None:
    """Discard buffered writes without replaying them."""
    removed = 0
    for write_id in write_ids:
        try:
            if delete_pending_write(write_id):
                removed += 1
        except ValueError as exc:
            error(str(exc))
            raise typer.Exit(code=1) from exc
    success(f"Discarded {removed} pending write{'s' if removed != 1 else ''}")


@app.command("flush")
def flush_writes(
    write_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Pending write IDs or prefixes. Omit to flush all."),
    ] = None,
) -> None:
    """Replay buffered writes."""
    try:
        selected = _selected_writes(write_ids or [])
    except (FileNotFoundError, ValueError) as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc
    if not selected:
        success("No pending writes")
        return

    @run_async
    async def run_flush() -> None:
        failures = 0
        for item in selected:
            write_id = str(item["id"])
            current = increment_attempts(write_id)
            try:
                async with SibylClient(base_url=str(current["base_url"])) as client:
                    await client._request(
                        str(current["method"]),
                        str(current["path"]),
                        json=current.get("json"),
                        params=current.get("params"),
                        _buffer_pending=False,
                        _pending_write_id=write_id,
                        _idempotency_key=str(current["idempotency_key"]),
                    )
                success(f"Flushed {write_id[:12]}")
            except SibylClientError as exc:
                failures += 1
                error(f"Failed {write_id[:12]}: {exc.detail or exc}")
        if failures:
            raise typer.Exit(code=1)

    run_flush()
