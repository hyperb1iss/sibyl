from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.jobs import privacy


@pytest.mark.asyncio
async def test_purge_due_deleted_personal_memories_audits_purged_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid4()
    organization_id = uuid4()
    capture_id = uuid4()
    purge_due = AsyncMock(
        return_value=[
            {
                "uuid": str(capture_id),
                "principal_id": str(user_id),
                "organization_id": str(organization_id),
                "deleted_at": "2026-05-01T12:00:00",
                "purge_after": "2026-05-31T12:00:00",
            }
        ]
    )
    audit = AsyncMock()

    monkeypatch.setattr(privacy, "purge_due_deleted_raw_captures", purge_due)
    monkeypatch.setattr(privacy, "log_memory_audit_event", audit)

    result = await privacy.purge_due_deleted_personal_memories({})

    assert result == {"purged": 1}
    audit.assert_awaited_once_with(
        action="memory.delete.personal_purged",
        user_id=str(user_id),
        organization_id=str(organization_id),
        request=None,
        memory_scope="private",
        source_ids=[str(capture_id)],
        policy_allowed=True,
        policy_reason="retention_window_elapsed",
        details={
            "deleted_at": "2026-05-01T12:00:00",
            "purge_after": "2026-05-31T12:00:00",
        },
    )
