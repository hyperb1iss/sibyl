"""Correction replay reuses the write while honoring the current reader."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.api.routes import memory_sources
from sibyl.api.schemas import MemoryCorrectionRequest, MemoryCorrectionResponse
from sibyl.mcp_tools import context, management
from sibyl.mcp_tools.context import McpContext
from sibyl.services import memory_correction_disclosure as disclosure
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


@pytest.fixture
def correction_disclosure_rows(monkeypatch):
    monkeypatch.setattr(context, "get_writable_projects", AsyncMock(return_value=set()))
    monkeypatch.setattr(memory_sources.memory_auth, "authorize_project_scope_write", AsyncMock())
    org = str(uuid4())
    root = RawMemory(
        id="root",
        organization_id=org,
        source_id="root",
        principal_id="owner",
        memory_scope=MemoryScope.PROJECT,
        scope_key="project-a",
    )
    private = RawMemory(
        id="private",
        organization_id=org,
        source_id="private",
        principal_id="owner",
    )
    raw = AsyncMock(
        side_effect=lambda **kwargs: {"root": root, "private": private}.get(kwargs["memory_id"])
    )
    graph = AsyncMock(
        return_value=SimpleNamespace(
            metadata={
                "memory_scope": "private",
                "principal_id": "owner",
            }
        )
    )
    monkeypatch.setattr(disclosure, "get_raw_memory", raw)
    monkeypatch.setattr(
        disclosure,
        "get_surreal_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                entity_manager=SimpleNamespace(get=graph),
            )
        ),
    )
    return root


def stored_correction():
    return {
        "allowed": True,
        "applied": True,
        "source_id": "root",
        "action": "hide",
        "reason": "outdated",
        "target_lifecycle_state": "active",
        "reversible": True,
        "audit_action": "memory.correction.hide",
        "affected_source_ids": ["root"],
        "affected_derived_ids": ["private-graph"],
        "lifecycle": {"derived_ids": ["private-graph"]},
        "recall_impact": {
            "graph_entity_ids": ["private-graph"],
            "refused_entity_ids": ["private-graph"],
            "derived_raw_memory_ids": ["private"],
            "propagation_complete": True,
        },
        "metadata": {"replacement_source_id": "private", "requested_source_id": "root"},
        "reflection_finding": {"related_source_ids": ["private", "root"], "reason": "outdated"},
        "mutation_receipt": {
            "operation_id": "original",
            "applied": True,
            "revision": 2,
            "idempotency_key": "original",
            "replayed": True,
            "affected_records": [
                "raw_captures:root",
                "raw_captures:private",
                "entity:private-graph",
            ],
        },
    }


@pytest.mark.parametrize("private", [False, True])
async def test_correction_scope_replay_filters_only_inaccessible_details(
    correction_disclosure_rows, private
):
    original = stored_correction()
    before = deepcopy(original)
    keys = ["project\x1fproject-a"] + (["private\x1fowner"] if private else [])
    result = await disclosure.filter_correction_disclosure(
        original,
        organization_id=correction_disclosure_rows.organization_id,
        principal_id="owner",
        accessible_projects={"project-a"},
        accessible_teams=None,
        allowed_memory_scope_keys=iter(keys),
    )
    assert original == before
    assert result["mutation_receipt"]["operation_id"] == "original"
    assert result["mutation_receipt"]["revision"] == 2
    assert result["recall_impact"]["propagation_complete"] is True
    assert result["metadata"]["requested_source_id"] == "root"
    assert ("replacement_source_id" in result["metadata"]) is private
    assert result["reflection_finding"]["related_source_ids"] == (
        ["private", "root"] if private else ["root"]
    )
    assert bool(result["recall_impact"]["graph_entity_ids"]) is private
    assert bool(result["recall_impact"]["refused_entity_ids"]) is private
    assert bool(result["affected_derived_ids"]) is private
    assert bool(result["lifecycle"]["derived_ids"]) is private
    assert bool(result["recall_impact"]["derived_raw_memory_ids"]) is private
    assert len(result["mutation_receipt"]["affected_records"]) == (3 if private else 1)


async def test_correction_scope_rest_replay_rechecks_grants_without_reapplying(
    correction_disclosure_rows, monkeypatch
):
    root = correction_disclosure_rows
    ctx = SimpleNamespace(user_id="owner", api_key_memory_scope_keys={"project\x1fproject-a"})
    monkeypatch.setattr(
        memory_sources.memory_auth, "load_memory_source_for_org", AsyncMock(return_value=root)
    )
    monkeypatch.setattr(memory_sources.memory_auth, "require_source_policy", AsyncMock())
    monkeypatch.setattr(
        memory_sources.memory_auth,
        "list_accessible_project_graph_ids",
        AsyncMock(return_value={"project-a"}),
    )
    monkeypatch.setattr(
        memory_sources.memory_auth, "list_accessible_team_scope_keys", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(
        memory_sources,
        "replay_idempotent_response",
        AsyncMock(return_value=MemoryCorrectionResponse.model_validate(stored_correction())),
    )
    apply = AsyncMock()
    monkeypatch.setattr(memory_sources, "apply_memory_correction", apply)
    result = await memory_sources.apply_memory_correction_route(
        "root",
        MemoryCorrectionRequest(action="hide", reason="outdated"),
        http_request=SimpleNamespace(headers={}),
        org=SimpleNamespace(id=root.organization_id),
        ctx=ctx,
    )
    apply.assert_not_awaited()
    assert result.mutation_receipt.replayed
    assert result.mutation_receipt.affected_records == ["raw_captures:root"]
    assert result.recall_impact["graph_entity_ids"] == []


async def test_correction_scope_mcp_replay_rechecks_grants_without_reapplying(
    correction_disclosure_rows, monkeypatch
):
    from sibyl_core.services import memory as memory_service
    from sibyl_core.services.memory_contract import MemoryCorrectionPreview, MemoryCorrectionResult

    root = correction_disclosure_rows
    ctx = McpContext(
        org_id=root.organization_id,
        user_id="owner",
        scopes=["mcp"],
        api_key_memory_scope_keys=["project\x1fproject-a"],
    )
    monkeypatch.setattr(context, "require_context", AsyncMock(return_value=ctx))
    monkeypatch.setattr(context, "get_accessible_projects", AsyncMock(return_value={"project-a"}))
    monkeypatch.setattr(management, "_authorize_mcp_manage_action", AsyncMock(return_value=None))
    data = {"action": "hide", "reason": "outdated", "idempotency_key": "original"}
    monkeypatch.setattr(management, "get_raw_memory", AsyncMock(return_value=root))
    monkeypatch.setattr(management, "log_memory_audit_event", AsyncMock())
    preview = MemoryCorrectionPreview(
        allowed=True,
        source_id=root.id,
        action="hide",
        reason="outdated",
        target_lifecycle_state="active",
        target_lifecycle_flags=["hidden"],
        affected_source_ids=[root.id],
        affected_derived_ids=["private-graph"],
        reversible=True,
        recall_impact={},
        synthesis_impact={},
        audit_action="memory.correction.hide",
    )
    monkeypatch.setattr(
        memory_service,
        "apply_memory_correction",
        AsyncMock(
            return_value=MemoryCorrectionResult(applied=True, preview=preview, updated_memory=root)
        ),
    )
    original_payload = await management._manage_memory_correction(
        ctx=McpContext(org_id=root.organization_id, user_id="owner", scopes=["mcp"]),
        entity_id=root.id,
        data=data,
        accessible_projects={"project-a"},
        policy_decision=None,
    )
    assert original_payload["data"]["affected_derived_ids"] == ["private-graph"]
    assert "recall_impact" not in original_payload["data"]
    stored = SimpleNamespace(
        request_hash=management.idempotency_request_hash({"entity_id": "root", "data": data}),
        response_body=original_payload,
    )
    monkeypatch.setattr(
        management, "reserve_idempotency_record", AsyncMock(return_value=(stored, False))
    )
    monkeypatch.setattr(management, "idempotency_record_pending", lambda record: False)
    apply = AsyncMock()
    monkeypatch.setattr(management, "_manage_memory_correction", apply)
    result = await management._manage_mcp_action.__wrapped__(
        action="correct_memory",
        entity_id="root",
        data=data,
    )
    apply.assert_not_awaited()
    assert result["data"]["mutation_receipt"]["replayed"]
    assert result["data"]["mutation_receipt"]["affected_records"] == ["raw_captures:root"]
    assert result["data"]["affected_derived_ids"] == []
    assert original_payload["data"]["affected_derived_ids"] == ["private-graph"]
    assert result["data"]["correction_action"] == "hide"


@pytest.mark.parametrize("surface", ["rest-preview", "rest-apply", "mcp"])
async def test_correction_scope_fresh_entry_points_forward_grants(
    correction_disclosure_rows, monkeypatch, surface
):
    from sibyl_core.auth import ProjectRole
    from sibyl_core.services.memory_contract import MemoryCorrectionPreview, MemoryCorrectionResult

    readable = {"project-a", "project-b", "project-c"}
    writable = {"project-a", "project-b"}
    monkeypatch.setattr(context, "get_writable_projects", AsyncMock(return_value=writable))
    root = correction_disclosure_rows
    keys = ["project\x1fproject-a"]
    preview = MemoryCorrectionPreview(
        allowed=True,
        source_id="root",
        action="hide",
        reason="outdated",
        target_lifecycle_state="active",
        target_lifecycle_flags=["hidden"],
        affected_source_ids=["root"],
        affected_derived_ids=[],
        reversible=True,
        audit_action="memory.correction.hide",
        recall_impact={},
        synthesis_impact={},
    )
    result = MemoryCorrectionResult(applied=True, preview=preview, updated_memory=root)
    apply = AsyncMock(return_value=result)
    if surface == "mcp":
        import sibyl_core.services.memory as memory_service

        ctx = McpContext(
            org_id=root.organization_id,
            user_id="owner",
            scopes=["mcp"],
            api_key_memory_scope_keys=keys,
        )
        monkeypatch.setattr(management, "get_raw_memory", AsyncMock(return_value=root))
        monkeypatch.setattr(management, "log_memory_audit_event", AsyncMock())
        monkeypatch.setattr(memory_service, "apply_memory_correction", apply)
        await management._manage_memory_correction(
            ctx=ctx,
            entity_id="root",
            data={"action": "hide", "reason": "outdated"},
            accessible_projects=readable,
            policy_decision=None,
        )
    else:
        ctx = SimpleNamespace(user_id="owner", api_key_memory_scope_keys=keys)
        monkeypatch.setattr(
            memory_sources.memory_auth, "load_memory_source_for_org", AsyncMock(return_value=root)
        )
        monkeypatch.setattr(memory_sources.memory_auth, "require_source_policy", AsyncMock())
        monkeypatch.setattr(
            memory_sources.memory_auth,
            "list_accessible_project_graph_ids",
            AsyncMock(
                side_effect=lambda ctx, required_role=None: (
                    writable if required_role == ProjectRole.CONTRIBUTOR else readable
                )
            ),
        )
        monkeypatch.setattr(
            memory_sources.memory_auth,
            "list_accessible_team_scope_keys",
            AsyncMock(return_value=set()),
        )
        monkeypatch.setattr(memory_sources.memory_auth, "log_memory_audit", AsyncMock())
        monkeypatch.setattr(
            memory_sources, "replay_idempotent_response", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(memory_sources, "save_idempotent_response", AsyncMock())
        monkeypatch.setattr(memory_sources, "apply_memory_correction", apply)
        if surface == "rest-preview":
            apply.return_value = preview
            monkeypatch.setattr(memory_sources, "preview_memory_correction", apply)
        route = (
            memory_sources.preview_memory_correction_route
            if surface == "rest-preview"
            else memory_sources.apply_memory_correction_route
        )
        await route(
            "root",
            MemoryCorrectionRequest(action="hide", reason="outdated"),
            http_request=SimpleNamespace(headers={}),
            org=SimpleNamespace(id=root.organization_id),
            ctx=ctx,
        )
    assert apply.await_args.kwargs["allowed_memory_scope_keys"] == keys

    assert apply.await_args.kwargs["writable_projects"] == writable
    assert apply.await_args.kwargs["accessible_projects"] == readable


@pytest.mark.parametrize("accessible", [False, True])
async def test_correction_scope_project_identity_requires_membership(monkeypatch, accessible):
    from sibyl_core.models.entities import Entity, EntityType

    project = Entity(id="project-b", name="Project B", entity_type=EntityType.PROJECT)
    monkeypatch.setattr(
        disclosure,
        "get_surreal_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                entity_manager=SimpleNamespace(get=AsyncMock(return_value=project)),
            )
        ),
    )
    output = await disclosure.filter_correction_disclosure(
        {"recall_impact": {"graph_entity_ids": ["project-b"]}},
        organization_id="org",
        principal_id="owner",
        accessible_projects={"project-b"} if accessible else {"project-a"},
        accessible_teams=None,
        allowed_memory_scope_keys={"project\x1fproject-b", "project\x1fproject-a"},
    )
    assert output["recall_impact"]["graph_entity_ids"] == (["project-b"] if accessible else [])


@pytest.mark.parametrize("private", [False, True])
async def test_correction_scope_real_preview_and_serializer_filter_declared_ids(
    correction_disclosure_rows, monkeypatch, private
):
    from sibyl.api.routes.memory_serialization import correction_result_response
    from sibyl_core.services import memory_correction, memory_lifecycle
    from sibyl_core.services.memory_lineage import CorrectionPropagation

    root = correction_disclosure_rows
    root.metadata = {"promoted_entity_id": "private-graph"}
    root.observed_revision = 1
    monkeypatch.setattr(memory_correction, "_load_correction_memory", AsyncMock(return_value=root))
    monkeypatch.setattr(
        memory_correction, "save_raw_memory", AsyncMock(side_effect=lambda row, **kwargs: row)
    )
    monkeypatch.setattr(
        memory_lifecycle, "get_surreal_graph_runtime", disclosure.get_surreal_graph_runtime
    )
    monkeypatch.setattr(
        memory_correction,
        "_project_correction_to_graph",
        AsyncMock(
            return_value=(
                [],
                ["private-graph"],
                False,
                CorrectionPropagation(),
            )
        ),
    )
    keys = {"project\x1fproject-a"} | ({"private\x1fowner"} if private else set())
    arguments = {
        "organization_id": root.organization_id,
        "source_id": root.id,
        "principal_id": "owner",
        "action": "hide",
        "accessible_projects": {"project-a"},
        "allowed_memory_scope_keys": keys,
    }
    preview = await memory_correction.preview_memory_correction(**arguments)
    result = await memory_correction.apply_memory_correction(**arguments)
    response = correction_result_response(result)
    assert preview.affected_derived_ids == (["private-graph"] if private else [])
    assert response.affected_derived_ids == (["private-graph"] if private else [])
    assert response.lifecycle["derived_ids"] == (["private-graph"] if private else [])
    assert response.recall_impact.get("refused_entity_ids", []) == (
        ["private-graph"] if private else []
    )
    assert ("private-graph" in response.model_dump_json()) is private


@pytest.mark.asyncio
async def test_private_root_retains_readable_project_reference(monkeypatch):
    from sibyl_core.models.entities import Entity, EntityType
    from sibyl_core.services import memory_correction

    root = RawMemory(
        id="root",
        organization_id="org",
        source_id="root",
        principal_id="owner",
        memory_scope=MemoryScope.PRIVATE,
        revision=2,
        observed_revision=2,
        metadata={"promoted_entity_id": "project-a"},
    )
    ctx = SimpleNamespace(
        user_id="owner", api_key_memory_scope_keys={"private\x1fowner", "project\x1fproject-a"}
    )
    project = Entity(id="project-a", name="Readable project", entity_type=EntityType.PROJECT)
    monkeypatch.setattr(memory_correction, "_load_correction_memory", AsyncMock(return_value=root))
    monkeypatch.setattr(
        memory_correction.memory_lifecycle,
        "get_surreal_graph_runtime",
        AsyncMock(
            return_value=SimpleNamespace(
                entity_manager=SimpleNamespace(get=AsyncMock(return_value=project))
            )
        ),
    )
    monkeypatch.setattr(
        memory_sources.memory_auth, "load_memory_source_for_org", AsyncMock(return_value=root)
    )
    monkeypatch.setattr(memory_sources.memory_auth, "require_source_policy", AsyncMock())
    projects = AsyncMock(return_value={"project-a"})
    monkeypatch.setattr(memory_sources.memory_auth, "list_accessible_project_graph_ids", projects)
    monkeypatch.setattr(
        memory_sources.memory_auth, "list_accessible_team_scope_keys", AsyncMock(return_value=set())
    )
    monkeypatch.setattr(memory_sources.memory_auth, "log_memory_audit", AsyncMock())
    response = await memory_sources.preview_memory_correction_route(
        "root",
        MemoryCorrectionRequest(action="hide", reason="outdated"),
        http_request=SimpleNamespace(headers={}),
        org=SimpleNamespace(id="org"),
        ctx=ctx,
    )
    assert response.allowed
    assert response.affected_derived_ids == ["project-a"]


@pytest.mark.parametrize("surface", ["preview", "apply"])
async def test_correction_rest_requires_project_contributor_before_execution(
    correction_disclosure_rows, monkeypatch, surface
):
    from fastapi import HTTPException

    root = correction_disclosure_rows
    monkeypatch.setattr(
        memory_sources.memory_auth, "load_memory_source_for_org", AsyncMock(return_value=root)
    )
    monkeypatch.setattr(memory_sources.memory_auth, "require_source_policy", AsyncMock())
    role = AsyncMock(side_effect=HTTPException(status_code=403, detail="contributor required"))
    monkeypatch.setattr(memory_sources.memory_auth, "authorize_project_scope_write", role)
    execution = AsyncMock()
    monkeypatch.setattr(memory_sources, "preview_memory_correction", execution)
    monkeypatch.setattr(memory_sources, "apply_memory_correction", execution)
    route = (
        memory_sources.preview_memory_correction_route
        if surface == "preview"
        else memory_sources.apply_memory_correction_route
    )
    ctx = SimpleNamespace(user_id="owner", api_key_memory_scope_keys=None)
    with pytest.raises(HTTPException) as error:
        await route(
            root.id,
            MemoryCorrectionRequest(action="hide", reason="outdated"),
            http_request=SimpleNamespace(headers={}),
            org=SimpleNamespace(id=root.organization_id),
            ctx=ctx,
        )
    assert error.value.status_code == 403
    role.assert_awaited_once_with(ctx=ctx, memory_scope="project", scope_key="project-a")
    execution.assert_not_awaited()


@pytest.mark.parametrize("contributor", [False, True])
async def test_correction_mcp_checks_actual_project_role(monkeypatch, contributor):
    from sibyl.auth.authorization import ProjectAuthorizationError
    from sibyl_core.auth import ProjectRole

    ctx = McpContext(org_id="org", user_id="owner", scopes=["mcp"])
    resolved = SimpleNamespace()
    monkeypatch.setattr(management, "resolve_auth_context", AsyncMock(return_value=resolved))
    check = AsyncMock(
        return_value=ProjectRole.CONTRIBUTOR,
        side_effect=None
        if contributor
        else ProjectAuthorizationError(
            project_id="project-a",
            required_role=ProjectRole.CONTRIBUTOR,
            actual_role=ProjectRole.VIEWER,
        ),
    )
    monkeypatch.setattr(management, "verify_entity_project_access", check)
    if contributor:
        await management._require_correction_project_write(ctx, "project-a")
    else:
        with pytest.raises(ValueError, match="project_write_not_allowed"):
            await management._require_correction_project_write(ctx, "project-a")
    check.assert_awaited_once_with(
        ctx=resolved,
        entity_project_id="project-a",
        required_role=ProjectRole.CONTRIBUTOR,
        require_existing_project=True,
    )


async def test_correction_mcp_entry_rejects_readable_project_without_write_role(
    correction_disclosure_rows, monkeypatch
):
    root = correction_disclosure_rows
    ctx = McpContext(org_id=root.organization_id, user_id="owner", scopes=["mcp"])
    monkeypatch.setattr(management, "get_raw_memory", AsyncMock(return_value=root))
    role = AsyncMock(side_effect=ValueError("project_write_not_allowed"))
    monkeypatch.setattr(management, "_require_correction_project_write", role)
    with pytest.raises(ValueError, match="project_write_not_allowed"):
        await management._authorize_mcp_manage_action(
            ctx=ctx, action="correct_memory", entity_id=root.id
        )
    role.assert_awaited_once_with(ctx, "project-a")


async def test_correction_mcp_project_binding_is_checked_before_role_lookup(monkeypatch):
    ctx = McpContext(
        org_id="org", user_id="owner", scopes=["mcp"], api_key_project_ids=["project-b"]
    )
    resolve = AsyncMock()
    monkeypatch.setattr(management, "resolve_auth_context", resolve)
    with pytest.raises(ValueError, match="project_write_not_allowed"):
        await management._require_correction_project_write(ctx, "project-a")
    resolve.assert_not_awaited()


async def test_mcp_writable_projects_resolves_roles_and_intersects_key(monkeypatch):
    from sibyl.persistence.surreal.auth_runtime import projects
    from sibyl_core.auth import ProjectRole

    auth_ctx = SimpleNamespace(
        organization=object(), org_role="member", user=SimpleNamespace(id=uuid4())
    )
    records = [
        {
            "uuid": name,
            "graph_project_id": name,
            "visibility": "org",
            "default_role": role.value,
        }
        for name, role in (
            ("project-a", ProjectRole.CONTRIBUTOR),
            ("project-b", ProjectRole.CONTRIBUTOR),
            ("read-only", ProjectRole.VIEWER),
        )
    ]
    resolve = AsyncMock(return_value=auth_ctx)
    load = AsyncMock(return_value=(records, {}))
    monkeypatch.setattr(projects, "_resolve_auth_context_from_claims", resolve)
    monkeypatch.setattr(projects, "_load_project_access_records", load)
    ctx = McpContext(
        org_id="org",
        user_id="owner",
        scopes=["mcp"],
        api_key_project_ids=["project-b", "read-only"],
    )
    assert await context.get_writable_projects(ctx) == {"project-b"}
    assert await context.get_accessible_projects(ctx) == {"project-b", "read-only"}
    load.assert_awaited_once_with(auth_ctx)
    resolve.assert_awaited_once()
