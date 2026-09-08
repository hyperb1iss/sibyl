"""Core correction dispatch keeps read, write, and credential grants distinct."""

import pytest

from sibyl_core.auth.memory_policy import memory_scope_policy_key
from sibyl_core.services.surreal_content import get_raw_memory, remember_raw_memory
from sibyl_core.tools.manage import manage
from tests.test_correction_scope_grants import correction_store as correction_store


@pytest.mark.parametrize("writable", [False, True])
@pytest.mark.parametrize("credential_allows", [False, True])
async def test_core_correction_requires_explicit_write_and_credential_grants(
    correction_store, writable, credential_allows
):
    org = correction_store.org
    source = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="manage-grants",
        raw_content="Evidence",
        memory_scope="project",
        scope_key="project-a",
        embedding_provider=None,
    )
    response = await manage(
        action="correct_memory",
        entity_id=source.id,
        organization_id=org,
        principal_id="owner",
        accessible_projects={"project-a"},
        writable_projects={"project-a"} if writable else set(),
        allowed_memory_scope_keys=(
            {memory_scope_policy_key("project", "project-a")} if credential_allows else set()
        ),
        data={
            "action": "hide",
            "reason": "Outdated evidence",
            "user_id": "owner",
            "accessible_projects": {"project-a"},
            "writable_projects": {"project-a"},
            "allowed_memory_scope_keys": {memory_scope_policy_key("project", "project-a")},
        },
    )
    assert response.success is (writable and credential_allows)
    current = await get_raw_memory(organization_id=org, memory_id=source.id)
    assert current is not None
    assert (current.revision > source.revision) is (writable and credential_allows)


@pytest.mark.parametrize("scope", ["private", "team"])
async def test_core_correction_ignores_identity_and_team_grants_in_action_data(
    correction_store, scope
):
    org = correction_store.org
    source = await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id="manage-forged-authority",
        raw_content="Evidence",
        memory_scope=scope,
        scope_key="team-a" if scope == "team" else None,
        embedding_provider=None,
    )
    response = await manage(
        action="correct_memory",
        entity_id=source.id,
        organization_id=org,
        principal_id="owner" if scope == "team" else "another-user",
        accessible_teams=set(),
        data={
            "action": "hide",
            "reason": "Outdated evidence",
            "user_id": "owner",
            "accessible_teams": {"team-a"},
        },
    )
    assert not response.success
    current = await get_raw_memory(organization_id=org, memory_id=source.id)
    assert current is not None and current.revision == source.revision
