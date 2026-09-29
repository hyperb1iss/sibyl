"""Raw recall serves promoted dream proposals, never drafts the critic has not accepted."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from sibyl_core.services import content_client, content_raw_recall, validation_promotion
from sibyl_core.services.content_models import (
    RawMemory,
    RawMemoryRecallResult,
    raw_memory_unpublished_reflection_candidate,
)
from sibyl_core.services.surreal_content import (
    MemoryScope,
    recall_raw_memory,
    recall_raw_memory_with_sources,
    remember_raw_memory,
)
from tests.test_reflection_identity import content_store as content_store


def _memory(**overrides: object) -> RawMemory:
    values: dict[str, object] = {
        "id": "candidate",
        "organization_id": "org",
        "source_id": "source",
        "principal_id": "owner",
        "memory_scope": MemoryScope.PRIVATE,
        "scope_key": None,
        "review_state": "pending",
        "entity_type": "procedure",
        "title": "Reject malformed percent escapes",
        "raw_content": "Validate every escape before decoding once.",
        "tags": [],
        "metadata": {},
        "provenance": {},
        "capture_surface": "reflection_candidate",
        "captured_at": datetime(2026, 9, 23, tzinfo=UTC),
        "created_at": datetime(2026, 9, 23, tzinfo=UTC),
        "revision": 1,
    }
    values.update(overrides)
    return RawMemory(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("overrides", "unpublished"),
    [
        ({}, True),
        ({"review_state": "PENDING"}, True),
        ({"review_state": "promoted"}, False),
        ({"review_state": " Promoted "}, False),
        ({"capture_surface": None, "metadata": {"capture_surface": "reflection_candidate"}}, True),
        ({"capture_surface": "verified_eval"}, False),
        ({"capture_surface": None}, False),
        # The stored column wins over caller metadata: an ordinary capture that
        # sends capture_surface in its metadata, or a migrated row whose column
        # records the migration, is not a candidate.
        (
            {"capture_surface": "api", "metadata": {"capture_surface": "reflection_candidate"}},
            False,
        ),
        (
            {
                "capture_surface": "migration",
                "metadata": {"capture_surface": "reflection_candidate"},
            },
            False,
        ),
    ],
)
def test_only_an_unpromoted_reflection_candidate_is_unpublished(overrides, unpublished) -> None:
    assert raw_memory_unpublished_reflection_candidate(_memory(**overrides)) is unpublished


async def _set_review_state(memory_id: str, review_state: str) -> None:
    async with content_client.surreal_content_client() as client:
        await client.execute_query(
            "UPDATE raw_captures SET review_state = $state WHERE uuid = $id;",
            state=review_state,
            id=memory_id,
        )


@pytest.mark.parametrize("recall", [recall_raw_memory, recall_raw_memory_with_sources])
@pytest.mark.parametrize("lexical_fallback", [False, True])
async def test_recall_serves_the_promoted_correction_not_its_draft_or_stalled_child(
    content_store, monkeypatch, recall, lexical_fallback
):
    """The 2026-09-23 scoped cycle's lineage, shaped the same way.

    A draft proposal was corrected, the correction was promoted, and the draft
    was retired as superseded in the supersession ledger while its raw row kept
    review_state pending. A second chain stalled with its correction child
    pending. Before the reader gate, recall returned the draft and the stalled
    child beside the promoted correction, so an agent read two unreviewed
    proposals as memory.
    """
    monkeypatch.setattr(
        content_raw_recall, "raw_memory_query_embedding", AsyncMock(return_value=None)
    )
    if lexical_fallback:
        monkeypatch.setattr(
            content_raw_recall,
            "_recall_raw_memory_fulltext",
            AsyncMock(side_effect=RuntimeError("exercise lexical fallback")),
        )
    org = str(uuid4())

    async def remember(source: str, **kwargs: object) -> RawMemory:
        return await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id=source,
            raw_content=f"Telescope routing lesson from {source}",
            embedding_provider=None,
            **kwargs,  # type: ignore[arg-type]
        )

    episode = await remember("episode")
    draft = await remember("draft", capture_surface="reflection_candidate")
    promoted = await remember(
        "correction",
        capture_surface="reflection_candidate",
        metadata={"automatic_correction": {"parent_id": draft.id, "execution_id": "exec-1"}},
    )
    stalled = await remember(
        "stalled-child",
        capture_surface="reflection_candidate",
        metadata={"automatic_correction": {"parent_id": "archived", "execution_id": "exec-2"}},
    )
    await _set_review_state(promoted.id, "promoted")

    result = await recall(organization_id=org, principal_id="owner", query="telescope")
    memories = result.memories if isinstance(result, RawMemoryRecallResult) else result
    recalled = {memory.id for memory in memories}

    assert episode.id in recalled
    assert promoted.id in recalled
    assert draft.id not in recalled
    assert stalled.id not in recalled


@pytest.mark.parametrize(
    "binding_json",
    [
        "not json",
        '{"execution_id": "bad"}',
        '{"execution_id":"%s","request_sha256":"%s","result_sha256":"%s",'
        '"input_sha256":"%s","newer_field":1}' % (("a" * 64,) * 4),
    ],
)
async def test_a_binding_this_build_cannot_parse_reads_unavailable(
    binding_json, monkeypatch
) -> None:
    """Parsed for real, not patched: invalid JSON, a failed pattern, an extra field.

    The model forbids extra fields and pins hex digests, so each raises a
    pydantic ValidationError. The check must return False rather than let the
    error escape into recall, where it fails the whole raw lane, and report the
    candidate once however often recall rereads it.
    """
    validation_promotion._report_unreadable_binding.cache_clear()
    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        validation_promotion.log,
        "warning",
        lambda event, **fields: warnings.append((event, fields)),
    )
    memory = _memory(organization_id="org", id="candidate")

    for _ in range(2):
        assert (
            await validation_promotion.validation_binding_current(
                memory, {"validation_binding_json": binding_json}
            )
            is False
        )
    assert warnings == [
        ("validation_binding_unreadable", {"organization_id": "org", "candidate_id": "candidate"})
    ]


def _unreadable_result_rig(monkeypatch, *, error: BaseException, failed_read: str):
    execution = AsyncMock()
    execution.load.return_value = {"state": "returned"}
    if failed_read == "execution_result":
        execution.result.side_effect = error
    monkeypatch.setattr(validation_promotion, "ValidationExecution", lambda *_args: execution)
    monkeypatch.setattr(
        validation_promotion,
        "validated_result",
        Mock(side_effect=error if failed_read == "binding_result" else None),
    )
    validation_promotion._report_unreadable_result.cache_clear()
    warnings: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        validation_promotion.log,
        "warning",
        lambda event, **fields: warnings.append((event, fields)),
    )
    return warnings


_BINDING = (
    '{"execution_id":"%s","request_sha256":"%s","result_sha256":"%s","input_sha256":"%s"}'
    % (("e" * 64,) * 4)
)


def _schema_error() -> ValidationError:
    class _Strict(BaseModel):
        status: int

    try:
        _Strict.model_validate({"status": "ordinary_cohort_proposal"})
    except ValidationError as exc:
        return exc
    raise AssertionError("expected a validation error")


@pytest.mark.parametrize("failed_read", ["binding_result", "execution_result"])
async def test_an_unreadable_stored_result_is_reported_once_per_execution(
    monkeypatch, failed_read
) -> None:
    warnings = _unreadable_result_rig(monkeypatch, error=_schema_error(), failed_read=failed_read)
    memory = replace(_memory(), organization_id="org-9", id="candidate-9")

    for _ in range(3):
        assert (
            await validation_promotion.validation_binding_current(
                memory, {"validation_binding_json": _BINDING}
            )
            is False
        )

    assert warnings == [
        (
            "validation_result_unreadable",
            {
                "organization_id": "org-9",
                "candidate_id": "candidate-9",
                "execution_id": "e" * 64,
                "error_count": 1,
            },
        )
    ]


@pytest.mark.parametrize("failed_read", ["binding_result", "execution_result"])
async def test_an_ordinary_invalidation_stays_quiet(monkeypatch, failed_read) -> None:
    warnings = _unreadable_result_rig(
        monkeypatch, error=ValueError("binding changed"), failed_read=failed_read
    )

    assert (
        await validation_promotion.validation_binding_current(
            _memory(), {"validation_binding_json": _BINDING}
        )
        is False
    )
    assert warnings == []


@pytest.mark.parametrize(
    ("surface", "review_state", "metadata"),
    [
        ("reflection_candidate", "pending", {}),
        ("reflection_candidate", "deferred", {}),
        (" Reflection_Candidate ", "pending", {}),
        (None, "pending", {"capture_surface": "reflection_candidate"}),
    ],
)
async def test_pending_candidates_never_take_the_slots_of_matching_memories(
    content_store, monkeypatch, surface, review_state, metadata
):
    """Eight newer pending drafts and one matching episode, at limit 2.

    Filtered after each lane's limit, the drafts filled every slot and recall
    returned nothing although the episode matched. Excluded in the query, the
    episode comes back.
    """
    monkeypatch.setattr(
        content_raw_recall, "raw_memory_query_embedding", AsyncMock(return_value=None)
    )
    org = str(uuid4())
    episode = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="episode",
        raw_content="telescope routing lesson",
        embedding_provider=None,
    )
    for index in range(8):
        draft = await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id=f"draft-{index}",
            raw_content="telescope routing lesson",
            capture_surface=surface,
            metadata=metadata,
            embedding_provider=None,
        )
        await _set_review_state(draft.id, review_state)

    for recall in (recall_raw_memory, recall_raw_memory_with_sources):
        result = await recall(organization_id=org, principal_id="owner", query="telescope", limit=2)
        memories = result.memories if isinstance(result, RawMemoryRecallResult) else result
        assert [memory.id for memory in memories] == [episode.id]


@pytest.mark.parametrize("surface", [None, "api"])
@pytest.mark.parametrize("metadata_surface", [False, 4, [], {"surface": "reflection_candidate"}])
async def test_non_string_surface_metadata_cannot_break_recall(
    content_store, monkeypatch, surface, metadata_surface
):
    monkeypatch.setattr(
        content_raw_recall, "raw_memory_query_embedding", AsyncMock(return_value=None)
    )
    org = str(uuid4())
    capture = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="ordinary-capture",
        raw_content="telescope routing lesson",
        capture_surface=surface,
        metadata={"capture_surface": metadata_surface},
        embedding_provider=None,
    )

    recalled = await recall_raw_memory(organization_id=org, principal_id="owner", query="telescope")

    assert [memory.id for memory in recalled] == [capture.id]


async def test_metadata_cannot_hide_an_ordinary_capture_from_recall(content_store, monkeypatch):
    monkeypatch.setattr(
        content_raw_recall, "raw_memory_query_embedding", AsyncMock(return_value=None)
    )
    org = str(uuid4())
    spoofed = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="api-capture",
        raw_content="telescope routing lesson",
        capture_surface="api",
        metadata={"capture_surface": "reflection_candidate"},
        embedding_provider=None,
    )

    recalled = await recall_raw_memory(organization_id=org, principal_id="owner", query="telescope")

    assert [memory.id for memory in recalled] == [spoofed.id]
