"""Verify source lifecycles before admitting a newly derived capture to recall."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from sibyl_core.errors import RevisionConflictError
from sibyl_core.memory_pipeline.lifecycle import raw_memory_lifecycle_recallable
from sibyl_core.memory_pipeline.source_lifecycle import (
    SOURCE_VALIDATION_PENDING_KEY,
    correction_event,
    merge_source_correction,
    source_revision_bindings,
)
from sibyl_core.services import content_client, content_models
from sibyl_core.services.content_raw_persistence import _RAW_MEMORY_EMBEDDING_AUTO
from sibyl_core.services.memory_policy import _authorize_share_source_read

SOURCE_VALIDATION_CONTEXT_KEY = "source_validation_context"
_PAGE_SIZE = 512


@dataclass(frozen=True, slots=True)
class SourceReadAuthority:
    """Resolved memberships, also used as the original capture's grant ceiling."""

    principal_id: str
    projects: frozenset[str] = frozenset()
    teams: frozenset[str] = frozenset()
    delegations: frozenset[str] = frozenset()
    scope_keys: frozenset[str] | None = None

    def ceiling_metadata(self) -> dict[str, object]:
        """Persist restrictions, never a bearer credential or renewable authority."""
        return {
            "version": 1,
            "principal_id": self.principal_id,
            "projects": sorted(self.projects),
            "teams": sorted(self.teams),
            "delegations": sorted(self.delegations),
            "scope_restricted": self.scope_keys is not None,
            "scope_keys": sorted(self.scope_keys or ()),
        }


type SourceAuthorityResolver = Callable[[str, str], Awaitable[SourceReadAuthority | None]]


def _saved_ceiling(memory: content_models.RawMemory) -> SourceReadAuthority | None:
    return source_authority_ceiling(
        memory.metadata.get(SOURCE_VALIDATION_CONTEXT_KEY), memory.principal_id
    )


def source_authority_ceiling(value: object, principal_id: str) -> SourceReadAuthority | None:
    if not isinstance(value, Mapping):
        return None
    if type(value.get("version")) is not int or value["version"] != 1:
        return None
    if value.get("principal_id") != principal_id or not principal_id:
        return None
    if type(value.get("scope_restricted")) is not bool:
        return None
    for key in ("projects", "teams", "delegations", "scope_keys"):
        items = value.get(key)
        if not isinstance(items, list) or any(not isinstance(v, str) or not v for v in items):
            return None
    if not value["scope_restricted"] and value["scope_keys"]:
        return None
    return SourceReadAuthority(
        principal_id=principal_id,
        projects=frozenset(value["projects"]),
        teams=frozenset(value["teams"]),
        delegations=frozenset(value["delegations"]),
        scope_keys=frozenset(value["scope_keys"]) if value["scope_restricted"] else None,
    )


async def _current_authority(
    memory: content_models.RawMemory, resolver: SourceAuthorityResolver
) -> SourceReadAuthority | None:
    ceiling = _saved_ceiling(memory)
    if ceiling is None:
        return None
    return await resolve_current_source_authority(ceiling, memory.organization_id, resolver)


async def resolve_current_source_authority(
    ceiling: SourceReadAuthority, organization_id: str, resolver: SourceAuthorityResolver
) -> SourceReadAuthority | None:
    """Intersect a retained ceiling with the principal's current memberships."""
    current = await resolver(organization_id, ceiling.principal_id)
    if current is None or current.principal_id != ceiling.principal_id:
        return None
    scopes = ceiling.scope_keys
    if current.scope_keys is not None:
        scopes = current.scope_keys if scopes is None else scopes & current.scope_keys
    return SourceReadAuthority(
        principal_id=ceiling.principal_id,
        projects=ceiling.projects & current.projects,
        teams=ceiling.teams & current.teams,
        delegations=ceiling.delegations & current.delegations,
        scope_keys=scopes,
    )


def _stored_source_ids(memory: content_models.RawMemory) -> set[str]:
    ids = set(source_revision_bindings([memory]))
    # Unretained input anchors explicitly have no capture or source revision.
    if re.fullmatch(r"reflection:input:[0-9a-f]{16}", memory.source_id):
        ids.discard(memory.source_id)
    ids.discard(memory.id)
    return ids


async def reconcile_raw_source_lifecycle(
    memory: content_models.RawMemory,
    *,
    principal_id: str,
    accessible_projects: Iterable[str] | None = None,
    accessible_teams: Iterable[str] | None = None,
    accessible_delegations: Iterable[str] | None = None,
    embedding_provider: object = _RAW_MEMORY_EMBEDDING_AUTO,
    allowed_memory_scope_keys: Iterable[str] | None = None,
    authority_resolver: SourceAuthorityResolver | None = None,
) -> content_models.RawMemory:
    """Read current source verdicts and release a pending capture under its CAS.

    Bindings describe the text that was read and are never refreshed here.
    Each source contributes its own lifecycle under its actual revision. Walking
    ancestors avoids copying a stale inherited verdict from an intermediate row.
    Missing or inaccessible evidence leaves the capture pending without exposing
    an inaccessible source's revision or inventing a correction clock.
    """
    from sibyl_core.services.content_raw_persistence import get_raw_memory, save_raw_memory

    projects = None if accessible_projects is None else tuple(accessible_projects)
    teams = None if accessible_teams is None else tuple(accessible_teams)
    delegations = None if accessible_delegations is None else tuple(accessible_delegations)
    grants = None if allowed_memory_scope_keys is None else frozenset(allowed_memory_scope_keys)
    while True:
        if authority_resolver is not None:
            authority = await _current_authority(memory, authority_resolver)
            if authority is None:
                return memory
            principal_id = authority.principal_id
            projects = tuple(authority.projects)
            teams = tuple(authority.teams)
            delegations = tuple(authority.delegations)
            grants = authority.scope_keys
        if not _authorize_share_source_read(
            memory=memory,
            principal_id=principal_id,
            accessible_projects=projects,
            accessible_teams=teams,
            accessible_delegations=delegations,
            allowed_memory_scope_keys=grants,
        ).allowed:
            if authority_resolver is not None:
                return memory
            raise PermissionError("Memory capture is no longer accessible.")
        if memory.observed_revision is None:
            raise ValueError("Memory capture revision is unavailable.")
        metadata = dict(memory.metadata)
        frontier = _stored_source_ids(memory)
        seen = {memory.id}
        complete = True
        while frontier:
            seen.update(frontier)
            records = []
            async with content_client.surreal_content_client() as client:
                for batch in content_client.value_batches(sorted(frontier)):
                    records.extend(
                        await content_client.select_many(
                            client,
                            "SELECT * FROM raw_captures WHERE organization_id = $organization_id "
                            "AND uuid IN $source_ids;",
                            organization_id=memory.organization_id,
                            source_ids=batch,
                        )
                    )
            sources = [content_models.raw_memory_from_record(record) for record in records]
            if {source.id for source in sources} != frontier:
                complete = False
            next_frontier: set[str] = set()
            for source in sources:
                if not _authorize_share_source_read(
                    memory=source,
                    principal_id=principal_id,
                    accessible_projects=projects,
                    accessible_teams=teams,
                    accessible_delegations=delegations,
                    allowed_memory_scope_keys=grants,
                ).allowed:
                    complete = False
                    continue
                if source.observed_revision is None:
                    complete = False
                    continue
                metadata = merge_source_correction(
                    metadata,
                    correction_event(
                        source,
                        blocking=not raw_memory_lifecycle_recallable(
                            source, include_source_corrections=False
                        ),
                    ),
                )
                next_frontier.update(_stored_source_ids(source) - seen)
            frontier = next_frontier
        from sibyl_core.services.memory_derivations import raw_derivation_current

        complete = complete and await raw_derivation_current(
            memory,
            SourceReadAuthority(
                principal_id=principal_id,
                projects=frozenset(projects or ()),
                teams=frozenset(teams or ()),
                delegations=frozenset(delegations or ()),
                scope_keys=grants,
            ),
        )
        if complete:
            metadata[SOURCE_VALIDATION_PENDING_KEY] = False
        else:
            metadata[SOURCE_VALIDATION_PENDING_KEY] = True
        if metadata == memory.metadata:
            return memory
        try:
            return await save_raw_memory(
                replace(memory, metadata=metadata),
                expected_revision=memory.observed_revision,
                embedding_provider=embedding_provider,
            )
        except RevisionConflictError:
            current = await get_raw_memory(
                organization_id=memory.organization_id, memory_id=memory.id
            )
            if current is None:
                raise
            memory = current


async def repair_raw_source_lifecycle(
    organization_id: str,
    *,
    authority_resolver: SourceAuthorityResolver,
    embedding_provider: object = _RAW_MEMORY_EMBEDDING_AUTO,
):
    """Page past unresolved captures so unavailable evidence cannot starve recovery."""
    from sibyl_core.projection.repair import LifecycleRepairResult
    from sibyl_core.services.content_raw_persistence import get_raw_memory

    counts = {"checked": 0, "recovered": 0, "pending": 0, "failed": 0}
    cursor = ""

    async def recover(memory_id: str) -> str:
        memory = await get_raw_memory(organization_id=organization_id, memory_id=memory_id)
        if memory is None:
            return "missing"
        if not memory.metadata.get(SOURCE_VALIDATION_PENDING_KEY):
            return "recovered"
        memory = await reconcile_raw_source_lifecycle(
            memory,
            principal_id=memory.principal_id,
            authority_resolver=authority_resolver,
            embedding_provider=embedding_provider,
        )
        return "pending" if memory.metadata.get(SOURCE_VALIDATION_PENDING_KEY) else "recovered"

    while True:
        async with content_client.surreal_content_client() as client:
            records = await content_client.select_many(
                client,
                "SELECT uuid FROM raw_captures WITH INDEX idx_raw_captures_source_validation "
                "WHERE organization_id = $organization_id "
                "AND metadata.source_validation_pending = true AND uuid > $cursor "
                "ORDER BY uuid LIMIT $limit;",
                organization_id=organization_id,
                cursor=cursor,
                limit=_PAGE_SIZE,
            )
        if not records:
            return LifecycleRepairResult(**counts)
        ids = [str(row["uuid"]) for row in records]
        outcomes = await asyncio.gather(*(recover(id_) for id_ in ids), return_exceptions=True)
        for outcome in outcomes:
            counts["checked"] += 1
            if isinstance(outcome, BaseException):
                counts["failed"] += 1
            elif outcome in counts:
                counts[outcome] += 1
        cursor = ids[-1]
