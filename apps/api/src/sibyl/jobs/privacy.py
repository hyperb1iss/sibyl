"""Privacy lifecycle jobs."""

from __future__ import annotations

from typing import Any

import structlog

from sibyl.persistence.auth_runtime import log_memory_audit_event
from sibyl.persistence.content_runtime import purge_due_deleted_raw_captures

log = structlog.get_logger()


async def purge_due_deleted_personal_memories(ctx: dict[str, Any]) -> dict[str, int]:
    del ctx
    purged = await purge_due_deleted_raw_captures()
    for row in purged:
        await log_memory_audit_event(
            action="memory.delete.personal_purged",
            user_id=_optional_str(row.get("principal_id")),
            organization_id=_optional_str(row.get("organization_id")),
            request=None,
            memory_scope="private",
            source_ids=[source_id] if (source_id := _optional_str(row.get("uuid"))) else None,
            policy_allowed=True,
            policy_reason="retention_window_elapsed",
            details={
                "deleted_at": _optional_str(row.get("deleted_at")),
                "purge_after": _optional_str(row.get("purge_after")),
            },
        )
    log.info("deleted_personal_memories_purged", count=len(purged))
    return {"purged": len(purged)}


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None
