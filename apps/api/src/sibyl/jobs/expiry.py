"""Retire time-boxed captures whose expiry has passed."""

from __future__ import annotations

from typing import Any

import structlog

from sibyl.persistence.auth_runtime import log_memory_audit_event
from sibyl_core.services.memory_expiry import CAPTURE_EXPIRY_REASON, expire_due_captures

log = structlog.get_logger()


async def expire_time_boxed_captures(ctx: dict[str, Any]) -> dict[str, int]:
    del ctx
    result = await expire_due_captures()
    for item in result.expired:
        if not item.corrected:
            continue
        memory = item.memory
        await log_memory_audit_event(
            action="memory.correction.mark_stale",
            user_id=memory.principal_id or None,
            organization_id=memory.organization_id,
            request=None,
            memory_scope=memory.memory_scope.value,
            scope_key=memory.scope_key,
            source_surface="capture_expiry",
            source_ids=[memory.id],
            derived_ids=list(item.affected_entity_ids),
            policy_allowed=True,
            policy_reason=CAPTURE_EXPIRY_REASON,
            details={"action": "mark_stale", "expires_at": item.expires_at},
        )
    summary = {
        "expired": sum(1 for item in result.expired if item.corrected),
        "already_retired": result.already_retired,
        "refused": result.refused,
        "conflicts": result.conflicts,
        "failed": result.failed,
    }
    if any(summary.values()):
        log.info("time_boxed_captures_expired", **summary)
    return summary
