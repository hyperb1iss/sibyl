from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_default_memory_loop_runs_without_graphiti_imports() -> None:
    script = r"""
import asyncio
import builtins
import os
import sys

original_import = builtins.__import__


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "graphiti_core" or name.startswith("graphiti_core."):
        raise AssertionError(f"Graphiti import forbidden: {name}")
    return original_import(name, globals, locals, fromlist, level)


builtins.__import__ = guarded_import
os.environ["SIBYL_NATIVE_WRITE"] = "enabled"


async def main():
    from sibyl_core.models.context import ContextLayer
    from sibyl_core.services.native_graph import (
        NativeEntityManager,
        NativeGraphRuntime,
        NativeRelationshipManager,
        NativeSurrealGraphClient,
        prepare_native_graph_schema,
    )
    import sibyl_core.retrieval.native as native_retrieval
    import sibyl_core.services.native_memory as native_memory
    import sibyl_core.tools.add as add_module
    import sibyl_core.tools.context as context_module
    import sibyl_core.tools.core as core_module
    import sibyl_core.tools.reflect as reflect_module

    group_id = "no-graphiti-default-loop"
    principal_id = "principal-no-graphiti"
    project_title = "No Graphiti Project"
    client = NativeSurrealGraphClient(group_id=group_id, url="memory://")
    await client.connect()
    await prepare_native_graph_schema(client)
    runtime = NativeGraphRuntime(
        client=client,
        entity_manager=NativeEntityManager(client, group_id=group_id),
        relationship_manager=NativeRelationshipManager(client, group_id=group_id),
    )

    async def runtime_factory(requested_group_id):
        assert requested_group_id == group_id
        return runtime

    async def empty_raw_recall(**_kwargs):
        return []

    add_module.get_native_graph_runtime = runtime_factory
    context_module.get_native_graph_runtime = runtime_factory
    native_retrieval.get_native_graph_runtime = runtime_factory
    native_memory.get_native_graph_runtime = runtime_factory

    try:
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
        assert "graphiti_core" not in sys.modules
    finally:
        await client.close()


asyncio.run(main())
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False,
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_default_entrypoints_import_without_graphiti() -> None:
    script = r"""
import builtins
import importlib
import os
import runpy
import sys
from pathlib import Path

original_import = builtins.__import__


def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "graphiti_core" or name.startswith("graphiti_core."):
        raise AssertionError(f"Graphiti import forbidden: {name}")
    return original_import(name, globals, locals, fromlist, level)


builtins.__import__ = guarded_import
os.environ["SIBYL_AUTH_STORE"] = "surreal"
os.environ["SIBYL_COORDINATION_BACKEND"] = "local"
os.environ["SIBYL_MCP_AUTH_MODE"] = "off"
os.environ["SIBYL_NATIVE_WRITE"] = "enabled"
os.environ["SIBYL_STORE"] = "surreal"

cli_main = importlib.import_module("sibyl_cli.main")
import sibyl.jobs.entities
from sibyl.server import create_mcp_server

assert cli_main.app is not None
assert create_mcp_server(host="127.0.0.1", port=3334) is not None

root = Path(os.environ["SIBYL_REPO_ROOT"])
runpy.run_path(str(root / "apps/cli/src/sibyl_cli/data/hooks/session-start.py"))
runpy.run_path(str(root / "apps/cli/src/sibyl_cli/data/hooks/user-prompt-submit.py"))

assert "graphiti_core" not in sys.modules
"""
    repo_root = Path(__file__).resolve().parents[4]
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(sys.path),
        "SIBYL_REPO_ROOT": str(repo_root),
    }
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        check=False,
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr + result.stdout
