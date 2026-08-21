from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from sibyl_cli.client import SibylClient
from sibyl_cli.client_admin import ClientAdminMixin
from sibyl_cli.client_auth import ClientAuthMixin
from sibyl_cli.client_graph import ClientGraphMixin
from sibyl_cli.client_memory import ClientMemoryMixin
from sibyl_cli.client_sources import ClientSourcesMixin
from sibyl_cli.client_transport import ClientTransportMixin
from sibyl_cli.client_work import ClientWorkMixin

PUBLIC_METHOD_OWNERS = {
    ClientTransportMixin: {
        "close",
        "get",
        "post",
        "patch",
        "delete",
    },
    ClientAuthMixin: {
        "list_api_keys",
        "create_api_key",
        "revoke_api_key",
        "local_signup",
        "local_login",
        "list_orgs",
        "create_org",
        "switch_org",
        "list_org_members",
        "add_org_member",
        "update_org_member_role",
        "remove_org_member",
        "preview_memory_space_access",
        "list_teams",
        "create_team",
        "add_team_member",
        "remove_team_member",
        "link_team_project",
        "unlink_team_project",
    },
    ClientGraphMixin: {
        "list_entities",
        "get_entity",
        "list_raw_captures",
        "get_raw_capture",
        "create_entity",
        "update_entity",
        "delete_entity",
        "resolve_id_prefix",
        "search",
        "explore",
        "temporal_query",
        "add_knowledge",
        "link_graph",
        "link_graph_status",
    },
    ClientWorkMixin: {
        "start_task",
        "block_task",
        "unblock_task",
        "submit_review",
        "complete_task",
        "archive_task",
        "create_task",
        "update_task",
        "create_note",
        "list_notes",
    },
    ClientMemoryMixin: {
        "remember_raw_memory",
        "recall_raw_memory",
        "memory_audit",
        "cite_memory",
        "memory_inspect",
        "memory_blame",
        "correct_memory",
        "preview_reflection_promotion",
        "preview_memory_promotion",
        "promote_memory",
        "auto_review_reflection_promotion",
        "drain_reflection_review",
        "enqueue_reflection_dream_cycle",
        "preview_memory_share",
        "share_memory",
        "synthesis_plan",
        "synthesis_draft",
        "synthesis_handbook",
        "context_pack",
        "reflect",
    },
    ClientSourcesMixin: {
        "source_import_status",
        "start_source_import",
        "start_document_import",
        "list_document_collections",
        "ingestion_source_import_status",
        "resume_source_import",
        "cancel_source_import",
        "create_crawl_source",
        "list_crawl_sources",
        "get_crawl_source",
        "delete_crawl_source",
        "start_crawl",
        "get_crawl_status",
        "list_crawl_documents",
        "get_crawl_document",
        "crawler_stats",
        "crawler_health",
    },
    ClientAdminMixin: {
        "list_jobs",
        "health",
        "stats",
    },
}


def test_public_methods_resolve_directly_to_one_canonical_owner() -> None:
    expected = set().union(*PUBLIC_METHOD_OWNERS.values())
    actual = {
        name
        for name, value in inspect.getmembers(SibylClient, predicate=inspect.isfunction)
        if not name.startswith("_")
    }

    assert len(expected) == 88
    assert actual == expected
    assert {
        name
        for name, value in SibylClient.__dict__.items()
        if inspect.isfunction(value) and not name.startswith("_")
    } == set()

    for owner, method_names in PUBLIC_METHOD_OWNERS.items():
        for method_name in method_names:
            assert getattr(SibylClient, method_name) is owner.__dict__[method_name]


@pytest.mark.asyncio
async def test_owner_mixins_preserve_representative_wire_contracts() -> None:
    client = SibylClient(base_url="http://example.test/api", auth_token="token")
    response = {"marker": object()}
    request = AsyncMock(return_value=response)
    client._request = request

    result = await client.create_api_key(
        name="deploy",
        live=False,
        scopes=["memory:read"],
        project_ids=["project:1"],
        memory_space_ids=["space:1"],
        expires_days=2,
    )
    assert result is response
    request.assert_awaited_once_with(
        "POST",
        "/auth/api-keys",
        json={
            "name": "deploy",
            "live": False,
            "scopes": ["memory:read"],
            "project_ids": ["project:1"],
            "memory_space_ids": ["space:1"],
            "expires_days": 2,
        },
    )
    request.reset_mock()

    result = await client.resolve_id_prefix("task: a/b", entity_type="task", limit=4)
    assert result is response
    request.assert_awaited_once_with(
        "GET",
        "/resolve/task%3A%20a%2Fb",
        params={"limit": 4, "entity_type": "task"},
    )
    request.reset_mock()

    result = await client.create_task(
        "Ship it",
        "project:1",
        description="Ready",
        priority="high",
        complexity="low",
        status="doing",
        assignees=["nova"],
        epic_id="epic:1",
        feature="harmony",
        tags=["cli"],
        technologies=["python"],
        depends_on=["task:0"],
    )
    assert result is response
    request.assert_awaited_once_with(
        "POST",
        "/tasks",
        json={
            "title": "Ship it",
            "project_id": "project:1",
            "priority": "high",
            "complexity": "low",
            "status": "doing",
            "description": "Ready",
            "assignees": ["nova"],
            "epic_id": "epic:1",
            "feature": "harmony",
            "tags": ["cli"],
            "technologies": ["python"],
            "depends_on": ["task:0"],
        },
    )
    request.reset_mock()

    result = await client.reflect(
        "session notes",
        source_title="Review",
        intent="build",
        domain="cli",
        project="sibyl",
        related_to=["task:1"],
        persist=True,
        persist_source=False,
        persist_review=True,
        limit=6,
        cited_ids=["memory:1"],
    )
    assert result is response
    request.assert_awaited_once_with(
        "POST",
        "/context/reflect",
        json={
            "content": "session notes",
            "source_title": "Review",
            "intent": "build",
            "persist": True,
            "persist_source": False,
            "persist_review": True,
            "limit": 6,
            "domain": "cli",
            "project": "sibyl",
            "related_to": ["task:1"],
            "cited_ids": ["memory:1"],
        },
    )
    request.reset_mock()

    result = await client.start_document_import(
        kind="url",
        source_uri="https://example.test/doc",
        title="Guide",
        collection="manuals",
        target_scope_key="project:1",
        batch_size=32,
        promotion_preview_approved=True,
        allow_private_network=True,
    )
    assert result is response
    request.assert_awaited_once_with(
        "POST",
        "/ingestion/documents",
        json={
            "kind": "url",
            "source_uri": "https://example.test/doc",
            "text": None,
            "title": "Guide",
            "collection": "manuals",
            "target_scope_key": "project:1",
            "batch_size": 32,
            "promotion_preview_approved": True,
            "allow_private_network": True,
        },
    )
    request.reset_mock()

    result = await client.list_jobs(function="reflection_dream", limit=7)
    assert result is response
    request.assert_awaited_once_with(
        "GET",
        "/jobs",
        params={"limit": 7, "function": "reflection_dream"},
    )
    request.reset_mock()

    result = await client.patch(
        "/entities/entity:1",
        json={"name": "Updated"},
        params={"sync": "true"},
    )
    assert result is response
    request.assert_awaited_once_with(
        "PATCH",
        "/entities/entity:1",
        json={"name": "Updated"},
        params={"sync": "true"},
    )
