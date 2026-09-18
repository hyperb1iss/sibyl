"""Retire reflection drafts that a promoted correction descendant superseded.

Automatic correction replaces a draft rather than editing it: the critic's
findings produce a fresh candidate row whose id is bound to the correction
execution, and promotion lands on that child. The parent draft keeps
``review_state = "pending"`` forever, so every later dream drain re-resolves
the same chain, reports ``candidate_already_promoted`` and leaves the row
exactly where it was.

The terminal state cannot be a write to the draft row. A candidate's
``snapshot_sha256`` is a hash over the complete ``raw_captures`` record
(``ORDINARY_SNAPSHOT`` in ``reflection_validation``), and every descendant's
lineage proof compares its stored ``parent_candidate_sha256`` against that
digest recomputed live. Any write to the draft, lifecycle or otherwise, moves
``revision`` and ``updated_at`` and so permanently breaks the promoted child's
evidence chain. The draft is immutable evidence for as long as its descendant
is published.

So supersession is recorded beside the row instead of inside it, in
``reflection_supersessions``. The record is the draft's terminal state: the
candidate reader stops returning it, the promoted candidate it lost to is named
in the record, and the draft itself is left byte-identical and undeleted.
"""

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import structlog

from sibyl_core.services import content_client
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.content_raw_persistence import get_raw_memory

log = structlog.get_logger(__name__)

SUPERSESSION_TABLE = "reflection_supersessions"
SUPERSEDED_ARCHIVE_REASON = "superseded_by_promotion"
PROMOTED_REVIEW_STATE = "promoted"
PENDING_REVIEW_STATE = "pending"

# A correction chain is linear and short; the cap only bounds a corrupted cycle
# that the per-row identity check would otherwise walk forever.
_MAX_CHAIN_DEPTH = 64


def correction_parent_id(memory: RawMemory) -> str | None:
    """Return the draft this candidate corrected, or None when it is a root.

    The marker is server-written by ``automatic_reflection._persist_corrected``,
    and the candidate id is
    ``uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + execution_id)``.
    Re-deriving that id proves the row really is the child of the execution the
    marker names, the same binding ``_reflection_root`` enforces, so a rewritten
    marker cannot redirect the walk at another draft.
    """
    marker = (memory.metadata or {}).get("automatic_correction")
    if not isinstance(marker, dict):
        return None
    parent_id = marker.get("parent_id")
    execution_id = marker.get("execution_id")
    if not isinstance(parent_id, str) or not isinstance(execution_id, str):
        return None
    if not parent_id or not execution_id:
        return None
    if memory.id != str(uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + execution_id)):
        return None
    return parent_id


async def superseded_draft_ids(organization_id: str) -> list[str]:
    """Return every candidate in this organization already retired as superseded.

    The candidate reader excludes these inside its own query rather than after
    it, so a retired draft never consumes a page slot and never shortens a page
    into a false end of the walk.
    """
    async with content_client.surreal_content_client() as client:
        rows = await content_client.select_many(
            client,
            f"SELECT draft_id FROM {SUPERSESSION_TABLE} WHERE organization_id = $organization_id;",
            organization_id=organization_id,
        )
    return sorted({str(row["draft_id"]) for row in rows if row.get("draft_id")})


async def _record_supersession(draft: RawMemory, promoted: RawMemory) -> bool:
    """Write the draft's terminal record, leaving an existing one as it stands.

    The unique ``(organization_id, draft_id)`` index is the real guard, so a
    concurrent pass loses the race rather than writing a second record.
    """
    promoted_entity_id = (promoted.metadata or {}).get("promoted_entity_id")
    async with content_client.surreal_content_client() as client:
        existing = await content_client.select_one(
            client,
            f"SELECT draft_id FROM {SUPERSESSION_TABLE} "
            "WHERE organization_id = $organization_id AND draft_id = $draft_id LIMIT 1;",
            organization_id=draft.organization_id,
            draft_id=draft.id,
        )
        if existing is not None:
            return False
        try:
            await content_client.select_many(
                client,
                f"CREATE {SUPERSESSION_TABLE} SET organization_id = $organization_id,"
                "principal_id = $principal_id, draft_id = $draft_id,"
                "superseded_by_candidate_id = $frontier_id,"
                "promoted_entity_id = $promoted_entity_id,"
                "archive_reason = $archive_reason, archived_at = time::now();",
                organization_id=draft.organization_id,
                principal_id=draft.principal_id,
                draft_id=draft.id,
                frontier_id=promoted.id,
                promoted_entity_id=promoted_entity_id
                if isinstance(promoted_entity_id, str)
                else None,
                archive_reason=SUPERSEDED_ARCHIVE_REASON,
            )
        except RuntimeError as failure:
            if "reflection_supersession_draft" not in str(failure):
                raise
            return False
    return True


async def retire_superseded_reflection_drafts(
    *,
    organization_id: str,
    promoted_candidate_id: str,
) -> list[str]:
    """Retire the pending ancestors of a promoted correction candidate.

    Walks upward from the promoted row, so a draft is only retired once its own
    chain has reached a terminal state. Nothing is deleted and the draft rows
    are not touched; an ancestor that already carries a record is left alone,
    which makes repeated passes idempotent and lets existing databases heal.
    """
    promoted = await get_raw_memory(
        organization_id=organization_id, memory_id=promoted_candidate_id
    )
    if promoted is None or promoted.review_state != PROMOTED_REVIEW_STATE:
        return []
    retired: list[str] = []
    seen = {promoted.id}
    current = promoted
    for _ in range(_MAX_CHAIN_DEPTH):
        parent_id = correction_parent_id(current)
        if parent_id is None or parent_id in seen:
            break
        seen.add(parent_id)
        parent = await get_raw_memory(organization_id=organization_id, memory_id=parent_id)
        if parent is None or parent.principal_id != promoted.principal_id:
            break
        current = parent
        if parent.review_state != PENDING_REVIEW_STATE or parent.deleted_at is not None:
            # Already terminal on its own row. Keep climbing: an abstained child
            # can still sit above a draft nobody has retired yet.
            continue
        if not await _record_supersession(parent, promoted):
            continue
        retired.append(parent.id)
        log.info(
            "reflection_superseded_draft_retired",
            candidate_id=parent.id,
            promoted_candidate_id=promoted.id,
            archived_at=datetime.now(UTC).isoformat(),
        )
    return retired
