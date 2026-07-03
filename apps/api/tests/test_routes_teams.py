from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.routes.teams import (
    TeamCreateRequest,
    TeamMemberCreateRequest,
    TeamProjectLinkRequest,
    add_team_member,
    create_team,
    link_team_project,
    list_teams,
    remove_team_member,
    unlink_team_project,
)
from sibyl_core.auth import OrganizationRole, ProjectRole


def _org() -> MagicMock:
    org = MagicMock()
    org.id = uuid4()
    return org


def _ctx(user_id: str) -> MagicMock:
    ctx = MagicMock()
    ctx.user_id = user_id
    return ctx


def _team(**overrides: object) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "name": "Memory Guild",
        "slug": "memory-guild",
        "description": "Shared team memory",
        "is_default": False,
        "graph_entity_id": None,
        "memory_space_id": uuid4(),
        "memory_scope_key": None,
        "created_at": datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        "members": [],
        "projects": [],
    }
    values.update(overrides)
    if values["memory_scope_key"] is None:
        values["memory_scope_key"] = str(values["id"])
    return SimpleNamespace(**values)


def _member(**overrides: object) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "team_id": uuid4(),
        "user_id": uuid4(),
        "role": "member",
        "joined_at": datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        "created_at": datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _project(**overrides: object) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "team_id": uuid4(),
        "project_id": uuid4(),
        "graph_project_id": "project_alpha",
        "role": ProjectRole.CONTRIBUTOR,
        "created_at": datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 3, 12, 0, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_list_teams_returns_memory_scope_keys() -> None:
    org = _org()
    team = _team(organization_id=org.id)

    with patch(
        "sibyl.api.routes.teams.runtime_list_teams", AsyncMock(return_value=[team])
    ) as list_fn:
        response = await list_teams(org=org)

    list_fn.assert_awaited_once_with(organization_id=org.id)
    assert response.teams[0].id == str(team.id)
    assert response.teams[0].memory_scope_key == str(team.id)
    assert response.teams[0].memory_space_id == str(team.memory_space_id)


@pytest.mark.asyncio
async def test_create_team_uses_authenticated_actor() -> None:
    org = _org()
    actor_id = uuid4()
    team = _team(organization_id=org.id)

    with patch(
        "sibyl.api.routes.teams.runtime_create_team", AsyncMock(return_value=team)
    ) as create:
        response = await create_team(
            TeamCreateRequest(name="Memory Guild", slug="memory-guild"),
            org=org,
            ctx=_ctx(str(actor_id)),
        )

    create.assert_awaited_once_with(
        organization_id=org.id,
        created_by_user_id=actor_id,
        name="Memory Guild",
        slug="memory-guild",
        description=None,
    )
    assert response.slug == "memory-guild"


@pytest.mark.asyncio
async def test_add_team_member_passes_role_value() -> None:
    org = _org()
    user_id = uuid4()
    member = _member(user_id=user_id, role="admin")

    with patch(
        "sibyl.api.routes.teams.runtime_add_team_member",
        AsyncMock(return_value=member),
    ) as add_member:
        response = await add_team_member(
            "memory-guild",
            TeamMemberCreateRequest(user_id=user_id, role=OrganizationRole.ADMIN),
            org=org,
        )

    add_member.assert_awaited_once_with(
        organization_id=org.id,
        team_ref="memory-guild",
        user_id=user_id,
        role="admin",
    )
    assert response.role == "admin"


@pytest.mark.asyncio
async def test_team_project_link_and_unlink_use_project_reference() -> None:
    org = _org()
    linked = _project(organization_id=org.id, role=ProjectRole.MAINTAINER)

    with patch(
        "sibyl.api.routes.teams.runtime_link_team_project",
        AsyncMock(return_value=linked),
    ) as link_project:
        response = await link_team_project(
            "memory-guild",
            TeamProjectLinkRequest(
                project_id="project_alpha",
                role=ProjectRole.MAINTAINER,
            ),
            org=org,
        )

    link_project.assert_awaited_once_with(
        organization_id=org.id,
        team_ref="memory-guild",
        project_ref="project_alpha",
        role=ProjectRole.MAINTAINER,
    )
    assert response.graph_project_id == "project_alpha"
    assert response.role == ProjectRole.MAINTAINER

    with patch(
        "sibyl.api.routes.teams.runtime_unlink_team_project",
        AsyncMock(return_value=True),
    ) as unlink_project:
        removed = await unlink_team_project("memory-guild", "project_alpha", org=org)

    unlink_project.assert_awaited_once_with(
        organization_id=org.id,
        team_ref="memory-guild",
        project_ref="project_alpha",
    )
    assert removed == {"success": True}


@pytest.mark.asyncio
async def test_remove_team_member_returns_success() -> None:
    org = _org()
    user_id = uuid4()

    with patch(
        "sibyl.api.routes.teams.runtime_remove_team_member",
        AsyncMock(return_value=True),
    ) as remove_member:
        response = await remove_team_member("memory-guild", user_id, org=org)

    remove_member.assert_awaited_once_with(
        organization_id=org.id,
        team_ref="memory-guild",
        user_id=user_id,
    )
    assert response == {"success": True}
