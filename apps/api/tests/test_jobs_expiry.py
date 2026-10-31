from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import expiry
from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services.content_models import RawMemory
from sibyl_core.services.memory_expiry import ExpiredCapture, ExpirySweepResult


def _memory(memory_id: str) -> RawMemory:
    return RawMemory(
        id=memory_id,
        organization_id="org-1",
        source_id="incident",
        principal_id="oncall-agent",
        memory_scope=MemoryScope.TEAM,
        scope_key="team-1",
        raw_content="Checkout is down.",
    )


@pytest.mark.asyncio
async def test_expiry_job_audits_only_the_captures_it_corrected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sweep = ExpirySweepResult(
        expired=[
            ExpiredCapture(
                memory=_memory("expired"),
                expires_at="2026-10-30T13:00:00Z",
                corrected=True,
                affected_entity_ids=("note-1",),
            ),
            ExpiredCapture(
                memory=_memory("resolved"),
                expires_at="2026-10-30T13:00:00Z",
                corrected=False,
            ),
        ],
        already_retired=1,
    )
    audit = AsyncMock()
    monkeypatch.setattr(expiry, "expire_due_captures", AsyncMock(return_value=sweep))
    monkeypatch.setattr(expiry, "log_memory_audit_event", audit)

    result = await expiry.expire_time_boxed_captures({})

    assert result == {
        "expired": 1,
        "already_retired": 1,
        "refused": 0,
        "conflicts": 0,
        "failed": 0,
    }
    audit.assert_awaited_once_with(
        action="memory.correction.mark_stale",
        user_id="oncall-agent",
        organization_id="org-1",
        request=None,
        memory_scope="team",
        scope_key="team-1",
        source_surface="capture_expiry",
        source_ids=["expired"],
        derived_ids=["note-1"],
        policy_allowed=True,
        policy_reason="expired",
        details={"action": "mark_stale", "expires_at": "2026-10-30T13:00:00Z"},
    )
