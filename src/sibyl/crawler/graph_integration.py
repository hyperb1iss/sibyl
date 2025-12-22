"""Graph-RAG integration for document↔entity linking.

This module implements deep integration between crawled documents
and the knowledge graph, following SOTA Graph-RAG techniques:

1. Entity Extraction: Extract entities from document chunks using LLM
2. Entity Linking: Match extracted entities to existing graph entities
3. Bidirectional References: Store doc→entity and entity→doc links
4. Unified Search: Query both systems with cross-references

References:
- Microsoft GraphRAG: https://arxiv.org/abs/2404.16130
- Anthropic Contextual Retrieval
- /Users/bliss/dev/sibyl/docs/graph-rag-sota-research.md
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

import structlog

from sibyl.config import settings
from sibyl.db import DocumentChunk, get_session

if TYPE_CHECKING:
    from sibyl.graph.client import GraphClient

log = structlog.get_logger()


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class ExtractedEntity:
    """Entity extracted from a document chunk."""

    name: str
    entity_type: str  # pattern, tool, language, concept, etc.
    description: str
    confidence: float
    source_chunk_id: str | None = None
    source_url: str | None = None


@dataclass
class EntityLink:
    """Link between a document chunk and a graph entity."""

    chunk_id: str
    entity_uuid: str
    entity_name: str
    entity_type: str
    confidence: float
    relationship_type: str = "DOCUMENTED_IN"


@dataclass
class IntegrationStats:
    """Statistics from graph integration run."""

    chunks_processed: int = 0
    entities_extracted: int = 0
    entities_linked: int = 0
    new_entities_created: int = 0
    errors: int = 0


# =============================================================================
# Entity Extraction (LLM-based)
# =============================================================================


class EntityExtractor:
    """Extract entities from document chunks using LLM.

    Uses structured output to extract entities with types matching
    our knowledge graph schema.
    """

    _api_key_validated: bool = False

    EXTRACTION_PROMPT = '''Extract entities from this documentation chunk.

Chunk Content:
{content}

Context (from document):
{context}

Entity types to extract:
- pattern: Coding pattern, best practice, or design pattern
- tool: Library, framework, package, or development tool
- language: Programming language
- concept: Abstract concept, principle, or technique
- api: API endpoint, method, or interface
- warning: Gotcha, pitfall, or common mistake
- example: Code example or usage pattern

Return a JSON object with an "entities" array. Each entity should have:
- name: Concise entity name
- type: One of the types above
- description: Brief 1-sentence description
- confidence: 0.0-1.0 confidence score

Only extract entities that are clearly mentioned or demonstrated.
Do not infer entities that aren't explicitly present.'''

    def __init__(self, model: str | None = None):
        """Initialize the extractor.

        Args:
            model: LLM model to use (default: claude-haiku-4-5 for cost efficiency)
        """
        self.model = model or "claude-haiku-4-5"
        self._client = None

        # Validate API key on init (fail fast)
        if not EntityExtractor._api_key_validated:
            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                log.error(
                    "Anthropic API key not configured",
                    hint="Set ANTHROPIC_API_KEY or SIBYL_ANTHROPIC_API_KEY environment variable",
                )
                raise ValueError(
                    "Anthropic API key not configured. "
                    "Set ANTHROPIC_API_KEY or SIBYL_ANTHROPIC_API_KEY."
                )
            EntityExtractor._api_key_validated = True
            log.info("Entity extractor initialized", model=self.model)

    async def _get_client(self):
        """Lazily initialize Anthropic client."""
        if self._client is None:
            from anthropic import AsyncAnthropic

            api_key = settings.anthropic_api_key.get_secret_value()
            if not api_key:
                raise ValueError("Anthropic API key not configured")

            self._client = AsyncAnthropic(api_key=api_key)

        return self._client

    async def extract_from_chunk(
        self,
        content: str,
        context: str | None = None,
        url: str | None = None,
    ) -> list[ExtractedEntity]:
        """Extract entities from a single chunk.

        Args:
            content: Chunk content text
            context: Optional contextual prefix
            url: Source URL for attribution

        Returns:
            List of extracted entities
        """
        import json

        try:
            client = await self._get_client()

            prompt = self.EXTRACTION_PROMPT.format(
                content=content[:4000],  # Limit to avoid token overflow
                context=context or "No additional context",
            )

            response = await client.messages.create(
                model=self.model,
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract JSON from response
            response_text = response.content[0].text if response.content else "{}"

            # Handle case where model wraps JSON in markdown code blocks
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]

            result = json.loads(response_text.strip())
            entities = []

            for item in result.get("entities", []):
                entities.append(
                    ExtractedEntity(
                        name=item.get("name", ""),
                        entity_type=item.get("type", "concept"),
                        description=item.get("description", ""),
                        confidence=float(item.get("confidence", 0.5)),
                        source_url=url,
                    )
                )

            log.debug(
                "Extracted entities from chunk",
                count=len(entities),
                url=url,
            )

            return entities

        except Exception as e:
            log.warning("Entity extraction failed", error=str(e), url=url)
            return []

    async def extract_batch(
        self,
        chunks: list[tuple[str, str | None, str | None]],  # (content, context, url)
        max_concurrent: int = 5,
    ) -> list[ExtractedEntity]:
        """Extract entities from multiple chunks concurrently.

        Args:
            chunks: List of (content, context, url) tuples
            max_concurrent: Maximum concurrent extractions

        Returns:
            All extracted entities
        """
        if not chunks:
            return []

        log.info("Starting entity extraction", chunk_count=len(chunks), concurrency=max_concurrent)

        semaphore = asyncio.Semaphore(max_concurrent)

        async def extract_with_limit(content, context, url):
            async with semaphore:
                return await self.extract_from_chunk(content, context, url)

        tasks = [
            extract_with_limit(content, context, url)
            for content, context, url in chunks
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_entities = []
        failures = 0
        for result in results:
            if isinstance(result, list):
                all_entities.extend(result)
            elif isinstance(result, Exception):
                failures += 1
                # Log first few failures with details
                if failures <= 3:
                    log.warning("Extraction task failed", error=str(result))

        if failures > 0:
            log.warning(
                "Batch extraction completed with failures",
                total=len(chunks),
                failures=failures,
                entities_extracted=len(all_entities),
            )
        else:
            log.info(
                "Batch extraction complete",
                chunks=len(chunks),
                entities=len(all_entities),
            )

        return all_entities


# =============================================================================
# Entity Linker (Match to Graph)
# =============================================================================


class EntityLinker:
    """Link extracted entities to existing knowledge graph entities.

    Uses embedding similarity to find matching entities, falling back
    to name-based fuzzy matching.
    """

    def __init__(self, graph_client: GraphClient, similarity_threshold: float = 0.75):
        """Initialize the linker.

        Args:
            graph_client: Connected GraphClient
            similarity_threshold: Minimum similarity for linking
        """
        self.graph_client = graph_client
        self.similarity_threshold = similarity_threshold
        self._entity_cache: dict[str, list[dict]] = {}

    async def _get_graph_entities(self, entity_type: str | None = None) -> list[dict]:
        """Get entities from graph, with caching.

        Args:
            entity_type: Optional type filter

        Returns:
            List of entity dicts with uuid, name, entity_type
        """
        cache_key = entity_type or "all"

        if cache_key not in self._entity_cache:
            # Query graph for entities
            query = """
            MATCH (n)
            WHERE (n:Episodic OR n:Entity)
            AND n.entity_type IS NOT NULL
            """

            if entity_type:
                query += f" AND n.entity_type = '{entity_type}'"

            query += " RETURN n.uuid AS uuid, n.name AS name, n.entity_type AS entity_type LIMIT 1000"

            result = await self.graph_client.client.driver.execute_query(query)
            records = result[0] if result else []

            self._entity_cache[cache_key] = [
                {"uuid": r["uuid"], "name": r["name"], "entity_type": r["entity_type"]}
                for r in records
                if r.get("uuid") and r.get("name")
            ]

        return self._entity_cache[cache_key]

    async def link_entity(
        self,
        extracted: ExtractedEntity,
    ) -> EntityLink | None:
        """Try to link an extracted entity to an existing graph entity.

        Args:
            extracted: Extracted entity to link

        Returns:
            EntityLink if match found, None otherwise
        """
        # Get candidate entities of matching type
        candidates = await self._get_graph_entities(extracted.entity_type)

        if not candidates:
            return None

        # Simple name matching (case-insensitive)
        # TODO: Add embedding-based similarity for better matching
        extracted_name_lower = extracted.name.lower().strip()

        best_match = None
        best_score = 0.0

        for candidate in candidates:
            candidate_name_lower = candidate["name"].lower().strip()

            # Exact match
            if extracted_name_lower == candidate_name_lower:
                return EntityLink(
                    chunk_id="",  # Will be set by caller
                    entity_uuid=candidate["uuid"],
                    entity_name=candidate["name"],
                    entity_type=candidate["entity_type"],
                    confidence=1.0,
                )

            # Partial match (one contains the other)
            if (
                extracted_name_lower in candidate_name_lower
                or candidate_name_lower in extracted_name_lower
            ):
                # Score based on length ratio
                score = min(len(extracted_name_lower), len(candidate_name_lower)) / max(
                    len(extracted_name_lower), len(candidate_name_lower)
                )

                if score > best_score and score >= self.similarity_threshold:
                    best_score = score
                    best_match = candidate

        if best_match:
            return EntityLink(
                chunk_id="",
                entity_uuid=best_match["uuid"],
                entity_name=best_match["name"],
                entity_type=best_match["entity_type"],
                confidence=best_score,
            )

        return None

    async def link_batch(
        self,
        entities: list[ExtractedEntity],
    ) -> tuple[list[EntityLink], list[ExtractedEntity]]:
        """Link multiple entities, returning linked and unlinked.

        Args:
            entities: Extracted entities to link

        Returns:
            Tuple of (linked entities, unlinked entities)
        """
        linked = []
        unlinked = []

        for entity in entities:
            link = await self.link_entity(entity)
            if link:
                linked.append(link)
            else:
                unlinked.append(entity)

        return linked, unlinked


# =============================================================================
# Graph Integration Service
# =============================================================================


class GraphIntegrationService:
    """Orchestrates document↔graph integration.

    Ties together extraction, linking, and relationship storage.
    """

    def __init__(
        self,
        graph_client: GraphClient,
        *,
        extract_entities: bool = True,
        create_new_entities: bool = False,
    ):
        """Initialize the integration service.

        Args:
            graph_client: Connected GraphClient
            extract_entities: Whether to extract entities from chunks
            create_new_entities: Whether to create new graph entities for unlinked
        """
        self.graph_client = graph_client
        self.extract_entities = extract_entities
        self.create_new_entities = create_new_entities

        self.extractor = EntityExtractor() if extract_entities else None
        self.linker = EntityLinker(graph_client)

    async def process_chunks(
        self,
        chunks: list[DocumentChunk],
        source_name: str,
    ) -> IntegrationStats:
        """Process document chunks to link with graph.

        Args:
            chunks: DocumentChunks to process
            source_name: Name of the source (for logging)

        Returns:
            IntegrationStats with results
        """
        stats = IntegrationStats()

        if not self.extract_entities or not self.extractor:
            return stats

        # Extract entities from chunks
        chunk_data = [
            (chunk.content, chunk.context, str(chunk.document_id))
            for chunk in chunks
        ]

        extracted = await self.extractor.extract_batch(chunk_data)
        stats.entities_extracted = len(extracted)
        stats.chunks_processed = len(chunks)

        if not extracted:
            return stats

        # Link to existing graph entities
        linked, unlinked = await self.linker.link_batch(extracted)
        stats.entities_linked = len(linked)

        # Update chunk entity_ids in database
        async with get_session() as session:
            for i, chunk in enumerate(chunks):
                # Find links for this chunk
                chunk_links = [
                    link for link in linked
                    if str(chunk.id) in (link.chunk_id or "") or extracted[i].source_url
                ]

                if chunk_links:
                    chunk.entity_ids = [link.entity_uuid for link in chunk_links]
                    chunk.has_entities = True
                    session.add(chunk)

        # Optionally create new entities for unlinked
        if self.create_new_entities and unlinked:
            # TODO: Implement new entity creation via Graphiti
            stats.new_entities_created = 0

        log.info(
            "Graph integration complete",
            source=source_name,
            chunks=stats.chunks_processed,
            extracted=stats.entities_extracted,
            linked=stats.entities_linked,
        )

        return stats

    async def create_doc_relationships(
        self,
        document_id: UUID,
        entity_uuids: list[str],
    ) -> int:
        """Create DOCUMENTED_IN relationships from entities to document.

        This enables graph traversal to find relevant documentation.

        Args:
            document_id: Document UUID
            entity_uuids: List of entity UUIDs to link

        Returns:
            Number of relationships created
        """
        if not entity_uuids:
            return 0

        # Create relationships in graph
        created = 0
        for entity_uuid in entity_uuids:
            try:
                query = """
                MATCH (e)
                WHERE (e:Episodic OR e:Entity) AND e.uuid = $entity_uuid
                MERGE (d:Document {uuid: $doc_uuid})
                MERGE (e)-[r:DOCUMENTED_IN]->(d)
                SET r.created_at = timestamp()
                RETURN count(r) as count
                """

                await self.graph_client.execute_write(
                    query,
                    entity_uuid=entity_uuid,
                    doc_uuid=str(document_id),
                )
                created += 1

            except Exception as e:
                log.warning(
                    "Failed to create doc relationship",
                    entity_uuid=entity_uuid,
                    doc_uuid=str(document_id),
                    error=str(e),
                )

        return created


# =============================================================================
# Convenience Functions
# =============================================================================


async def integrate_document_with_graph(
    _document_id: UUID,
    chunks: list[DocumentChunk],
    source_name: str,
) -> IntegrationStats:
    """Convenience function to integrate a document with the knowledge graph.

    Args:
        _document_id: Document UUID (reserved for future use)
        chunks: Document chunks
        source_name: Source name for logging

    Returns:
        IntegrationStats
    """
    from sibyl.graph.client import get_graph_client

    try:
        graph_client = await get_graph_client()
    except Exception as e:
        log.warning("Graph not available for integration", error=str(e))
        return IntegrationStats()

    service = GraphIntegrationService(graph_client)
    return await service.process_chunks(chunks, source_name)
