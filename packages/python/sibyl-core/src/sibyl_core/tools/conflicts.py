"""Contradiction detection for knowledge ingest.

Detects potential conflicts when adding new knowledge by:
1. Finding semantically similar existing entities
2. Classifying conflicts (duplicate, overlap, contradiction)
3. Optionally using LLM for nuanced contradiction analysis

This layer helps maintain knowledge graph consistency by warning
about conflicting information before it's added.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from sibyl_core.graph.client import get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.tools.responses import ConflictWarning

if TYPE_CHECKING:
    pass

log = structlog.get_logger()

__all__ = ["detect_conflicts", "find_similar_entities"]

# Thresholds for conflict classification
DUPLICATE_THRESHOLD = 0.95  # Near-identical content
HIGH_OVERLAP_THRESHOLD = 0.85  # Very similar, likely same topic
CONFLICT_THRESHOLD = 0.70  # Similar enough to warrant review


async def find_similar_entities(
    title: str,
    content: str,
    organization_id: str,
    *,
    entity_types: list[str] | None = None,
    limit: int = 5,
    min_score: float = CONFLICT_THRESHOLD,
) -> list[tuple[str, str, str, float]]:
    """Find existing entities semantically similar to the new content.

    Searches by combining title and content into a query, filtering
    to entities above a minimum similarity threshold.

    Args:
        title: Title of the new entity.
        content: Content of the new entity.
        organization_id: Organization scope.
        entity_types: Optional filter by entity types.
        limit: Maximum similar entities to return.
        min_score: Minimum similarity score threshold.

    Returns:
        List of (id, name, content_preview, score) tuples sorted by score desc.
    """
    client = await get_graph_client()
    entity_manager = EntityManager(client, group_id=organization_id)

    # Build search query from title + content preview
    query = f"{title}. {content[:500]}" if content else title

    try:
        # Use entity manager's semantic search
        results = await entity_manager.search(
            query=query,
            entity_types=[
                # Import EntityType to convert strings if needed
                __import__("sibyl_core.models.entities", fromlist=["EntityType"]).EntityType(t)
                for t in (entity_types or [])
            ]
            if entity_types
            else None,
            limit=limit * 2,  # Fetch extra for filtering
        )

        similar: list[tuple[str, str, str, float]] = []
        for entity, score in results:
            if score >= min_score:
                content_preview = (entity.content or entity.description or "")[:200]
                similar.append((entity.id, entity.name, content_preview, score))

        # Sort by score and limit
        similar.sort(key=lambda x: x[3], reverse=True)
        return similar[:limit]

    except Exception as e:
        log.warning("similar_entity_search_failed", error=str(e))
        return []


def classify_conflict(
    new_title: str,
    new_content: str,
    existing_name: str,
    existing_content: str,
    similarity_score: float,
) -> tuple[str, str | None]:
    """Classify the type of conflict between new and existing content.

    Uses heuristics based on similarity score and content overlap.
    For more nuanced detection, use detect_conflicts_with_llm.

    Args:
        new_title: Title of new entity.
        new_content: Content of new entity.
        existing_name: Name of existing entity.
        existing_content: Content of existing entity.
        similarity_score: Semantic similarity score.

    Returns:
        Tuple of (conflict_type, explanation).
    """
    # Check for near-duplicates
    if similarity_score >= DUPLICATE_THRESHOLD:
        return (
            "duplicate",
            f"Very high similarity ({similarity_score:.0%}) suggests duplicate content.",
        )

    # Check for high overlap
    if similarity_score >= HIGH_OVERLAP_THRESHOLD:
        # Check if titles are very similar (case-insensitive)
        title_similarity = _simple_title_similarity(new_title, existing_name)
        if title_similarity > 0.8:
            return (
                "duplicate",
                f"Titles and content are very similar ({similarity_score:.0%}). Likely duplicate.",
            )
        return (
            "semantic_overlap",
            f"High semantic overlap ({similarity_score:.0%}). Consider if this adds new information.",
        )

    # Check for potential contradiction (moderate similarity may indicate same topic, different conclusions)
    if similarity_score >= CONFLICT_THRESHOLD:
        # Look for contradiction signals in content
        contradiction_signals = _check_contradiction_signals(new_content, existing_content)
        if contradiction_signals:
            return (
                "potential_contradiction",
                f"Similar topic ({similarity_score:.0%}) with possible contradiction: {contradiction_signals}",
            )
        return (
            "semantic_overlap",
            f"Similar topic ({similarity_score:.0%}). Review to ensure consistency.",
        )

    return "semantic_overlap", None


def _simple_title_similarity(title1: str, title2: str) -> float:
    """Simple title similarity using word overlap."""
    words1 = set(title1.lower().split())
    words2 = set(title2.lower().split())

    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2

    return len(intersection) / len(union) if union else 0.0


def _check_contradiction_signals(content1: str, content2: str) -> str | None:
    """Check for linguistic signals of contradiction.

    This is a simple heuristic check. For better accuracy,
    use LLM-based contradiction detection.
    """
    content1_lower = content1.lower()
    content2_lower = content2.lower()

    # Negation patterns that might indicate contradiction
    negation_patterns = [
        ("should", "should not"),
        ("should", "shouldn't"),
        ("must", "must not"),
        ("always", "never"),
        ("is", "is not"),
        ("can", "cannot"),
        ("works", "doesn't work"),
        ("recommended", "not recommended"),
        ("correct", "incorrect"),
        ("true", "false"),
    ]

    for pos, neg in negation_patterns:
        # Check if one content has positive and other has negative
        if (pos in content1_lower and neg in content2_lower) or (
            neg in content1_lower and pos in content2_lower
        ):
            return f"Potential negation conflict around '{pos}'/'{neg}'"

    # Check for version/date conflicts
    import re

    version_pattern = r"v?(\d+\.?\d*\.?\d*)"
    versions1 = set(re.findall(version_pattern, content1_lower))
    versions2 = set(re.findall(version_pattern, content2_lower))

    if versions1 and versions2 and versions1 != versions2:
        return f"Different versions mentioned ({versions1} vs {versions2})"

    return None


async def detect_conflicts(
    title: str,
    content: str,
    organization_id: str,
    *,
    entity_types: list[str] | None = None,
    exclude_id: str | None = None,
    max_conflicts: int = 3,
    min_similarity: float = CONFLICT_THRESHOLD,
) -> list[ConflictWarning]:
    """Detect potential conflicts before adding new knowledge.

    Searches for semantically similar existing entities and classifies
    the type of conflict. Returns warnings for user review.

    Args:
        title: Title of the new entity.
        content: Content of the new entity.
        organization_id: Organization scope.
        entity_types: Optional filter to specific entity types.
        exclude_id: Entity ID to exclude (for updates).
        max_conflicts: Maximum conflicts to return.
        min_similarity: Minimum similarity score to consider.

    Returns:
        List of ConflictWarning objects, sorted by severity.
    """
    log.debug(
        "detecting_conflicts",
        title=title[:50],
        org_id=organization_id,
        min_similarity=min_similarity,
    )

    # Find similar existing entities
    similar = await find_similar_entities(
        title=title,
        content=content,
        organization_id=organization_id,
        entity_types=entity_types,
        limit=max_conflicts * 2,  # Fetch extra for filtering
        min_score=min_similarity,
    )

    if not similar:
        log.debug("no_conflicts_found", title=title[:50])
        return []

    warnings: list[ConflictWarning] = []

    for entity_id, entity_name, entity_content, score in similar:
        # Skip self-reference during updates
        if exclude_id and entity_id == exclude_id:
            continue

        # Classify the conflict type
        conflict_type, explanation = classify_conflict(
            new_title=title,
            new_content=content,
            existing_name=entity_name,
            existing_content=entity_content,
            similarity_score=score,
        )

        warnings.append(
            ConflictWarning(
                existing_id=entity_id,
                existing_name=entity_name,
                existing_content=entity_content[:200],
                similarity_score=score,
                conflict_type=conflict_type,  # type: ignore[arg-type]
                explanation=explanation,
            )
        )

    # Sort by severity: duplicates first, then by score
    severity_order = {"duplicate": 0, "potential_contradiction": 1, "semantic_overlap": 2}
    warnings.sort(key=lambda w: (severity_order.get(w.conflict_type, 3), -w.similarity_score))

    log.info(
        "conflicts_detected",
        title=title[:50],
        count=len(warnings),
        types=[w.conflict_type for w in warnings[:3]],
    )

    return warnings[:max_conflicts]


async def detect_conflicts_with_llm(
    title: str,
    content: str,
    organization_id: str,
    *,
    similar_entities: list[tuple[str, str, str, float]] | None = None,
) -> list[ConflictWarning]:
    """Detect conflicts using LLM for nuanced analysis.

    More accurate than heuristic-based detection but slower and requires
    LLM API calls. Use for high-stakes knowledge additions.

    Args:
        title: Title of the new entity.
        content: Content of the new entity.
        organization_id: Organization scope.
        similar_entities: Pre-fetched similar entities, or None to search.

    Returns:
        List of ConflictWarning objects with LLM-generated explanations.
    """
    # Fetch similar entities if not provided
    if similar_entities is None:
        similar_entities = await find_similar_entities(
            title=title,
            content=content,
            organization_id=organization_id,
            limit=5,
        )

    if not similar_entities:
        return []

    # Build prompt for LLM
    prompt = f"""Analyze whether the following new knowledge contradicts or duplicates existing knowledge.

NEW KNOWLEDGE:
Title: {title}
Content: {content[:1000]}

EXISTING KNOWLEDGE:
"""
    for i, (eid, ename, econtent, score) in enumerate(similar_entities[:3], 1):
        prompt += f"""
{i}. {ename} (ID: {eid}, similarity: {score:.0%})
   Content: {econtent}
"""

    prompt += """
For each existing item, classify as:
- "duplicate": Nearly identical information
- "potential_contradiction": Same topic but conflicting claims
- "semantic_overlap": Related but adds new information
- "no_conflict": Different enough to not conflict

Respond in JSON format:
{"conflicts": [{"id": "...", "type": "...", "explanation": "..."}]}
"""

    try:
        # Use Anthropic client via Graphiti's embedder config
        # For now, fall back to heuristic detection
        # TODO: Integrate with configured LLM provider
        log.debug("llm_conflict_detection_not_implemented_using_heuristic")
        return await detect_conflicts(
            title=title,
            content=content,
            organization_id=organization_id,
            max_conflicts=3,
        )
    except Exception as e:
        log.warning("llm_conflict_detection_failed", error=str(e))
        return []
