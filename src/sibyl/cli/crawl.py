"""Web crawling and documentation ingestion CLI commands.

Commands for crawling documentation sites and managing the ingestion pipeline.
Uses Crawl4AI for web crawling and PostgreSQL for document storage.
"""

from typing import Annotated

import typer

from sibyl.cli.common import (
    CORAL,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    NEON_CYAN,
    SUCCESS_GREEN,
    console,
    create_table,
    error,
    info,
    print_db_hint,
    run_async,
    spinner,
    success,
    truncate,
    warn,
)

app = typer.Typer(
    name="crawl",
    help="Web crawling and documentation ingestion",
    no_args_is_help=True,
)


@app.command("ingest")
def ingest(
    url: Annotated[str, typer.Argument(help="Documentation URL to crawl")],
    name: Annotated[str | None, typer.Option("--name", "-n", help="Source name")] = None,
    max_pages: Annotated[
        int, typer.Option("--max-pages", "-p", help="Maximum pages to crawl")
    ] = 50,
    max_depth: Annotated[int, typer.Option("--depth", "-d", help="Maximum link depth")] = 3,
    pattern: Annotated[
        list[str] | None, typer.Option("--pattern", "-i", help="URL patterns to include")
    ] = None,
    no_embed: Annotated[bool, typer.Option("--no-embed", help="Skip embedding generation")] = False,
) -> None:
    """Ingest a documentation site.

    Crawls the site, chunks content, generates embeddings, and stores in PostgreSQL.

    Examples:
        sibyl crawl ingest https://fastapi.tiangolo.com
        sibyl crawl ingest https://docs.python.org -n "Python Docs" -p 100
        sibyl crawl ingest https://react.dev --pattern "/docs/*"
    """

    @run_async
    async def _ingest() -> None:
        from sibyl.crawler import IngestionPipeline, create_source, get_source_by_url
        from sibyl.db import check_postgres_health

        # Check PostgreSQL health first
        health = await check_postgres_health()
        if health["status"] != "healthy":
            error(f"PostgreSQL not healthy: {health.get('error', 'Unknown error')}")
            print_db_hint()
            return

        # Get or create source
        source_name = name or url.split("//")[-1].split("/")[0]
        source = await get_source_by_url(url)

        if source:
            info(f"Using existing source: {source.name}")
        else:
            source = await create_source(
                name=source_name,
                url=url,
                include_patterns=pattern or [],
            )
            success(f"Created source: {source.name}")

        console.print(f"\n[{ELECTRIC_PURPLE}]Starting ingestion...[/{ELECTRIC_PURPLE}]")
        console.print(f"  URL: [{NEON_CYAN}]{url}[/{NEON_CYAN}]")
        console.print(f"  Max pages: {max_pages}")
        console.print(f"  Max depth: {max_depth}")
        console.print(f"  Embeddings: {'No' if no_embed else 'Yes'}")
        console.print()

        try:
            async with IngestionPipeline(generate_embeddings=not no_embed) as pipeline:
                with spinner("Crawling...") as progress:
                    task = progress.add_task("Crawling pages...", total=max_pages)

                    stats = await pipeline.ingest_source(
                        source,
                        max_pages=max_pages,
                        max_depth=max_depth,
                    )

                    progress.update(task, completed=stats.documents_crawled)

            # Show results
            console.print(f"\n[{SUCCESS_GREEN}]Ingestion complete![/{SUCCESS_GREEN}]\n")
            console.print(f"  Documents crawled: [{CORAL}]{stats.documents_crawled}[/{CORAL}]")
            console.print(f"  Documents stored: [{CORAL}]{stats.documents_stored}[/{CORAL}]")
            console.print(f"  Chunks created: [{CORAL}]{stats.chunks_created}[/{CORAL}]")
            console.print(f"  Embeddings: [{CORAL}]{stats.embeddings_generated}[/{CORAL}]")
            console.print(f"  Duration: [{CORAL}]{stats.duration_seconds:.1f}s[/{CORAL}]")

            if stats.errors > 0:
                warn(f"  Errors: {stats.errors}")

        except Exception as e:
            error(f"Ingestion failed: {e}")
            raise typer.Exit(1) from e

    _ingest()


@app.command("sources")
def list_sources(
    status: Annotated[str | None, typer.Option("--status", "-s", help="Filter by status")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
) -> None:
    """List all crawl sources."""

    @run_async
    async def _list() -> None:
        from sibyl.crawler import list_sources as get_sources
        from sibyl.db import CrawlStatus, check_postgres_health

        health = await check_postgres_health()
        if health["status"] != "healthy":
            error(f"PostgreSQL not healthy: {health.get('error')}")
            return

        try:
            crawl_status = CrawlStatus(status) if status else None
            sources = await get_sources(status=crawl_status, limit=limit)

            if not sources:
                info("No sources found")
                return

            table = create_table("Crawl Sources", "ID", "Name", "URL", "Status", "Docs", "Chunks")

            for src in sources:
                status_color = {
                    "completed": SUCCESS_GREEN,
                    "in_progress": ELECTRIC_YELLOW,
                    "failed": "red",
                    "pending": "dim",
                }.get(src.crawl_status, "white")

                table.add_row(
                    str(src.id)[:8] + "...",
                    truncate(src.name, 20),
                    truncate(src.url, 30),
                    f"[{status_color}]{src.crawl_status}[/{status_color}]",
                    str(src.document_count),
                    str(src.chunk_count),
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(sources)} source(s)[/dim]")

        except Exception as e:
            error(f"Failed to list sources: {e}")

    _list()


@app.command("documents")
def list_documents(
    source_id: Annotated[
        str | None, typer.Option("--source", "-s", help="Filter by source ID")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Max results")] = 20,
) -> None:
    """List crawled documents."""

    @run_async
    async def _list() -> None:
        from uuid import UUID

        from sqlalchemy import select
        from sqlmodel import col

        from sibyl.db import CrawledDocument, get_session

        try:
            async with get_session() as session:
                query = select(CrawledDocument)
                if source_id:
                    query = query.where(col(CrawledDocument.source_id) == UUID(source_id))
                query = query.order_by(col(CrawledDocument.crawled_at).desc()).limit(limit)

                result = await session.execute(query)
                documents = result.scalars().all()

            if not documents:
                info("No documents found")
                return

            table = create_table("Documents", "ID", "Title", "URL", "Words", "Chunks")

            for doc in documents:
                table.add_row(
                    str(doc.id)[:8] + "...",
                    truncate(doc.title, 25),
                    truncate(doc.url, 35),
                    str(doc.word_count),
                    "-",  # Would need to count chunks
                )

            console.print(table)
            console.print(f"\n[dim]Showing {len(documents)} document(s)[/dim]")

        except Exception as e:
            error(f"Failed to list documents: {e}")

    _list()


@app.command("stats")
def stats() -> None:
    """Show crawling statistics."""

    @run_async
    async def _stats() -> None:
        from sqlalchemy import func, select

        from sibyl.db import CrawledDocument, CrawlSource, DocumentChunk, get_session

        try:
            async with get_session() as session:
                # Count sources
                result = await session.execute(select(func.count(CrawlSource.id)))
                source_count = result.scalar() or 0

                # Count documents
                result = await session.execute(select(func.count(CrawledDocument.id)))
                doc_count = result.scalar() or 0

                # Count chunks
                result = await session.execute(select(func.count(DocumentChunk.id)))
                chunk_count = result.scalar() or 0

                # Count chunks with embeddings
                from sqlmodel import col

                result = await session.execute(
                    select(func.count(DocumentChunk.id)).where(
                        col(DocumentChunk.embedding).is_not(None)
                    )
                )
                embedded_count = result.scalar() or 0

            console.print(f"\n[{ELECTRIC_PURPLE}]Crawl Statistics[/{ELECTRIC_PURPLE}]\n")
            console.print(f"  Sources: [{CORAL}]{source_count}[/{CORAL}]")
            console.print(f"  Documents: [{CORAL}]{doc_count}[/{CORAL}]")
            console.print(f"  Chunks: [{CORAL}]{chunk_count}[/{CORAL}]")
            console.print(f"  With embeddings: [{CORAL}]{embedded_count}[/{CORAL}]")

        except Exception as e:
            error(f"Failed to get stats: {e}")

    _stats()


@app.command("health")
def health() -> None:
    """Check crawl system health."""

    @run_async
    async def _health() -> None:
        from sibyl.db import check_postgres_health

        console.print(f"\n[{ELECTRIC_PURPLE}]Crawl System Health[/{ELECTRIC_PURPLE}]\n")

        # Check PostgreSQL
        pg_health = await check_postgres_health()
        if pg_health["status"] == "healthy":
            pg_version = pg_health.get("postgres_version") or "unknown"
            success(f"PostgreSQL: {pg_version[:30]}...")
            info(f"  pgvector: {pg_health.get('pgvector_version', 'unknown')}")
        else:
            error(f"PostgreSQL: {pg_health.get('error', 'Unhealthy')}")

        # Check Crawl4AI
        try:
            from crawl4ai import AsyncWebCrawler

            async with AsyncWebCrawler():
                pass
            success("Crawl4AI: Ready")
        except Exception as e:
            error(f"Crawl4AI: {e}")

    _health()
