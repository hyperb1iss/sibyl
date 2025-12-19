"""Mutation tools for the Conventions MCP Server.

Provides tools for adding new knowledge to the graph.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from sibyl.graph.client import get_graph_client
from sibyl.graph.entities import EntityManager
from sibyl.models.entities import EntityType, Episode, Pattern

log = structlog.get_logger()


# Input validation constants
MAX_TITLE_LENGTH = 200
MAX_CONTENT_LENGTH = 50000
MAX_CATEGORY_LENGTH = 50


@dataclass
class MutationResult:
    """Result of a mutation operation."""

    success: bool
    entity_id: str | None
    message: str
    timestamp: datetime


def _validate_input(title: str, content: str, category: str) -> str | None:
    """Validate input parameters for mutation operations.

    Args:
        title: Title to validate.
        content: Content to validate.
        category: Category to validate.

    Returns:
        Error message if validation fails, None if valid.
    """
    if not title or not title.strip():
        return "Title cannot be empty"
    if len(title) > MAX_TITLE_LENGTH:
        return f"Title exceeds maximum length of {MAX_TITLE_LENGTH} characters"

    if not content or not content.strip():
        return "Content cannot be empty"
    if len(content) > MAX_CONTENT_LENGTH:
        return f"Content exceeds maximum length of {MAX_CONTENT_LENGTH} characters"

    if not category or not category.strip():
        return "Category cannot be empty"
    if len(category) > MAX_CATEGORY_LENGTH:
        return f"Category exceeds maximum length of {MAX_CATEGORY_LENGTH} characters"

    return None


async def add_learning(
    title: str,
    content: str,
    category: str,
    languages: list[str] | None = None,
    _related_to: list[str] | None = None,  # TODO: Implement relationship creation
    source: str | None = None,
) -> MutationResult:
    """Add a new piece of wisdom or learning to the graph.

    Creates a new Episode entity with the provided content and
    optionally links it to related entities.

    Args:
        title: Title of the learning.
        content: Detailed content/description.
        category: Category (e.g., "debugging", "architecture", "performance").
        languages: Applicable programming languages.
        related_to: IDs of related entities to link.
        source: Source of the learning (e.g., "debugging-session", "code-review").

    Returns:
        MutationResult indicating success or failure.
    """
    # Sanitize inputs
    title = title.strip()
    content = content.strip()
    category = category.strip()

    # Validate inputs
    validation_error = _validate_input(title, content, category)
    if validation_error:
        return MutationResult(
            success=False,
            entity_id=None,
            message=f"Validation error: {validation_error}",
            timestamp=datetime.now(UTC),
        )

    log.info(
        "Adding new learning",
        title=title,
        category=category,
        languages=languages,
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Generate ID
        entity_id = _generate_id("learning", title, category)

        # Create the episode entity
        episode = Episode(
            id=entity_id,
            entity_type=EntityType.EPISODE,
            name=title,
            description=content[:500] if len(content) > 500 else content,
            content=content,
            metadata={
                "category": category,
                "languages": languages or [],
                "source": source,
                "added_at": datetime.now(UTC).isoformat(),
            },
        )

        # Store in graph
        created_id = await entity_manager.create(episode)

        # TODO: Create relationships to related_to entities
        # This would use relationship_manager.create() for each

        return MutationResult(
            success=True,
            entity_id=created_id,
            message=f"Successfully added learning: {title}",
            timestamp=datetime.now(UTC),
        )

    except Exception as e:
        log.exception("Failed to add learning", error=str(e))
        return MutationResult(
            success=False,
            entity_id=None,
            message=f"Failed to add learning: {e}",
            timestamp=datetime.now(UTC),
        )


async def record_debugging_victory(
    problem: str,
    root_cause: str,
    solution: str,
    prevention: str | None = None,
    languages: list[str] | None = None,
    tools: list[str] | None = None,
    time_spent: str | None = None,
) -> MutationResult:
    """Record a debugging victory with full context.

    Creates a structured debugging victory entry that can be
    searched and referenced for similar problems in the future.

    Args:
        problem: Description of the problem encountered.
        root_cause: The root cause that was discovered.
        solution: How the problem was solved.
        prevention: How to prevent this in the future.
        languages: Programming languages involved.
        tools: Tools involved in debugging or the fix.
        time_spent: Approximate time spent debugging (e.g., "2 hours").

    Returns:
        MutationResult indicating success or failure.
    """
    # Sanitize inputs
    problem = problem.strip()
    root_cause = root_cause.strip()
    solution = solution.strip()

    # Validate required fields (using problem as title, combined content for validation)
    combined_content = f"{problem}\n{root_cause}\n{solution}"
    if prevention:
        combined_content += f"\n{prevention.strip()}"

    validation_error = _validate_input(problem, combined_content, "debugging")
    if validation_error:
        return MutationResult(
            success=False,
            entity_id=None,
            message=f"Validation error: {validation_error}",
            timestamp=datetime.now(UTC),
        )

    log.info(
        "Recording debugging victory",
        problem=problem[:100],
        languages=languages,
    )

    try:
        client = await get_graph_client()
        entity_manager = EntityManager(client)

        # Generate ID
        entity_id = _generate_id("debug", problem, root_cause)

        # Build structured content
        content_parts = [
            f"## Problem\n{problem}",
            f"\n## Root Cause\n{root_cause}",
            f"\n## Solution\n{solution}",
        ]
        if prevention:
            content_parts.append(f"\n## Prevention\n{prevention.strip()}")
        if time_spent:
            content_parts.append(f"\n## Time Spent\n{time_spent}")

        full_content = "\n".join(content_parts)

        # Create as a Pattern entity (for searchability)
        pattern = Pattern(
            id=entity_id,
            entity_type=EntityType.PATTERN,
            name=f"Debug: {problem[:50]}{'...' if len(problem) > 50 else ''}",
            description=f"Root cause: {root_cause[:200]}",
            content=full_content,
            category="debugging",
            languages=languages or [],
            metadata={
                "type": "debugging_victory",
                "problem": problem,
                "root_cause": root_cause,
                "solution": solution,
                "prevention": prevention,
                "tools": tools or [],
                "time_spent": time_spent,
                "recorded_at": datetime.now(UTC).isoformat(),
            },
        )

        created_id = await entity_manager.create(pattern)

        return MutationResult(
            success=True,
            entity_id=created_id,
            message="Successfully recorded debugging victory",
            timestamp=datetime.now(UTC),
        )

    except Exception as e:
        log.exception("Failed to record debugging victory", error=str(e))
        return MutationResult(
            success=False,
            entity_id=None,
            message=f"Failed to record: {e}",
            timestamp=datetime.now(UTC),
        )


def _generate_id(prefix: str, *parts: str) -> str:
    """Generate a deterministic entity ID.

    Args:
        prefix: ID prefix (e.g., "learning", "debug").
        parts: Components to hash for uniqueness.

    Returns:
        Short hash-based ID.
    """
    combined = ":".join(str(p)[:100] for p in parts)  # Limit each part
    hash_bytes = hashlib.sha256(combined.encode()).hexdigest()[:12]
    return f"{prefix}_{hash_bytes}"
