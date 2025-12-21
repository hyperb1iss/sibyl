"""Documentation source management CLI commands.

Commands for managing crawlable documentation sources.
"""

from typing import Annotated

import typer

from sibyl.cli.common import (
    ELECTRIC_PURPLE,
    NEON_CYAN,
    console,
    create_table,
    error,
    info,
    print_db_hint,
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


@app.command("list")
def list_sources(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
    format_: Annotated[
        str, typer.Option("--format", "-f", help="Output format: table, json")
    ] = "table",
) -> None:
    """List all documentation sources."""

    @run_async
    async def _list() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner("Loading sources...") as progress:
                progress.add_task("Loading sources...", total=None)
                response = await explore(
                    mode="list",
                    types=["source"],
                    limit=limit,
                )

            entities = response.entities or []

            if format_ == "json":
                import json

                console.print(json.dumps([e.model_dump() for e in entities], indent=2, default=str))
                return

            if not entities:
                info("No sources found")
                return

            table = create_table("Documentation Sources", "ID", "Name", "Type", "URL", "Status")
            for e in entities:
                meta = e.metadata or {}
                table.add_row(
                    e.id[:8] + "...",
                    truncate(e.name, 25),
                    meta.get("source_type", "website"),
                    truncate(meta.get("url", "-"), 30),
                    meta.get("crawl_status", "pending"),
                )

            console.print(table)

        except Exception as e:
            error(f"Failed to list sources: {e}")
            print_db_hint()

    _list()


@app.command("add")
def add_source(
    url: Annotated[str, typer.Argument(help="Source URL")],
    name: Annotated[str | None, typer.Option("--name", "-n", help="Source name")] = None,
    source_type: Annotated[
        str, typer.Option("--type", "-t", help="Source type: website, github, api_docs")
    ] = "website",
    depth: Annotated[int, typer.Option("--depth", "-d", help="Crawl depth")] = 2,
) -> None:
    """Add a new documentation source."""

    @run_async
    async def _add() -> None:
        from sibyl.tools.core import add

        try:
            source_name = name or url.split("//")[-1].split("/")[0]

            with spinner("Adding source...") as progress:
                progress.add_task("Adding source...", total=None)
                response = await add(
                    title=source_name,
                    content=f"Documentation source: {url}",
                    entity_type="episode",  # Sources stored as episodes for now
                    metadata={
                        "url": url,
                        "source_type": source_type,
                        "crawl_depth": depth,
                        "crawl_status": "pending",
                    },
                )

            if response.success:
                success(f"Source added: {response.id}")
                info(f"Run 'sibyl source crawl {response.id}' to start crawling")
            else:
                error(f"Failed to add source: {response.message}")

        except Exception as e:
            error(f"Failed to add source: {e}")
            print_db_hint()

    _add()


@app.command("crawl")
def crawl_source(
    source_id: Annotated[str, typer.Argument(help="Source ID to crawl")],
) -> None:
    """Trigger a crawl for a documentation source."""

    @run_async
    async def _crawl() -> None:
        from sibyl.tools.core import manage

        try:
            with spinner("Starting crawl...") as progress:
                progress.add_task("Starting crawl...", total=None)
                response = await manage(
                    action="crawl",
                    entity_id=source_id,
                )

            if response.success:
                success("Crawl started")
                info("Check status with 'sibyl source status {source_id}'")
            else:
                error(f"Failed to start crawl: {response.message}")

        except Exception as e:
            error(f"Failed to start crawl: {e}")
            print_db_hint()

    _crawl()


@app.command("status")
def source_status(
    source_id: Annotated[str, typer.Argument(help="Source ID")],
) -> None:
    """Show crawl status for a source."""

    @run_async
    async def _status() -> None:
        from sibyl.graph.entities import EntityManager

        try:
            with spinner("Loading status...") as progress:
                progress.add_task("Loading status...", total=None)
                manager = EntityManager()
                entity = await manager.get(source_id)

            if not entity:
                error(f"Source not found: {source_id}")
                return

            meta = entity.metadata or {}

            console.print(f"\n[{ELECTRIC_PURPLE}]Source Status[/{ELECTRIC_PURPLE}]\n")
            console.print(f"  Name: [{NEON_CYAN}]{entity.name}[/{NEON_CYAN}]")
            console.print(f"  URL: {meta.get('url', '-')}")
            console.print(f"  Status: {meta.get('crawl_status', 'pending')}")
            console.print(f"  Documents: {meta.get('document_count', 0)}")
            console.print(f"  Last Crawled: {meta.get('last_crawled', 'never')}")

            if meta.get("crawl_error"):
                error(f"Last Error: {meta['crawl_error']}")

        except Exception as e:
            error(f"Failed to get status: {e}")
            print_db_hint()

    _status()


@app.command("documents")
def list_documents(
    source_id: Annotated[str, typer.Argument(help="Source ID")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 50,
) -> None:
    """List documents crawled from a source."""

    @run_async
    async def _docs() -> None:
        from sibyl.tools.core import explore

        try:
            with spinner("Loading documents...") as progress:
                progress.add_task("Loading documents...", total=None)
                # Filter documents by source using their metadata
                response = await explore(
                    mode="list",
                    types=["document"],
                    limit=limit * 5,  # Fetch more to filter
                )
                # Filter by source
                if response.entities:
                    response.entities = [
                        e for e in response.entities if e.metadata.get("source_id") == source_id
                    ][:limit]

            entities = response.entities or []

            if not entities:
                info("No documents found for this source")
                return

            table = create_table("Documents", "ID", "Title", "URL", "Words")
            for e in entities:
                meta = e.metadata or {}
                table.add_row(
                    e.id[:8] + "...",
                    truncate(e.name, 35),
                    truncate(meta.get("url", "-"), 30),
                    str(meta.get("word_count", 0)),
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(entities)} document(s)[/dim]")

        except Exception as e:
            error(f"Failed to list documents: {e}")
            print_db_hint()

    _docs()
