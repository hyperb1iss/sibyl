from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("fastapi") is None,
    reason="no-Graphiti default-loop smoke imports API entrypoints",
)


def _subprocess_env() -> dict[str, str]:
    pythonpath = os.pathsep.join(
        dict.fromkeys(
            [
                str(_REPO_ROOT / "apps/api/src"),
                str(_REPO_ROOT / "apps/cli/src"),
                *sys.path,
            ]
        )
    )
    return {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "SIBYL_REPO_ROOT": str(_REPO_ROOT),
    }


def test_default_memory_loop_runs_end_to_end() -> None:
    script = r"""
import asyncio
import os
import sys
import uuid
from types import SimpleNamespace


async def main():
    from sibyl_core.models.context import ContextLayer
    from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
    from sibyl_core.tasks.dependencies import (
        get_blocking_tasks,
        get_task_dependencies,
        suggest_task_order,
    )
    from sibyl.ingestion.extractor import ExtractedEntity, ExtractedEntityType
    from sibyl.ingestion.relationships import ExtractedRelationship, RelationType
    from sibyl_core.services.graph import (
        EntityManager,
        GraphRuntime,
        RelationshipManager,
        SurrealGraphClient,
        prepare_graph_schema,
    )
    import sibyl_core.retrieval.search as retrieval_module
    import sibyl_core.services.memory as memory_module
    import sibyl_core.tools.add as add_module
    import sibyl_core.tools.context as context_module
    import sibyl_core.tools.core as core_module
    import sibyl_core.tools.explore as explore_module
    import sibyl_core.tools.health as health_module
    import sibyl_core.tools.manage as manage_module
    import sibyl_core.tools.reflect as reflect_module
    import sibyl_core.tools.search as search_module
    import sibyl_core.tools.temporal as temporal_module
    import sibyl.api.dependencies as api_dependencies
    import sibyl.crawler.graph_integration as graph_integration
    import sibyl.ingestion.storage as ingestion_storage
    import sibyl.jobs.consolidation as consolidation_module
    import sibyl.persistence.graph_runtime as graph_runtime_module

    group_id = "no-graphiti-default-loop"
    principal_id = "principal-no-graphiti"
    project_title = "No Graphiti Project"
    client = SurrealGraphClient(group_id=group_id, url="memory://")
    await client.connect()
    await prepare_graph_schema(client)
    runtime = GraphRuntime(
        client=client,
        entity_manager=EntityManager(client, group_id=group_id),
        relationship_manager=RelationshipManager(client, group_id=group_id),
    )

    async def runtime_factory(requested_group_id, **_kwargs):
        assert requested_group_id == group_id
        return runtime

    async def empty_raw_recall(**_kwargs):
        return []

    add_module.get_surreal_graph_runtime = runtime_factory
    context_module.get_surreal_graph_runtime = runtime_factory
    explore_module.get_graph_runtime = runtime_factory
    health_module.get_graph_runtime = runtime_factory
    manage_module.get_graph_runtime = runtime_factory
    retrieval_module.get_surreal_graph_runtime = runtime_factory
    memory_module.get_surreal_graph_runtime = runtime_factory
    search_module.get_graph_runtime = runtime_factory
    temporal_module.get_graph_runtime = runtime_factory
    consolidation_module._get_graph_runtime = runtime_factory
    graph_runtime_module._get_graph_runtime = runtime_factory
    ingestion_storage._get_graph_runtime = runtime_factory

    try:
        ingestion_entities = [
            ExtractedEntity(
                entity_type=ExtractedEntityType.CONCEPT,
                name="Native Ingestion Source",
                description="Default loop ingestion source.",
                confidence=0.91,
                source_episode_id="episode-no-graphiti",
                context="Native ingestion source context.",
            ),
            ExtractedEntity(
                entity_type=ExtractedEntityType.CONCEPT,
                name="Native Ingestion Target",
                description="Default loop ingestion target.",
                confidence=0.92,
                source_episode_id="episode-no-graphiti",
                context="Native ingestion target context.",
            ),
        ]
        ingestion_entity_ids, ingestion_errors = await ingestion_storage.store_entities(
            ingestion_entities,
            group_id=group_id,
        )
        assert ingestion_errors == []
        assert set(ingestion_entity_ids) == {
            "native ingestion source",
            "native ingestion target",
        }
        stored_rels, skipped_rels, relationship_errors = (
            await ingestion_storage.store_relationships(
                [
                    ExtractedRelationship(
                        source_name="Native Ingestion Source",
                        target_name="Native Ingestion Target",
                        relation_type=RelationType.RELATED_TO,
                        confidence=0.88,
                        source_episode_id="episode-no-graphiti",
                        evidence="Native ingestion source references target.",
                    )
                ],
                ingestion_entity_ids,
                group_id=group_id,
            )
        )
        assert stored_rels == 1
        assert skipped_rels == 0
        assert relationship_errors == []

        project = await core_module.add(
            title=project_title,
            content="Project anchor for native default loop smoke coverage.",
            entity_type="project",
            metadata={"organization_id": group_id},
            sync=True,
            check_conflicts=False,
        )
        assert project.success, project.message
        project_id = str(project.id)

        remembered = await add_module.add(
            title="No Graphiti Default Loop Decision",
            content="Native Surreal recall should find this default loop decision.",
            entity_type="decision",
            metadata={"organization_id": group_id, "project_id": project_id},
            sync=True,
            check_conflicts=False,
        )
        assert remembered.success, remembered.message
        await runtime.relationship_manager.create(
            Relationship(
                id="rel_no_graphiti_temporal",
                relationship_type=RelationshipType.RELATED_TO,
                source_id=str(remembered.id),
                target_id=project_id,
                metadata={
                    "fact": "No Graphiti temporal reads use native Surreal edges.",
                    "valid_at": "2026-05-12T00:00:00+00:00",
                },
            )
        )
        task_a = Entity(
            id="task_no_graphiti_manage_a",
            entity_type=EntityType.TASK,
            name="No Graphiti Manage Task A",
            description="Manage tool native task A.",
            organization_id=group_id,
            metadata={
                "project_id": project_id,
                "status": "todo",
                "priority": "high",
                "task_order": 2,
            },
        )
        task_b = Entity(
            id="task_no_graphiti_manage_b",
            entity_type=EntityType.TASK,
            name="No Graphiti Manage Task B",
            description="Manage tool native task B.",
            organization_id=group_id,
            metadata={
                "project_id": project_id,
                "status": "todo",
                "priority": "medium",
                "task_order": 1,
            },
        )
        await runtime.entity_manager.create_direct(task_a)
        await runtime.entity_manager.create_direct(task_b)
        await runtime.relationship_manager.create(
            Relationship(
                id="rel_no_graphiti_manage_dep_a_b",
                relationship_type=RelationshipType.DEPENDS_ON,
                source_id=task_a.id,
                target_id=task_b.id,
            )
        )
        await runtime.relationship_manager.create(
            Relationship(
                id="rel_no_graphiti_manage_dep_b_a",
                relationship_type=RelationshipType.DEPENDS_ON,
                source_id=task_b.id,
                target_id=task_a.id,
            )
        )

        search = await core_module.search(
            "native surreal recall",
            types=["decision"],
            include_documents=False,
            organization_id=group_id,
        )
        assert any(result.id == remembered.id for result in search.results)

        explored = await explore_module.explore(
            mode="list",
            types=["decision"],
            project=project_id,
            organization_id=group_id,
        )
        assert any(result.id == remembered.id for result in explored.entities)

        temporal = await temporal_module.temporal_query(
            mode="history",
            entity_id=str(remembered.id),
            organization_id=group_id,
        )
        temporal_edge = next(
            edge for edge in temporal.edges if edge.id == "rel_no_graphiti_temporal"
        )
        assert temporal_edge.source_name == "No Graphiti Default Loop Decision"
        assert temporal_edge.target_name == project_title

        updated = await manage_module.manage(
            action="update_task",
            entity_id=task_a.id,
            data={"status": "doing"},
            organization_id=group_id,
        )
        assert updated.success, updated.message

        prioritized = await manage_module.manage(
            action="prioritize",
            entity_id=project_id,
            organization_id=group_id,
        )
        assert prioritized.success, prioritized.message
        assert prioritized.data["tasks"][0]["id"] == task_a.id

        cycles = await manage_module.manage(
            action="detect_cycles",
            entity_id=project_id,
            organization_id=group_id,
        )
        assert cycles.success, cycles.message
        assert cycles.data["has_cycles"] is True

        dependencies = await get_task_dependencies(runtime.client, task_a.id, group_id)
        assert dependencies.dependencies == [task_b.id]
        assert dependencies.blockers == [task_b.id]

        blocking = await get_blocking_tasks(runtime.client, task_b.id, group_id)
        assert blocking.dependencies == [task_a.id]
        assert blocking.blockers == [task_a.id]

        suggested_order = await suggest_task_order(runtime.client, group_id, project_id=project_id)
        assert sorted(suggested_order.unordered_tasks) == sorted([task_a.id, task_b.id])
        assert suggested_order.warnings

        doc_links = await graph_integration.GraphIntegrationService(
            runtime.client,
            group_id,
            extract_entities=False,
        ).create_doc_relationships(
            uuid.UUID("00000000-0000-0000-0000-000000000123"),
            [str(remembered.id)],
            document_title="No Graphiti Docs",
            document_url="https://docs.example.test/no-graphiti",
        )
        assert doc_links == 1

        consolidation = await consolidation_module.consolidate_org(
            {},
            group_id=group_id,
            max_merges_per_run=1,
        )
        assert consolidation["duplicates_found"] == 0

        decay = await consolidation_module.priority_decay(
            {},
            group_id=group_id,
            min_age_days=9999,
            max_archives_per_run=1,
        )
        assert decay["archived"] == 0

        health = await core_module.get_health(organization_id=group_id)
        assert health["status"] == "healthy"
        assert health["graph_connected"] is True

        stats = await core_module.get_stats(organization_id=group_id)
        assert stats["total_entities"] >= 2

        graph_stats = await graph_runtime_module.get_graph_stats_payload(group_id)
        assert graph_stats["total_entities"] >= 2

        task_runtime = await graph_runtime_module.get_entity_graph_runtime(group_id)
        assert task_runtime.entity_manager is runtime.entity_manager

        graph_adapter = await graph_runtime_module.get_graph_query_adapter(group_id)
        connection_counts = await graph_adapter.get_connection_counts([task_a.id, task_b.id])
        assert connection_counts[task_a.id] >= 1

        graph_store = await api_dependencies.get_graph_store(
            org=SimpleNamespace(id=group_id),
        )
        assert await graph_store.entities.count() >= 2

        pack = await context_module.compile_context(
            "native Surreal default loop decision",
            intent="build",
            layer=ContextLayer.WAKE,
            project=project_id,
            accessible_projects={project_id},
            principal_id=principal_id,
            organization_id=group_id,
            include_related=True,
            raw_memory_recall_fn=empty_raw_recall,
        )
        assert pack.total_items >= 1
        markdown = context_module.context_pack_to_markdown(pack)
        assert "No Graphiti Default Loop Decision" in markdown

        reflection = await reflect_module.reflect_memory(
            "Decision: native reflection writes stay on direct Surreal records.",
            source_title="No Graphiti Reflection",
            intent="build",
            project=project_id,
            organization_id=group_id,
            principal_id=principal_id,
            accessible_projects={project_id},
            persist=True,
            persist_source=True,
            limit=3,
        )
        assert reflection.persisted_count >= 1

        recall = await context_module.compile_context(
            "native reflection direct Surreal records",
            intent="build",
            layer="recall",
            project=project_id,
            accessible_projects={project_id},
            principal_id=principal_id,
            organization_id=group_id,
            include_related=False,
            raw_memory_recall_fn=empty_raw_recall,
        )
        recall_markdown = context_module.context_pack_to_markdown(recall)
        assert "No Graphiti Reflection" in recall_markdown
    finally:
        await client.close()


asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False,
        cwd=os.getcwd(),
        env=_subprocess_env(),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_default_entrypoints_import_and_wire_routes() -> None:
    script = r"""
import importlib
import os
import runpy
import sys
from pathlib import Path

os.environ["SIBYL_AUTH_STORE"] = "surreal"
os.environ["SIBYL_COORDINATION_BACKEND"] = "local"
os.environ["SIBYL_MCP_AUTH_MODE"] = "off"
os.environ["SIBYL_STORE"] = "surreal"

cli_main = importlib.import_module("sibyl_cli.main")
from sibyl.api.app import create_api_app
from sibyl.main import create_combined_app
import sibyl.jobs.backup
import sibyl.jobs.consolidation
import sibyl.jobs.crawl
import sibyl.jobs.entities
import sibyl.jobs.pending
import sibyl.jobs.queue
import sibyl.jobs.worker
import sibyl.crawler.pipeline
import sibyl.ingestion.storage
import sibyl_core.retrieval.search
import sibyl_core.tools.admin
import sibyl_core.tools.conflicts
import sibyl_core.tools.explore
import sibyl_core.tools.health
import sibyl_core.tools.manage
import sibyl_core.tools.search
import sibyl_core.tools.temporal
import sibyl.crawler.graph_integration
from sibyl.server import create_mcp_server

assert cli_main.app is not None
api_app = create_api_app()
mcp = create_mcp_server(host="127.0.0.1", port=3334)
combined_app = create_combined_app(host="127.0.0.1", port=3334)
api_paths = {getattr(route, "path", "") for route in api_app.routes}
combined_routes = {
    (getattr(route, "path", ""), getattr(route, "name", "")) for route in combined_app.routes
}
assert "/health" in api_paths
assert any(str(path).startswith("/memory") for path in api_paths)
assert mcp is not None
assert ("/api", "api") in combined_routes
assert ("", "mcp") in combined_routes

root = Path(os.environ["SIBYL_REPO_ROOT"])
runpy.run_path(str(root / "apps/cli/src/sibyl_cli/data/hooks/session-start.py"))
"""
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False,
        cwd=os.getcwd(),
        env=_subprocess_env(),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
