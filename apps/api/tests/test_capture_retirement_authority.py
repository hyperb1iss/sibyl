"""Taking someone else's capture out of recall takes the authority a correction does.

Two automatic paths retire captures: promotion stamps whatever a candidate
supersedes or contradicts as no longer valid, and the review drain archives
duplicate and stale candidates. Both answer to `authorize_memory_correction`:
the author may act, anyone else needs project maintainer on the capture's
project, and org owners and admins hold maintainer on every project. These
tests drive each caller with storage stubbed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.api.routes.memory_promotion import promote_memory, promote_reflection_candidate
from sibyl.api.routes.memory_review import (
    auto_review_reflection_candidate,
    drain_reflection_review,
)
from sibyl.api.schemas import (
    ReflectionAutonomyRequest,
    ReflectionPromotionRequest,
    ReflectionReviewDrainRequest,
)
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.services.memory import ReflectionPromotionPreview, ReflectionPromotionResult
from sibyl_core.services.surreal_content import MemoryScope, RawMemory

ORG_ID = "00000000-0000-0000-0000-000000000111"
READER = "00000000-0000-0000-0000-000000000222"
AUTHOR = "00000000-0000-0000-0000-000000000333"
PROJECT = "project_123"
OTHER_PROJECT = "project_456"


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = READER
    ctx.organization_id = ORG_ID
    ctx.org_role = OrganizationRole.MEMBER
    ctx.api_key_memory_scope_keys = None
    return ctx


def _http_request() -> SimpleNamespace:
    return SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.5"),
        headers={"user-agent": "SibylTest/1.0"},
    )


@contextmanager
def _grants(
    *,
    read: set[str] = frozenset(),
    write: set[str] = frozenset(),
    maintain: set[str] = frozenset(),
) -> Iterator[AsyncMock]:
    by_role = {
        ProjectRole.VIEWER: read,
        ProjectRole.CONTRIBUTOR: write,
        ProjectRole.MAINTAINER: maintain,
    }
    lookup = AsyncMock(
        side_effect=lambda _ctx, required_role=ProjectRole.VIEWER: set(by_role[required_role])
    )
    with (
        patch("sibyl.api.routes.memory_auth.list_accessible_project_graph_ids", lookup),
        patch(
            "sibyl.api.routes.memory_auth.list_accessible_team_scope_keys",
            AsyncMock(return_value=set()),
        ),
        patch("sibyl.api.routes.memory_auth.log_memory_audit_event", AsyncMock()),
    ):
        yield lookup


def _promoted(candidate_id: str = "candidate-1") -> ReflectionPromotionResult:
    return ReflectionPromotionResult(
        success=True,
        candidate_id=candidate_id,
        promoted_id="decision_new",
        reason="promoted",
        review_state="promoted",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=READER,
        raw_source_ids=["source-1"],
    )


@pytest.mark.parametrize("route", [promote_reflection_candidate, promote_memory])
async def test_rest_promotion_hands_maintainer_grants_to_the_write(route) -> None:
    with (
        _grants(read={PROJECT, OTHER_PROJECT}, write={PROJECT, OTHER_PROJECT}, maintain={PROJECT}),
        patch(
            "sibyl.api.routes.memory_promotion.promote_reflection_candidate_review",
            AsyncMock(return_value=_promoted()),
        ) as promote,
    ):
        response = await route(
            ReflectionPromotionRequest(candidate_id="candidate-1", promote_to_scope="private"),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=_ctx(),
        )

    assert response.success is True
    assert promote.await_args.kwargs["writable_projects"] == {PROJECT, OTHER_PROJECT}
    assert promote.await_args.kwargs["maintained_projects"] == {PROJECT}


async def test_rest_raw_promotion_hands_maintainer_grants_to_the_write() -> None:
    not_candidate = replace(_promoted("raw-1"), success=False, reason="not_reflection_candidate")
    with (
        _grants(read={PROJECT}, write={PROJECT}, maintain={PROJECT}),
        patch(
            "sibyl.api.routes.memory_promotion.promote_reflection_candidate_review",
            AsyncMock(return_value=not_candidate),
        ),
        patch("sibyl.api.routes.memory_auth.authorize_raw_promotion_api_key_scopes", AsyncMock()),
        patch(
            "sibyl.api.routes.memory_promotion.promote_raw_memory",
            AsyncMock(return_value=_promoted("raw-1")),
        ) as promote_raw,
    ):
        response = await promote_memory(
            ReflectionPromotionRequest(candidate_id="raw-1", promote_to_scope="private"),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=_ctx(),
        )

    assert response.success is True
    assert promote_raw.await_args.kwargs["maintained_projects"] == {PROJECT}


async def test_auto_review_hands_maintainer_grants_to_the_write() -> None:
    safe_preview = ReflectionPromotionPreview(
        allowed=True,
        candidate_id="candidate-1",
        reason="promotion_preview_allowed",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
        metadata={"confidence": 0.95},
    )
    with (
        _grants(read={PROJECT}, write={PROJECT}, maintain={PROJECT}),
        patch(
            "sibyl.api.routes.memory_review.preview_reflection_candidate_promotion",
            AsyncMock(return_value=safe_preview),
        ),
        patch(
            "sibyl.api.routes.memory_review.promote_reflection_candidate_review",
            AsyncMock(return_value=_promoted()),
        ) as promote,
    ):
        response = await auto_review_reflection_candidate(
            ReflectionAutonomyRequest(candidate_id="candidate-1", promote_to_scope="private"),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=_ctx(),
        )

    assert response.outcome == "auto_promote"
    assert promote.await_args.kwargs["maintained_projects"] == {PROJECT}


def _project_candidate(author: str) -> RawMemory:
    return RawMemory(
        id="candidate-duplicate",
        organization_id=ORG_ID,
        source_id="source-1",
        principal_id=author,
        memory_scope=MemoryScope.PROJECT,
        scope_key=PROJECT,
        metadata={"project_id": PROJECT},
        capture_surface="reflection_candidate",
    )


def _duplicate_preview() -> ReflectionPromotionPreview:
    return ReflectionPromotionPreview(
        allowed=True,
        candidate_id="candidate-duplicate",
        reason="promotion_preview_allowed",
        review_state="pending",
        memory_scope=MemoryScope.PRIVATE,
        scope_key=None,
        raw_source_ids=["source-1"],
        metadata={"candidate_duplicate_of_source_id": "source-0", "confidence": 0.94},
    )


async def _drain_duplicate(candidate: RawMemory, ctx: object):
    archived = replace(candidate, review_state="archived")
    with (
        patch(
            "sibyl.api.routes.memory_review.list_reflection_candidate_reviews",
            AsyncMock(return_value=[candidate]),
        ),
        patch(
            "sibyl.api.routes.memory_review.preview_reflection_candidate_promotion",
            AsyncMock(return_value=_duplicate_preview()),
        ),
        patch(
            "sibyl.api.routes.memory_review.promote_reflection_candidate_review", AsyncMock()
        ) as promote,
        patch("sibyl.api.routes.memory_auth.get_raw_memory", AsyncMock(return_value=candidate)),
        patch(
            "sibyl.api.routes.memory_review.save_raw_memory", AsyncMock(return_value=archived)
        ) as save,
    ):
        response = await drain_reflection_review(
            ReflectionReviewDrainRequest(
                dry_run=False,
                limit=1,
                promote_to_scope="private",
                archive_exceptions=True,
            ),
            http_request=_http_request(),
            org=SimpleNamespace(id=ORG_ID),
            ctx=ctx,
        )
    promote.assert_not_awaited()
    return response, save


@pytest.mark.parametrize(
    ("grants", "reason"),
    [
        pytest.param({"read": {PROJECT}}, "unverified_membership", id="viewer"),
        pytest.param(
            {"read": {PROJECT}, "write": {PROJECT}},
            "project_maintainer_required",
            id="contributor",
        ),
        pytest.param(
            {"read": {PROJECT}, "write": {PROJECT}, "maintain": {OTHER_PROJECT}},
            "project_maintainer_required",
            id="maintainer-elsewhere",
        ),
    ],
)
async def test_drain_leaves_another_principals_candidate_pending(grants, reason) -> None:
    with _grants(**grants):
        response, save = await _drain_duplicate(_project_candidate(AUTHOR), _ctx())

    save.assert_not_awaited()
    item = response.results[0]
    assert item.outcome == "exception"
    assert item.exception_reasons == ["duplicate_candidate"]
    assert item.archived is False
    assert item.review_state == "pending"
    assert reason in item.policy_reasons
    assert response.archived_count == 0


@pytest.mark.parametrize(
    ("author", "grants"),
    [
        pytest.param(
            AUTHOR,
            {"read": {PROJECT}, "write": {PROJECT}, "maintain": {PROJECT}},
            id="maintainer",
        ),
        pytest.param(READER, {"read": {PROJECT}, "write": {PROJECT}}, id="author"),
    ],
)
async def test_drain_archives_for_the_author_or_a_maintainer(author, grants) -> None:
    with _grants(**grants):
        response, save = await _drain_duplicate(_project_candidate(author), _ctx())

    save.assert_awaited_once()
    assert save.await_args.args[0].review_state == "archived"
    item = response.results[0]
    assert item.archived is True
    assert item.review_state == "archived"
    assert response.archived_count == 1


@pytest.mark.parametrize(
    ("org_role", "archived"),
    [(OrganizationRole.MEMBER, False), (OrganizationRole.ADMIN, True)],
)
async def test_org_admin_drain_archives_any_project_candidate(org_role, archived) -> None:
    """Admins resolve to maintainer on every project through the real grant lookup."""
    from sibyl.persistence.surreal.auth_runtime import projects

    ctx = SimpleNamespace(
        user=SimpleNamespace(id=READER),
        user_id=READER,
        organization=SimpleNamespace(id=ORG_ID),
        organization_id=ORG_ID,
        org_role=org_role,
        api_key_project_ids=None,
        api_key_memory_scope_keys=None,
        accessible_teams=frozenset(),
        accessible_delegations=frozenset(),
    )
    record = {
        "uuid": "project-uuid",
        "graph_project_id": PROJECT,
        "visibility": "org",
        "default_role": ProjectRole.CONTRIBUTOR.value,
    }
    with (
        patch.object(
            projects, "_load_project_access_records", AsyncMock(return_value=([record], {}))
        ),
        patch(
            "sibyl.api.routes.memory_auth.list_accessible_team_scope_keys",
            AsyncMock(return_value=set()),
        ),
        patch("sibyl.api.routes.memory_auth.log_memory_audit_event", AsyncMock()),
    ):
        response, save = await _drain_duplicate(_project_candidate(AUTHOR), ctx)

    assert response.results[0].archived is archived
    assert save.await_count == (1 if archived else 0)
    if not archived:
        assert "project_maintainer_required" in response.results[0].policy_reasons
