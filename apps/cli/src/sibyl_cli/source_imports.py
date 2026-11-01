"""Drain and report a server-side source import, for every command that starts one."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import typer

from sibyl_cli.common import error, info, print_json, success
from sibyl_cli.memory_views import print_source_import_status

TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})
_FAILED_STATUSES = frozenset({"failed", "canceled"})


async def wait_for_import(
    client: Any,
    import_id: str,
    *,
    poll_interval: float,
    timeout: float | None,
) -> dict[str, object]:
    started_at = time.monotonic()
    while True:
        data = await client.source_import_status(import_id)
        if str(data.get("status") or "") in TERMINAL_STATUSES:
            return data
        if timeout is not None and time.monotonic() - started_at >= timeout:
            return data
        await asyncio.sleep(poll_interval)


async def finish_import(
    client: Any,
    data: dict[str, object],
    *,
    noun: str,
    title: str,
    default_scope: str,
    drain: bool,
    poll_interval: float,
    timeout: float | None,
    json_out: bool,
) -> None:
    """Optionally wait for the import to drain, then report it; a failure exits 1.

    ``noun`` names the import in status lines ("Import", "Document import").
    """
    if drain and (import_id := str(data.get("import_id") or "")):
        if not json_out:
            info(f"Waiting for {noun.lower()} drain {import_id}")
        data = await wait_for_import(
            client, import_id, poll_interval=poll_interval, timeout=timeout
        )
    status = str(data.get("status") or "")
    if json_out:
        print_json(data)
        if status in _FAILED_STATUSES:
            raise typer.Exit(1)
        return

    if status == "completed":
        success(f"{noun} completed")
    elif status in _FAILED_STATUSES:
        print_source_import_status(data, title=title, default_scope=default_scope)
        error(f"{noun} {status}")
        raise typer.Exit(1)
    elif drain and status not in TERMINAL_STATUSES:
        info(f"{noun} drain is still running")
    else:
        success(f"{noun} queued")
    print_source_import_status(data, title=title, default_scope=default_scope)
