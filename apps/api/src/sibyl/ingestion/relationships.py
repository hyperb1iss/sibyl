"""Relationship builder for connecting entities in the knowledge graph."""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from sibyl.ingestion.chunker import Episode
from sibyl.ingestion.extractor import ExtractedEntity


class RelationType(StrEnum):
    """Types of relationships between entities."""

    APPLIES_TO = "APPLIES_TO"  # Pattern applies to language
    REQUIRES = "REQUIRES"  # Entity requires another
    CONFLICTS_WITH = "CONFLICTS_WITH"  # Entities are incompatible
    SUPERSEDES = "SUPERSEDES"  # Newer replaces older
    DOCUMENTED_IN = "DOCUMENTED_IN"  # Entity documented in episode
    RELATED_TO = "RELATED_TO"  # General association
    PART_OF = "PART_OF"  # Hierarchical relationship
    ENABLES = "ENABLES"  # Tool enables pattern
    WARNS_ABOUT = "WARNS_ABOUT"  # Rule warns about practice


@dataclass
class ExtractedRelationship:
    """A relationship extracted from content."""

    source_name: str
    target_name: str
    relation_type: RelationType
    confidence: float
    source_episode_id: str
    evidence: str  # Text that supports this relationship


class RelationshipBuilder:
    """Builds relationships between entities based on content analysis.

    Identifies relationships through:
    - Co-occurrence in the same episode
    - Explicit references in text
    - Hierarchical structure (sections/subsections)
    - Cross-references to other documents
    """

    # Patterns for explicit relationships
    REQUIRES_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"requires?\s+(\w+)", re.IGNORECASE),
        re.compile(r"depends?\s+on\s+(\w+)", re.IGNORECASE),
        re.compile(r"needs?\s+(\w+)", re.IGNORECASE),
    ]

    CONFLICTS_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"conflicts?\s+with\s+(\w+)", re.IGNORECASE),
        re.compile(r"incompatible\s+with\s+(\w+)", re.IGNORECASE),
        re.compile(r"don't\s+use\s+(?:with\s+)?(\w+)", re.IGNORECASE),
    ]

    SUPERSEDES_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"supersedes?\s+(\w+)", re.IGNORECASE),
        re.compile(r"replaces?\s+(\w+)", re.IGNORECASE),
        re.compile(r"deprecated\s+in\s+favor\s+of\s+(\w+)", re.IGNORECASE),
    ]

    def __init__(self) -> None:
        """Initialize the relationship builder."""

    def build_relationships(
        self,
        entities: list[ExtractedEntity],
        episodes: list[Episode],
    ) -> list[ExtractedRelationship]:
        """Build relationships between entities.

        Args:
            entities: Extracted entities.
            episodes: Source episodes.

        Returns:
            List of relationships.
        """
        relationships: list[ExtractedRelationship] = []

        # Build episode lookup
        episode_map = {ep.id: ep for ep in episodes}

        # Group entities by episode
        entities_by_episode: dict[str, list[ExtractedEntity]] = {}
        for entity in entities:
            ep_id = entity.source_episode_id
            if ep_id not in entities_by_episode:
                entities_by_episode[ep_id] = []
            entities_by_episode[ep_id].append(entity)

        # Build co-occurrence relationships
        for ep_id, ep_entities in entities_by_episode.items():
            episode = episode_map.get(ep_id)
            if not episode:
                continue

            # DOCUMENTED_IN relationships
            relationships.extend(
                ExtractedRelationship(
                    source_name=entity.name,
                    target_name=episode.title,
                    relation_type=RelationType.DOCUMENTED_IN,
                    confidence=1.0,
                    source_episode_id=ep_id,
                    evidence=f"Entity '{entity.name}' extracted from episode '{episode.title}'",
                )
                for entity in ep_entities
            )

            # APPLIES_TO relationships (patterns/rules to languages)
            languages = [e for e in ep_entities if e.entity_type.value == "language"]
            patterns_rules = [
                e
                for e in ep_entities
                if e.entity_type.value in ("pattern", "rule", "tip", "warning")
            ]

            relationships.extend(
                ExtractedRelationship(
                    source_name=pr.name,
                    target_name=lang.name,
                    relation_type=RelationType.APPLIES_TO,
                    confidence=0.8,
                    source_episode_id=ep_id,
                    evidence=f"'{pr.name}' co-occurs with {lang.name} in '{episode.title}'",
                )
                for pr in patterns_rules
                for lang in languages
            )

            # ENABLES relationships (tools to patterns)
            tools = [e for e in ep_entities if e.entity_type.value == "tool"]
            patterns = [e for e in ep_entities if e.entity_type.value in ("pattern", "tip")]

            relationships.extend(
                ExtractedRelationship(
                    source_name=tool.name,
                    target_name=pattern.name,
                    relation_type=RelationType.ENABLES,
                    confidence=0.6,
                    source_episode_id=ep_id,
                    evidence=f"Tool '{tool.name}' mentioned with pattern in '{episode.title}'",
                )
                for tool in tools
                for pattern in patterns
            )

        # Extract explicit relationships from text
        for episode in episodes:
            explicit_rels = self._extract_explicit_relationships(episode)
            relationships.extend(explicit_rels)

        # Build hierarchical relationships from episode structure
        for episode in episodes:
            if episode.parent_id:
                parent = episode_map.get(episode.parent_id)
                if parent:
                    relationships.append(
                        ExtractedRelationship(
                            source_name=episode.title,
                            target_name=parent.title,
                            relation_type=RelationType.PART_OF,
                            confidence=1.0,
                            source_episode_id=episode.id,
                            evidence=f"'{episode.title}' is subsection of '{parent.title}'",
                        )
                    )

        return relationships

    def _extract_explicit_relationships(
        self,
        episode: Episode,
    ) -> list[ExtractedRelationship]:
        """Extract explicitly stated relationships from episode text.

        Args:
            episode: Episode to analyze.

        Returns:
            List of explicit relationships.
        """
        relationships: list[ExtractedRelationship] = []
        content = episode.content

        # Check for REQUIRES patterns
        for pattern in self.REQUIRES_PATTERNS:
            for match in pattern.finditer(content):
                target = match.group(1)
                relationships.append(
                    ExtractedRelationship(
                        source_name=episode.title,
                        target_name=target,
                        relation_type=RelationType.REQUIRES,
                        confidence=0.85,
                        source_episode_id=episode.id,
                        evidence=match.group(0),
                    )
                )

        # Check for CONFLICTS patterns
        for pattern in self.CONFLICTS_PATTERNS:
            for match in pattern.finditer(content):
                target = match.group(1)
                relationships.append(
                    ExtractedRelationship(
                        source_name=episode.title,
                        target_name=target,
                        relation_type=RelationType.CONFLICTS_WITH,
                        confidence=0.8,
                        source_episode_id=episode.id,
                        evidence=match.group(0),
                    )
                )

        # Check for SUPERSEDES patterns
        for pattern in self.SUPERSEDES_PATTERNS:
            for match in pattern.finditer(content):
                target = match.group(1)
                relationships.append(
                    ExtractedRelationship(
                        source_name=episode.title,
                        target_name=target,
                        relation_type=RelationType.SUPERSEDES,
                        confidence=0.9,
                        source_episode_id=episode.id,
                        evidence=match.group(0),
                    )
                )

        return relationships


def build_all_relationships(
    entities: list[ExtractedEntity],
    episodes: list[Episode],
) -> list[ExtractedRelationship]:
    """Build all relationships between entities and episodes.

    Args:
        entities: All extracted entities.
        episodes: All episodes.

    Returns:
        All relationships.
    """
    builder = RelationshipBuilder()
    return builder.build_relationships(entities, episodes)
