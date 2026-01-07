"""Document CLI commands.

Commands for viewing crawled documents and their chunks.
"""

from typing import Annotated, Any

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_panel,
    error,
    info,
    print_json,
    run_async,
    truncate,
)

app = typer.Typer(
    name="document",
    help="View crawled documents",
    no_args_is_help=True,
)


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1."""
    if "Cannot connect" in str(e):
        error(str(e))
    elif e.status_code == 404:
        error(f"Document not found: {e.detail}")
    elif e.status_code == 400:
        error(f"Invalid request: {e.detail}")
    else:
        error(str(e))
    raise typer.Exit(1)


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
        sibyl document show 22d4cf79-8561-4be0-8067-da8673e3439d
    """

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            doc = await client.get_crawl_document(document_id)

            if json_out:
                print_json(doc)
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

            panel = create_panel("\n".join(lines), title="Document")
            console.print(panel)

            if url:
                console.print(f"\n[dim]Open in browser:[/dim] [{NEON_CYAN}]{url}[/{NEON_CYAN}]")

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

            if json_out:
                print_json(docs)
                return

            if not docs:
                info("No documents found")
                return

            from sibyl_cli.common import create_table

            table = create_table("Documents", "ID", "Title", "URL", "Chunks")
            for doc in docs:
                table.add_row(
                    truncate(doc.get("id", ""), 36),
                    truncate(doc.get("title", ""), 30),
                    truncate(doc.get("url", ""), 40),
                    str(len(doc.get("chunks", []))),
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(docs)} document(s)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _list()
