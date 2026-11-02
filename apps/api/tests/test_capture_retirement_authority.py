"""Taking someone else's capture out of recall takes the authority a correction does.

Promotion stamps whatever a candidate supersedes or contradicts as no longer
valid, which answers to `authorize_memory_correction`: the author may act, and
anyone else needs project maintainer on the capture's project. Every promotion
caller hands the services the acting principal's maintainer grants.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sibyl.api.routes.memory_promotion import promote_memory, promote_reflection_candidate
from sibyl.api.routes.memory_review import auto_review_reflection_candidate
from sibyl.api.schemas import ReflectionAutonomyRequest, ReflectionPromotionRequest
from sibyl_core.auth import OrganizationRole, ProjectRole
from sibyl_core.services.memory import ReflectionPromotionPreview, ReflectionPromotionResult
from sibyl_core.services.surreal_content import MemoryScope

ORG_ID = "00000000-0000-0000-0000-000000000111"
READER = "00000000-0000-0000-0000-000000000222"
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
