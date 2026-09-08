"""Public memory responses omit live foreign clocks, including clear tombstones."""

from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from sibyl.api.routes.entity_serialization import serialize_raw_capture
from sibyl.api.routes.memory_serialization import (
    memory_source_inspect_response,
    raw_memory_response,
)
from sibyl.api.routes.search import explore
from sibyl.api.schemas import EntityResponse, ExploreRequest
from sibyl.api.schemas.search import SearchResponse as ApiSearchResponse
from sibyl.api.schemas.traverse import ExpandNeighborsResponse as ApiNeighborsResponse
from sibyl.mcp_tools.serialization import to_dict
from sibyl.persistence.content_common import RawCaptureRecord
from sibyl_core.auth import ProjectRole
from sibyl_core.auth.memory_policy import MemoryPolicyAction, MemoryPolicyDecision
from sibyl_core.services.content_models import MemoryScope, RawMemory
from sibyl_core.tools.responses import (
    EntitySummary,
    ExpandNeighborsResponse,
    ExploreResponse,
    NeighborEntity,
    SearchResponse,
    SearchResult,
)
from tests.harness.auth import stub_auth_context


@pytest.fixture
def clock_metadata() -> dict:
    return {
        "correction_blockers": {"foreign-root": {"revision": 17, "blocking": False}},
        "source_bindings": {"known-source": 2},
        "source_validation_pending": False,
        "raw_source_ids": ["declared-source"],
        "authored": {"metadata": {"correction_blockers": "literal example"}},
    }


def _assert_public(public: dict, original: dict) -> None:
    assert "correction_blockers" not in public
    assert "source_bindings" not in public
    for key, value in original.items():
        if key not in {"correction_blockers", "source_bindings"}:
            assert public[key] == value
    assert original["correction_blockers"] == {"foreign-root": {"revision": 17, "blocking": False}}


def test_source_clock_visibility_raw_response(clock_metadata: dict) -> None:
    memory = RawMemory(
        id="derivative",
        organization_id="org",
        source_id="manual",
        principal_id="owner",
        metadata=clock_metadata,
        revision=9,
        raw_content="Own body",
    )
    response = raw_memory_response(memory)
    _assert_public(response.metadata, clock_metadata)
    assert response.revision == 9
    assert response.raw_content == "Own body"


@pytest.mark.parametrize("allowed", [False, True])
def test_source_clock_visibility_inspect_and_blame(clock_metadata: dict, allowed: bool) -> None:
    memory = RawMemory(
        id="derivative",
        organization_id="org",
        source_id="manual",
        principal_id="owner",
        metadata=clock_metadata,
        revision=9,
        raw_content="Own body",
    )
    response = memory_source_inspect_response(
        memory=memory,
        policy_decision=MemoryPolicyDecision(
            action=MemoryPolicyAction.READ,
            allowed=allowed,
            reason="allowed" if allowed else "private_principal_mismatch",
            memory_scope=MemoryScope.PRIVATE,
        ),
        audit_events=[],
    )
    _assert_public(response.metadata, clock_metadata)
    assert response.content_redacted is not allowed
    assert response.revision == 9
    assert "foreign-root" not in str(response.lifecycle)
    assert "foreign-root" not in str(response.visibility)


def test_source_clock_visibility_raw_capture(clock_metadata: dict) -> None:
    capture = RawCaptureRecord(
        organization_id=uuid4(),
        title="Capture",
        raw_content="Own body",
        entity_type="pattern",
        metadata=clock_metadata,
        created_at=datetime(2026, 9, 7, tzinfo=UTC),
    )
    response = serialize_raw_capture(capture)
    _assert_public(response.metadata, clock_metadata)
    assert response.raw_content == "Own body"


def test_source_clock_visibility_entity_output(clock_metadata: dict) -> None:
    response = EntityResponse(
        id="derivative",
        entity_type="pattern",
        name="Pattern",
        content="Own body",
        metadata=clock_metadata,
    )
    _assert_public(response.metadata, clock_metadata)
    _assert_public(response.model_dump()["metadata"], clock_metadata)


@pytest.mark.parametrize("surface", ["search", "explore", "neighbors"])
def test_source_clock_visibility_mcp_rows(clock_metadata: dict, surface: str) -> None:
    before = deepcopy(clock_metadata)
    if surface == "search":
        result = SearchResponse(
            results=[
                SearchResult(
                    id="derivative",
                    type="pattern",
                    name="Pattern",
                    content="Own body",
                    score=1,
                    metadata=clock_metadata,
                )
            ],
            total=1,
            query="pattern",
            filters={},
        )
        key = "results"
    elif surface == "explore":
        result = ExploreResponse(
            mode="list",
            entities=[
                EntitySummary(
                    id="derivative",
                    type="pattern",
                    name="Pattern",
                    description="Own body",
                    metadata=clock_metadata,
                )
            ],
            total=1,
            filters={},
        )
        key = "entities"
    else:
        result = ExpandNeighborsResponse(
            origins=["seed"],
            neighbors=[
                NeighborEntity(
                    id="derivative",
                    type="pattern",
                    name="Pattern",
                    relationship="supports",
                    direction="outgoing",
                    distance=1,
                    score=1,
                    metadata=clock_metadata,
                )
            ],
            total=1,
            depth=1,
            limit=10,
        )
        key = "neighbors"
    payload = to_dict(result)
    _assert_public(payload[key][0]["metadata"], clock_metadata)
    assert clock_metadata == before
    # Arbitrary caller dictionaries are not response DTOs.
    assert to_dict({"metadata": clock_metadata}) == {"metadata": clock_metadata}


@pytest.mark.parametrize("surface", ["search", "neighbors"])
def test_source_clock_visibility_rest_nested_rows(clock_metadata: dict, surface: str) -> None:
    row = {
        "id": "derivative",
        "type": "pattern",
        "name": "Pattern",
        "score": 1,
        "content": "Own body",
        "metadata": clock_metadata,
    }
    if surface == "search":
        response = ApiSearchResponse(results=[row], total=1, query="pattern", filters={})
        public = response.results[0].metadata
    else:
        row.update(relationship="supports", direction="outgoing")
        response = ApiNeighborsResponse(
            origins=["seed"],
            neighbors=[row],
            total=1,
            depth=1,
            limit=10,
        )
        public = response.neighbors[0].metadata
    _assert_public(public, clock_metadata)


@pytest.mark.asyncio
@pytest.mark.parametrize("as_mapping", [False, True])
async def test_source_clock_visibility_rest_explore(clock_metadata: dict, as_mapping: bool) -> None:
    entity = EntitySummary(
        id="derivative",
        type="pattern",
        name="Pattern",
        description="Own body",
        metadata=clock_metadata,
    )
    row = vars(entity) if as_mapping else entity
    result = SimpleNamespace(mode="list", entities=[row], total=1, filters={})
    with (
        patch(
            "sibyl.api.routes.search.verify_entity_project_access",
            AsyncMock(return_value=ProjectRole.VIEWER),
        ),
        patch("sibyl_core.tools.core.explore", AsyncMock(return_value=result)),
    ):
        response = await explore(
            request=ExploreRequest(mode="list", project_ids=["project"]),
            org=SimpleNamespace(id=uuid4()),
            ctx=stub_auth_context(),
        )
    _assert_public(response.entities[0]["metadata"], clock_metadata)


@pytest.mark.parametrize("surface", ["promotion", "share", "autonomy"])
def test_source_clock_visibility_promotion_result_wrappers(clock_metadata, surface):
    from sibyl.api.routes.memory_serialization import (
        autonomy_response,
        promotion_response,
        share_response,
    )
    from sibyl_core.services.memory_autonomy import (
        ReflectionAutonomyAction,
        ReflectionAutonomyDecision,
        ReflectionAutonomyOutcome,
    )
    from sibyl_core.services.memory_contract import (
        MemorySharePreview,
        MemoryShareResult,
        ReflectionPromotionPreview,
        ReflectionPromotionResult,
    )

    before = deepcopy(clock_metadata)
    result = ReflectionPromotionResult(
        success=True,
        candidate_id="candidate",
        promoted_id="promoted",
        reason="promoted",
        review_state="promoted",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source"],
        metadata=clock_metadata,
    )
    if surface == "promotion":
        public = promotion_response(result)
    elif surface == "share":
        preview = MemorySharePreview(
            allowed=True,
            reason="allowed",
            target_scope=MemoryScope.PRIVATE,
            target_scope_key=None,
            source_ids=["source"],
            visible_source_ids=["source"],
            denied_source_ids=[],
            missing_source_ids=[],
            redacted_count=0,
            hidden_but_relevant_count=0,
        )
        response = share_response(
            MemoryShareResult(
                applied=True,
                reason="shared",
                preview=preview,
                promotions=(result,),
            ),
            audit_event_ids=[],
        )
        public = response.promotions[0]
    else:
        decision = ReflectionAutonomyDecision(
            outcome=ReflectionAutonomyOutcome.AUTO_PROMOTE,
            recommended_action=ReflectionAutonomyAction.PROMOTE,
            candidate_id="candidate",
            reason="promoted",
            review_state="promoted",
            memory_scope=MemoryScope.PRIVATE,
            scope_key=None,
            raw_source_ids=["source"],
            policy_reasons=[],
            exception_reasons=[],
            confidence=1.0,
            confidence_threshold=0.9,
        )
        preview = ReflectionPromotionPreview(
            allowed=True,
            candidate_id="candidate",
            reason="allowed",
            review_state="pending",
            memory_scope=MemoryScope.PRIVATE,
            scope_key=None,
            raw_source_ids=["source"],
        )
        response = autonomy_response(decision=decision, preview=preview, promotion=result)
        public = response.promotion
    _assert_public(public.metadata, clock_metadata)
    assert clock_metadata == before
