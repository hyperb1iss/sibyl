"""Pending write buffer CLI commands."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Annotated, Any

import typer

from sibyl_cli.auth_store import (
    credential_scope,
    normalize_api_url,
    read_server_credentials,
)
from sibyl_cli.client import (
    SibylClient,
    SibylClientError,
    _auth_replay_scope,
    _is_read_like_post,
)
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
from sibyl_cli.pending_identity import normalize_replay_identity, pending_identity_matches
from sibyl_cli.pending_writes import (
    claim_pending_write_replay_scope,
    delete_pending_write,
    increment_attempts,
    is_canonical_pending_write_id,
    is_corrupt_pending_write,
    list_pending_writes,
    pending_replay_lock,
    pending_write_label,
    pending_write_resource,
    pending_writes_dir,
    read_pending_write,
    record_pending_metric,
    retry_pending_write,
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
        "status": item.get("status", "pending"),
        "ownership": "verified" if item.get("replay_identity") else "legacy",
        "last_failure": item.get("last_failure"),
    }


def _state_label(item: dict[str, Any]) -> str:
    failure = item.get("last_failure")
    reason = failure.get("category") if isinstance(failure, dict) else None
    status_code = failure.get("status_code") if isinstance(failure, dict) else None
    state = str(item.get("status", "pending"))
    if item.get("ownership") == "legacy":
        state += " / legacy ownership"
    if reason:
        state += f" / {reason}"
    if status_code:
        state += f" ({status_code})"
    return state


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


def _context_name_for_base_url(
    base_url: str,
    replay_scope: object = None,
) -> str | None:
    from sibyl_cli import config_store

    if isinstance(replay_scope, str) and replay_scope:
        for ctx in config_store.list_contexts():
            context_url = normalize_api_url(f"{ctx.server_url}/api")
            context_scope = credential_scope(ctx.name, ctx.org_slug)
            credentials = read_server_credentials(
                context_url,
                credential_scope=context_scope,
            )
            context_token = str(credentials.get("access_token") or "").strip() or None
            stored_replay_scope = str(credentials.get("pending_replay_scope") or "").strip() or None
            context_replay_scope = _auth_replay_scope(stored_replay_scope, context_token)
            if context_url == normalize_api_url(base_url) and context_replay_scope == replay_scope:
                return ctx.name
        return None

    ctx = config_store.get_active_context()
    if ctx is None:
        return None
    if normalize_api_url(base_url) == normalize_api_url(f"{ctx.server_url}/api"):
        return ctx.name
    return None


def _legacy_claim_candidate(item: dict[str, Any]) -> bool:
    scope = item.get("replay_scope")
    return scope is None or str(scope).startswith("context:")


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
    table.add_column("State / reason")
    for item in summaries:
        if item.get("status") == "corrupt":
            table.add_row(
                str(item["id"])[:12],
                "CORRUPT",
                str(item["filename"]),
                "repair",
                str(item["error"]),
                "-",
                "corrupt",
            )
            continue
        table.add_row(
            str(item["id"])[:12],
            str(item["method"]),
            str(item["path"]),
            str(item["kind"]),
            str(item["title"]),
            str(item["attempts"]),
            _state_label(item),
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


@app.command("claim")
def claim_writes(
    write_ids: Annotated[
        list[str] | None,
        typer.Argument(help="Pending write IDs or prefixes. Omit to claim all legacy writes."),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Approve the displayed user and organization."),
    ] = False,
    unverified: Annotated[
        bool,
        typer.Option(
            "--unverified", help="Adopt explicitly named legacy writes from an older login."
        ),
    ] = False,
) -> None:
    """Claim ambiguous legacy writes, then retry them automatically."""
    mark_pending_writes_reported()
    if unverified and not write_ids:
        error("Use explicit write IDs when adopting writes with unknown historical ownership.")
        raise typer.Exit(code=1)
    try:
        selected = _selected_writes(write_ids or [])
    except (FileNotFoundError, ValueError) as exc:
        error(str(exc))
        raise typer.Exit(code=1) from exc
    candidates = [
        item
        for item in selected
        if not is_corrupt_pending_write(item)
        and not _is_buffered_read_like(item)
        and item.get("replay_identity") is None
        and (_legacy_claim_candidate(item) or unverified)
    ]
    if not candidates:
        success("No ambiguous legacy writes matched")
        return

    from sibyl_cli import config_store

    context_name = config_store.resolve_context_name()

    @run_async
    async def run_claim() -> None:
        async with SibylClient(context_name=context_name) as client:
            if client._replay_scope is None:
                error("No authenticated credential is available for this context.")
                raise typer.Exit(code=1)
            matching = [
                item
                for item in candidates
                if normalize_api_url(str(item["base_url"])) == client.base_url
            ]
            if len(matching) != len(candidates):
                error("Some selected writes belong to another server; select its context first.")
                raise typer.Exit(code=1)

            try:
                replay_identity = await client._ensure_pending_identity()
                identity = await client._request("GET", "/auth/me", _replay_pending=False)
            except SibylClientError as exc:
                error(exc.detail or str(exc))
                raise typer.Exit(code=1) from exc
            if replay_identity is None:
                error("Upgrade the server to verify write ownership before claiming legacy writes.")
                raise typer.Exit(code=1)
            user = identity.get("user") if isinstance(identity.get("user"), dict) else {}
            organization = (
                identity.get("organization")
                if isinstance(identity.get("organization"), dict)
                else {}
            )
            user_label = str(user.get("email") or user.get("name") or user.get("id") or "unknown")
            org_label = str(
                organization.get("slug")
                or organization.get("name")
                or organization.get("id")
                or "unknown"
            )
            warn(
                f"Claim {len(matching)} legacy write"
                f"{'s' if len(matching) != 1 else ''} for {user_label} in {org_label}."
            )
            if unverified:
                warn(
                    "Original ownership cannot be verified. Proceed only if these writes belong "
                    "to this account and organization."
                )
            for item in matching:
                warn(
                    f"{str(item['id'])[:12]} {item['method']} {item['path']} at {item['base_url']}"
                )
            if not yes and not typer.confirm("Continue?"):
                raise typer.Abort()

            with pending_replay_lock() as acquired:
                if not acquired:
                    error("Another pending-write replay is already running; claim did not run.")
                    raise typer.Exit(code=1)
                claim_failures: list[tuple[str, str]] = []
                claimed = 0
                for item in matching:
                    write_id = str(item["id"])
                    try:
                        claim_pending_write_replay_scope(
                            write_id,
                            client._replay_scope,
                            replay_identity=replay_identity,
                            adopt_unverified=unverified,
                        )
                    except (FileNotFoundError, ValueError) as exc:
                        claim_failures.append((write_id, str(exc)))
                    else:
                        claimed += 1
            for write_id, reason in claim_failures:
                error(f"Could not claim {write_id[:12]}: {reason}")
            if claimed == 0:
                raise typer.Exit(code=1)
            claimed_message = (
                f"Claimed {claimed} pending write{'s' if claimed != 1 else ''}; retrying now."
            )
            if claim_failures:
                warn(claimed_message)
            else:
                success(claimed_message)
            while True:
                before = sum(
                    1
                    for item in list_pending_writes()
                    if item.get("replay_scope") == client._replay_scope
                    and str(item.get("base_url")) == client.base_url
                )
                if before == 0:
                    break
                await client._maybe_replay_pending_writes(ignore_backoff=True)
                after = sum(
                    1
                    for item in list_pending_writes()
                    if item.get("replay_scope") == client._replay_scope
                    and str(item.get("base_url")) == client.base_url
                )
                if after >= before:
                    break
            if claim_failures:
                raise typer.Exit(code=1)

    run_claim()


@app.command("retry")
def retry_writes(
    write_ids: Annotated[list[str], typer.Argument(help="Exact write IDs or prefixes to retry")],
) -> None:
    """Re-enable selected writes after resolving their reported failure."""
    with pending_replay_lock() as acquired:
        if not acquired:
            error("Another pending-write replay is already running.")
            raise typer.Exit(code=1)
        for write_id in write_ids:
            try:
                retry_pending_write(write_id)
            except (FileNotFoundError, ValueError) as exc:
                error(str(exc))
                raise typer.Exit(code=1) from exc
    flush_writes(write_ids)


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
        from sibyl_cli import config_store

        failures = len(corrupt)
        replayed = 0
        blocked: dict[str, set[str]] = {}
        selected_ids = {str(item["id"]) for item in replayable}
        all_writes = [item for item in list_pending_writes() if not is_corrupt_pending_write(item)]
        async with AsyncExitStack() as stack:
            clients: dict[tuple[str, str | None], SibylClient] = {}
            for item in sorted(
                all_writes, key=lambda row: (str(row.get("created_at", "")), str(row["id"]))
            ):
                write_id = str(item["id"])
                base_url = normalize_api_url(str(item["base_url"]))
                replay_scope = item.get("replay_scope")
                owner = normalize_replay_identity(item.get("replay_identity"))
                lane = json.dumps([base_url, owner or replay_scope], sort_keys=True)
                blocked_resources = blocked.setdefault(lane, set())
                resource = pending_write_resource(item)
                if write_id not in selected_ids:
                    if not _is_buffered_read_like(item):
                        blocked_resources.add(resource)
                    continue
                if (
                    "*" in blocked_resources
                    or resource in blocked_resources
                    or (resource == "*" and blocked_resources)
                    or item.get("status") == "attention"
                ):
                    blocked_resources.add(resource)
                    failures += 1
                    warn(f"Skipped {write_id[:12]}: needs attention or an earlier related write.")
                    continue
                names = [_context_name_for_base_url(base_url, replay_scope)]
                if owner:
                    names.extend(
                        ctx.name
                        for ctx in config_store.list_contexts()
                        if normalize_api_url(f"{ctx.server_url}/api") == base_url
                    )
                selected_client = None
                for context_name in dict.fromkeys(names):
                    key = (base_url, context_name)
                    client = clients.get(key)
                    if client is None:
                        client = await stack.enter_async_context(
                            SibylClient(base_url=base_url, context_name=context_name)
                        )
                        clients[key] = client
                    try:
                        identity = await client._ensure_pending_identity()
                    except SibylClientError:
                        continue
                    if pending_identity_matches(item, identity, client._replay_scope):
                        selected_client = client
                        break
                if selected_client is None:
                    failures += 1
                    blocked_resources.add(resource)
                    error(f"Skipped {write_id[:12]}: no verified matching owner is signed in.")
                    continue
                try:
                    current = increment_attempts(write_id)
                    await selected_client._request(
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
                except FileNotFoundError:
                    continue
                except SibylClientError as exc:
                    failures += 1
                    blocked_resources.add(resource)
                    error(f"Failed {write_id[:12]}: {exc.detail or exc}")
            if failures:
                warn(
                    f"{replayed} replayed, {failures} unresolved; payloads remain local. "
                    "Inspect pending-writes list for ownership and failure reasons."
                )
                raise typer.Exit(code=1)

    with pending_replay_lock() as acquired:
        if not acquired:
            warn("Another pending-write replay is already running.")
            return
        run_flush()
