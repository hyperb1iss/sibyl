from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.reflection import (
    DeterministicFakeReflectionExtractor,
    HeuristicReflectionExtractor,
    ReflectionExtractionRequest,
    apply_reflection_lifecycle_decisions,
    ground_reflection_candidate,
    validate_reflection_candidates,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory
from sibyl_core.tools.reflect import reflect_memory
from sibyl_core.tools.responses import AddResponse


@pytest.mark.asyncio
async def test_heuristic_extractor_structures_claims_tasks_artifacts_and_sensitivity() -> None:
    extractor = HeuristicReflectionExtractor()

    candidates = await extractor.extract(
        ReflectionExtractionRequest(
            content=(
                "Observed API token rotation must stay in private review. "
                "Task follow up: finish mailbox importer. "
                "Run `moon run core:check` to verify the workflow. "
                "Validated docs/architecture/SIBYL_V012_REFLECTION_OS_PLAN.md as the source."
            ),
            source_title="Dogfood diary",
            intent="build",
            domain="sibyl",
            project="project_123",
            source_ids=("raw-source-1",),
            limit=8,
        )
    )

    kinds = {candidate.kind for candidate in candidates}
    claim = next(candidate for candidate in candidates if candidate.kind == "claim")
    sensitive = next(candidate for candidate in candidates if candidate.sensitivity_flags)
    project_relationship = next(
        relationship
        for candidate in candidates
        for relationship in candidate.relationship_records
    )

    assert {"artifact", "claim", "procedure", "task"} <= kinds
    assert claim.claim_records[0].source_ids == ["raw-source-1"]
    assert claim.reflection_findings[0].kind == "claim"
    assert claim.reflection_findings[0].source_ids == ["raw-source-1"]
    assert sensitive.metadata["contains_sensitive"] is True
    assert "sensitive" in sensitive.tags
    assert project_relationship.target_id == "project_123"
    assert project_relationship.source_ids == ["raw-source-1"]


@pytest.mark.asyncio
async def test_reflect_memory_persists_claim_receipts_with_source_grounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    add_fn = AsyncMock(side_effect=AssertionError("graph add path should not run"))

    async def fake_source_review(**kwargs: Any) -> AddResponse:
        calls.append(("source", kwargs))
        return AddResponse(
            success=True,
            id="raw-source-1",
            message="stored",
            timestamp=datetime.now(UTC),
        )

    async def fake_candidate_review(**kwargs: Any) -> RawMemory:
        calls.append(("candidate", kwargs))
        candidate = kwargs["candidate"]
        return RawMemory(
            id="raw-candidate-1",
            organization_id=kwargs["organization_id"],
            source_id=kwargs["source_id"],
            principal_id=kwargs["principal_id"],
            memory_scope=MemoryScope.PROJECT,
            scope_key="project_123",
            project_id="project_123",
            review_state="pending",
            entity_type=candidate.kind,
            title=candidate.title,
            raw_content=candidate.content,
            tags=list(candidate.tags),
            metadata=dict(candidate.metadata),
            capture_surface="reflection_candidate",
        )

    monkeypatch.setattr(
        "sibyl_core.tools.reflect._persist_reflection_source_review",
        fake_source_review,
    )
    monkeypatch.setattr(
        "sibyl_core.tools.reflect._persist_reflection_candidate_review",
        fake_candidate_review,
    )
    monkeypatch.setattr(
        "sibyl_core.tools.reflect.list_raw_memories_for_scope",
        AsyncMock(return_value=[]),
    )

    pack = await reflect_memory(
        "Observed reflection quality gate is now a named release check.",
        source_title="Reflection diary",
        intent="build",
        domain="sibyl",
        project="project_123",
        organization_id="org_123",
        principal_id="user_123",
        accessible_projects={"project_123"},
        memory_scope="project",
        scope_key="project_123",
        persist=True,
        persist_review=True,
        add_fn=add_fn,
    )

    candidate_call = calls[1][1]
    stored_candidate = candidate_call["candidate"]
    claim = stored_candidate.claim_records[0]
    finding = stored_candidate.reflection_findings[0]

    assert pack.persisted_count == 1
    assert pack.candidates[0].claim_records[0].source_ids == ["raw-source-1"]
    assert candidate_call["raw_source_ids"] == ["raw-source-1"]
    assert claim.source_ids == ["raw-source-1"]
    assert finding.source_ids == ["raw-source-1"]
    assert stored_candidate.metadata["claim_records"][0]["source_ids"] == ["raw-source-1"]
    assert stored_candidate.metadata["reflection_findings"][0]["source_ids"] == [
        "raw-source-1"
    ]


@pytest.mark.asyncio
async def test_reflect_memory_marks_duplicate_candidates_before_review_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    prior = _raw_memory(
        "older-source-1",
        "Observed reflection quality gate is now a named release check.",
    )

    async def fake_source_review(**kwargs: Any) -> AddResponse:
        calls.append(("source", kwargs))
        return AddResponse(
            success=True,
            id="raw-source-1",
            message="stored",
            timestamp=datetime.now(UTC),
        )

    async def fake_candidate_review(**kwargs: Any) -> RawMemory:
        calls.append(("candidate", kwargs))
        candidate = kwargs["candidate"]
        return replace_raw_memory(
            _raw_memory("raw-candidate-1", candidate.content),
            source_id=kwargs["source_id"],
            principal_id=kwargs["principal_id"],
            raw_content=candidate.content,
            metadata=dict(candidate.metadata),
            capture_surface="reflection_candidate",
        )

    monkeypatch.setattr(
        "sibyl_core.tools.reflect._persist_reflection_source_review",
        fake_source_review,
    )
    monkeypatch.setattr(
        "sibyl_core.tools.reflect._persist_reflection_candidate_review",
        fake_candidate_review,
    )
    monkeypatch.setattr(
        "sibyl_core.tools.reflect.list_raw_memories_for_scope",
        AsyncMock(return_value=[prior]),
    )

    pack = await reflect_memory(
        "Observed reflection quality gate is now a named release check.",
        source_title="Reflection diary",
        intent="build",
        domain="sibyl",
        project="project_123",
        organization_id="org_123",
        principal_id="user_123",
        accessible_projects={"project_123"},
        memory_scope="project",
        scope_key="project_123",
        persist=True,
        persist_review=True,
    )

    metadata = pack.candidates[0].metadata
    finding = metadata["reflection_findings"][-1]

    assert metadata["candidate_duplicate_of_source_id"] == "older-source-1"
    assert metadata["lifecycle_state"] == "duplicate"
    assert finding["kind"] == "duplicate"
    assert finding["related_source_ids"] == ["older-source-1"]


def test_reflection_lifecycle_decisions_mark_duplicate_candidates() -> None:
    candidate = _grounded_candidate("Sibyl review is enabled.")

    [result] = apply_reflection_lifecycle_decisions(
        [candidate],
        prior_memories=[_raw_memory("memory-1", "Sibyl review is enabled.")],
    )

    assert result.metadata["candidate_duplicate_of_source_id"] == "memory-1"
    assert result.metadata["lifecycle_state"] == "duplicate"
    assert result.reflection_findings[-1].kind == "duplicate"


def test_reflection_lifecycle_decisions_route_contradictions_to_review() -> None:
    candidate = _grounded_candidate("Sibyl review is disabled.")

    [result] = apply_reflection_lifecycle_decisions(
        [candidate],
        prior_memories=[_raw_memory("memory-1", "Sibyl review is enabled.")],
    )

    assert result.metadata["contradiction_source_ids"] == ["memory-1"]
    assert result.metadata["conflicts_with_source_ids"] == ["memory-1"]
    assert result.reflection_findings[-1].kind == "contradiction"


def test_reflection_lifecycle_decisions_prefer_explicit_supersession() -> None:
    candidate = _grounded_candidate("Sibyl review is disabled and supersedes memory-1.")

    [result] = apply_reflection_lifecycle_decisions(
        [candidate],
        prior_memories=[_raw_memory("memory-1", "Sibyl review is enabled.")],
    )

    assert result.metadata["supersedes_source_ids"] == ["memory-1"]
    assert "contradiction_source_ids" not in result.metadata
    assert result.reflection_findings[-1].kind == "supersession"


def test_reflection_lifecycle_decisions_emit_stale_findings() -> None:
    candidate = _grounded_candidate("memory-1 is outdated.")

    [result] = apply_reflection_lifecycle_decisions(
        [candidate],
        prior_memories=[_raw_memory("memory-1", "Old memory text.")],
    )

    assert result.metadata["stale_source_ids"] == ["memory-1"]
    assert result.reflection_findings[-1].kind == "stale"


@pytest.mark.asyncio
async def test_deterministic_fake_extractor_runs_through_schema_validation() -> None:
    extractor = DeterministicFakeReflectionExtractor(
        [
            ReflectionCandidate(
                kind="mystery",
                title="Mystery",
                content="Unsupported extraction kind.",
                reason="fake provider emitted unsupported data",
                confidence=0.9,
            )
        ]
    )

    with pytest.raises(ValueError, match="unsupported reflection candidate kind"):
        await reflect_memory(
            "fake input",
            source_title="Fake",
            organization_id="org_123",
            extractor=extractor,
        )

    assert extractor.requests[0].source_title == "Fake"


def _grounded_candidate(content: str, *, kind: str = "claim") -> ReflectionCandidate:
    return ground_reflection_candidate(
        ReflectionCandidate(
            kind=kind,
            title=f"{kind.title()}: {content}",
            content=content,
            reason="test candidate",
            confidence=0.91,
        ),
        raw_source_ids=["raw-new"],
        suggested_memory_scope="project",
        suggested_scope_key="project_123",
        extraction_prompt_metadata={"extractor": "test"},
        source_id="raw-new",
    )


def _raw_memory(memory_id: str, content: str) -> RawMemory:
    return RawMemory(
        id=memory_id,
        organization_id="org_123",
        source_id=memory_id,
        principal_id="user_123",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        project_id="project_123",
        review_state="promoted",
        entity_type="claim",
        title=f"Memory {memory_id}",
        raw_content=content,
        tags=["reflection", "claim"],
        metadata={},
        capture_surface="reflection_candidate",
    )


def replace_raw_memory(memory: RawMemory, **updates: Any) -> RawMemory:
    data = {
        "id": memory.id,
        "organization_id": memory.organization_id,
        "source_id": memory.source_id,
        "principal_id": memory.principal_id,
        "memory_scope": memory.memory_scope,
        "scope_key": memory.scope_key,
        "agent_id": memory.agent_id,
        "project_id": memory.project_id,
        "review_state": memory.review_state,
        "entity_type": memory.entity_type,
        "title": memory.title,
        "raw_content": memory.raw_content,
        "tags": list(memory.tags),
        "metadata": dict(memory.metadata),
        "provenance": dict(memory.provenance),
        "capture_surface": memory.capture_surface,
        "captured_at": memory.captured_at,
        "created_at": memory.created_at,
        "score": memory.score,
    }
    data.update(updates)
    return RawMemory(**data)


def test_validation_rejects_persisted_candidates_without_source_ids() -> None:
    with pytest.raises(ValueError, match="lacks source_ids"):
        validate_reflection_candidates(
            [
                ReflectionCandidate(
                    kind="claim",
                    title="Claim: unsupported",
                    content="Unsupported without source.",
                    reason="missing source",
                    confidence=0.9,
                )
            ],
            require_source_ids=True,
        )
