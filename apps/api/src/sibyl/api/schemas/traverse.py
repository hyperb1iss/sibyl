"""Bounded traversal verb request/response models.

The bounds are the core clamps, imported rather than restated, so a budget change
lands in one place and cannot drift between the HTTP contract and the walk that
honors it.

They are documented rather than enforced as validation, because the MCP tools and
these routes are one verb with one contract. A tool signature cannot express a
range, so MCP clamps; rejecting here would mean the same request that succeeds
for an agent 422s for a script. Both surfaces clamp, and every response reports
the bound it actually applied.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

from sibyl_core.tools.traverse import (
    DEFAULT_EXPAND_LIMIT,
    DEFAULT_NEIGHBOR_CONTENT_MAX_CHARS,
    DEFAULT_SLICE_CONTENT_MAX_CHARS,
    DEFAULT_SLICE_WINDOW,
    DEFAULT_TRAVERSAL_DEPTH,
    MAX_EXPAND_LIMIT,
    MAX_EXPAND_ORIGINS,
    MAX_SLICE_WINDOW,
    MAX_TRAVERSAL_CONTENT_MAX_CHARS,
    MAX_TRAVERSAL_DEPTH,
)


class ExpandNeighborsRequest(BaseModel):
    """One bounded neighbor-expansion step over the graph."""

    entity_ids: list[str] = Field(
        ...,
        min_length=1,
        description=(
            f"Seed entity IDs to expand from. At most {MAX_EXPAND_ORIGINS} are walked; "
            "the remainder come back in `unresolved`."
        ),
    )
    relationship_types: list[str] | None = Field(
        default=None,
        description="Restrict hops to these relationship names (DEPENDS_ON, PART_OF, ...)",
    )
    types: list[str] | None = Field(
        default=None, description="Restrict neighbors to these entity types"
    )
    depth: int = Field(
        default=DEFAULT_TRAVERSAL_DEPTH,
        description=(
            f"Hops to walk from the seeds, clamped to 1-{MAX_TRAVERSAL_DEPTH}. "
            "The response reports the depth actually walked."
        ),
    )
    limit: int = Field(
        default=DEFAULT_EXPAND_LIMIT,
        description=(
            f"Maximum neighbors returned, clamped to 1-{MAX_EXPAND_LIMIT}. "
            "The response reports the limit actually applied."
        ),
    )
    content_max_chars: int = Field(
        default=DEFAULT_NEIGHBOR_CONTENT_MAX_CHARS,
        description=(
            f"Preview characters per neighbor, clamped to 0-{MAX_TRAVERSAL_CONTENT_MAX_CHARS}."
        ),
    )
    include_incoming: bool = Field(
        default=True,
        description="Follow edges pointing at the seeds as well as away from them",
    )
    project: str | None = Field(
        default=None, description="Scope the walk to one project the caller can read"
    )


class NeighborEntityResponse(BaseModel):
    """One entity reached by a bounded traversal step."""

    id: str
    type: str
    name: str
    relationship: str
    direction: Literal["outgoing", "incoming"]
    distance: int = 1
    score: float = 0.0
    content: str = ""
    project_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExpandNeighborsResponse(BaseModel):
    """Neighbors from one bounded expansion, highest path score first."""

    origins: list[str]
    neighbors: list[NeighborEntityResponse]
    total: int
    depth: int
    limit: int
    unresolved: list[str] = Field(
        default_factory=list,
        description="Seed IDs that resolved to nothing this reader may read",
    )
    truncated: bool = Field(
        default=False, description="More neighbors existed than the limit returned"
    )
    filters: dict[str, Any] = Field(default_factory=dict)
    usage_hint: str = ""


class FetchSliceRequest(BaseModel):
    """One span-window read of a memory."""

    entity_id: str = Field(
        ...,
        min_length=1,
        description="A passage entity ID, or the ID of the memory it was cut from",
    )
    window: int = Field(
        default=DEFAULT_SLICE_WINDOW,
        description=(
            f"Adjacent spans to return, clamped to 1-{MAX_SLICE_WINDOW}. "
            "The response reports the window actually applied."
        ),
    )
    content_max_chars: int = Field(
        default=DEFAULT_SLICE_CONTENT_MAX_CHARS,
        description=(
            "Character budget for the whole window, clamped to "
            f"0-{MAX_TRAVERSAL_CONTENT_MAX_CHARS}."
        ),
    )
    project: str | None = Field(
        default=None, description="Scope the read to one project the caller can read"
    )


class SlicePassageResponse(BaseModel):
    """One span of a sliced memory, or the whole memory when it was never cut."""

    id: str
    name: str
    content: str
    passage_index: int | None = None
    passage_total: int | None = None
    breadcrumb: str | None = None
    truncated: bool = False


class FetchSliceResponse(BaseModel):
    """An ordered span window plus the parent a citation resolves to."""

    entity_id: str
    parent_id: str
    parent_name: str
    parent_type: str
    passages: list[SlicePassageResponse]
    window: int
    sliced: bool = Field(..., description="False when the memory was never cut into spans")
    total: int = 0
    window_start: int | None = None
    passage_total: int | None = None
    covers_parent: bool = False
    project_id: str | None = None
    content_chars: int = 0
    filters: dict[str, Any] = Field(default_factory=dict)
    usage_hint: str = ""
