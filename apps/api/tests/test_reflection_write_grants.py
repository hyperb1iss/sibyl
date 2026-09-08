"""REST reflection carries project write roles separately from readable evidence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from sibyl.api.routes.context import reflect_context
from sibyl.api.schemas import ReflectionRequest
from sibyl_core.auth import ProjectRole
from tests.test_routes_context import _ctx, _reflection_pack


async def test_rest_reflection_preserves_separate_project_write_grants(monkeypatch):
    def projects(ctx, required_role=ProjectRole.VIEWER):
        return {"writer"} if required_role == ProjectRole.CONTRIBUTOR else {"reader", "writer"}

    memberships = AsyncMock(side_effect=projects)
    reflection = AsyncMock(return_value=_reflection_pack(project=None, source_id=None))
    monkeypatch.setattr("sibyl.api.routes.context.list_accessible_project_graph_ids", memberships)
    monkeypatch.setattr("sibyl_core.tools.core.reflect_memory", reflection)
    monkeypatch.setattr("sibyl.api.routes.context.log_reflection_audit", AsyncMock())
    await reflect_context(
        ReflectionRequest(content="Useful session evidence", persist=True, active_task=False),
        org=SimpleNamespace(id=UUID("00000000-0000-0000-0000-000000000111")),
        ctx=_ctx(),
    )
    assert reflection.await_args.kwargs["accessible_projects"] == {"reader", "writer"}
    assert reflection.await_args.kwargs["writable_projects"] == {"writer"}
