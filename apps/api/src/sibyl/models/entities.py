"""Entity and relationship models for the knowledge graph."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EntityType(StrEnum):
    """Types of entities in the knowledge graph."""

    PATTERN = "pattern"
    RULE = "rule"
    TEMPLATE = "template"
    CONVENTION = "convention"  # Coding conventions, style guides, best practices
    TOOL = "tool"
    LANGUAGE = "language"
    TOPIC = "topic"
    EPISODE = "episode"
    KNOWLEDGE_SOURCE = "knowledge_source"
    CONFIG_FILE = "config_file"
    SLASH_COMMAND = "slash_command"

    # Task management types
    PROJECT = "project"
    EPIC = "epic"  # Feature initiative grouping tasks
    TASK = "task"
    TEAM = "team"
    ERROR_PATTERN = "error_pattern"
    MILESTONE = "milestone"

    # Documentation crawling types
    SOURCE = "source"  # A crawlable documentation source (URL, repo, local)
    DOCUMENT = "document"  # A crawled document/page from a source

    # Graph-RAG types
    COMMUNITY = "community"  # Entity cluster from community detection


class RelationshipType(StrEnum):
    """Types of relationships between entities."""

    # Existing knowledge relationships
    APPLIES_TO = "APPLIES_TO"
    REQUIRES = "REQUIRES"
    CONFLICTS_WITH = "CONFLICTS_WITH"
    SUPERSEDES = "SUPERSEDES"
    DOCUMENTED_IN = "DOCUMENTED_IN"
    ENABLES = "ENABLES"
    BREAKS = "BREAKS"
    PART_OF = "PART_OF"
    RELATED_TO = "RELATED_TO"
    DERIVED_FROM = "DERIVED_FROM"

    # Task management relationships
    BELONGS_TO = "BELONGS_TO"  # Task -> Project, Epic -> Project
    CONTAINS = "CONTAINS"  # Project -> Epic, Epic -> Task
    DEPENDS_ON = "DEPENDS_ON"  # Task -> Task (blocking)
    BLOCKS = "BLOCKS"  # Task -> Task (inverse of DEPENDS_ON)
    ASSIGNED_TO = "ASSIGNED_TO"  # Task -> Person
    MEMBER_OF = "MEMBER_OF"  # Person -> Team
    OWNS = "OWNS"  # Team -> Project
    INVOLVES = "INVOLVES"  # Project -> Topic/Domain
    REFERENCES = "REFERENCES"  # Task -> Pattern/Rule/Template
    ENCOUNTERED = "ENCOUNTERED"  # Task -> ErrorPattern
    IMPLEMENTED = "IMPLEMENTED"  # Task -> Pattern/Feature
    VALIDATED_BY = "VALIDATED_BY"  # Task -> Rule (verified compliance)

    # Documentation crawling relationships
    CRAWLED_FROM = "CRAWLED_FROM"  # Document -> Source
    CHILD_OF = "CHILD_OF"  # Document -> Document (page hierarchy)
    MENTIONS = "MENTIONS"  # Document -> Entity (extracted reference)


class Entity(BaseModel):
    """Base entity model for all knowledge graph nodes."""

    id: str = Field(description="Unique identifier for the entity")
    entity_type: EntityType = Field(description="Type of the entity")
    name: str = Field(description="Human-readable name")
    description: str = Field(default="", description="Detailed description")
    content: str = Field(default="", description="Full content/body")
    organization_id: str | None = Field(
        default=None,
        description="Organization/tenant id for scoping (UUID as string)",
    )
    created_by: str | None = Field(
        default=None,
        description="Creator identity (user id/email) when known",
    )
    modified_by: str | None = Field(
        default=None,
        description="Last modifier identity (user id/email) when known",
    )
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_file: str | None = Field(default=None, description="Source file path")
    embedding: list[float] | None = Field(default=None, description="Vector embedding")


class Pattern(Entity):
    """A reusable development pattern or practice."""

    entity_type: EntityType = EntityType.PATTERN
    category: str = Field(default="", description="Pattern category (e.g., 'error-handling')")
    languages: list[str] = Field(default_factory=list, description="Applicable languages")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="Confidence score")


class Rule(Entity):
    """A sacred rule or invariant that must be followed."""

    entity_type: EntityType = EntityType.RULE
    severity: str = Field(default="error", description="Violation severity: error, warning, info")
    enforcement: str = Field(default="manual", description="How the rule is enforced")
    exceptions: list[str] = Field(default_factory=list, description="Known exceptions")


class Template(Entity):
    """A code or configuration template."""

    entity_type: EntityType = EntityType.TEMPLATE
    template_type: str = Field(default="code", description="Type: code, config, project, etc.")
    file_extension: str = Field(default="", description="Expected file extension")
    variables: list[str] = Field(default_factory=list, description="Template variables")


class Tool(Entity):
    """A development tool or utility."""

    entity_type: EntityType = EntityType.TOOL
    tool_type: str = Field(default="cli", description="Type: cli, library, service, etc.")
    installation: str = Field(default="", description="Installation instructions")
    version: str = Field(default="", description="Recommended version")


class Language(Entity):
    """A programming language with its conventions."""

    entity_type: EntityType = EntityType.LANGUAGE
    ecosystem: str = Field(default="", description="Package ecosystem (npm, pip, cargo, etc.)")
    style_guide: str = Field(default="", description="Reference to style guide")


class Topic(Entity):
    """A high-level topic or concept."""

    entity_type: EntityType = EntityType.TOPIC
    parent_topic: str | None = Field(default=None, description="Parent topic for hierarchy")


class Episode(Entity):
    """A temporal knowledge episode (Graphiti concept)."""

    entity_type: EntityType = EntityType.EPISODE
    episode_type: str = Field(default="wisdom", description="Type of episode")
    source_url: str | None = Field(default=None, description="Original source URL")
    valid_from: datetime | None = Field(
        default=None, description="When this knowledge became valid"
    )
    valid_to: datetime | None = Field(
        default=None, description="When this knowledge was superseded"
    )


class KnowledgeSource(Entity):
    """A source document that contains knowledge."""

    entity_type: EntityType = EntityType.KNOWLEDGE_SOURCE
    source_type: str = Field(default="markdown", description="Type: markdown, yaml, json, etc.")
    file_path: str = Field(default="", description="Path to the source file")
    word_count: int = Field(default=0, description="Word count of the source")
    last_ingested: datetime | None = Field(default=None, description="Last ingestion timestamp")


class ConfigFile(Entity):
    """A configuration file template or example."""

    entity_type: EntityType = EntityType.CONFIG_FILE
    config_type: str = Field(default="", description="Type: pyproject, tsconfig, docker, etc.")
    file_name: str = Field(default="", description="Expected filename")
    required_fields: list[str] = Field(
        default_factory=list, description="Required configuration fields"
    )


class SlashCommand(Entity):
    """A Claude Code slash command definition."""

    entity_type: EntityType = EntityType.SLASH_COMMAND
    command_name: str = Field(default="", description="Command name without slash")
    trigger: str = Field(default="", description="Full trigger pattern")
    agent_type: str | None = Field(default=None, description="Associated agent type if any")


class Relationship(BaseModel):
    """A relationship between two entities."""

    id: str = Field(description="Unique identifier for the relationship")
    relationship_type: RelationshipType = Field(description="Type of relationship")
    source_id: str = Field(description="Source entity ID")
    target_id: str = Field(description="Target entity ID")
    weight: float = Field(default=1.0, ge=0.0, description="Relationship strength")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
