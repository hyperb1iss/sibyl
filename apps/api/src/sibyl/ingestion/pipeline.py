"""Main ingestion pipeline for the conventions knowledge graph."""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

from sibyl.ingestion.cataloger import (
    CatalogedConfig,
    CatalogedSlashCommand,
    CatalogedTemplate,
    catalog_repository,
)
from sibyl.ingestion.chunker import ChunkedDocument, Episode, SemanticChunker
from sibyl.ingestion.extractor import ExtractedEntity, extract_entities_from_episodes
from sibyl.ingestion.parser import MarkdownParser, ParsedDocument, parse_directory
from sibyl.ingestion.relationships import (
    ExtractedRelationship,
    build_all_relationships,
)
from sibyl.ingestion.storage import StorageResult, store_ingestion_results

log = structlog.get_logger()


@dataclass
class IngestionStats:
    """Statistics from an ingestion run."""

    files_processed: int = 0
    episodes_created: int = 0
    entities_extracted: int = 0
    relationships_built: int = 0
    templates_cataloged: int = 0
    configs_cataloged: int = 0
    commands_cataloged: int = 0
    entities_stored: int = 0
    relationships_stored: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class IngestionResult:
    """Complete result of an ingestion run."""

    stats: IngestionStats
    documents: list[ParsedDocument]
    chunked_documents: list[ChunkedDocument]
    episodes: list[Episode]
    entities: list[ExtractedEntity]
    relationships: list[ExtractedRelationship]
    templates: list[CatalogedTemplate]
    configs: list[CatalogedConfig]
    slash_commands: list[CatalogedSlashCommand]
    storage_result: StorageResult | None = None

    @property
    def success(self) -> bool:
        """Return True if ingestion completed without errors."""
        return len(self.stats.errors) == 0

    @property
    def errors(self) -> list[str]:
        """Return list of errors from ingestion."""
        return self.stats.errors


class IngestionPipeline:
    """Main pipeline for ingesting knowledge from the conventions repository.

    Pipeline stages:
    1. Parse markdown files into structured documents
    2. Chunk documents into semantic episodes
    3. Extract entities from episodes
    4. Build relationships between entities
    5. Catalog templates, configs, and slash commands
    """

    def __init__(
        self,
        repo_root: Path,
        wisdom_patterns: list[str] | None = None,
        *,
        group_id: str,
    ) -> None:
        """Initialize the pipeline.

        Args:
            repo_root: Root path of the conventions repository.
            wisdom_patterns: Glob patterns for wisdom docs (default: docs/wisdom/**/*.md).
            group_id: Organization ID for multi-tenant graph operations.
        """
        self.repo_root = repo_root
        self.wisdom_patterns = wisdom_patterns or ["docs/wisdom/**/*.md"]
        self.group_id = group_id

        self.parser = MarkdownParser()
        self.chunker = SemanticChunker()

    async def run(self) -> IngestionResult:
        """Run the full ingestion pipeline.

        Returns:
            Complete ingestion result with all extracted data.
        """
        start_time = datetime.now(UTC)
        stats = IngestionStats()
        errors: list[str] = []

        log.info("Starting ingestion pipeline", repo_root=str(self.repo_root))

        # Stage 1: Parse markdown files
        log.info("Stage 1: Parsing markdown files")
        documents = await self._parse_documents(stats, errors)

        # Stage 2: Chunk into episodes
        log.info("Stage 2: Chunking documents into episodes")
        chunked_documents = await self._chunk_documents(documents, stats)
        all_episodes = [ep for doc in chunked_documents for ep in doc.episodes]
        stats.episodes_created = len(all_episodes)

        # Stage 3: Extract entities
        log.info("Stage 3: Extracting entities from episodes")
        entities = await self._extract_entities(all_episodes, stats)
        stats.entities_extracted = len(entities)

        # Stage 4: Build relationships
        log.info("Stage 4: Building relationships")
        relationships = await self._build_relationships(entities, all_episodes, stats)
        stats.relationships_built = len(relationships)

        # Stage 5: Catalog templates and configs
        log.info("Stage 5: Cataloging templates, configs, and commands")
        templates, configs, commands = await self._catalog_repository(stats)
        stats.templates_cataloged = len(templates)
        stats.configs_cataloged = len(configs)
        stats.commands_cataloged = len(commands)

        # Stage 6: Store to graph
        log.info("Stage 6: Storing to knowledge graph")
        storage_result = await self._store_to_graph(entities, relationships, stats, errors)
        stats.entities_stored = storage_result.entities_stored
        stats.relationships_stored = storage_result.relationships_stored
        errors.extend(storage_result.errors)

        # Calculate duration
        stats.duration_seconds = (datetime.now(UTC) - start_time).total_seconds()
        stats.errors = errors

        log.info(
            "Ingestion pipeline complete",
            files=stats.files_processed,
            episodes=stats.episodes_created,
            entities=stats.entities_extracted,
            relationships=stats.relationships_built,
            entities_stored=stats.entities_stored,
            relationships_stored=stats.relationships_stored,
            templates=stats.templates_cataloged,
            configs=stats.configs_cataloged,
            commands=stats.commands_cataloged,
            duration=f"{stats.duration_seconds:.2f}s",
            errors=len(errors),
        )

        return IngestionResult(
            stats=stats,
            documents=documents,
            chunked_documents=chunked_documents,
            episodes=all_episodes,
            entities=entities,
            relationships=relationships,
            templates=templates,
            configs=configs,
            slash_commands=commands,
            storage_result=storage_result,
        )

    async def _parse_documents(
        self,
        stats: IngestionStats,
        errors: list[str],
    ) -> list[ParsedDocument]:
        """Parse all wisdom documents.

        Args:
            stats: Stats to update.
            errors: Error list to append to.

        Returns:
            List of parsed documents.
        """
        documents: list[ParsedDocument] = []

        for pattern in self.wisdom_patterns:
            pattern_path = self.repo_root / pattern.split("/")[0]
            if not pattern_path.exists():
                log.warning("Wisdom directory not found", pattern=pattern)
                continue

            # Parse in thread pool to avoid blocking
            loop = asyncio.get_running_loop()
            parsed = await loop.run_in_executor(
                None,
                parse_directory,
                self.repo_root,
                pattern,
            )
            documents.extend(parsed)

        stats.files_processed = len(documents)
        log.info(f"Parsed {len(documents)} markdown files")
        return documents

    async def _chunk_documents(
        self,
        documents: list[ParsedDocument],
        stats: IngestionStats,
    ) -> list[ChunkedDocument]:
        """Chunk documents into episodes.

        Args:
            documents: Parsed documents.
            stats: Stats to update.

        Returns:
            List of chunked documents.
        """
        chunked: list[ChunkedDocument] = []

        for doc in documents:
            chunked_doc = self.chunker.chunk_document(doc)
            chunked.append(chunked_doc)

        total_episodes = sum(len(doc.episodes) for doc in chunked)
        log.info(f"Created {total_episodes} episodes from {len(documents)} documents")
        return chunked

    async def _extract_entities(
        self,
        episodes: list[Episode],
        stats: IngestionStats,
    ) -> list[ExtractedEntity]:
        """Extract entities from episodes.

        Args:
            episodes: All episodes.
            stats: Stats to update.

        Returns:
            Extracted entities.
        """
        # Run in thread pool for CPU-bound work
        loop = asyncio.get_running_loop()
        entities = await loop.run_in_executor(
            None,
            extract_entities_from_episodes,
            episodes,
        )

        log.info(f"Extracted {len(entities)} entities")
        return entities

    async def _build_relationships(
        self,
        entities: list[ExtractedEntity],
        episodes: list[Episode],
        stats: IngestionStats,
    ) -> list[ExtractedRelationship]:
        """Build relationships between entities.

        Args:
            entities: Extracted entities.
            episodes: All episodes.
            stats: Stats to update.

        Returns:
            Built relationships.
        """
        loop = asyncio.get_running_loop()
        relationships = await loop.run_in_executor(
            None,
            build_all_relationships,
            entities,
            episodes,
        )

        log.info(f"Built {len(relationships)} relationships")
        return relationships

    async def _catalog_repository(
        self,
        stats: IngestionStats,
    ) -> tuple[
        list[CatalogedTemplate],
        list[CatalogedConfig],
        list[CatalogedSlashCommand],
    ]:
        """Catalog templates, configs, and slash commands.

        Args:
            stats: Stats to update.

        Returns:
            Tuple of (templates, configs, commands).
        """
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            catalog_repository,
            self.repo_root,
        )

        templates, configs, commands = result
        log.info(
            f"Cataloged {len(templates)} templates, "
            f"{len(configs)} configs, {len(commands)} slash commands"
        )
        return templates, configs, commands

    async def _store_to_graph(
        self,
        entities: list[ExtractedEntity],
        relationships: list[ExtractedRelationship],
        stats: IngestionStats,
        errors: list[str],
    ) -> StorageResult:
        """Store extracted data to the knowledge graph.

        Args:
            entities: Extracted entities.
            relationships: Extracted relationships.
            stats: Stats to update.
            errors: Error list to append to.

        Returns:
            StorageResult with storage statistics.
        """
        try:
            result = await store_ingestion_results(entities, relationships, group_id=self.group_id)
            log.info(
                "Graph storage complete",
                entities_stored=result.entities_stored,
                relationships_stored=result.relationships_stored,
                errors=len(result.errors),
            )
            return result
        except Exception as e:
            log.exception("Graph storage failed", error=str(e))
            errors.append(f"Graph storage failed: {e}")
            return StorageResult(
                entities_stored=0,
                relationships_stored=0,
                entities_skipped=len(entities),
                relationships_skipped=len(relationships),
                errors=[str(e)],
            )


async def run_ingestion(repo_root: Path, *, group_id: str) -> IngestionResult:
    """Convenience function to run the full ingestion pipeline.

    Args:
        repo_root: Root path of the conventions repository.
        group_id: Organization ID for multi-tenant graph operations.

    Returns:
        Ingestion result.
    """
    pipeline = IngestionPipeline(repo_root, group_id=group_id)
    return await pipeline.run()
