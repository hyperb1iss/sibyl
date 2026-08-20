"""Pending write buffer CLI commands."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Annotated, Any

import typer

from sibyl_cli.auth_store import normalize_api_url
from sibyl_cli.client import SibylClient, SibylClientError, _is_read_like_post
from sibyl_cli.common import (
    console,
    create_table,
    error,
    mark_pending_writes_reported,
    print_json,
    run_async,
    success,
    warn,
)
from sibyl_cli.pending_writes import (
    delete_pending_write,
    increment_attempts,
    is_canonical_pending_write_id,
    is_corrupt_pending_write,
    list_pending_writes,
    pending_write_label,
    pending_writes_dir,
    read_pending_write,
    record_pending_metric,
)

app = typer.Typer(help="Inspect and replay locally buffered writes")


def _summary(item: dict[str, Any]) -> dict[str, Any]:
    if is_corrupt_pending_write(item):
        return {
            "id": item["id"],
            "status": "corrupt",
            "filename": item["filename"],
            "error": item["error"],
        }
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


def _is_buffered_read_like(item: dict[str, Any]) -> bool:
    return str(item.get("method") or "").upper() == "POST" and _is_read_like_post(
        str(item.get("path") or "")
    )


def _partition_replayable(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    replayable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for item in items:
        if _is_buffered_read_like(item):
            skipped.append(item)
        else:
            replayable.append(item)
    return replayable, skipped


def _context_name_for_base_url(base_url: str) -> str | None:
    from sibyl_cli import config_store

    ctx = config_store.get_active_context()
    if ctx is None:
        return None
    if normalize_api_url(base_url) == normalize_api_url(f"{ctx.server_url}/api"):
        return ctx.name
    return None


def _should_abort_flush(exc: SibylClientError) -> bool:
    return exc.error_code == "token_refresh_failed" or exc.status_code in {401, 429}


@app.command("list")
def list_writes(
    json_output: Annotated[bool, typer.Option("--json", "-j", help="Output JSON")] = False,
) -> None:
    """List buffered writes without printing sensitive payload bodies."""
    mark_pending_writes_reported()
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
        if item.get("status") == "corrupt":
            table.add_row(
                str(item["id"])[:12],
                "CORRUPT",
                str(item["filename"]),
                "repair",
                str(item["error"]),
                "-",
            )
            continue
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
    write_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Pending write IDs or prefixes"),
    ] = None,
    read_like: Annotated[
        bool,
        typer.Option(
            "--read-like",
            help="Discard buffered read-like requests from older CLI versions.",
        ),
    ] = False,
) -> None:
    """Discard buffered writes without replaying them."""
    if read_like:
        selected = [
            str(item["id"]) for item in list_pending_writes() if _is_buffered_read_like(item)
        ]
    else:
        selected = write_ids or []
    if not selected:
        success("No pending writes matched")
        return
    # Deduplicated, since the same id named twice discards once and the second
    # pass would otherwise count as a miss.
    selected = list(dict.fromkeys(selected))
    removed = 0
    missed = 0
    for write_id in selected:
        try:
            if delete_pending_write(write_id):
                removed += 1
                record_pending_metric("discarded")
            else:
                missed += 1
        except ValueError as exc:
            error(str(exc))
            raise typer.Exit(code=1) from exc

    success(f"Discarded {removed} pending write{'s' if removed != 1 else ''}")
    # A named id that matched nothing is a refusal, not an empty result: the
    # caller asked for a specific write and the queue still holds it.
    if not read_like and missed:
        error(f"No pending write matched {missed} of the given IDs")
        raise typer.Exit(code=1)


@app.command("flush")
def flush_writes(
    write_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Pending write IDs or prefixes. Omit to flush all."),
    ] = None,
) -> None:
    """Replay buffered writes."""
    mark_pending_writes_reported()
    try:
        selected = _selected_writes(write_ids or [])
    except (FileNotFoundError, ValueError) as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc
    if not selected:
        success("No pending writes")
        return
    corrupt = [item for item in selected if is_corrupt_pending_write(item)]
    valid = [item for item in selected if not is_corrupt_pending_write(item)]
    for item in corrupt:
        error(f"Cannot replay {item['filename']}: {item['error']}")
        path = pending_writes_dir() / str(item["filename"])
        if is_canonical_pending_write_id(str(item["id"])):
            warn(
                f"Repair {path}, or discard it explicitly with: "
                f"sibyl pending-writes discard {item['id']}"
            )
        else:
            warn(f"Inspect and remove this exact file manually: {path}")

    replayable, skipped = _partition_replayable(valid)
    if skipped:
        warn(
            f"Skipped {len(skipped)} read-like pending request"
            f"{'s' if len(skipped) != 1 else ''}; rerun those commands instead."
        )
        warn("To drop them from the queue: sibyl pending-writes discard --read-like")
    if not replayable:
        if corrupt:
            raise typer.Exit(code=1)
        success("No replayable pending writes")
        return

    @run_async
    async def run_flush() -> None:
        failures = len(corrupt)
        replay_failures = 0
        replayed = 0
        contended = 0
        async with AsyncExitStack() as stack:
            clients: dict[tuple[str, str | None], SibylClient] = {}
            for item in replayable:
                write_id = str(item["id"])
                current = increment_attempts(write_id)
                base_url = str(current["base_url"])
                context_name = _context_name_for_base_url(base_url)
                client_key = (normalize_api_url(base_url), context_name)
                client = clients.get(client_key)
                if client is None:
                    client = await stack.enter_async_context(
                        SibylClient(base_url=base_url, context_name=context_name)
                    )
                    clients[client_key] = client
                try:
                    await client._request(
                        str(current["method"]),
                        str(current["path"]),
                        json=current.get("json"),
                        params=current.get("params"),
                        _buffer_pending=False,
                        _pending_write_id=write_id,
                        _idempotency_key=str(current["idempotency_key"]),
                    )
                    record_pending_metric("replayed")
                    replayed += 1
                    success(f"Flushed {write_id[:12]}")
                except SibylClientError as exc:
                    failures += 1
                    replay_failures += 1
                    error(f"Failed {write_id[:12]}: {exc.detail or exc}")
                    if exc.status_code == 409:
                        contended += 1
                    if _should_abort_flush(exc):
                        error("Stopping flush; remaining writes are still buffered.")
                        break
        if failures:
            warn(f"{replayed} replayed, {failures} failed; all failed entries stay buffered.")
            if replay_failures:
                warn(
                    f"{replay_failures} replay failure"
                    f"{'s are' if replay_failures != 1 else ' is'} safe to flush again."
                )
            if corrupt:
                warn(
                    f"{len(corrupt)} corrupt queue entr"
                    f"{'ies' if len(corrupt) != 1 else 'y'} cannot replay until repaired "
                    "or explicitly discarded."
                )
            if contended:
                warn(
                    f"{contended} hit an identical request still executing on the "
                    "server; those clear on their own, flush again shortly."
                )
            raise typer.Exit(code=1)

    run_flush()
