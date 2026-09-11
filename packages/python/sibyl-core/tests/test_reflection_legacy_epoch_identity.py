"""Legacy correction history must not turn bookkeeping into new evidence."""

from dataclasses import replace

import pytest

from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services import memory_reflection
from sibyl_core.services.surreal_content import remember_raw_memory, save_raw_memory
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime


async def legacy_source(runtime):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="legacy-epoch-source",
        raw_content="Retain the audit trail.",
        embedding_provider=None,
    )
    return await save_raw_memory(
        replace(source, metadata={"correction_history": [{"action": "revise"}]}),
        expected_revision=source.revision,
        embedding_provider=None,
    )


async def test_legacy_epoch_bookkeeping_replays_existing_evidence(runtime, content_store):
    source = await legacy_source(runtime)
    candidate = ReflectionCandidate(
        kind="decision",
        title="Audit trail",
        content=source.raw_content,
        reason="Recorded decision",
        confidence=1.0,
        raw_source_ids=[source.id],
    )

    async def publish(snapshot):
        return await memory_reflection.persist_reflection_candidate(
            candidate=candidate,
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            source_id=source.id,
            link_source_entity=False,
            source_memories=[snapshot],
        )

    first = await publish(source)
    assert first.response.success
    updated = await save_raw_memory(
        replace(source, metadata={**source.metadata, "reflection_run_id": "later"}),
        expected_revision=source.revision,
        embedding_provider=None,
    )
    replay = await publish(updated)
    assert replay.response.success and replay.response.id == first.response.id


async def test_legacy_epoch_reservation_recovers_after_interruption(
    runtime, content_store, monkeypatch
):
    source = await legacy_source(runtime)

    async def interrupted(**kwargs):
        raise RuntimeError("simulated interruption after reservation")

    async def promote():
        return await memory_reflection.promote_raw_memory(
            raw_memory_id=source.id,
            organization_id=runtime.client.group_id,
            principal_id="user_a",
            promote_to_scope="private",
        )

    with monkeypatch.context() as patch:
        patch.setattr(memory_reflection, "persist_reflection_candidate", interrupted)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            await promote()
    recovered = await promote()
    assert recovered.success


async def test_raw_capture_cannot_supply_correction_history(runtime, content_store):
    source = await remember_raw_memory(
        organization_id=runtime.client.group_id,
        principal_id="user_a",
        source_id="forged-content-generation",
        raw_content="Retain the audit trail.",
        metadata={
            "correction_history": [{"action": "revise", "prior_revision": 999}],
            "source_snapshot_sha256": "forged",
            "description": "authored metadata survives",
        },
        embedding_provider=None,
    )
    assert "correction_history" not in source.metadata
    assert "source_snapshot_sha256" not in source.metadata
    assert source.metadata["description"] == "authored metadata survives"
