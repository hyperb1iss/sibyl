"""Content ingestion pipeline for the knowledge graph.

This module provides tools for:
- Parsing markdown files with frontmatter (parser.py)
- Chunking documents into semantic episodes (chunker.py)
- Extracting entities from content (extractor.py)
- Building relationships between entities (relationships.py)
- Cataloging templates and configs (cataloger.py)
- Running the full pipeline (pipeline.py)
"""

from sibyl.ingestion.cataloger import (
    CatalogedConfig,
    CatalogedSlashCommand,
    CatalogedTemplate,
    TemplateCataloger,
    catalog_repository,
)
from sibyl.ingestion.chunker import (
    ChunkedDocument,
    Episode,
    SemanticChunker,
    chunk_documents,
)
from sibyl.ingestion.extractor import (
    EntityExtractor,
    ExtractedEntity,
    ExtractedEntityType,
    extract_entities_from_episodes,
)
from sibyl.ingestion.parser import (
    CodeBlock,
    MarkdownParser,
    ParsedDocument,
    Section,
    parse_directory,
)
from sibyl.ingestion.pipeline import (
    IngestionPipeline,
    IngestionResult,
    IngestionStats,
    run_ingestion,
)
from sibyl.ingestion.relationships import (
    ExtractedRelationship,
    RelationshipBuilder,
    RelationType,
    build_all_relationships,
)

__all__ = [
    "CatalogedConfig",
    "CatalogedSlashCommand",
    "CatalogedTemplate",
    "ChunkedDocument",
    "CodeBlock",
    "EntityExtractor",
    "Episode",
    "ExtractedEntity",
    "ExtractedEntityType",
    "ExtractedRelationship",
    "IngestionPipeline",
    "IngestionResult",
    "IngestionStats",
    "MarkdownParser",
    "ParsedDocument",
    "RelationshipBuilder",
    "RelationType",
    "Section",
    "SemanticChunker",
    "TemplateCataloger",
    "build_all_relationships",
    "catalog_repository",
    "chunk_documents",
    "extract_entities_from_episodes",
    "parse_directory",
    "run_ingestion",
]
