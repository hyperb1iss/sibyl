"""Community summarization using LLM.

Generates searchable summaries for detected communities
using GPT-4o-mini following the GraphRAG approach.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sibyl_core.graph.client import GraphClient

log = structlog.get_logger()


@dataclass
class SummaryConfig:
    """Configuration for community summarization.

    Attributes:
        model: OpenAI model to use for summarization.
        max_members_per_summary: Maximum members to include in prompt.
        max_content_tokens: Approximate max tokens for member content.
        extract_concepts: Whether to extract key concepts.
        max_concepts: Maximum key concepts to extract.
    """

    model: str = "gpt-4o-mini"
    max_members_per_summary: int = 20
    max_content_tokens: int = 4000
    extract_concepts: bool = True
    max_concepts: int = 5


@dataclass
class CommunitySummary:
    """Generated summary for a community.

    Attributes:
        community_id: Community UUID.
        summary: Generated summary text.
        key_concepts: Extracted key concepts/themes.
        representative_entities: Most central entity IDs.
    """

    community_id: str
    summary: str
    key_concepts: list[str] = field(default_factory=list)
    representative_entities: list[str] = field(default_factory=list)


SUMMARY_PROMPT = """You are analyzing a community of related software development knowledge.

The community contains the following entities:

{entities}

Generate a concise summary (2-4 sentences) that:
1. Identifies the main theme or purpose of this community
2. Highlights key patterns, technologies, or concepts
3. Explains how the members relate to each other

Also extract 3-5 key concepts that represent this community.

Respond in JSON format:
{{
    "summary": "Your 2-4 sentence summary here",
    "key_concepts": ["concept1", "concept2", "concept3"]
}}
"""


def format_entity_for_prompt(entity: dict[str, Any], max_chars: int = 500) -> str:
    """Format an entity for inclusion in prompt.

    Args:
        entity: Entity dict with name, type, description.
        max_chars: Maximum characters for description.

    Returns:
        Formatted entity string.
    """
    name = entity.get("name", "Unknown")
    entity_type = entity.get("type", "entity")
    description = entity.get("description", "")

    # Truncate description if too long
    if len(description) > max_chars:
        description = description[:max_chars] + "..."

    return f"- [{entity_type}] {name}: {description}"


async def get_community_content(
    client: GraphClient,
    organization_id: str,
    community_id: str,
    max_members: int = 20,
) -> list[dict[str, Any]]:
    """Fetch member entities for a community.

    Args:
        client: Graph client.
        community_id: Community UUID.
        max_members: Maximum members to fetch.

    Returns:
        List of entity dicts.
    """
    query = """
    MATCH (c:Entity {uuid: $community_id})<-[:BELONGS_TO]-(e:Entity)
    WHERE e.entity_type <> 'community'
    RETURN e.uuid AS id,
           e.name AS name,
           e.entity_type AS type,
           e.description AS description,
           e.content AS content
    LIMIT $limit
    """

    members: list[dict[str, Any]] = []

    try:
        result = await client.execute_read_org(
            query,
            organization_id,
            community_id=community_id,
            limit=max_members,
        )

        for record in result:
            if isinstance(record, (list, tuple)):
                member = {
                    "id": record[0] if len(record) > 0 else None,
                    "name": record[1] if len(record) > 1 else "",
                    "type": record[2] if len(record) > 2 else "",
                    "description": record[3] if len(record) > 3 else "",
                    "content": record[4] if len(record) > 4 else "",
                }
            else:
                member = {
                    "id": record.get("id"),
                    "name": record.get("name", ""),
                    "type": record.get("type", ""),
                    "description": record.get("description", ""),
                    "content": record.get("content", ""),
                }

            if member["id"]:
                members.append(member)

    except Exception as e:
        log.warning("get_community_content_failed", community_id=community_id, error=str(e))

    return members


async def summarize_with_openai(
    entities: list[dict[str, Any]],
    config: SummaryConfig,
) -> CommunitySummary | None:
    """Generate summary using OpenAI API.

    Args:
        entities: List of member entities.
        config: Summary configuration.

    Returns:
        CommunitySummary or None if failed.
    """
    try:
        from openai import AsyncOpenAI
    except ImportError:
        log.warning("openai package required for summarization. Install with: pip install openai")
        return None

    if not entities:
        return None

    # Format entities for prompt
    entity_texts = [format_entity_for_prompt(e) for e in entities]
    entities_str = "\n".join(entity_texts)

    # Build prompt
    prompt = SUMMARY_PROMPT.format(entities=entities_str)

    try:
        client = AsyncOpenAI()

        response = await client.chat.completions.create(
            model=config.model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that analyzes software development knowledge.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=500,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content
        if not content:
            return None

        # Parse JSON response
        data = json.loads(content)

        return CommunitySummary(
            community_id="",  # Will be set by caller
            summary=data.get("summary", ""),
            key_concepts=data.get("key_concepts", [])[: config.max_concepts],
            representative_entities=[e["id"] for e in entities[:3]],
        )

    except Exception as e:
        log.warning("openai_summarization_failed", error=str(e))
        return None


async def generate_community_summary(
    client: GraphClient,
    organization_id: str,
    community_id: str,
    config: SummaryConfig | None = None,
) -> CommunitySummary | None:
    """Generate summary for a single community.

    Args:
        client: Graph client.
        community_id: Community UUID.
        config: Summary configuration.

    Returns:
        CommunitySummary or None if failed.
    """
    if config is None:
        config = SummaryConfig()

    log.debug("generate_community_summary_start", community_id=community_id)

    # Fetch member content
    members = await get_community_content(
        client,
        organization_id,
        community_id,
        max_members=config.max_members_per_summary,
    )

    if not members:
        log.debug("generate_community_summary_no_members", community_id=community_id)
        return None

    # Generate summary with OpenAI
    summary = await summarize_with_openai(members, config)

    if summary:
        summary.community_id = community_id

    return summary


async def store_community_summary(
    client: GraphClient,
    organization_id: str,
    summary: CommunitySummary,
) -> bool:
    """Store summary in community entity.

    Args:
        client: Graph client.
        summary: Generated summary.

    Returns:
        True if stored successfully.
    """
    query = """
    MATCH (c:Entity {uuid: $community_id, entity_type: 'community'})
    SET c.summary = $summary,
        c.key_concepts = $key_concepts,
        c.representative_entities = $representative_entities,
        c.name = $name
    RETURN c.uuid AS id
    """

    # Generate better name from key concepts
    if summary.key_concepts:
        name = ", ".join(summary.key_concepts[:3])
    else:
        name = summary.summary[:50] + "..." if len(summary.summary) > 50 else summary.summary

    try:
        result = await client.execute_write_org(
            query,
            organization_id,
            community_id=summary.community_id,
            summary=summary.summary,
            key_concepts=summary.key_concepts,
            representative_entities=summary.representative_entities,
            name=name,
        )

        success = bool(result)
        if success:
            log.debug("store_community_summary_success", community_id=summary.community_id)
        return success

    except Exception as e:
        log.warning(
            "store_community_summary_failed", community_id=summary.community_id, error=str(e)
        )
        return False


async def generate_community_summaries(
    client: GraphClient,
    organization_id: str,
    community_ids: list[str] | None = None,
    config: SummaryConfig | None = None,
    store: bool = True,
) -> list[CommunitySummary]:
    """Generate summaries for multiple communities.

    Args:
        client: Graph client.
        community_ids: Specific community IDs (or all if None).
        config: Summary configuration.
        store: Whether to store summaries in graph.

    Returns:
        List of generated summaries.
    """
    if config is None:
        config = SummaryConfig()

    log.info("generate_community_summaries_start", community_ids=community_ids)

    # Fetch community IDs if not provided
    if community_ids is None:
        query = """
        MATCH (c:Entity {entity_type: 'community'})
        WHERE c.summary IS NULL OR c.summary = ''
        RETURN c.uuid AS id
        ORDER BY c.level DESC, c.member_count DESC
        """

        try:
            result = await client.execute_read_org(query, organization_id)
            community_ids = []
            for record in result:
                if isinstance(record, (list, tuple)):
                    cid = record[0] if len(record) > 0 else None
                else:
                    cid = record.get("id")
                if cid:
                    community_ids.append(cid)
        except Exception as e:
            log.warning("fetch_communities_failed", error=str(e))
            return []

    # Generate summaries
    summaries: list[CommunitySummary] = []

    for community_id in community_ids:
        summary = await generate_community_summary(client, organization_id, community_id, config)

        if summary:
            if store:
                await store_community_summary(client, organization_id, summary)
            summaries.append(summary)

    log.info(
        "generate_community_summaries_complete",
        total=len(community_ids),
        generated=len(summaries),
    )

    return summaries


async def update_stale_summaries(
    client: GraphClient,
    organization_id: str,
    config: SummaryConfig | None = None,
) -> int:
    """Update summaries for communities with changed members.

    Detects communities where member content has been updated
    since the summary was generated.

    Args:
        client: Graph client.
        config: Summary configuration.

    Returns:
        Number of summaries updated.
    """
    # Find communities with stale summaries
    # (members updated after community last updated)
    query = """
    MATCH (c:Entity {entity_type: 'community'})<-[:BELONGS_TO]-(e:Entity)
    WHERE c.summary IS NOT NULL
      AND e.updated_at > c.updated_at
    WITH DISTINCT c.uuid AS community_id
    RETURN community_id
    """

    stale_ids: list[str] = []

    try:
        result = await client.execute_read_org(query, organization_id)
        for record in result:
            if isinstance(record, (list, tuple)):
                cid = record[0] if len(record) > 0 else None
            else:
                cid = record.get("community_id")
            if cid:
                stale_ids.append(cid)
    except Exception as e:
        log.warning("find_stale_summaries_failed", error=str(e))
        return 0

    if not stale_ids:
        log.info("no_stale_summaries_found")
        return 0

    log.info("update_stale_summaries_start", count=len(stale_ids))

    # Regenerate summaries
    summaries = await generate_community_summaries(
        client,
        organization_id,
        community_ids=stale_ids,
        config=config,
        store=True,
    )

    return len(summaries)
