"""Older public archives cannot undo newer source visibility decisions.

Runs on the embedded engine and, with SIBYL_LIVE_SURREAL_TESTS=1, on the
server SIBYL_SURREAL_URL names. Recall picks its SurrealQL dialect from the
configured URL, so each case points the settings at the engine it opened.
"""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.backends.surreal.url_schemes import is_embedded_surreal_url
from sibyl_core.config import settings
from sibyl_core.services.graph_client import SurrealGraphClient, prepare_graph_schema
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.services.graph_relationships import RelationshipManager
from sibyl_core.services.graph_runtime import GraphRuntime
from sibyl_core.services.memory_correction import apply_memory_correction
from sibyl_core.services.surreal_content import recall_raw_memory, remember_raw_memory


def engine_url(engine: str) -> str:
    if engine == "embedded":
        return "memory://"
    if os.environ.get("SIBYL_LIVE_SURREAL_TESTS") != "1":
        pytest.skip("live SurrealDB tests are disabled")
    url = os.environ.get("SIBYL_SURREAL_URL", "")
    if not url or is_embedded_surreal_url(url):
        pytest.skip("live SurrealDB tests require SIBYL_SURREAL_URL to point at a server")
    return url


def _credentials(url: str) -> dict[str, str]:
    if is_embedded_surreal_url(url):
        return {}
    return {
        "username": os.environ.get("SIBYL_SURREAL_USERNAME", "root"),
        "password": os.environ.get("SIBYL_SURREAL_PASSWORD", "root"),
    }


@pytest.fixture(params=["embedded", "live"])
def url(request, monkeypatch):
    url = engine_url(request.param)
    monkeypatch.setattr(settings, "surreal_url", url)
    return url


@pytest.fixture
async def open_store(url, monkeypatch):
    """Open content stores on the engine under test and make one the active store."""
    opened = []

    async def open_one():
        client = SurrealContentClient(
            url=url, namespace=f"verify_revocation_{uuid4().hex}", **_credentials(url)
        )
        opened.append((client, client.close))
        await bootstrap_content_schema(client)
        monkeypatch.setattr(client, "close", AsyncMock())
        monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: client)

        @asynccontextmanager
        async def session():
            yield client

        monkeypatch.setattr("sibyl_core.services.content_client.surreal_content_client", session)
        return client

    try:
        yield open_one
    finally:
        for client, close in opened:
            if not is_embedded_surreal_url(url):
                await client.execute_query(f"REMOVE NAMESPACE IF EXISTS {client.namespace};")
            await close()


@pytest.fixture
async def revocation_store(url, open_store, monkeypatch):
    org = str(uuid4())
    graph = SurrealGraphClient(
        group_id=org,
        url=url,
        namespace_prefix=f"verify_revocation_{uuid4().hex}_",
        **_credentials(url),
    )
    try:
        client = await open_store()
        await prepare_graph_schema(graph)
        runtime = GraphRuntime(
            client=graph,
            entity_manager=EntityManager(graph, group_id=org),
            relationship_manager=RelationshipManager(graph, group_id=org),
        )
        for path in (
            "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
            "sibyl_core.services.memory_lifecycle.get_surreal_graph_runtime",
        ):
            monkeypatch.setattr(path, AsyncMock(return_value=runtime))
        monkeypatch.setattr(
            "sibyl_core.services.content_models.configured_raw_memory_embedding_provider",
            lambda: None,
        )
        yield client, org
    finally:
        if not is_embedded_surreal_url(url):
            await graph.execute_query(f"REMOVE NAMESPACE IF EXISTS {graph.namespace};")
        await graph.close()


async def capture(org, source_id="original"):
    return await remember_raw_memory(
        organization_id=org,
        principal_id="owner",
        source_id=source_id,
        raw_content="amethyst deployment evidence",
        embedding_provider=None,
    )


async def visible(org):
    return {
        row.id
        for row in await recall_raw_memory(
            organization_id=org,
            principal_id="owner",
            query="amethyst",
        )
    }


async def source_fingerprint(client, source_id):
    return await client.execute_query(
        "RETURN crypto::sha256(type::string({"
        "row: (SELECT * FROM raw_captures WHERE uuid=$source_id),"
        "state: (SELECT * FROM source_states WHERE source_id=$source_id)}));",
        source_id=source_id,
    )


@pytest.mark.parametrize("clean", [False, True])
@pytest.mark.parametrize("action", ["hide", "delete", "supersede"])
async def test_public_restore_retains_newer_raw_revocation(revocation_store, clean, action):
    client, org = revocation_store
    source = await capture(org)
    replacement = await capture(org, "replacement")
    archive = await content_archive.export_content_archive_payload(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action=action,
        **({"replacement_source_id": replacement.id} if action == "supersede" else {}),
    )
    assert source.id not in await visible(org)
    before = await source_fingerprint(client, source.id)
    result = await content_archive.restore_content_archive_payload(archive, clean=clean)
    assert result.success, result.errors
    assert source.id not in await visible(org)
    assert await source_fingerprint(client, source.id) == before
    assert any(
        row["source_id"] == source.id and row["reason"] == "retained_source_revocation"
        for row in result.integrity_conflicts
    )


async def test_clean_omission_retains_tombstone_before_later_restore(revocation_store):
    client, org = revocation_store
    empty = await content_archive.export_content_archive_payload(org)
    source = await capture(org)
    archive = await content_archive.export_content_archive_payload(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action="hide",
    )
    result = await content_archive.restore_content_archive_payload(empty, clean=True)
    assert result.success, result.errors
    assert not await client.execute_query(
        "SELECT * FROM raw_captures WHERE uuid=$id;", id=source.id
    )
    state = await client.execute_query(
        "SELECT * FROM source_states WHERE source_id=$id;", id=source.id
    )
    assert state[0]["deleted"] is True
    before = await source_fingerprint(client, source.id)
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert result.success, result.errors
    assert source.id not in await visible(org)
    assert await source_fingerprint(client, source.id) == before
    assert any(row["reason"] == "retained_tombstone" for row in result.integrity_conflicts)


async def test_forward_archive_restores_explicitly_unhidden_source(revocation_store, open_store):
    _, org = revocation_store
    source = await capture(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action="hide",
    )
    hidden = await content_archive.export_content_archive_payload(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action="restore",
    )
    active = await content_archive.export_content_archive_payload(org)
    await open_store()
    result = await content_archive.restore_content_archive_payload(hidden, clean=True)
    assert result.success, result.errors
    assert source.id not in await visible(org)
    result = await content_archive.restore_content_archive_payload(active, clean=True)
    assert result.success, result.errors
    assert not result.integrity_conflicts
    assert source.id in await visible(org)


async def test_older_active_content_remains_restorable_with_same_audience(revocation_store):
    from sibyl_core.services.surreal_content import get_raw_memory

    _, org = revocation_store
    source = await capture(org)
    archive = await content_archive.export_content_archive_payload(org)
    await apply_memory_correction(
        organization_id=org,
        principal_id="owner",
        source_id=source.id,
        action="revise",
        revised_content="amethyst changed deployment",
    )
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert result.success, result.errors
    restored = await get_raw_memory(memory_id=source.id, organization_id=org)
    assert restored.raw_content == source.raw_content
    assert source.id in await visible(org)


async def test_older_archive_cannot_change_current_raw_owner(revocation_store):
    from dataclasses import replace

    from sibyl_core.services.content_raw_persistence import save_raw_memory
    from sibyl_core.services.surreal_content import get_raw_memory

    client, org = revocation_store
    source = await capture(org)
    archive = await content_archive.export_content_archive_payload(org)
    await save_raw_memory(replace(source, principal_id="other-owner"), embedding_provider=None)
    before = await source_fingerprint(client, source.id)
    result = await content_archive.restore_content_archive_payload(archive, clean=True)
    assert result.success, result.errors
    assert await source_fingerprint(client, source.id) == before
    assert source.id not in await visible(org)
    restored = await get_raw_memory(memory_id=source.id, organization_id=org)
    assert restored.principal_id == "other-owner"
    assert any(row["reason"] == "retained_source_authority" for row in result.integrity_conflicts)
