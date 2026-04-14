"""Web crawling and documentation ingestion CLI commands.

Commands for crawling documentation sites and managing the ingestion pipeline.
All commands communicate with the REST API.
"""

from typing import Annotated

import typer

from sibyl_cli.client import SibylClientError, get_client
from sibyl_cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_table,
    error,
    info,
    print_json,
    run_async,
    success,
    truncate,
)
from sibyl_cli.crawl_shared import add_crawl_source, show_source_status, start_crawl_source

app = typer.Typer(
    name="crawl",
    help="Web crawling and documentation ingestion",
    no_args_is_help=True,
)


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages and exit with code 1."""
    if "Cannot connect" in str(e):
        error(str(e))
    elif e.status_code == 404:
        error(f"Not found: {e.detail}")
    elif e.status_code == 400:
        error(f"Invalid request: {e.detail}")
    elif e.status_code == 409:
        error(f"Conflict: {e.detail}")
    else:
        error(str(e))
    raise typer.Exit(1)


@app.command("sources")
def list_sources(
    status: Annotated[str | None, typer.Option("--status", "-s", help="Filter by status")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """List all crawl sources. Default: table output."""

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            response = await client.list_crawl_sources(status=status, limit=limit)
            sources = response.get("sources", [])

            # JSON output (default)
            if json_out:
                print_json(response)
                return

            # Table output
            if not sources:
                info("No sources found")
                return

            table = create_table("Crawl Sources", "ID", "Name", "URL", "Status", "Docs", "Chunks")

            for src in sources:
                status_val = src.get("crawl_status", "pending")
                status_color = {
                    "completed": SUCCESS_GREEN,
                    "in_progress": ELECTRIC_YELLOW,
                    "failed": "red",
                    "partial": ELECTRIC_YELLOW,
                    "pending": "dim",
                }.get(status_val, "white")

                table.add_row(
                    src.get("id", ""),
                    truncate(src.get("name", ""), 20),
                    truncate(src.get("url", ""), 30),
                    f"[{status_color}]{status_val}[/{status_color}]",
                    str(src.get("document_count", 0)),
                    str(src.get("chunk_count", 0)),
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(sources)} source(s)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _list()


@app.command("add")
def add_source(
    url: Annotated[str, typer.Argument(help="Documentation URL to add")],
    name: Annotated[str | None, typer.Option("--name", "-n", help="Source name")] = None,
    source_type: Annotated[
        str, typer.Option("--type", "-T", help="Source type: website, github, api_docs")
    ] = "website",
    depth: Annotated[int, typer.Option("--depth", "-d", help="Crawl depth")] = 2,
    pattern: Annotated[
        list[str] | None,
        typer.Option(
            "--pattern",
            "--include",
            "-p",
            help="URL patterns to include",
        ),
    ] = None,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Add a new documentation source. Default: table output."""
    add_crawl_source(
        url,
        name=name,
        source_type=source_type,
        depth=depth,
        include_patterns=pattern,
        json_out=json_out,
        handle_client_error=_handle_client_error,
        next_step_command="sibyl crawl ingest",
    )


@app.command("ingest")
def ingest(
    source_id: Annotated[str, typer.Argument(help="Source ID to crawl")],
    max_pages: Annotated[
        int, typer.Option("--max-pages", "-p", help="Maximum pages to crawl")
    ] = 50,
    max_depth: Annotated[int, typer.Option("--depth", "-d", help="Maximum link depth")] = 3,
    no_embed: Annotated[bool, typer.Option("--no-embed", help="Skip embedding generation")] = False,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Start crawling a documentation source. Default: table output.

    Examples:
        sibyl crawl ingest abc123 --max-pages 100
        sibyl crawl ingest abc123 --depth 2 --no-embed
    """

    start_crawl_source(
        source_id,
        max_pages=max_pages,
        max_depth=max_depth,
        generate_embeddings=not no_embed,
        json_out=json_out,
        handle_client_error=_handle_client_error,
        status_command="sibyl crawl status <source_id>",
    )


@app.command("status")
def crawl_status(
    source_id: Annotated[str, typer.Argument(help="Source ID")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Get status of a crawl source using the current source-status contract."""
    show_source_status(
        source_id,
        json_out=json_out,
        handle_client_error=_handle_client_error,
    )


@app.command("documents")
def list_documents(
    source_id: Annotated[
        str | None, typer.Option("--source", "-s", help="Filter by source ID")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """List crawled documents. Default: table output."""

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            response = await client.list_crawl_documents(source_id=source_id, limit=limit)
            documents = response.get("documents", [])

            # JSON output (default)
            if json_out:
                print_json(response)
                return

            # Table output
            if not documents:
                info("No documents found")
                return

            table = create_table("Documents", "ID", "Title", "URL", "Words", "Code")

            for doc in documents:
                table.add_row(
                    doc.get("id", ""),
                    truncate(doc.get("title", ""), 25),
                    truncate(doc.get("url", ""), 35),
                    str(doc.get("word_count", 0)),
                    "Yes" if doc.get("has_code") else "No",
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(documents)} document(s)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _list()


@app.command("stats")
def stats(
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Show crawling statistics. Default: table output."""

    @run_async
    async def _stats() -> None:
        client = get_client()

        try:
            response = await client.crawler_stats()

            # JSON output (default)
            if json_out:
                print_json(response)
                return

            # Table output
            console.print(f"\n[{ELECTRIC_PURPLE}]Crawl Statistics[/{ELECTRIC_PURPLE}]\n")
            console.print(f"  Sources: [{CORAL}]{response.get('total_sources', 0)}[/{CORAL}]")
            console.print(f"  Documents: [{CORAL}]{response.get('total_documents', 0)}[/{CORAL}]")
            console.print(f"  Chunks: [{CORAL}]{response.get('total_chunks', 0)}[/{CORAL}]")
            console.print(
                f"  With embeddings: [{CORAL}]{response.get('chunks_with_embeddings', 0)}[/{CORAL}]"
            )

            if sources_by_status := response.get("sources_by_status"):
                console.print(f"\n[{NEON_CYAN}]Sources by Status:[/{NEON_CYAN}]")
                for status_name, count in sources_by_status.items():
                    console.print(f"    {status_name}: {count}")

        except SibylClientError as e:
            _handle_client_error(e)

    _stats()


@app.command("health")
def health(
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Check crawl system health. Default: table output."""

    @run_async
    async def _health() -> None:
        client = get_client()

        try:
            response = await client.crawler_health()

            # JSON output (default)
            if json_out:
                print_json(response)
                return

            # Table output
            console.print(f"\n[{ELECTRIC_PURPLE}]Crawl System Health[/{ELECTRIC_PURPLE}]\n")

            # Check PostgreSQL
            if response.get("postgres_healthy"):
                pg_version = response.get("postgres_version") or "unknown"
                success(f"PostgreSQL: {pg_version[:30]}...")
                info(f"  pgvector: {response.get('pgvector_version', 'unknown')}")
            else:
                error(f"PostgreSQL: {response.get('error', 'Unhealthy')}")

            # Check Crawl4AI
            if response.get("crawl4ai_available"):
                success("Crawl4AI: Ready")
            else:
                error("Crawl4AI: Not available")

        except SibylClientError as e:
            _handle_client_error(e)

    _health()


@app.command("delete")
def delete_source(
    source_id: Annotated[str, typer.Argument(help="Source ID to delete")],
    json_out: Annotated[
        bool, typer.Option("--json", "-j", help="JSON output (for scripting)")
    ] = False,
) -> None:
    """Delete a crawl source and all its documents. Default: table output."""

    @run_async
    async def _delete() -> None:
        client = get_client()

        try:
            response = await client.delete_crawl_source(source_id)

            # JSON output (default)
            if json_out:
                print_json(response)
                return

            # Table output
            if response.get("deleted"):
                success(f"Source deleted: {source_id}")
            else:
                error("Failed to delete source")

        except SibylClientError as e:
            _handle_client_error(e)

    _delete()
