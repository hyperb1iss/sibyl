from __future__ import annotations

from sibyl_core.models.reflection import (
    ClaimRecord,
    MemoryLifecycle,
    MemoryLifecycleFlag,
    MemoryLifecycleState,
    ReflectionFinding,
    ReflectionFindingKind,
    claim_records_from_metadata,
    correction_finding_kind,
    memory_lifecycle_from_metadata,
    reflection_findings_from_metadata,
    with_claim_record_metadata,
    with_memory_lifecycle_metadata,
    with_reflection_finding_metadata,
)


def test_reflection_metadata_helpers_round_trip_structured_records() -> None:
    metadata: dict[str, object] = {}
    metadata = with_memory_lifecycle_metadata(
        metadata,
        MemoryLifecycle(
            state=MemoryLifecycleState.ACTIVE,
            source_id="source-1",
            action="promote",
            reason="accepted",
            derived_ids=["decision-1"],
        ),
    )
    metadata = with_reflection_finding_metadata(
        metadata,
        ReflectionFinding(
            kind=ReflectionFindingKind.PROMOTION,
            target_source_id="source-1",
            reason="accepted",
            action="promote",
            lifecycle_state=MemoryLifecycleState.ACTIVE,
            source_ids=["source-1"],
            related_source_ids=["decision-1"],
            policy_reasons=["same_scope_write_allowed"],
        ),
    )
    metadata = with_claim_record_metadata(
        metadata,
        ClaimRecord(
            title="Claim: reflection has receipts",
            content="Reflection writes include lifecycle receipts.",
            source_ids=["source-1"],
            raw_source_ids=["raw-1"],
            confidence=0.9,
            memory_scope="project",
            scope_key="project_123",
        ),
    )

    lifecycle = memory_lifecycle_from_metadata(
        metadata,
        source_id="source-1",
        review_state="pending",
    )
    findings = reflection_findings_from_metadata(metadata)
    claims = claim_records_from_metadata(metadata)

    assert metadata["lifecycle_state"] == "active"
    assert metadata["lifecycle_flags"] == []
    assert lifecycle.state == "active"
    assert lifecycle.derived_ids == ["decision-1"]
    assert findings[0].kind == "promotion"
    assert findings[0].policy_reasons == ["same_scope_write_allowed"]
    assert claims[0].content == "Reflection writes include lifecycle receipts."
    assert claims[0].memory_scope == "project"


def test_memory_lifecycle_from_legacy_metadata_preserves_correction_state() -> None:
    lifecycle = memory_lifecycle_from_metadata(
        {
            "lifecycle_state": "stale",
            "lifecycle_action": "mark_stale",
            "lifecycle_reason": "outdated",
            "prior_review_state": "pending",
            "superseded_by_source_id": "source-2",
        },
        source_id="source-1",
        review_state="pending",
    )

    assert lifecycle.state == "contested"
    assert lifecycle.action == "mark_stale"
    assert lifecycle.replacement_source_id == "source-2"
    assert correction_finding_kind("supersede") is ReflectionFindingKind.SUPERSESSION


def test_memory_lifecycle_from_legacy_visibility_state_extracts_flag() -> None:
    lifecycle = memory_lifecycle_from_metadata(
        {"lifecycle_state": "redacted"},
        source_id="source-1",
    )

    assert lifecycle.state == MemoryLifecycleState.ACTIVE
    assert lifecycle.flags == [MemoryLifecycleFlag.REDACTED.value]


def test_memory_lifecycle_from_unknown_state_fails_closed() -> None:
    lifecycle = memory_lifecycle_from_metadata(
        {"lifecycle_state": "bogus"},
        source_id="source-1",
    )

    assert lifecycle.state == MemoryLifecycleState.CONTESTED
    assert lifecycle.flags == []
