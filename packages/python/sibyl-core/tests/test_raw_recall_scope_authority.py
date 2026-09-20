"""Raw recall must honor every shared scope its caller already authorized.

The publication filter inside recall validates each candidate against a
SourceReadAuthority built from the requested scope. A scope key that never
reaches that authority makes validation read a listed memory as unavailable,
so recall silently drops what listing returns. Delegated scope did exactly
that between PR 525 and this test.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from sibyl_core.models.memory_scope import MemoryScope
from sibyl_core.services.surreal_content import recall_raw_memory, remember_raw_memory
from tests.test_reflection_identity import content_store as content_store


@pytest.mark.parametrize(
    "memory_scope",
    [MemoryScope.DELEGATED, MemoryScope.PROJECT, MemoryScope.TEAM],
)
async def test_keyed_scope_recall_returns_what_the_scope_owns(
    content_store: None, memory_scope: MemoryScope
) -> None:
    org = str(uuid4())
    scope_key = str(uuid4())
    remembered = await remember_raw_memory(
        organization_id=org,
        principal_id="member",
        source_id=f"source-{memory_scope.value}",
        raw_content=f"On-call rotation handoff for the {memory_scope.value} lane",
        memory_scope=memory_scope,
        scope_key=scope_key,
        embedding_provider=None,
    )

    recalled = await recall_raw_memory(
        organization_id=org,
        principal_id="member",
        query="on-call rotation handoff",
        memory_scope=memory_scope,
        scope_key=scope_key,
        limit=10,
    )

    assert [memory.id for memory in recalled] == [remembered.id]
