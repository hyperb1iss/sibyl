"""Authenticated public captures use retained source and current authority owners."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from sibyl.api.routes import experience as route
from sibyl.jobs.lifecycle_repair import resolve_source_authority
from sibyl.persistence.auth_runtime import create_api_key_for_user
from sibyl_core.backends.surreal import SurrealAuthClient, SurrealContentClient
from sibyl_core.backends.surreal.auth_schema import bootstrap_auth_schema
from sibyl_core.backends.surreal.content_schema import bootstrap_content_schema
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.runtime_ports import install_source_authority_resolver
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime


@pytest.fixture
async def capture_service(monkeypatch):
    org, project = str(uuid4()), str(uuid4())
    auth = SurrealAuthClient(url="memory://")
    content = SurrealContentClient(url="memory://")
    graph = SurrealGraphClient(url="memory://", group_id=org)
    await bootstrap_auth_schema(auth)
    await bootstrap_content_schema(content)
    await prepare_graph_schema(graph)
    runtime = GraphRuntime(
        client=graph,
        entity_manager=EntityManager(graph, group_id=org),
        relationship_manager=RelationshipManager(graph, group_id=org),
    )
    await runtime.entity_manager.create_direct(
        Entity(id=project, name="Capture project", entity_type=EntityType.PROJECT)
    )

    @asynccontextmanager
    async def content_scope():
        yield content

    monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", content_scope)
    monkeypatch.setattr(
        "sibyl.persistence.surreal.auth.get_shared_surreal_auth_client",
        AsyncMock(return_value=auth),
    )
    monkeypatch.setattr("sibyl.persistence.surreal.auth.build_surreal_auth_client", lambda: auth)
    monkeypatch.setattr(route, "get_experience_graph_runtime", AsyncMock(return_value=runtime))
    monkeypatch.setattr(
        "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
        AsyncMock(return_value=runtime),
    )
    monkeypatch.setattr(
        "sibyl_core.services.graph.get_surreal_graph_client", AsyncMock(return_value=graph)
    )
    install_source_authority_resolver(resolve_source_authority)
    queued = AsyncMock(return_value="owned-job")
    monkeypatch.setattr("sibyl.jobs.queue.enqueue_entity_embedding_backfill", queued)
    note_queue = AsyncMock(return_value="owned-note")
    monkeypatch.setattr("sibyl.jobs.queue.enqueue_operational_note_distillation", note_queue)
    await auth.execute_query(
        "CREATE organizations CONTENT $row;", row={"uuid": org, "name": "Owned", "slug": org}
    )
    project_record = str(uuid4())
    await auth.execute_query(
        "CREATE projects CONTENT $row;",
        row={
            "uuid": project_record,
            "organization_id": org,
            "graph_project_id": project,
            "visibility": "private",
            "name": "Owned",
        },
    )

    async def actor(role="project_contributor"):
        user = uuid4()
        await auth.execute_query(
            "CREATE users CONTENT $row;",
            row={"uuid": str(user), "email": f"{user}@capture.invalid"},
        )
        await auth.execute_query(
            "CREATE organization_members CONTENT $row;",
            row={
                "uuid": str(uuid4()),
                "organization_id": org,
                "user_id": str(user),
                "role": "member",
            },
        )
        await auth.execute_query(
            "CREATE project_members CONTENT $row;",
            row={
                "uuid": str(uuid4()),
                "organization_id": org,
                "user_id": str(user),
                "project_id": project_record,
                "role": role,
            },
        )
        key, bearer = await create_api_key_for_user(
            organization_id=UUID(org),
            user_id=user,
            name="Owned capture",
            live=False,
            scopes=["api:read", "api:write"],
            project_ids=[project],
            expires_at=None,
            request=None,
        )
        return str(user), key, {"Authorization": f"Bearer {bearer}"}

    app = FastAPI()
    app.include_router(route.router, prefix="/api")
    from sibyl.api.routes import search as search_route

    app.include_router(search_route.router, prefix="/api")
    monkeypatch.setattr(
        "sibyl_core.tools.search.get_graph_runtime", AsyncMock(return_value=runtime)
    )
    assert not app.dependency_overrides
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://owned"
        ) as api:
            yield SimpleNamespace(
                api=api,
                auth=auth,
                content=content,
                runtime=runtime,
                org=org,
                project=project,
                actor=actor,
                queued=queued,
                note_queue=note_queue,
            )
    finally:
        install_source_authority_resolver(None)
        await graph.close()
        await content.close()
        await auth.close()


def payload(project):
    return {
        "experience": {
            "source_id": "ordinary-capture",
            "project_id": project,
            "goal": "Inspect page",
            "outcome": "",
            "observations": [
                {
                    "id": "state",
                    "ordinal": 0,
                    "evidence": [
                        {"id": "screen", "content": 'Page says \\"pending\\"\nNo success observed.'}
                    ],
                }
            ],
        },
        "defer_embeddings": True,
        "note_distillation": False,
    }


async def test_native_public_capture_retains_and_binds_deferred_source(capture_service):
    service = capture_service
    user, _, headers = await service.actor()
    request = payload(service.project)
    assert (await service.api.post("/api/memory/experience", json=request)).status_code == 401
    response = await service.api.post("/api/memory/experience", json=request, headers=headers)
    assert response.status_code == 201, response.text
    rows = await service.content.execute_query("SELECT * FROM raw_captures;")
    assert len(rows) == 1
    assert rows[0]["principal_id"] == user
    from sibyl.services.operational_capture import OperationalPublicationJob

    job = OperationalPublicationJob.model_validate_json(
        __import__("json").dumps(service.queued.call_args.kwargs["operational_source"])
    )
    source = await job.source()
    _, experience = await source.current()
    assert experience.outcome == ""
    assert (
        experience.observations[0].evidence[0].content
        == request["experience"]["observations"][0]["evidence"][0]["content"]
    )
    from sibyl_core.services.graph_derivations import unavailable_graph_derivation_ids

    ids = response.json()["entity_ids"]
    assert not await unavailable_graph_derivation_ids(service.org, ids)


@pytest.mark.parametrize(
    "change", ["none", "revoked", "source", "no_envelope", "midflight_revoke", "lost_ack", "scope"]
)
async def test_native_deferred_capture_current_authority(capture_service, monkeypatch, change):
    from sibyl.jobs import entities as jobs
    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata

    service = capture_service
    _, key, headers = await service.actor()
    request = payload(service.project)
    response = await service.api.post("/api/memory/experience", json=request, headers=headers)
    assert response.status_code == 201, response.text
    queued = service.queued.call_args
    calls = []

    class Spy(DeterministicEmbeddingProvider):
        async def embed_texts(self, texts, *, input_kind="document"):
            calls.extend(texts)
            if change == "midflight_revoke":
                await service.auth.execute_query(
                    "UPDATE api_keys SET revoked_at=time::now() WHERE uuid=$id;", id=str(key.id)
                )
            return await super().embed_texts(texts, input_kind=input_kind)

    from sibyl_core.backends.surreal.schema import EMBEDDING_DIM

    provider = Spy(EmbeddingMetadata("test", "one", EMBEDDING_DIM, "test", "bytes"))
    monkeypatch.setattr(jobs, "configured_embedding_provider", lambda: provider)
    service.runtime.entity_manager._embedding_provider = provider
    service.runtime.relationship_manager._embedding_provider = provider
    monkeypatch.setattr(jobs, "get_surreal_graph_runtime", AsyncMock(return_value=service.runtime))
    if change == "revoked":
        await service.auth.execute_query(
            "UPDATE api_keys SET revoked_at=time::now() WHERE uuid=$id;", id=str(key.id)
        )
    elif change == "scope":
        await service.auth.execute_query(
            "DELETE api_key_project_scopes WHERE api_key_id=$id;", id=str(key.id)
        )
    elif change == "source":
        request["experience"]["outcome"] = "unknown"
        assert (
            await service.api.post("/api/memory/experience", json=request, headers=headers)
        ).status_code == 201
    kwargs = dict(queued.kwargs)
    if change == "no_envelope":
        kwargs.pop("operational_source")
    if change == "lost_ack":
        complete = service.runtime.entity_manager.publish_operational_manifest

        async def lost_ack(*args, **values):
            await complete(*args, **values)
            raise RuntimeError("owned lost acknowledgement")

        monkeypatch.setattr(
            service.runtime.entity_manager, "publish_operational_manifest", lost_ack
        )
        with pytest.raises(RuntimeError, match="owned lost acknowledgement"):
            await jobs.backfill_entity_embeddings({}, *queued.args, **kwargs)
        monkeypatch.setattr(
            service.runtime.entity_manager, "publish_operational_manifest", complete
        )
        calls.clear()
    result = await jobs.backfill_entity_embeddings({}, *queued.args, **kwargs)
    if change not in {"none", "lost_ack"}:
        assert bool(calls) is (change == "midflight_revoke")
        assert result["entities"] == 0
    else:
        assert bool(calls) is (change == "none")
        assert result["manifest_state"] in {"complete", "completed"}
        edges = await service.runtime.client.execute_query("SELECT fact_embedding FROM relates_to;")
        assert edges
        assert all(len(edge["fact_embedding"]) == EMBEDDING_DIM for edge in edges)
        calls.clear()
        repeated = await jobs.backfill_entity_embeddings({}, *queued.args, **kwargs)
        assert repeated["manifest_state"] == "complete"
        assert not calls


@pytest.mark.parametrize("raw_only", [False, True])
async def test_native_capture_preserves_original_creator(capture_service, monkeypatch, raw_only):
    service = capture_service
    creator, _, original = await service.actor()
    request = payload(service.project)
    if raw_only:
        publish = service.runtime.entity_manager.publish_operational_entities
        monkeypatch.setattr(
            service.runtime.entity_manager,
            "publish_operational_entities",
            AsyncMock(side_effect=RuntimeError("owned crash after raw write")),
        )
        with pytest.raises(RuntimeError, match="owned crash"):
            await service.api.post("/api/memory/experience", json=request, headers=original)
        monkeypatch.setattr(service.runtime.entity_manager, "publish_operational_entities", publish)
    else:
        assert (
            await service.api.post("/api/memory/experience", json=request, headers=original)
        ).status_code == 201
    _, _, contributor = await service.actor()
    assert (
        await service.api.post("/api/memory/experience", json=request, headers=contributor)
    ).status_code == 403
    _, _, maintainer = await service.actor("project_maintainer")
    response = await service.api.post("/api/memory/experience", json=request, headers=maintainer)
    assert response.status_code == 201, response.text
    rows = await service.content.execute_query("SELECT principal_id FROM raw_captures;")
    assert rows == [{"principal_id": creator}]
    records = await service.runtime.client.execute_query(
        "SELECT created_by FROM entity WHERE derivation_required=true;"
    )
    assert records
    assert all(row["created_by"] == creator for row in records)


async def test_native_capture_deferred_fresh_process_replay(capture_service, tmp_path):
    import asyncio
    import json
    import os
    import sys
    from pathlib import Path

    service = capture_service
    if not service.runtime.client._url.startswith("ws"):
        pytest.skip("separate process requires an owned native fixture")
    _, _, headers = await service.actor()
    response = await service.api.post(
        "/api/memory/experience", json=payload(service.project), headers=headers
    )
    assert response.status_code == 201, response.text
    queued = service.queued.call_args
    values = {
        "url": service.runtime.client._url,
        "org": service.org,
        "auth": service.auth.namespace,
        "content": service.content.namespace,
        "graph_prefix": service.runtime.client.namespace.removesuffix(service.org.replace("-", "")),
        "args": queued.args,
        "kwargs": queued.kwargs,
    }
    results = []
    for attempt in range(2):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            str(Path(__file__).with_name("operational_capture_process.py")),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        output, _ = await process.communicate(json.dumps(values).encode())
        (tmp_path / f"owned-process-{attempt}.log").write_bytes(output)
        assert process.returncode == 0, output.decode()[-8000:]
        results.append(
            json.loads(
                next(
                    line.removeprefix("OWNED_RESULT=")
                    for line in output.decode().splitlines()
                    if line.startswith("OWNED_RESULT=")
                )
            )
        )
    assert results[0]["calls"] > 0
    assert results[1] == {"state": "complete", "calls": 0}


@pytest.mark.parametrize("change", ["none", "source_during_extraction", "no_envelope"])
async def test_native_bound_note_job_retains_usage_and_source(capture_service, monkeypatch, change):
    from sibyl.jobs import operational_distillation as jobs
    from sibyl_core.ai.llm import ExtractionUsage
    from sibyl_core.ai.operational_distillation import DistilledOperationalNotes

    service = capture_service
    _, _, headers = await service.actor()
    request = payload(service.project)
    request["note_distillation"] = True
    response = await service.api.post("/api/memory/experience", json=request, headers=headers)
    assert response.status_code == 201, response.text
    queued = service.note_queue.call_args
    calls = []

    class Extractor:
        async def extract_with_usage(self, prompt):
            calls.append(prompt)
            if change == "source_during_extraction":
                request["experience"]["outcome"] = "unknown"
                assert (
                    await service.api.post("/api/memory/experience", json=request, headers=headers)
                ).status_code == 201
            return SimpleNamespace(
                output=DistilledOperationalNotes(
                    workflow="Inspect the current page before changing it."
                ),
                usage=ExtractionUsage(
                    provider="test",
                    model="fixture",
                    requests=1,
                    input_tokens=120,
                    output_tokens=40,
                    total_tokens=160,
                    cost_usd=0,
                    cost_complete=True,
                ),
            )

    monkeypatch.setattr(jobs, "operational_note_distiller", lambda **_: Extractor())
    monkeypatch.setattr(jobs, "get_surreal_graph_runtime", AsyncMock(return_value=service.runtime))
    kwargs = dict(queued.kwargs)
    if change == "no_envelope":
        kwargs.pop("operational_source")
    result = await jobs.distill_operational_experience_notes({}, *queued.args, **kwargs)
    if change == "no_envelope":
        assert not calls
    else:
        assert calls
        assert (
            result["total_tokens"]
            if change == "none"
            else result["extraction_usage"]["total_tokens"]
        ) == 160
    if change != "none":
        assert result["status"] == "skipped"
    else:
        assert result["written_note_ids"]
        from sibyl_core.services.graph_derivations import unavailable_graph_derivation_ids

        assert not await unavailable_graph_derivation_ids(service.org, result["written_note_ids"])


@pytest.mark.parametrize("interrupted", [False, True])
async def test_native_source_revision_retires_exact_inventory(
    capture_service, monkeypatch, interrupted
):
    service = capture_service
    _, _, headers = await service.actor()
    request = payload(service.project)
    request["experience"]["observations"].append(
        {
            "id": "later",
            "ordinal": 1,
            "action": "Inspect another page",
            "evidence": [{"id": "later-screen", "content": "Second page evidence"}],
        }
    )
    first = await service.api.post("/api/memory/experience", json=request, headers=headers)
    assert first.status_code == 201, first.text
    request["experience"]["observations"].pop()
    if interrupted:
        publish = service.runtime.relationship_manager.publish_operational_relationships
        monkeypatch.setattr(
            service.runtime.relationship_manager,
            "publish_operational_relationships",
            AsyncMock(side_effect=RuntimeError("owned interruption after entities")),
        )
        with pytest.raises(RuntimeError, match="owned interruption"):
            await service.api.post("/api/memory/experience", json=request, headers=headers)
        monkeypatch.setattr(
            service.runtime.relationship_manager, "publish_operational_relationships", publish
        )
    second = await service.api.post("/api/memory/experience", json=request, headers=headers)
    assert second.status_code == 201, second.text
    old_entities = set(first.json()["entity_ids"]) - set(second.json()["entity_ids"])
    old_edges = set(first.json()["relationship_ids"]) - set(second.json()["relationship_ids"])
    assert old_entities
    assert old_edges
    assert second.json()["retired_entities"] == len(old_entities)
    assert second.json()["retired_relationships"] == len(old_edges)
    from sibyl_core.services.graph_derivations import unavailable_graph_derivation_ids

    assert await unavailable_graph_derivation_ids(service.org, list(old_entities)) == old_entities
    rows = await service.runtime.client.execute_query(
        "SELECT uuid,invalid_at,expired_at FROM relates_to WHERE uuid IN $ids;",
        ids=sorted(old_edges),
    )
    assert {row["uuid"] for row in rows} == old_edges
    assert all(row["invalid_at"] is not None and row["expired_at"] is not None for row in rows)


async def test_native_capture_is_immediately_retrievable(capture_service, monkeypatch):
    from sibyl.api.routes import search as search_route
    from sibyl_core.backends.surreal.schema import EMBEDDING_DIM
    from sibyl_core.embeddings.providers import DeterministicEmbeddingProvider, EmbeddingMetadata

    service = capture_service
    provider = DeterministicEmbeddingProvider(
        EmbeddingMetadata("test", "one", EMBEDDING_DIM, "test", "bytes")
    )
    monkeypatch.setattr(search_route, "configured_embedding_provider", lambda: provider)
    monkeypatch.setattr("sibyl_core.tools.search.configured_embedding_provider", lambda: provider)
    _, _, headers = await service.actor()
    request = payload(service.project)
    request.pop("note_distillation")
    request.pop("defer_embeddings")
    response = await service.api.post("/api/memory/experience", json=request, headers=headers)
    assert response.status_code == 201, response.text
    found = await service.api.post(
        "/api/search",
        headers=headers,
        json={
            "query": "pending",
            "project": service.project,
            "memory_scope": "project",
            "scope_key": service.project,
            "include_documents": False,
        },
    )
    assert found.status_code == 200, found.text
    assert any(row["id"] in response.json()["entity_ids"] for row in found.json()["results"]), (
        found.json()
    )
