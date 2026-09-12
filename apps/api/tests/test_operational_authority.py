"""Deferred operational writes retain intake limits and current actor authority."""

from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from sibyl.auth.api_key_common import ApiKeyAuth
from sibyl.services import operational_authority as owner
from sibyl_core.auth import AuthOrganization, AuthUser, OrganizationRole, ProjectRole
from sibyl_core.auth.context import AuthContext
from sibyl_core.services.source_observations import SourceUnavailableError


def context(**kwargs):
    return AuthContext(
        user=AuthUser(id=uuid4(), email="synthetic@example.test"),
        organization=AuthOrganization(id=uuid4(), name="Synthetic", slug="synthetic"),
        org_role=OrganizationRole.MEMBER,
        **kwargs,
    )


@pytest.mark.parametrize("creator_is_actor", [True, False])
async def test_current_membership_and_creator_determine_write_role(monkeypatch, creator_is_actor):
    ctx = context()
    authority = owner.OperationalWriteAuthority.from_context(ctx, "project-a")
    resolve = AsyncMock(return_value=ctx)
    verify = AsyncMock()
    monkeypatch.setattr(owner, "resolve_auth_context", resolve)
    monkeypatch.setattr(owner, "verify_entity_project_access", verify)
    creator = ctx.user_id if creator_is_actor else str(uuid4())
    assert await authority.current(creator_id=creator) == ctx
    assert verify.await_args.kwargs["required_role"] == (
        ProjectRole.CONTRIBUTOR if creator_is_actor else ProjectRole.MAINTAINER
    )
    resolve.return_value = replace(ctx, org_role=OrganizationRole.VIEWER)
    with pytest.raises(SourceUnavailableError):
        await authority.current(creator_id=creator)


async def test_revoked_key_stops_before_project_authorization(monkeypatch):
    ctx = context(api_key_id=str(uuid4()), scopes=frozenset({"api:write"}))
    authority = owner.OperationalWriteAuthority.from_context(ctx, "project-a")
    key = AsyncMock(return_value=None)
    resolve = AsyncMock()
    monkeypatch.setattr(owner, "resolve_api_key_authority", key)
    monkeypatch.setattr(owner, "resolve_auth_context", resolve)
    with pytest.raises(SourceUnavailableError):
        await authority.current(creator_id=ctx.user_id)
    resolve.assert_not_awaited()
    assert str(key.await_args.kwargs["user_id"]) == ctx.user_id


@pytest.mark.parametrize("restriction", ["original_empty", "current_empty", "write_scope"])
async def test_current_key_never_expands_intake_ceiling(monkeypatch, restriction):
    ctx = context(
        api_key_id=str(uuid4()),
        scopes=frozenset({"api:write"}),
        api_key_project_ids=frozenset() if restriction == "original_empty" else None,
    )
    authority = owner.OperationalWriteAuthority.from_context(ctx, "project-a")
    key = ApiKeyAuth(
        api_key_id=UUID(ctx.api_key_id),
        user_id=ctx.user.id,
        organization_id=ctx.organization.id,
        scopes=["api:read"] if restriction == "write_scope" else ["api:write"],
    )
    current = replace(
        ctx, api_key_project_ids=frozenset() if restriction == "current_empty" else None
    )
    monkeypatch.setattr(owner, "resolve_api_key_authority", AsyncMock(return_value=key))
    monkeypatch.setattr(owner, "resolve_auth_context", AsyncMock(return_value=current))

    async def check(_session, actor, _project, **_kwargs):
        if actor.api_key_project_ids == frozenset():
            raise HTTPException(403, "project denied")

    monkeypatch.setattr(owner, "verify_entity_project_access", check)
    with pytest.raises(SourceUnavailableError):
        await authority.current(creator_id=ctx.user_id)


def test_job_authority_encoding_requires_explicit_key_identity_and_limits():
    authority = owner.OperationalWriteAuthority.from_context(context(), "project-a")
    assert (
        owner.OperationalWriteAuthority.model_validate_json(authority.model_dump_json())
        == authority
    )
    for field in ("api_key_id", "scopes", "project_ids", "memory_scope_keys"):
        payload = authority.model_dump()
        del payload[field]
        with pytest.raises(ValidationError):
            owner.OperationalWriteAuthority.model_validate(payload)
