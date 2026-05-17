from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from sibyl_core.models.reflection import (
    memory_lifecycle_from_metadata,
    reflection_findings_from_metadata,
)
from sibyl_core.services import native_memory
from sibyl_core.services.native_memory import (
    NativeReflectionWriteResult,
    NativeWriteMode,
    apply_memory_correction,
    coerce_native_write_mode,
    native_reflection_write_enabled,
    native_write_mode_from_env,
    preview_memory_access,
    preview_memory_correction,
    preview_memory_share,
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
async def test_share_preview_denies_organization_target_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _raw_review_candidate(id="source-1")
    monkeypatch.setattr(native_memory, "get_raw_memory", AsyncMock(return_value=source))
    save = AsyncMock()
    monkeypatch.setattr(native_memory, "save_raw_memory", save)

    result = await preview_memory_share(
        source_ids=["source-1"],
        organization_id="org-1",
        principal_id="user-1",
        target_scope="organization",
    )

    assert not result.allowed
    assert result.reason == "scope_not_enabled"
    assert result.target_scope is MemoryScope.ORGANIZATION
    assert result.source_ids == ["source-1"]
    assert result.visible_source_ids == ["source-1"]
    assert result.denied_source_ids == []
    assert result.redacted_count == 0
    assert result.hidden_but_relevant_count == 0
    assert result.metadata is not None
    assert result.metadata["policy_reasons"] == [
        "scope_not_enabled",
        "private_principal_bound",
    ]
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_share_preview_redacts_unreadable_and_missing_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _raw_review_candidate(id="source-1", principal_id="other-user")
    monkeypatch.setattr(
        native_memory,
        "get_raw_memory",
        AsyncMock(side_effect=[source, None]),
    )
    save = AsyncMock()
    monkeypatch.setattr(native_memory, "save_raw_memory", save)

    result = await preview_memory_share(
        source_ids=["source-1", "missing-1"],
        organization_id="org-1",
        principal_id="user-1",
        target_scope="shared",
    )

    assert not result.allowed
    assert result.reason == "scope_not_enabled"
    assert result.visible_source_ids == []
    assert result.denied_source_ids == ["source-1", "missing-1"]
    assert result.missing_source_ids == ["missing-1"]
    assert result.redacted_count == 1
    assert result.hidden_but_relevant_count == 1
    assert result.metadata is not None
    assert result.metadata["input_scopes"] == []
    assert result.metadata["missing_source_ids"] == ["missing-1"]
    assert result.metadata["source_denial_reasons"] == {
        "source-1": "principal_mismatch",
        "missing-1": "source_not_found",
    }
    assert result.metadata["policy_reasons"] == [
        "scope_not_enabled",
        "principal_mismatch",
        "source_not_found",
    ]
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_preview_uses_selected_project_space_as_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_memory = _raw_review_candidate(
        id="raw-1",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        principal_id="user-2",
    )
    list_memories = AsyncMock(return_value=[raw_memory])
    monkeypatch.setattr(native_memory, "list_raw_memories_for_scope", list_memories)

    result = await preview_memory_access(
        organization_id="org-1",
        actor_user_id="user-1",
        target_principal_type="agent",
        target_principal_id="agent:nova",
        memory_spaces=[
            {
                "id": "space-1",
                "memory_scope": "project",
                "scope_key": "project_123",
                "state": "active",
            }
        ],
        limit=25,
    )

    assert result.allowed
    assert result.reason == "access_preview_allowed"
    assert result.visible_source_ids == ["raw-1"]
    assert result.metadata is not None
    assert result.metadata["access_state"] == "allowed"
    assert result.metadata["policy_reasons"] == ["project_access_verified"]
    list_memories.assert_awaited_once_with(
        organization_id="org-1",
        principal_id="user-1",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        agent_id=None,
        limit=100,
        include_lifecycle_hidden=True,
    )


@pytest.mark.asyncio
async def test_access_preview_denies_disabled_space_without_listing_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    list_memories = AsyncMock()
    monkeypatch.setattr(native_memory, "list_raw_memories_for_scope", list_memories)

    result = await preview_memory_access(
        organization_id="org-1",
        actor_user_id="user-1",
        target_principal_type="agent",
        target_principal_id="agent:nova",
        memory_spaces=[
            {
                "id": "space-1",
                "memory_scope": "team",
                "scope_key": "team_123",
                "state": "disabled",
                "disabled_reason": "scope_not_enabled",
            }
        ],
    )

    assert not result.allowed
    assert result.reason == "scope_not_enabled"
    assert result.visible_source_ids == []
    assert result.redacted_count == 1
    assert result.hidden_but_relevant_count == 1
    assert result.metadata is not None
    assert result.metadata["access_state"] == "denied"
    assert result.metadata["denied_memory_space_ids"] == ["space-1"]
    list_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_access_preview_keeps_denials_after_source_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_memory = _raw_review_candidate(
        id="raw-1",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        principal_id="user-2",
    )
    list_memories = AsyncMock(return_value=[raw_memory])
    monkeypatch.setattr(native_memory, "list_raw_memories_for_scope", list_memories)

    result = await preview_memory_access(
        organization_id="org-1",
        actor_user_id="user-1",
        target_principal_type="agent",
        target_principal_id="agent:nova",
        memory_spaces=[
            {
                "id": "space-1",
                "memory_scope": "project",
                "scope_key": "project_123",
                "state": "active",
            },
            {
                "id": "space-2",
                "memory_scope": "team",
                "scope_key": "team_123",
                "state": "disabled",
                "disabled_reason": "scope_not_enabled",
            },
        ],
        limit=1,
    )

    assert not result.allowed
    assert result.reason == "scope_not_enabled"
    assert result.visible_source_ids == ["raw-1"]
    assert result.redacted_count == 1
    assert result.metadata is not None
    assert result.metadata["access_state"] == "partial"
    assert result.metadata["denied_memory_space_ids"] == ["space-2"]
    assert result.metadata["policy_reasons"] == [
        "project_access_verified",
        "scope_not_enabled",
    ]


@pytest.mark.asyncio
async def test_access_preview_counts_lifecycle_hidden_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden_memory = _raw_review_candidate(
        id="hidden-1",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        principal_id="user-2",
        review_state="hidden",
    )
    visible_memory = _raw_review_candidate(
        id="visible-1",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
        principal_id="user-2",
    )
    list_memories = AsyncMock(return_value=[hidden_memory, visible_memory])
    monkeypatch.setattr(native_memory, "list_raw_memories_for_scope", list_memories)

    result = await preview_memory_access(
        organization_id="org-1",
        actor_user_id="user-1",
        target_principal_type="agent",
        target_principal_id="agent:nova",
        memory_spaces=[
            {
                "id": "space-1",
                "memory_scope": "project",
                "scope_key": "project_123",
                "state": "active",
            }
        ],
        limit=5,
    )

    assert not result.allowed
    assert result.reason == "lifecycle_hidden"
    assert result.visible_source_ids == ["visible-1"]
    assert result.denied_source_ids == ["hidden-1"]
    assert result.redacted_count == 1
    assert result.metadata is not None
    assert result.metadata["access_state"] == "partial"
    assert result.metadata["lifecycle_hidden_source_ids"] == ["hidden-1"]


@pytest.mark.asyncio
async def test_memory_correction_preview_requires_supersede_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_review_candidate(id="source-1")
    monkeypatch.setattr(native_memory, "get_raw_memory", AsyncMock(return_value=memory))
    monkeypatch.setattr(native_memory, "get_raw_memory_by_source_id", AsyncMock())

    result = await preview_memory_correction(
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-1",
        action="supersede",
    )

    assert not result.allowed
    assert result.reason == "missing_replacement_source"
    assert result.target_review_state == "superseded"


@pytest.mark.asyncio
async def test_apply_memory_correction_marks_hidden_and_preserves_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_review_candidate(
        id="source-1",
        principal_id="user-1",
        metadata={"correction_history": [{"action": "mark_stale"}]},
    )
    save_raw_memory = AsyncMock(side_effect=lambda updated: updated)
    monkeypatch.setattr(native_memory, "get_raw_memory", AsyncMock(return_value=memory))
    monkeypatch.setattr(native_memory, "get_raw_memory_by_source_id", AsyncMock())
    monkeypatch.setattr(native_memory, "save_raw_memory", save_raw_memory)

    result = await apply_memory_correction(
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-1",
        action="hide",
        reason="No longer useful",
    )

    assert result.applied
    assert result.preview.allowed
    assert result.preview.audit_action == "memory.correction.hide"
    assert result.updated_memory is not None
    assert result.updated_memory.review_state == "hidden"
    assert result.updated_memory.metadata["lifecycle_state"] == "hidden"
    assert result.updated_memory.metadata["lifecycle_reason"] == "No longer useful"
    lifecycle = memory_lifecycle_from_metadata(
        result.updated_memory.metadata,
        source_id="source-1",
        review_state=result.updated_memory.review_state,
    )
    findings = reflection_findings_from_metadata(result.updated_memory.metadata)
    assert lifecycle.state == "hidden"
    assert lifecycle.action == "hide"
    assert findings[-1].kind == "correction"
    assert findings[-1].target_source_id == "source-1"
    assert result.updated_memory.metadata["correction_history"][0] == {
        "action": "mark_stale"
    }
    assert result.updated_memory.metadata["correction_history"][1]["action"] == "hide"
    save_raw_memory.assert_awaited_once()


@pytest.mark.asyncio
async def test_memory_correction_preview_canonicalizes_supersede_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_review_candidate(id="source-1")
    replacement = _raw_review_candidate(id="replacement-1", source_id="external:replacement")
    monkeypatch.setattr(
        native_memory,
        "get_raw_memory",
        AsyncMock(side_effect=[memory, None]),
    )
    monkeypatch.setattr(
        native_memory,
        "get_raw_memory_by_source_id",
        AsyncMock(return_value=replacement),
    )

    result = await preview_memory_correction(
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-1",
        action="supersede",
        replacement_source_id="external:replacement",
    )

    assert result.allowed
    assert result.metadata is not None
    assert result.metadata["replacement_source_id"] == "replacement-1"


@pytest.mark.asyncio
async def test_memory_correction_preview_rejects_self_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_review_candidate(id="source-1")
    monkeypatch.setattr(
        native_memory,
        "get_raw_memory",
        AsyncMock(side_effect=[memory, memory]),
    )
    monkeypatch.setattr(native_memory, "get_raw_memory_by_source_id", AsyncMock())

    result = await preview_memory_correction(
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-1",
        action="mark_duplicate",
        duplicate_of_source_id="source-1",
    )

    assert not result.allowed
    assert result.reason == "duplicate_source_self_reference"


@pytest.mark.asyncio
async def test_apply_memory_correction_restore_preserves_prior_review_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = _raw_review_candidate(
        id="source-1",
        principal_id="user-1",
        review_state="hidden",
        metadata={
            "correction_history": [{"action": "hide"}],
            "hidden_at": "2026-05-14T12:00:00+00:00",
            "lifecycle_state": "hidden",
            "prior_review_state": "promoted",
        },
    )
    save_raw_memory = AsyncMock(side_effect=lambda updated: updated)
    monkeypatch.setattr(native_memory, "get_raw_memory", AsyncMock(return_value=memory))
    monkeypatch.setattr(native_memory, "get_raw_memory_by_source_id", AsyncMock())
    monkeypatch.setattr(native_memory, "save_raw_memory", save_raw_memory)

    result = await apply_memory_correction(
        organization_id="org-1",
        source_id="source-1",
        principal_id="user-1",
        action="restore",
    )

    assert result.applied
    assert result.updated_memory is not None
    assert result.updated_memory.review_state == "promoted"
    assert result.updated_memory.metadata["correction_history"][1]["action"] == "restore"
    assert "prior_review_state" not in result.updated_memory.metadata
    assert "hidden_at" not in result.updated_memory.metadata
    lifecycle = memory_lifecycle_from_metadata(
        result.updated_memory.metadata,
        source_id="source-1",
        review_state=result.updated_memory.review_state,
    )
    findings = reflection_findings_from_metadata(result.updated_memory.metadata)
    assert lifecycle.state == "promoted"
    assert lifecycle.action == "restore"
    assert findings[-1].action == "restore"


@pytest.mark.asyncio
async def test_share_preview_allows_visible_project_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _raw_review_candidate(
        id="source-1",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_123",
    )
    monkeypatch.setattr(native_memory, "get_raw_memory", AsyncMock(return_value=source))
    save = AsyncMock()
    monkeypatch.setattr(native_memory, "save_raw_memory", save)

    result = await preview_memory_share(
        source_ids=["source-1"],
        organization_id="org-1",
        principal_id="user-1",
        target_scope="shared",
        accessible_projects={"project_123"},
    )

    assert not result.allowed
    assert result.reason == "scope_not_enabled"
    assert result.visible_source_ids == ["source-1"]
    assert result.denied_source_ids == []
    assert result.missing_source_ids == []
    assert result.metadata is not None
    assert result.metadata["input_scopes"] == [
        {
            "id": "source-1",
            "memory_scope": "project",
            "scope_key": "project_123",
        }
    ]
    assert result.metadata["policy_reasons"] == [
        "scope_not_enabled",
        "project_access_verified",
    ]
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_share_preview_cross_org_remains_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _raw_review_candidate(id="source-1")
    monkeypatch.setattr(native_memory, "get_raw_memory", AsyncMock(return_value=source))
    save = AsyncMock()
    monkeypatch.setattr(native_memory, "save_raw_memory", save)

    result = await preview_memory_share(
        source_ids=["source-1"],
        organization_id="org-1",
        principal_id="user-1",
        target_scope="project",
        target_scope_key="project_123",
        recipient_organization_id="org-2",
        accessible_projects={"project_123"},
    )

    assert not result.allowed
    assert result.reason == "scope_not_enabled"
    assert result.target_scope is MemoryScope.PROJECT
    assert result.visible_source_ids == ["source-1"]
    assert result.metadata is not None
    assert result.metadata["cross_organization"] is True
    assert result.metadata["target_policy_reason"] == "scope_not_enabled"
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
async def test_promote_review_candidate_denies_inaccessible_source_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _raw_review_candidate(
        memory_scope=MemoryScope.PROJECT,
        scope_key="project_secret",
        principal_id="victim-user",
    )
    source = _raw_review_candidate(id="source-1")
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
        principal_id="attacker-user",
        promote_to_scope="private",
        accessible_projects=set(),
    )

    assert not result.success
    assert result.reason == "unverified_membership"
    assert result.memory_scope is MemoryScope.PROJECT
    assert result.scope_key == "project_secret"
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
    lifecycle = memory_lifecycle_from_metadata(
        saved[0].metadata,
        source_id="candidate-1",
        review_state=saved[0].review_state,
    )
    findings = reflection_findings_from_metadata(saved[0].metadata)
    assert lifecycle.state == "promoted"
    assert lifecycle.derived_ids == ["decision_123"]
    assert findings[-1].kind == "promotion"
    assert findings[-1].related_source_ids == ["decision_123"]
