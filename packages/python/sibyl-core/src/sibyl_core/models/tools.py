"""Input models for tools."""

from pydantic import BaseModel, Field

from sibyl_core.models.entities import EntityType, RelationshipType


class SearchInput(BaseModel):
    """Input for semantic search tools."""

    query: str = Field(description="Natural language search query")
    entity_types: list[EntityType] | None = Field(
        default=None,
        description="Filter by entity types (default: all)",
    )
    languages: list[str] | None = Field(
        default=None,
        description="Filter by programming languages",
    )
    limit: int = Field(default=10, ge=1, le=50, description="Maximum results to return")
    include_content: bool = Field(
        default=True,
        description="Include full content in results",
    )


class GetTemplateInput(BaseModel):
    """Input for template retrieval."""

    template_name: str = Field(description="Name or identifier of the template")
    template_type: str | None = Field(
        default=None,
        description="Filter by template type (code, config, project)",
    )
    language: str | None = Field(
        default=None,
        description="Filter by programming language",
    )


class GetLanguageGuideInput(BaseModel):
    """Input for language-specific guide retrieval."""

    language: str = Field(description="Programming language (e.g., 'python', 'typescript')")
    topic: str | None = Field(
        default=None,
        description="Specific topic within the language guide",
    )


class GetRelatedInput(BaseModel):
    """Input for finding related entities."""

    entity_id: str = Field(description="ID of the entity to find relations for")
    relationship_types: list[RelationshipType] | None = Field(
        default=None,
        description="Filter by relationship types",
    )
    depth: int = Field(
        default=1,
        ge=1,
        le=3,
        description="Traversal depth (1-3)",
    )
    limit: int = Field(default=20, ge=1, le=100, description="Maximum results")


class ListEntitiesInput(BaseModel):
    """Input for listing entities."""

    entity_type: EntityType = Field(description="Type of entities to list")
    category: str | None = Field(default=None, description="Filter by category")
    language: str | None = Field(default=None, description="Filter by language")
    limit: int = Field(default=50, ge=1, le=200, description="Maximum results")
    offset: int = Field(default=0, ge=0, description="Pagination offset")


class AddLearningInput(BaseModel):
    """Input for adding new learning/wisdom."""

    title: str = Field(description="Title of the learning")
    content: str = Field(description="Detailed content/description")
    category: str = Field(description="Category (e.g., 'debugging', 'architecture')")
    languages: list[str] | None = Field(
        default=None,
        description="Applicable programming languages",
    )
    related_to: list[str] | None = Field(
        default=None,
        description="IDs of related entities",
    )
    source: str | None = Field(
        default=None,
        description="Source of the learning (e.g., 'debugging-session')",
    )


class RecordDebuggingInput(BaseModel):
    """Input for recording a debugging victory."""

    problem: str = Field(description="Description of the problem encountered")
    root_cause: str = Field(description="The root cause that was discovered")
    solution: str = Field(description="How the problem was solved")
    prevention: str | None = Field(
        default=None,
        description="How to prevent this in the future",
    )
    languages: list[str] | None = Field(
        default=None,
        description="Programming languages involved",
    )
    tools: list[str] | None = Field(
        default=None,
        description="Tools involved in the debugging",
    )
    time_spent: str | None = Field(
        default=None,
        description="Approximate time spent debugging",
    )
