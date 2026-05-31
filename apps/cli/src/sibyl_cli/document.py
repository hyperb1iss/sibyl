"""Document CLI commands.

Commands for viewing crawled documents and their chunks.
"""

from __future__ import annotations

import asyncio
import time
from functools import partial
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_table,
    error,
    handle_client_error,
    info,
    print_json,
    resolve_content_input,
    run_async,
    success,
    truncate,
)
from sibyl_cli.config_store import resolve_project_from_cwd
from sibyl_cli.project_refs import PROJECT_RELINK_HINT, resolve_project_reference
from sibyl_cli.view_shared import maybe_print_json, render_detail_panel, render_table_or_empty

app = typer.Typer(
    name="documents",
    help="Browse crawled documents",
    no_args_is_help=True,
)
docs_app = typer.Typer(
    name="docs",
    help="Import and list document collections",
    no_args_is_help=True,
)

_handle_client_error = partial(handle_client_error, not_found_label="Document not found")
_TERMINAL_STATUSES = {"completed", "failed", "canceled"}


def _resolve_document_path(source: Path) -> Path:
    path = source.expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    for component in (path, *path.parents):
        if component.exists() and component.is_symlink():
            error(f"Document source cannot include symlinks: {component}")
            raise typer.Exit(1)
    path = path.resolve()
    if not path.exists():
        error(f"Document source not found: {path}")
        raise typer.Exit(1)
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink():
                error(f"Document source cannot include symlinks: {child}")
                raise typer.Exit(1)
    return path


def _document_kind_and_source(source: str, *, recursive: bool) -> tuple[str, str]:
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return "url", source
    path = _resolve_document_path(Path(source))
    if path.is_dir():
        if not recursive:
            error("Directory document imports require --recursive")
            raise typer.Exit(1)
        return "folder", str(path)
    if path.is_file():
        return "file", str(path)
    error(f"Document source is not a file, directory, or URL: {path}")
    raise typer.Exit(1)


async def _resolve_target_project(client: Any, project: str | None) -> str:
    if project:
        try:
            return await resolve_project_reference(client, project)
        except ValueError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc
    linked_project = resolve_project_from_cwd()
    if linked_project:
        return linked_project
    error(f"No linked project found. {PROJECT_RELINK_HINT} or pass --project.")
    raise typer.Exit(1)


def _progress(data: dict[str, object]) -> dict[str, object]:
    progress = data.get("progress")
    if not isinstance(progress, dict):
        return {}
    return {str(key): value for key, value in progress.items()}


def _scope_label(data: dict[str, object]) -> str:
    scope = str(data.get("target_memory_scope") or "project")
    if scope_key := data.get("target_scope_key"):
        return f"{scope}:{scope_key}"
    return scope


def _print_document_import_status(data: dict[str, object]) -> None:
    progress = _progress(data)
    table = create_table("Document Import", "Field", "Value", expand=False)
    table.add_row("Import Id", str(data.get("import_id") or ""))
    table.add_row("Status", str(data.get("status") or ""))
    table.add_row("Adapter", str(data.get("adapter_name") or ""))
    table.add_row("Source", str(data.get("source_identity") or ""))
    table.add_row("Target scope", _scope_label(data))
    table.add_row("Imported", str(progress.get("imported_count") or 0))
    table.add_row("Deduped", str(progress.get("dedupe_count") or 0))
    table.add_row("Skipped", str(progress.get("skipped_count") or 0))
    table.add_row("Errors", str(progress.get("error_count") or 0))
    console.print(table)

    raw_memory_ids = data.get("raw_memory_ids")
    if isinstance(raw_memory_ids, list) and raw_memory_ids:
        console.print("\n[bold]Raw memory receipts[/bold]")
        for raw_memory_id in raw_memory_ids[:18]:
            console.print(f"  [{CORAL}]{raw_memory_id}[/{CORAL}]")


async def _wait_for_import(
    client: Any,
    import_id: str,
    *,
    poll_interval: float,
    timeout: float | None,
) -> dict[str, object]:
    started_at = time.monotonic()
    while True:
        data = await client.ingestion_source_import_status(import_id)
        if str(data.get("status") or "") in _TERMINAL_STATUSES:
            return data
        if timeout is not None and time.monotonic() - started_at >= timeout:
            return data
        await asyncio.sleep(poll_interval)


async def _finish_document_import(
    client: Any,
    data: dict[str, object],
    *,
    drain: bool,
    poll_interval: float,
    timeout: float | None,
    json_out: bool,
) -> None:
    if drain:
        import_id = str(data.get("import_id") or "")
        if import_id:
            if not json_out:
                info(f"Waiting for document import drain {import_id}")
            data = await _wait_for_import(
                client,
                import_id,
                poll_interval=poll_interval,
                timeout=timeout,
            )
    status = str(data.get("status") or "")
    if json_out:
        print_json(data)
        if status in {"failed", "canceled"}:
            raise typer.Exit(1)
        return

    if status == "completed":
        success("Document import completed")
    elif status in {"failed", "canceled"}:
        _print_document_import_status(data)
        error(f"Document import {status}")
        raise typer.Exit(1)
    elif drain and status not in _TERMINAL_STATUSES:
        info("Document import drain is still running")
    else:
        success("Document import queued")
    _print_document_import_status(data)


@docs_app.command("add")
def add_document_source(
    source: Annotated[str, typer.Argument(help="Document file, directory, or URL")],
    recursive: Annotated[
        bool, typer.Option("--recursive", "-r", help="Import a directory recursively")
    ] = False,
    collection: Annotated[
        str | None, typer.Option("--collection", "-c", help="Collection label")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Target project id or name")
    ] = None,
    batch_size: Annotated[int, typer.Option("--batch-size", help="Import batch size")] = 100,
    drain: Annotated[bool, typer.Option("--drain", help="Wait for the background drain")] = False,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between drain status checks"),
    ] = 1.0,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Maximum seconds to wait when draining"),
    ] = None,
    allow_private_network: Annotated[
        bool,
        typer.Option(
            "--allow-private-network",
            help="Allow URL imports from private hosts (owners/admins only)",
        ),
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output for scripting")
    ] = False,
) -> None:
    """Import a document file, directory, or URL."""

    @run_async
    async def _run() -> None:
        kind, source_uri = _document_kind_and_source(source, recursive=recursive)
        try:
            async with get_client() as client:
                target_project = await _resolve_target_project(client, project)
                data = await client.start_document_import(
                    kind=kind,
                    source_uri=source_uri,
                    collection=collection,
                    target_scope_key=target_project,
                    batch_size=batch_size,
                    promotion_preview_approved=False,
                    allow_private_network=allow_private_network,
                )
                await _finish_document_import(
                    client,
                    data,
                    drain=drain,
                    poll_interval=poll_interval,
                    timeout=timeout,
                    json_out=json_out,
                )
        except SibylClientError as exc:
            handle_client_error(exc, invalid_request_label="Document import rejected")

    _run()


@docs_app.command("paste")
def paste_document(
    text: Annotated[str | None, typer.Argument(help="Document text, or '-' for stdin")] = None,
    content_file: Annotated[
        str | None, typer.Option("--file", "-f", help="Read document text from a file")
    ] = None,
    title: Annotated[str | None, typer.Option("--title", "-t", help="Document title")] = None,
    collection: Annotated[
        str | None, typer.Option("--collection", "-c", help="Collection label")
    ] = None,
    project: Annotated[
        str | None, typer.Option("--project", "-p", help="Target project id or name")
    ] = None,
    batch_size: Annotated[int, typer.Option("--batch-size", help="Import batch size")] = 100,
    drain: Annotated[bool, typer.Option("--drain", help="Wait for the background drain")] = False,
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between drain status checks"),
    ] = 1.0,
    timeout: Annotated[
        float | None,
        typer.Option("--timeout", help="Maximum seconds to wait when draining"),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output for scripting")
    ] = False,
) -> None:
    """Import pasted text as a document."""

    @run_async
    async def _run() -> None:
        try:
            content = resolve_content_input(text, content_file=content_file)
        except ValueError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc
        if not content or not content.strip():
            error("Document paste requires text, stdin, or --file")
            raise typer.Exit(1)
        try:
            async with get_client() as client:
                target_project = await _resolve_target_project(client, project)
                data = await client.start_document_import(
                    kind="text",
                    text=content,
                    title=title,
                    collection=collection,
                    target_scope_key=target_project,
                    batch_size=batch_size,
                    promotion_preview_approved=False,
                )
                await _finish_document_import(
                    client,
                    data,
                    drain=drain,
                    poll_interval=poll_interval,
                    timeout=timeout,
                    json_out=json_out,
                )
        except SibylClientError as exc:
            handle_client_error(exc, invalid_request_label="Document import rejected")

    _run()


@docs_app.command("list")
def list_document_collections(
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output for scripting")
    ] = False,
) -> None:
    """List imported document collections."""

    @run_async
    async def _list() -> None:
        try:
            async with get_client() as client:
                response = await client.list_document_collections()
            collections: list[dict[str, Any]] = response.get("collections", [])

            if maybe_print_json(collections, json_out=json_out):
                return

            rows = [
                (
                    truncate(item.get("name", ""), 36),
                    str(item.get("document_count", 0)),
                    str(item.get("updated_at") or ""),
                )
                for item in collections
            ]
            render_table_or_empty(
                title="Document Collections",
                columns=("Collection", "Documents", "Updated"),
                rows=rows,
                empty_message="No document collections found",
                empty_printer=info,
                footer=(
                    f"\n[dim]Showing {len(collections)} collection(s)[/dim]"
                    if collections
                    else None
                ),
            )
        except SibylClientError as exc:
            handle_client_error(exc, not_found_label="Document collections not found")

    _list()


@app.command("show")
def show_document(
    document_id: Annotated[str, typer.Argument(help="Document ID (from search results metadata)")],
    raw: Annotated[bool, typer.Option("--raw", "-r", help="Show raw markdown content")] = False,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show full document content.

    Use the document_id from search result metadata to fetch the complete document.

    Example:
        sibyl search "proto config"
        # Note the document_id in metadata
        sibyl crawl documents show 22d4cf79-8561-4be0-8067-da8673e3439d
    """

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            doc = await client.get_crawl_document(document_id)

            if maybe_print_json(doc, json_out=json_out):
                return

            if raw:
                # Just print raw content
                content = doc.get("raw_content") or doc.get("content", "")
                console.print(content)
                return

            # Rich formatted output
            title = doc.get("title", "Untitled")
            url = doc.get("url", "")
            source_name = doc.get("source_name", "")
            # Prefer markdown_content (assembled from chunks) over raw_content (HTML)
            content = doc.get("markdown_content") or doc.get("raw_content") or ""
            chunks = doc.get("chunks", [])

            lines = [
                f"[{ELECTRIC_PURPLE}]Title:[/{ELECTRIC_PURPLE}] {title}",
                f"[{ELECTRIC_PURPLE}]ID:[/{ELECTRIC_PURPLE}] {document_id}",
            ]

            if url:
                lines.append(f"[{ELECTRIC_PURPLE}]URL:[/{ELECTRIC_PURPLE}] {url}")
            if source_name:
                lines.append(f"[{ELECTRIC_PURPLE}]Source:[/{ELECTRIC_PURPLE}] {source_name}")

            lines.append(f"[{ELECTRIC_PURPLE}]Chunks:[/{ELECTRIC_PURPLE}] {len(chunks)}")
            lines.append("")

            if content:
                lines.append(f"[{NEON_CYAN}]Content:[/{NEON_CYAN}]")
                lines.append("")
                # Show content with reasonable limit
                if len(content) > 5000:
                    lines.append(content[:5000])
                    lines.append("")
                    lines.append(
                        f"[dim]... truncated ({len(content)} chars total, use --raw for full)[/dim]"
                    )
                else:
                    lines.append(content)
            else:
                lines.append("[dim]No content available[/dim]")

            render_detail_panel(
                title="Document",
                lines=lines,
                footer=(
                    f"\n[dim]Open in browser:[/dim] [{NEON_CYAN}]{url}[/{NEON_CYAN}]"
                    if url
                    else None
                ),
            )

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("list")
def list_documents(
    source_id: Annotated[
        str | None, typer.Option("--source", "-s", help="Filter by source ID")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """List crawled documents."""

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            response = await client.list_crawl_documents(source_id=source_id, limit=limit)
            docs: list[dict[str, Any]] = response.get("documents", [])

            if maybe_print_json(docs, json_out=json_out):
                return

            rows = [
                (
                    truncate(doc.get("id", ""), 36),
                    truncate(doc.get("title", ""), 30),
                    truncate(doc.get("url", ""), 40),
                    str(len(doc.get("chunks", []))),
                )
                for doc in docs
            ]
            render_table_or_empty(
                title="Documents",
                columns=("ID", "Title", "URL", "Chunks"),
                rows=rows,
                empty_message="No documents found",
                empty_printer=info,
                footer=f"\n[dim]Showing {len(docs)} document(s)[/dim]" if docs else None,
            )

        except SibylClientError as e:
            _handle_client_error(e)

    _list()
