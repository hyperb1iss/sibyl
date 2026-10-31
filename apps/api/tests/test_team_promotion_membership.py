"""Team promotion resolves the reviewer's memberships from the auth store.

Each caller of the promotion services (the REST preview and promote routes,
auto-review and the review drain, and the dream cycle) has to hand the policy
the principal's team memberships. These tests drive the real promotion
planner and policy behind each caller, with only storage and the auth store
stubbed, so a missing membership shows up as the denial a user would see.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.routes.memory_promotion import (
    preview_memory_promotion,
    preview_reflection_promotion,
    promote_memory,
    promote_reflection_candidate,
)
from sibyl.api.routes.memory_review import (
    auto_review_reflection_candidate,
    drain_reflection_review,
)
from sibyl.api.schemas import (
    ReflectionAutonomyRequest,
    ReflectionPromotionRequest,
    ReflectionReviewDrainRequest,
)
from sibyl.jobs.reflection import run_reflection_dream_cycle
from sibyl_core.auth import OrganizationRole
from sibyl_core.memory_pipeline.observations import (
    SourceIdentity,
    SourceKind,
    SourceObservation,
    evidence_hash,
)
from sibyl_core.services import memory_reflection
from sibyl_core.services.memory import ReflectionPromotionResult
from sibyl_core.services.source_state_store import RawSourceSnapshot
from sibyl_core.services.surreal_content import MemoryScope, RawMemory

ORG_ID = "00000000-0000-0000-0000-000000000111"
USER_ID = "00000000-0000-0000-0000-000000000222"
TEAM = "0f8b6c52-4d2e-4a7a-9c1d-6e5b2a9f3d10"
OTHER_TEAM = "7d1e2f3a-0000-4000-8000-000000000999"


def _org() -> MagicMock:
    org = MagicMock()
    org.id = uuid4()
    return org


def _ctx(api_key_memory_scope_keys: set[str] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = USER_ID
    ctx.organization_id = ORG_ID
    ctx.org_role = OrganizationRole.MEMBER
    ctx.api_key_memory_scope_keys = api_key_memory_scope_keys
    return ctx


def _http_request() -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"user-agent": "SibylTest/1.0"},
    )


def _row(**overrides: object) -> RawMemory:
    values: dict[str, object] = {
        "id": "source-1",
        "organization_id": ORG_ID,
        "source_id": "cli:manual",
        "principal_id": USER_ID,
        "memory_scope": MemoryScope.PRIVATE,
        "scope_key": None,
        "review_state": "pending",
        "entity_type": "rule",
        "title": "Review ritual",
        "raw_content": "Verifier passes are mandatory.",
        "tags": ["reflection"],
        "metadata": {"domain": "sibyl"},
        "provenance": {},
        "capture_surface": "cli",
        "captured_at": datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC),
        "created_at": datetime(2026, 10, 30, 12, 0, 0, tzinfo=UTC),
    }
    values.update(overrides)
    values.setdefault("observed_revision", values.get("revision", 1))
    return RawMemory(**values)


def _candidate(nominated_team: str | None = TEAM) -> RawMemory:
    """A pending candidate reflected from a `--propose-scope team` capture."""
    return _row(
        id="candidate-1",
        source_id="source-1",
        capture_surface="reflection_candidate",
        metadata={
            "capture_surface": "reflection_candidate",
            "remember_kind": "rule",
            "confidence": 0.95,
            "raw_source_ids": ["source-1"],
            "suggested_memory_scope": "team",
            "suggested_scope_key": nominated_team,
            "domain": "sibyl",
        },
        provenance={"raw_source_ids": ["source-1"]},
    )


@pytest.fixture
def store() -> Iterator[dict[str, RawMemory]]:
    """Serve promotion sources from a dict instead of the content store."""
    rows: dict[str, RawMemory] = {"source-1": _row()}

    async def snapshot(organization_id: str, memory_id: str) -> RawSourceSnapshot | None:
        memory = rows.get(memory_id)
        if memory is None:
            return None
        return RawSourceSnapshot(
            memory,
            SourceObservation(
                SourceIdentity(organization_id, SourceKind.RAW_CAPTURE, memory.id),
                1,
                evidence_hash(memory.raw_content),
                memory.revision,
                True,
            ),
        )

    with patch.object(memory_reflection, "load_promotion_source", snapshot):
        yield rows


def _rest_auth(stack: ExitStack, *, teams: set[str]) -> AsyncMock:
    team_lookup = AsyncMock(return_value=teams)
    stack.enter_context(
        patch("sibyl.api.routes.memory_auth.list_accessible_team_scope_keys", team_lookup)
    )
    stack.enter_context(
        patch(
            "sibyl.api.routes.memory_auth.list_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        )
    )
    stack.enter_context(patch("sibyl.api.routes.memory_auth.log_memory_audit_event", AsyncMock()))
    return team_lookup


_PREVIEW_ROUTES = [preview_reflection_promotion, preview_memory_promotion]

# (memberships, requested key, nominated key, allowed, reason, resolved key)
_MEMBERSHIP_CASES = [
    pytest.param({TEAM}, TEAM, None, True, "promotion_preview_allowed", TEAM, id="member"),
    pytest.param({OTHER_TEAM}, TEAM, None, False, "unverified_membership", TEAM, id="non-member"),
    pytest.param({TEAM}, None, TEAM, True, "promotion_preview_allowed", TEAM, id="nominated-team"),
    pytest.param(
        {OTHER_TEAM},
        None,
        TEAM,
        False,
        "unverified_membership",
        TEAM,
        id="forged-nomination",
    ),
]


@pytest.mark.parametrize("route", _PREVIEW_ROUTES)
@pytest.mark.parametrize(
    ("teams", "requested", "nominated", "allowed", "reason", "resolved"), _MEMBERSHIP_CASES
)
async def test_rest_preview_authorizes_team_against_membership(
    store, route, teams, requested, nominated, allowed, reason, resolved
) -> None:
    store["candidate-1"] = _candidate(nominated)
    with ExitStack() as stack:
        team_lookup = _rest_auth(stack, teams=teams)
        response = await route(
            ReflectionPromotionRequest(
                candidate_id="candidate-1",
                promote_to_scope="team",
                promote_to_scope_key=requested,
            ),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=_ctx(),
        )

    team_lookup.assert_awaited()
    assert response.allowed is allowed
    assert response.reason == reason
    assert response.promote_to_scope == "team"
    assert response.promote_to_scope_key == resolved
    if not allowed:
        assert "unverified_membership" in response.policy_reasons


@pytest.mark.parametrize("route", [promote_reflection_candidate, promote_memory])
async def test_rest_promote_keeps_a_restricted_api_key_out_of_teams(store, route) -> None:
    """A key granted only private memory cannot publish its owner's candidate to a team."""
    store["candidate-1"] = _candidate()
    with ExitStack() as stack:
        _rest_auth(stack, teams={TEAM})
        persist = stack.enter_context(
            patch.object(memory_reflection, "persist_reflection_candidate", AsyncMock())
        )
        response = await route(
            ReflectionPromotionRequest(candidate_id="candidate-1", promote_to_scope="team"),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=_ctx(api_key_memory_scope_keys={f"private\x1f{USER_ID}"}),
        )

    persist.assert_not_awaited()
    assert response.success is False
    assert response.reason == "api_key_memory_space_denied"
    assert response.scope_key == TEAM


@pytest.mark.parametrize("route", [promote_reflection_candidate, promote_memory])
async def test_rest_promote_passes_resolved_membership(store, route) -> None:
    store["candidate-1"] = _candidate()
    promoted = ReflectionPromotionResult(
        success=True,
        candidate_id="candidate-1",
        promoted_id="rule_team",
        reason="promoted",
        review_state="promoted",
        memory_scope=MemoryScope.TEAM,
        scope_key=TEAM,
        raw_source_ids=["source-1"],
    )
    with ExitStack() as stack:
        _rest_auth(stack, teams={TEAM})
        promote = stack.enter_context(
            patch(
                "sibyl.api.routes.memory_promotion.promote_reflection_candidate_review",
                AsyncMock(return_value=promoted),
            )
        )
        response = await route(
            ReflectionPromotionRequest(candidate_id="candidate-1", promote_to_scope="team"),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=_ctx(),
        )

    assert promote.await_args.kwargs["accessible_teams"] == {TEAM}
    assert response.success is True
    assert response.scope_key == TEAM


@pytest.mark.parametrize(
    ("teams", "nominated", "success", "reason"),
    [
        ({TEAM}, TEAM, True, "promoted"),
        ({OTHER_TEAM}, TEAM, False, "unverified_membership"),
    ],
)
async def test_rest_promote_raw_team_nomination(store, teams, nominated, success, reason) -> None:
    """`sibyl remember --propose-scope team` captures promote for members only."""
    store["raw-1"] = _row(
        id="raw-1",
        capture_surface="raw_memory",
        metadata={"suggested_memory_scope": "team", "suggested_scope_key": nominated},
    )
    promoted = ReflectionPromotionResult(
        success=True,
        candidate_id="raw-1",
        promoted_id="rule_team",
        reason="promoted",
        review_state="promoted",
        memory_scope=MemoryScope.TEAM,
        scope_key=TEAM,
        raw_source_ids=["raw-1"],
    )
    with ExitStack() as stack:
        _rest_auth(stack, teams=teams)
        stack.enter_context(
            patch(
                "sibyl.api.routes.memory_auth.authorize_raw_promotion_api_key_scopes", AsyncMock()
            )
        )
        # Stop at the reservation that follows authorization; sibyl-core
        # covers the write itself.
        reserve = stack.enter_context(
            patch.object(memory_reflection, "_reserve_promotion", AsyncMock(return_value=promoted))
        )
        response = await promote_memory(
            ReflectionPromotionRequest(candidate_id="raw-1", promote_to_scope="team"),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=_ctx(),
        )

    assert response.success is success
    assert response.reason == reason
    assert response.scope_key == TEAM
    assert reserve.await_count == (1 if success else 0)


async def test_auto_review_leaves_member_team_candidates_as_exceptions(store) -> None:
    store["candidate-1"] = _candidate()
    with ExitStack() as stack:
        _rest_auth(stack, teams={TEAM})
        promote = stack.enter_context(
            patch(
                "sibyl.api.routes.memory_review.promote_reflection_candidate_review",
                AsyncMock(),
            )
        )
        response = await auto_review_reflection_candidate(
            ReflectionAutonomyRequest(candidate_id="candidate-1", promote_to_scope="team"),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=_ctx(),
        )

    promote.assert_not_awaited()
    assert response.preview.allowed is True
    assert response.outcome == "exception"
    assert response.recommended_action == "route_to_review"
    assert response.exception_reasons == ["high_impact_scope"]
    assert response.promote_to_scope_key == TEAM


@pytest.mark.parametrize(
    ("teams", "requested", "nominated", "allowed", "reason", "resolved"), _MEMBERSHIP_CASES
)
async def test_drain_authorizes_team_and_never_auto_promotes_it(
    store, teams, requested, nominated, allowed, reason, resolved
) -> None:
    candidate = _candidate(nominated)
    store["candidate-1"] = candidate
    with ExitStack() as stack:
        team_lookup = _rest_auth(stack, teams=teams)
        stack.enter_context(
            patch(
                "sibyl.api.routes.memory_review.list_reflection_candidate_reviews",
                AsyncMock(return_value=[candidate]),
            )
        )
        promote = stack.enter_context(
            patch(
                "sibyl.api.routes.memory_review.promote_reflection_candidate_review",
                AsyncMock(),
            )
        )
        response = await drain_reflection_review(
            ReflectionReviewDrainRequest(
                dry_run=False,
                limit=1,
                promote_to_scope="team",
                promote_to_scope_key=requested,
            ),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=_ctx(),
        )

    # One lookup for the whole drain, shared by every candidate.
    team_lookup.assert_awaited_once()
    promote.assert_not_awaited()
    item = response.results[0]
    assert item.outcome == "exception"
    assert item.applied is False
    assert "high_impact_scope" in item.exception_reasons
    if allowed:
        assert item.exception_reasons == ["high_impact_scope"]
    else:
        assert "policy_denied" in item.exception_reasons
        assert reason in item.policy_reasons


@pytest.mark.parametrize(
    ("teams", "nominated", "denied"),
    [
        pytest.param({TEAM}, TEAM, False, id="member"),
        pytest.param({OTHER_TEAM}, TEAM, True, id="non-member"),
        pytest.param({TEAM}, OTHER_TEAM, True, id="forged-nomination"),
    ],
)
async def test_dream_cycle_resolves_membership_for_the_capture_principal(
    store, teams, nominated, denied
) -> None:
    candidate = _candidate(nominated)
    store["candidate-1"] = candidate
    team_lookup = AsyncMock(return_value=teams)
    with (
        patch(
            "sibyl.jobs.reflection.list_reflection_candidate_reviews",
            AsyncMock(return_value=[candidate]),
        ),
        patch(
            "sibyl.jobs.reflection.resolve_accessible_project_graph_ids",
            AsyncMock(return_value=set()),
        ),
        patch("sibyl.jobs.reflection.resolve_accessible_team_scope_keys", team_lookup),
        patch("sibyl.jobs.reflection.promote_reflection_candidate_review", AsyncMock()) as promote,
        patch("sibyl.jobs.reflection.log_memory_audit_event", AsyncMock()),
    ):
        receipt = await run_reflection_dream_cycle(
            {},
            ORG_ID,
            dry_run=True,
            source_limit=0,
            candidate_limit=1,
        )

    team_lookup.assert_awaited_once_with(user_id=USER_ID, org_id=ORG_ID)
    promote.assert_not_awaited()
    reported = receipt["candidates"][0]
    assert reported["outcome"] == "exception"
    assert "high_impact_scope" in reported["exception_reasons"]
    assert ("policy_denied" in reported["exception_reasons"]) is denied
    assert ("unverified_membership" in reported["policy_reasons"]) is denied
