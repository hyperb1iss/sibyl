"""Shared scheduler registration and organization failure isolation."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from sibyl.jobs import lifecycle_repair
from sibyl.jobs.worker import WorkerSettings, get_schedule_specs
from sibyl_core.projection.repair import LifecycleRepairResult


async def test_scheduled_repair_continues_after_org_failure(monkeypatch):
    monkeypatch.setattr(lifecycle_repair, "list_org_ids", AsyncMock(return_value=["a", "b"]))
    entered = []
    closed = []
    runtime = object()

    @asynccontextmanager
    async def background(group_id):
        entered.append(group_id)
        if group_id == "a":
            raise ConnectionError("unavailable")
        try:
            yield runtime
        finally:
            closed.append(group_id)

    monkeypatch.setattr(lifecycle_repair, "background_graph_runtime", background)
    repair = AsyncMock(return_value=LifecycleRepairResult(checked=3, recovered=2, pending=1))
    monkeypatch.setattr(lifecycle_repair, "repair_graph_lifecycle", repair)
    raw_repair = AsyncMock(return_value=LifecycleRepairResult(checked=1, recovered=1))
    monkeypatch.setattr(lifecycle_repair, "repair_raw_source_lifecycle", raw_repair)
    result = await lifecycle_repair.repair_lifecycle_all_orgs({})
    assert result == {
        "organizations": 2,
        "failed_organizations": 1,
        "checked": 5,
        "recovered": 4,
        "pending": 1,
        "failed": 0,
    }
    repair.assert_awaited_once_with(runtime)
    assert entered == ["a", "b"]
    assert closed == ["b"]
    assert raw_repair.await_count == 2


def test_repair_uses_shared_local_and_redis_schedule():
    specs = [spec for spec in get_schedule_specs() if spec.name == "repair_lifecycle_all_orgs"]
    assert len(specs) == 1
    assert specs[0].function is lifecycle_repair.repair_lifecycle_all_orgs
    assert specs[0].schedule_label == "* * * * *"
    assert lifecycle_repair.repair_lifecycle_all_orgs in WorkerSettings.functions


async def test_repair_resolves_current_memberships_with_one_auth_context(monkeypatch):
    from types import SimpleNamespace

    context = SimpleNamespace(user_id="owner", organization_id="org", org_role="member")
    resolve = AsyncMock(return_value=context)
    projects = AsyncMock(return_value={"project"})
    teams = AsyncMock(return_value={"team"})
    delegated = AsyncMock(return_value={"delegated"})
    monkeypatch.setattr(lifecycle_repair, "resolve_auth_context", resolve)
    monkeypatch.setattr(lifecycle_repair, "list_accessible_project_graph_ids", projects)
    monkeypatch.setattr(lifecycle_repair, "list_accessible_team_scope_keys", teams)
    monkeypatch.setattr(lifecycle_repair, "list_accessible_delegated_scope_keys", delegated)
    authority = await lifecycle_repair.resolve_source_authority("org", "owner")
    assert authority.projects == {"project"}
    assert authority.teams == {"team"}
    assert authority.delegations == {"delegated"}
    assert authority.scope_keys is None
    resolve.assert_awaited_once_with(claims={"sub": "owner", "org": "org"})
    for reader in (projects, teams, delegated):
        reader.assert_awaited_once_with(context)


async def test_repair_rejects_revoked_org_membership_before_reading_grants(monkeypatch):
    from types import SimpleNamespace

    context = SimpleNamespace(user_id="owner", organization_id="org", org_role=None)
    projects = AsyncMock()
    monkeypatch.setattr(lifecycle_repair, "resolve_auth_context", AsyncMock(return_value=context))
    monkeypatch.setattr(lifecycle_repair, "list_accessible_project_graph_ids", projects)
    assert await lifecycle_repair.resolve_source_authority("org", "owner") is None
    projects.assert_not_awaited()


async def test_repair_rejects_deleted_user(monkeypatch):
    from sibyl.persistence.auth_common import UserNotFoundError

    monkeypatch.setattr(
        lifecycle_repair, "resolve_auth_context", AsyncMock(side_effect=UserNotFoundError("gone"))
    )
    assert await lifecycle_repair.resolve_source_authority("org", "owner") is None
