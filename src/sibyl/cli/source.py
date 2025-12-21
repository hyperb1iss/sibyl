"""Documentation source management CLI commands.

Commands for managing crawlable documentation sources.
All commands communicate with the REST API to ensure proper event broadcasting.
"""

from typing import Annotated

import typer

from sibyl.cli.client import SibylClientError, get_client
from sibyl.cli.common import (
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_table,
    error,
    info,
    print_json,
    run_async,
    spinner,
    success,
    truncate,
)

app = typer.Typer(
    name="source",
    help="Documentation source management",
    no_args_is_help=True,
)


def _handle_client_error(e: SibylClientError) -> None:
    """Handle client errors with helpful messages."""
    if "Cannot connect" in str(e):
        error(str(e))
        info("Start the server with: sibyl serve")
    elif e.status_code == 404:
        error(f"Not found: {e.detail}")
    elif e.status_code == 400:
        error(f"Invalid request: {e.detail}")
    else:
        error(str(e))


@app.command("list")
def list_sources(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """List all documentation sources. Default: JSON output."""
    format_ = "table" if table_out else "json"

    @run_async
    async def _list() -> None:
        client = get_client()

        try:
            if format_ == "json":
                response = await client.explore(
                    mode="list",
                    types=["source"],
                    limit=limit,
                )
            else:
                with spinner("Loading sources...") as progress:
                    progress.add_task("Loading sources...", total=None)
                    response = await client.explore(
                        mode="list",
                        types=["source"],
                        limit=limit,
                    )

            entities = response.get("entities", [])

            if format_ == "json":

                print_json(entities)
                return

            if not entities:
                info("No sources found")
                return

            table = create_table("Documentation Sources", "ID", "Name", "Type", "URL", "Status")
            for e in entities:
                meta = e.get("metadata", {})
                table.add_row(
                    e.get("id", "")[:8] + "...",
                    truncate(e.get("name", ""), 25),
                    meta.get("source_type", "website"),
                    truncate(meta.get("url", "-"), 30),
                    meta.get("crawl_status", "pending"),
                )

            console.print(table)

        except SibylClientError as e:
            _handle_client_error(e)

    _list()


@app.command("add")
def add_source(
    url: Annotated[str, typer.Argument(help="Source URL")],
    name: Annotated[str | None, typer.Option("--name", "-n", help="Source name")] = None,
    source_type: Annotated[
        str, typer.Option("--type", "-T", help="Source type: website, github, api_docs")
    ] = "website",
    depth: Annotated[int, typer.Option("--depth", "-d", help="Crawl depth")] = 2,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Add a new documentation source. Default: JSON output."""

    @run_async
    async def _add() -> None:

        client = get_client()

        try:
            source_name = name or url.split("//")[-1].split("/")[0]

            if table_out:
                with spinner("Adding source...") as progress:
                    progress.add_task("Adding source...", total=None)
                    response = await client.create_entity(
                        name=source_name,
                        content=f"Documentation source: {url}",
                        entity_type="source",
                        metadata={
                            "url": url,
                            "source_type": source_type,
                            "crawl_depth": depth,
                            "crawl_status": "pending",
                        },
                    )
            else:
                response = await client.create_entity(
                    name=source_name,
                    content=f"Documentation source: {url}",
                    entity_type="source",
                    metadata={
                        "url": url,
                        "source_type": source_type,
                        "crawl_depth": depth,
                        "crawl_status": "pending",
                    },
                )

            # JSON output (default)
            if not table_out:
                print_json(response)
                return

            # Table output
            if response.get("id"):
                success(f"Source added: {response['id']}")
                info(f"Run 'sibyl source crawl {response['id']}' to start crawling")
            else:
                error("Failed to add source")

        except SibylClientError as e:
            _handle_client_error(e)

    _add()


@app.command("show")
def show_source(
    source_id: Annotated[str, typer.Argument(help="Source ID")],
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show source details. Default: JSON output."""

    @run_async
    async def _show() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Loading source...") as progress:
                    progress.add_task("Loading source...", total=None)
                    entity = await client.get_entity(source_id)
            else:
                entity = await client.get_entity(source_id)

            # JSON output (default)
            if not table_out:

                print_json(entity)
                return

            # Table output
            meta = entity.get("metadata", {})

            console.print(f"\n[{ELECTRIC_PURPLE}]Source Details[/{ELECTRIC_PURPLE}]\n")
            console.print(f"  Name: [{NEON_CYAN}]{entity.get('name', '')}[/{NEON_CYAN}]")
            console.print(f"  ID: {entity.get('id', '')}")
            console.print(f"  URL: {meta.get('url', '-')}")
            console.print(f"  Type: {meta.get('source_type', 'website')}")
            console.print(f"  Status: {meta.get('crawl_status', 'pending')}")
            console.print(f"  Documents: {meta.get('document_count', 0)}")
            console.print(f"  Last Crawled: {meta.get('last_crawled', 'never')}")

            if meta.get("crawl_error"):
                error(f"Last Error: {meta['crawl_error']}")

        except SibylClientError as e:
            _handle_client_error(e)

    _show()


@app.command("crawl")
def crawl_source(
    source_id: Annotated[str, typer.Argument(help="Source ID to crawl")],
) -> None:
    """Trigger a crawl for a documentation source."""
    info(f"Crawl source {source_id} - Use 'sibyl crawl start {source_id}' for crawler")
    info("The source crawl workflow is handled by the crawler module")


@app.command("status")
def source_status(
    source_id: Annotated[str, typer.Argument(help="Source ID")],
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """Show crawl status for a source. Default: JSON output."""

    @run_async
    async def _status() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Loading status...") as progress:
                    progress.add_task("Loading status...", total=None)
                    entity = await client.get_entity(source_id)
            else:
                entity = await client.get_entity(source_id)

            meta = entity.get("metadata", {})

            # JSON output (default)
            if not table_out:

                status_data = {
                    "id": entity.get("id"),
                    "name": entity.get("name"),
                    "url": meta.get("url"),
                    "crawl_status": meta.get("crawl_status", "pending"),
                    "document_count": meta.get("document_count", 0),
                    "last_crawled": meta.get("last_crawled"),
                    "crawl_error": meta.get("crawl_error"),
                }
                print_json(status_data)
                return

            # Table output
            console.print(f"\n[{ELECTRIC_PURPLE}]Source Status[/{ELECTRIC_PURPLE}]\n")
            console.print(f"  Name: [{NEON_CYAN}]{entity.get('name', '')}[/{NEON_CYAN}]")
            console.print(f"  URL: {meta.get('url', '-')}")
            console.print(f"  Status: {meta.get('crawl_status', 'pending')}")
            console.print(f"  Documents: {meta.get('document_count', 0)}")
            console.print(f"  Last Crawled: {meta.get('last_crawled', 'never')}")

            if meta.get("crawl_error"):
                error(f"Last Error: {meta['crawl_error']}")

        except SibylClientError as e:
            _handle_client_error(e)

    _status()


@app.command("documents")
def list_documents(
    source_id: Annotated[str, typer.Argument(help="Source ID")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 50,
    table_out: Annotated[
        bool, typer.Option("--table", "-t", help="Table output (human-readable)")
    ] = False,
) -> None:
    """List documents crawled from a source. Default: JSON output."""

    @run_async
    async def _docs() -> None:
        client = get_client()

        try:
            if table_out:
                with spinner("Loading documents...") as progress:
                    progress.add_task("Loading documents...", total=None)
                    response = await client.explore(
                        mode="list",
                        types=["document"],
                        limit=limit * 5,  # Fetch more to filter
                    )
            else:
                response = await client.explore(
                    mode="list",
                    types=["document"],
                    limit=limit * 5,
                )

            # Filter by source
            all_entities = response.get("entities", [])
            entities = [
                e for e in all_entities if e.get("metadata", {}).get("source_id") == source_id
            ][:limit]

            # JSON output (default)
            if not table_out:

                print_json(entities)
                return

            # Table output
            if not entities:
                info("No documents found for this source")
                return

            table = create_table("Documents", "ID", "Title", "URL", "Words")
            for e in entities:
                meta = e.get("metadata", {})
                table.add_row(
                    e.get("id", "")[:8] + "...",
                    truncate(e.get("name", ""), 35),
                    truncate(meta.get("url", "-"), 30),
                    str(meta.get("word_count", 0)),
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} document(s)[/dim]")

        except SibylClientError as e:
            _handle_client_error(e)

    _docs()
