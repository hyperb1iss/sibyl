"""Dream bookkeeping must not replace corrections made during extraction."""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from sibyl.jobs.reflection import (
    _archive_dream_exception_candidate,
    _mark_source_processed,
    _mark_source_reflected,
)
from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.errors import RevisionConflictError
from sibyl_core.models.reflection import ReflectionPack
from sibyl_core.services.surreal_content import get_raw_memory, remember_raw_memory, save_raw_memory


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["reflected", "processed", "archive"])
@pytest.mark.parametrize("concurrent_correction", [False, True])
async def test_dream_writes_preserve_concurrent_source_corrections(
    monkeypatch, operation, concurrent_correction
):
    client = SurrealContentClient(url="memory://")
    try:
        await bootstrap_content_schema(client, reset=True)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
        source = await remember_raw_memory(
            organization_id="dream-test-org",
            principal_id="owner",
            source_id="dream-source",
            raw_content="Original advice",
        )
        current = source
        if concurrent_correction:
            current = await save_raw_memory(
                replace(
                    source,
                    raw_content="Corrected advice",
                    metadata={
                        **source.metadata,
                        "lifecycle_state": "hidden",
                        "correction_history": [{"action": "revise", "prior_revision": 1}],
                    },
                ),
                expected_revision=source.revision,
            )

        async def finish_dream():
            if operation == "reflected":
                return await _mark_source_reflected(
                    source,
                    pack=ReflectionPack(
                        source_title="Source",
                        source_id=source.id,
                        intent="maintenance",
                        domain=None,
                        project=None,
                        candidates=[],
                        total_candidates=0,
                        persisted_count=0,
                    ),
                    run_id="dream-run",
                    dry_run=False,
                )
            if operation == "processed":
                return await _mark_source_processed(
                    source,
                    run_id="dream-run",
                    dry_run=False,
                    outcome="skip",
                    reason="empty_source",
                )
            return await _archive_dream_exception_candidate(
                candidate=source,
                decision_reason="duplicate_candidate",
                exception_reasons=["duplicate_candidate"],
                run_id="dream-run",
            )

        conflict = False
        try:
            await finish_dream()
        except RevisionConflictError:
            conflict = True
        stored = await get_raw_memory(organization_id=source.organization_id, memory_id=source.id)
        assert stored is not None
        if concurrent_correction:
            assert stored.raw_content == current.raw_content
            assert stored.metadata == current.metadata
            assert stored.revision == current.revision
            assert stored.review_state == current.review_state
            assert conflict
        else:
            assert not conflict
            assert stored.revision == source.revision + 1
            assert stored.metadata["reflection_dream_run_id"] == "dream-run"
            assert stored.raw_content == source.raw_content
            if operation == "archive":
                assert stored.review_state == "archived"
                assert stored.metadata["review_state"] == "archived"
    finally:
        await client.close()
