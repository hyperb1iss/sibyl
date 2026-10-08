"""Team activity: what each member of the organization did in a recent window.

Every count and every listed event comes from a row the caller could already
read through the entity list or the capture list, filtered by the same
predicates those endpoints apply. A teammate's private memory, a row in a
project the caller cannot open, and anything archived, hidden, redacted,
marked sensitive, deleted or retired by a correction contribute nothing:
counting them would leak activity metadata the caller has no grant to see.

Three reads run concurrently, each bounded by the window and served by an
index: graph rows created in the window (``idx_entity_updated``, ranged on
``updated_at``, which every write stamps, the create included), tasks marked
done in the window (``idx_entity_type_status_updated``)
and standalone raw memories (``idx_raw_captures_org_created``). Decoding and
the per-row policy run off the event loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query

from sibyl.api.routes import entity_contracts as contracts, entity_policy as policy
from sibyl.api.routes.activity_rollup import (
    DEFAULT_WINDOW,
    DERIVED_ENTITY_TYPES,
    ActivityEvent,
    Member,
    Window,
    capture_event,
    capture_project_id,
    entity_events,
    members_from_rows,
    resolve_window,
    standalone_capture,
    summarize,
)
from sibyl.api.schemas.activity import ActivityWindowLabel, TeamActivityResponse
from sibyl.auth.context import AuthContext
from sibyl.auth.dependencies import get_auth_context, get_current_organization, require_org_role
from sibyl.persistence import content_runtime, organization_runtime
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl_core.auth import AuthOrganization
from sibyl_core.auth.memory_policy import private_scope_granted_for
from sibyl_core.memory_pipeline.lifecycle import raw_memory_lifecycle_recallable
from sibyl_core.models.entities import Entity
from sibyl_core.services.graph_capture_availability import available_capture_projection_rows
from sibyl_core.services.graph_entity_work_items import _private_memory_clauses
from sibyl_core.services.graph_records import _ENTITY_LIST_FIELDS, entity_from_surreal_row

log = structlog.get_logger()

router = APIRouter(
    prefix="/activity",
    tags=["activity"],
    dependencies=[Depends(require_org_role(*contracts.READ_ROLES))],
)


async def execute_surreal_graph_query(
    group_id: str,
    query: str,
    **params: object,
) -> list[dict[str, object]] | None:
    from sibyl.persistence.graph_runtime import execute_surreal_graph_query

    return await execute_surreal_graph_query(group_id, query, **params)


# Rows created in the window. Every write stamps updated_at, the create
# included, so a row created since the window opened was last updated since
# then too: ranging the ordered index on updated_at finds it, and the
# created_at bounds keep only those rows. Derived types are dropped here to
# keep the scan small; the rollup re-checks every row.
CREATED_IN_WINDOW = (
    f"SELECT {_ENTITY_LIST_FIELDS} FROM entity "  # noqa: S608
    "WHERE group_id = $group_id AND updated_at >= $since "
    "AND created_at >= $since AND created_at < $until "
    "AND entity_type NOT IN $derived_types"
)
# Tasks that are done and were touched in the window. A completion is written
# with its own update, so a task completed in the window was updated in it;
# the rollup reads the completion instant itself.
COMPLETED_IN_WINDOW = (
    f"SELECT {_ENTITY_LIST_FIELDS} FROM entity "  # noqa: S608
    "WHERE group_id = $group_id AND entity_type = 'task' AND status = 'done' "
    "AND updated_at >= $since"
)


@dataclass(frozen=True, slots=True)
class ReaderScope:
    """What the caller may read, resolved once before any row is loaded.

    ``graph_projects`` follows the entity list: with a project filter it is
    that project, otherwise every project the caller can open. Raw captures
    are judged against the full accessible set, as the capture list does, and
    then held to the filter.
    """

    user_id: str | None
    accessible_projects: set[str]
    graph_projects: set[str]
    project_ids: list[str]
    real_project_ids: list[str]
    has_unassigned: bool
    memory_grants: set[str] | None
    accessible_teams: set[str]
    accessible_delegations: set[str]
    project_filter: str | None

    @property
    def private_granted(self) -> bool:
        return self.user_id is not None and private_scope_granted_for(
            self.memory_grants, principal_id=self.user_id
        )


async def resolve_reader_scope(ctx: AuthContext, project_id: str | None) -> ReaderScope:
    (project_ids, real_project_ids, has_unassigned), accessible = await asyncio.gather(
        policy.resolve_entity_list_project_filter(
            ctx=ctx, project_ids=[project_id] if project_id else None
        ),
        policy.accessible_project_ids_for_read(ctx),
    )
    if project_id is not None and project_id not in accessible:
        # The capture list's answer to a project outside the caller's grants.
        raise HTTPException(status_code=403, detail="project_scope_denied")
    return ReaderScope(
        user_id=policy.reader_user_id(ctx),
        accessible_projects=accessible,
        graph_projects=set(real_project_ids),
        project_ids=project_ids,
        real_project_ids=real_project_ids,
        has_unassigned=bool(has_unassigned),
        memory_grants=policy.reader_memory_grants(ctx),
        accessible_teams=set(ctx.accessible_teams),
        accessible_delegations=set(ctx.accessible_delegations),
        project_filter=project_id,
    )


def private_row_clause(scope: ReaderScope) -> tuple[str, dict[str, object]]:
    """The reader's private-scope rule as SurrealQL, the way the entity list pushes it.

    It only removes rows the per-row policy would deny anyway, so the window
    arrives mostly readable; the policy still runs on every row returned.
    """
    params: dict[str, object] = {}
    clauses = _private_memory_clauses(
        exclude_private_memory=not scope.private_granted,
        private_memory_owner=scope.user_id if scope.private_granted else None,
        params=params,
    )
    return "".join(f" AND {clause}" for clause in clauses), params


def graph_activity_statement(scope: ReaderScope) -> tuple[str, dict[str, object]]:
    """One RETURN block holding both graph reads, with the private rule pushed in."""
    private, params = private_row_clause(scope)
    statement = (
        f"RETURN {{ created: ({CREATED_IN_WINDOW}{private}), "
        f"completed: ({COMPLETED_IN_WINDOW}{private}) }};"
    )
    return statement, {**params, "derived_types": sorted(DERIVED_ENTITY_TYPES)}


def _payload_rows(payload: object, key: str) -> list[Mapping[str, object]]:
    if not isinstance(payload, Mapping):
        return []
    rows = payload.get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


async def load_graph_rows(
    group_id: str, scope: ReaderScope, window: Window
) -> list[Mapping[str, object]]:
    statement, params = graph_activity_statement(scope)
    result = await execute_surreal_graph_query(
        group_id, statement, since=window.since, until=window.until, **params
    )
    payload = result[0] if result else {}
    return [*_payload_rows(payload, "created"), *_payload_rows(payload, "completed")]


def readable_entities(
    rows: Iterable[Mapping[str, object]], scope: ReaderScope
) -> dict[str, Entity]:
    """Decode graph rows and keep the ones the entity list would show this caller."""
    readable: dict[str, Entity] = {}
    for row in rows:
        entity = entity_from_surreal_row(row)
        if entity.id in readable:
            continue
        if policy.entity_matches_list_filters(
            entity,
            project_ids=scope.project_ids,
            real_project_ids=scope.real_project_ids,
            has_unassigned=scope.has_unassigned,
            reader_user_id=scope.user_id,
            accessible_projects=scope.graph_projects,
            allowed_memory_scope_keys=scope.memory_grants,
            accessible_teams=scope.accessible_teams,
            accessible_delegations=scope.accessible_delegations,
            language=None,
            category=None,
            search=None,
        ):
            readable[entity.id] = entity
    return readable


@dataclass(frozen=True, slots=True)
class _CaptureLifecycle:
    """The lifecycle view of a capture record, for the raw recall verdict."""

    id: str
    source_id: str
    revision: int
    review_state: str
    metadata: Mapping[str, object]


def readable_captures(
    captures: Iterable[RawCaptureRecord], scope: ReaderScope, ctx: AuthContext
) -> list[RawCaptureRecord]:
    """Keep the standalone captures this caller may read and recall.

    The scope check is the capture list's own; the lifecycle verdict is raw
    recall's, so hidden, redacted, sensitive, superseded and correction-blocked
    memories stay out even for their owner.
    """
    readable: list[RawCaptureRecord] = []
    for capture in captures:
        if not standalone_capture(capture):
            continue
        if scope.project_filter is not None and capture_project_id(capture) != (
            scope.project_filter
        ):
            continue
        lifecycle = _CaptureLifecycle(
            id=str(capture.id),
            source_id=capture.source_id,
            revision=1,
            review_state=capture.review_state,
            metadata=capture.metadata or {},
        )
        if not raw_memory_lifecycle_recallable(lifecycle):
            continue
        if not policy.raw_capture_visible_to_reader(
            capture, ctx=ctx, accessible_projects=scope.accessible_projects
        ):
            continue
        readable.append(capture)
    return readable


def activity_events(
    entities: Iterable[Entity],
    captures: Iterable[RawCaptureRecord],
    *,
    window: Window,
    members: Mapping[str, Member],
) -> list[ActivityEvent]:
    events: list[ActivityEvent] = []
    for entity in entities:
        events.extend(
            entity_events(
                entity,
                window=window,
                members=members,
                project_id=policy.entity_read_project_id(entity),
            )
        )
    events.extend(
        event
        for capture in captures
        if (event := capture_event(capture, window=window, members=members)) is not None
    )
    return events


async def build_team_activity(
    *,
    org: AuthOrganization,
    ctx: AuthContext,
    window_label: str,
    project_id: str | None,
    now: datetime | None = None,
) -> TeamActivityResponse:
    scope = await resolve_reader_scope(ctx, project_id)
    window = resolve_window(window_label, now=now or datetime.now(UTC))
    group_id = str(org.id)
    actor_id = UUID(str(ctx.user.id))

    member_rows, graph_rows, raw_rows = await asyncio.gather(
        organization_runtime.list_org_members(slug=org.slug, actor_id=actor_id),
        load_graph_rows(group_id, scope, window),
        content_runtime.list_raw_memories_created_between(
            None,
            organization_id=org.id,
            since=window.since,
            until=window.until,
            private_owner=scope.user_id if scope.private_granted else None,
        ),
    )
    members = members_from_rows(member_rows)
    entities, captures = await asyncio.gather(
        asyncio.to_thread(readable_entities, graph_rows, scope),
        asyncio.to_thread(readable_captures, raw_rows, scope, ctx),
    )
    # A row projected from a capture is readable only while that capture is:
    # the same source verdict the entity list applies after its own filters.
    available = await available_capture_projection_rows(
        group_id,
        entities,
        source_visible=partial(
            policy.entity_visible_to_reader,
            reader_user_id=scope.user_id,
            accessible_projects=scope.graph_projects,
            allowed_memory_scope_keys=scope.memory_grants,
            accessible_teams=scope.accessible_teams,
            accessible_delegations=scope.accessible_delegations,
        ),
    )
    events = await asyncio.to_thread(
        activity_events, available.values(), captures, window=window, members=members
    )
    log.debug(
        "team_activity_built",
        group_id=group_id,
        window=window.label,
        graph_rows=len(graph_rows),
        raw_rows=len(raw_rows),
        events=len(events),
    )
    return summarize(events, members=members, window=window, project_id=project_id)


@router.get("/team", response_model=TeamActivityResponse)
async def get_team_activity(
    org: AuthOrganization = Depends(get_current_organization),
    ctx: AuthContext = Depends(get_auth_context),
    window: ActivityWindowLabel = Query(
        default=DEFAULT_WINDOW, description="How far back to look: 24h, 7d or 30d"
    ),
    project_id: str | None = Query(
        default=None, min_length=1, description="Only count activity in this project"
    ),
) -> TeamActivityResponse:
    """What each member of the current organization did in the window.

    Counts and recent events cover only rows the caller can already read.
    """
    return await build_team_activity(org=org, ctx=ctx, window_label=window, project_id=project_id)


__all__ = [
    "COMPLETED_IN_WINDOW",
    "CREATED_IN_WINDOW",
    "DERIVED_ENTITY_TYPES",
    "ReaderScope",
    "build_team_activity",
    "graph_activity_statement",
    "private_row_clause",
    "router",
]
