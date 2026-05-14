from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from sibyl_core.services import native_memory
from sibyl_core.services.native_memory import (
    NativeReflectionWriteResult,
    NativeWriteMode,
    coerce_native_write_mode,
    native_reflection_write_enabled,
    native_write_mode_from_env,
    preview_reflection_candidate_promotion,
    promote_reflection_candidate_review,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory
from sibyl_core.tools.responses import AddResponse


def _raw_review_candidate(**overrides: object) -> RawMemory:
    values = {
        "id": "candidate-1",
        "organization_id": "org-1",
        "source_id": "source-1",
        "principal_id": "user-1",
        "memory_scope": MemoryScope.PRIVATE,
        "scope_key": None,
        "review_state": "pending",
        "entity_type": "decision",
        "title": "Decision: Promote review candidates",
        "raw_content": "We decided review candidates need explicit promotion.",
        "tags": ["reflection", "decision"],
        "metadata": {
            "capture_surface": "reflection_candidate",
            "remember_kind": "decision",
            "reflection_reason": "captures a durable decision",
            "reflection_confidence": 0.86,
            "raw_source_ids": ["source-1"],
            "suggested_memory_scope": "private",
            "suggested_scope_key": None,
            "domain": "sibyl",
        },
        "provenance": {"raw_source_ids": ["source-1"]},
        "capture_surface": "reflection_candidate",
        "captured_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
        "created_at": datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return RawMemory(**values)


def test_native_write_mode_defaults_enabled() -> None:
    assert coerce_native_write_mode(None) is NativeWriteMode.ENABLED
    assert coerce_native_write_mode("") is NativeWriteMode.ENABLED
    assert native_write_mode_from_env({}) is NativeWriteMode.ENABLED
    assert native_reflection_write_enabled({}) is True


def test_native_write_mode_accepts_enabled_values() -> None:
    assert coerce_native_write_mode("enabled") is NativeWriteMode.ENABLED
    assert coerce_native_write_mode("true") is NativeWriteMode.ENABLED
    assert native_write_mode_from_env({"SIBYL_NATIVE_WRITE": "1"}) is NativeWriteMode.ENABLED


def test_native_write_mode_accepts_disabled_values() -> None:
    assert coerce_native_write_mode("disabled") is NativeWriteMode.DISABLED
    assert coerce_native_write_mode("false") is NativeWriteMode.DISABLED
    assert native_write_mode_from_env({"SIBYL_NATIVE_WRITE": "0"}) is NativeWriteMode.DISABLED
    assert native_reflection_write_enabled({"SIBYL_NATIVE_WRITE": "off"}) is False


@pytest.mark.asyncio
async def test_preview_review_candidate_returns_policy_grounded_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _raw_review_candidate(
        metadata={
            **_raw_review_candidate().metadata,
            "suggested_memory_scope": "project",
            "suggested_scope_key": "project_123",
        }
    )
    source = _raw_review_candidate(id="source-1")
    monkeypatch.setattr(
        native_memory,
        "get_raw_memory",
        AsyncMock(side_effect=[candidate, source]),
    )
    persist = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(native_memory, "persist_reflection_candidate_native", persist)
    monkeypatch.setattr(native_memory, "save_raw_memory", save)

    result = await preview_reflection_candidate_promotion(
        candidate_id="candidate-1",
        organization_id="org-1",
        principal_id="user-1",
        promote_to_scope="project",
        promote_to_scope_key="project_123",
        project="project_123",
        accessible_projects={"project_123"},
    )

    assert result.allowed
    assert result.reason == "promotion_preview_allowed"
    assert result.memory_scope is MemoryScope.PROJECT
    assert result.scope_key == "project_123"
    assert result.raw_source_ids == ["source-1"]
    assert result.metadata is not None
    assert result.metadata["policy_reasons"] == [
        "same_scope_reflect_allowed",
        "same_scope_write_allowed",
    ]
    assert result.metadata["input_scopes"] == [
        {"id": "candidate-1", "memory_scope": "private", "scope_key": None},
        {"id": "source-1", "memory_scope": "private", "scope_key": None},
    ]
    persist.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_review_candidate_returns_missing_without_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_memory, "get_raw_memory", AsyncMock(return_value=None))
    persist = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(native_memory, "persist_reflection_candidate_native", persist)
    monkeypatch.setattr(native_memory, "save_raw_memory", save)

    result = await preview_reflection_candidate_promotion(
        candidate_id="candidate-1",
        organization_id="org-1",
        principal_id="user-1",
        promote_to_scope="project",
        promote_to_scope_key="project_123",
    )

    assert not result.allowed
    assert result.reason == "candidate_not_found"
    assert result.raw_source_ids == []
    persist.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_review_candidate_denies_unverified_project_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _raw_review_candidate()
    source = _raw_review_candidate(id="source-1")
    monkeypatch.setattr(
        native_memory,
        "get_raw_memory",
        AsyncMock(side_effect=[candidate, source]),
    )
    persist = AsyncMock()
    save = AsyncMock()
    monkeypatch.setattr(native_memory, "persist_reflection_candidate_native", persist)
    monkeypatch.setattr(native_memory, "save_raw_memory", save)

    result = await preview_reflection_candidate_promotion(
        candidate_id="candidate-1",
        organization_id="org-1",
        principal_id="user-1",
        promote_to_scope="project",
        promote_to_scope_key="project_123",
        accessible_projects={"project_other"},
    )

    assert not result.allowed
    assert result.reason == "unverified_membership"
    assert result.memory_scope is MemoryScope.PROJECT
    assert result.scope_key == "project_123"
    assert result.metadata is not None
    assert result.metadata["policy_reasons"] == [
        "unverified_membership",
        "unverified_membership",
    ]
    persist.assert_not_awaited()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_review_candidate_requires_explicit_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _raw_review_candidate()
    monkeypatch.setattr(
        native_memory,
        "get_raw_memory",
        AsyncMock(side_effect=[candidate, _raw_review_candidate(id="source-1")]),
    )
    persist = AsyncMock()
    monkeypatch.setattr(native_memory, "persist_reflection_candidate_native", persist)

    result = await promote_reflection_candidate_review(
        candidate_id="candidate-1",
        organization_id="org-1",
        principal_id="user-1",
        promote_to_scope=None,
    )

    assert not result.success
    assert result.reason == "missing_promote_to_scope"
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_review_candidate_denies_mixed_scope_without_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _raw_review_candidate()
    source = _raw_review_candidate(
        id="source-1",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
    )
    monkeypatch.setattr(
        native_memory,
        "get_raw_memory",
        AsyncMock(side_effect=[candidate, source]),
    )
    persist = AsyncMock()
    monkeypatch.setattr(native_memory, "persist_reflection_candidate_native", persist)

    result = await promote_reflection_candidate_review(
        candidate_id="candidate-1",
        organization_id="org-1",
        principal_id="user-1",
        promote_to_scope=None,
    )

    assert not result.success
    assert result.reason == "mixed_scope_inputs_require_promote_to_scope"
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_review_candidate_requires_broadest_mixed_scope_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _raw_review_candidate()
    source = _raw_review_candidate(
        id="source-1",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
    )
    monkeypatch.setattr(
        native_memory,
        "get_raw_memory",
        AsyncMock(side_effect=[candidate, source]),
    )
    persist = AsyncMock()
    monkeypatch.setattr(native_memory, "persist_reflection_candidate_native", persist)

    result = await promote_reflection_candidate_review(
        candidate_id="candidate-1",
        organization_id="org-1",
        principal_id="user-1",
        promote_to_scope="private",
    )

    assert not result.success
    assert result.reason == "promote_to_scope_must_match_broadest_input_scope"
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_review_candidate_persists_native_record_and_marks_promoted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _raw_review_candidate(
        metadata={
            **_raw_review_candidate().metadata,
            "suggested_memory_scope": "project",
            "suggested_scope_key": "project_123",
        }
    )
    source = _raw_review_candidate(id="source-1")
    saved: list[RawMemory] = []

    async def fake_persist(**kwargs):
        assert kwargs["memory_scope"] is MemoryScope.PROJECT
        assert kwargs["scope_key"] == "project_123"
        assert kwargs["source_id"] == "source-1"
        assert kwargs["accessible_projects"] == {"project_123"}
        assert kwargs["candidate"].metadata["review_capture_id"] == "candidate-1"
        return NativeReflectionWriteResult(
            response=AddResponse(
                success=True,
                id="decision_123",
                message="promoted",
                timestamp=datetime.now(UTC),
            ),
            metadata={
                "policy_allowed": True,
                "policy_reasons": [
                    "same_scope_reflect_allowed",
                    "same_scope_write_allowed",
                ],
                "native_write_path": "reflection_promotion",
            },
        )

    async def fake_save(memory: RawMemory) -> RawMemory:
        saved.append(memory)
        return memory

    monkeypatch.setattr(
        native_memory,
        "get_raw_memory",
        AsyncMock(side_effect=[candidate, source]),
    )
    monkeypatch.setattr(native_memory, "persist_reflection_candidate_native", fake_persist)
    monkeypatch.setattr(native_memory, "save_raw_memory", fake_save)

    result = await promote_reflection_candidate_review(
        candidate_id="candidate-1",
        organization_id="org-1",
        principal_id="user-1",
        promote_to_scope="project",
        promote_to_scope_key="project_123",
        project="project_123",
        accessible_projects={"project_123"},
    )

    assert result.success
    assert result.promoted_id == "decision_123"
    assert result.reason == "promoted"
    assert saved[0].review_state == "promoted"
    assert saved[0].metadata["promoted_entity_id"] == "decision_123"
