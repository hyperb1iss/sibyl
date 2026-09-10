"""Cross-store export drift preserves content while retiring mismatched trust."""

from copy import deepcopy

from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive_lineage import seal_archive_lineage
from sibyl_core.migrate.source_integrity import validate_integrity_archive
from sibyl_core.services import content_client
from sibyl_core.services.source_archive_store import export_source_integrity
from sibyl_core.services.surreal_content import remember_raw_memory
from tests.test_graph_derivation_publication import publish, share_plan
from tests.test_reflection_identity import content_store as content_store
from tests.test_reflection_identity import runtime as runtime
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)


async def test_export_drift_quarantines_actual_shared_graph_without_rebinding(
    runtime, content_store
):
    org = runtime.client.group_id
    source = await remember_raw_memory(
        organization_id=org,
        principal_id="user_a",
        source_id="archive-cross-store",
        raw_content="Approved deployment rule",
        embedding_provider=None,
    )
    published = await publish(runtime, await share_plan(runtime, source.id))
    assert published.success
    graph = {
        "version": "3.0",
        "source_integrity": await export_source_integrity(
            runtime.client.execute_query, kind=SourceKind.GRAPH_ENTITY, organizations=[org]
        ),
    }
    original = deepcopy(graph)
    standalone, _, pending = seal_archive_lineage(graph, None)
    assert any(
        row["source_id"] == published.promoted_id and row["status"] == "unresolved"
        for row in pending
    )
    assert (
        next(
            row
            for row in standalone["source_integrity"]["derivations"]
            if row["target_id"] == published.promoted_id
        )["active"]
        is True
    )
    async with content_client.surreal_content_client() as client:
        complete = {
            "version": "2.0",
            "source_integrity": await export_source_integrity(
                client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=[org]
            ),
        }
        _, _, good = seal_archive_lineage(graph, complete)
        assert not good
        await client.execute_query(
            "UPDATE raw_captures SET raw_content='Revoked rule', revision+=1 WHERE uuid=$uuid;",
            uuid=source.id,
        )
        later = {
            "version": "2.0",
            "source_integrity": await export_source_integrity(
                client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=[org]
            ),
        }
    sealed, _, report = seal_archive_lineage(graph, later)
    assert graph == original
    assert any(
        row["source_id"] == published.promoted_id and row["status"] == "quarantined"
        for row in report
    )
    rows, _, associations = validate_integrity_archive(
        sealed["source_integrity"], kind=SourceKind.GRAPH_ENTITY, organizations=[org]
    )
    archived = next(row for row in associations if row["target_id"] == published.promoted_id)
    previous = next(
        row
        for row in original["source_integrity"]["derivations"]
        if row["target_id"] == published.promoted_id
    )
    assert archived["active"] is False
    assert archived["observations"] == previous["observations"]
    assert (
        next(row for row in rows if row["uuid"] == published.promoted_id)["derivation_required"]
        is True
    )


async def test_actual_merge_retains_original_provenance_and_quarantines_memory(
    runtime, monkeypatch
):
    import json
    from dataclasses import asdict
    from pathlib import Path
    from unittest.mock import AsyncMock
    from uuid import uuid4

    from sibyl_core.backends.surreal.schema import bootstrap_schema
    from sibyl_core.migrate.archive import (
        GRAPH_FILENAME,
        LoadedArchive,
        build_manifest,
        graph_payload_from_archive,
    )
    from sibyl_core.migrate.merge import ArchiveMergeOptions, merge_archives
    from sibyl_core.services.graph_client import SurrealGraphClient
    from sibyl_core.services.graph_entities import EntityManager
    from sibyl_core.services.graph_relationships import RelationshipManager
    from sibyl_core.services.graph_runtime import GraphRuntime
    from sibyl_core.tools.admin import BackupData, create_backup, restore_backup
    from tests.test_memory_projection_derivations import protected_session

    source, target, _ = await protected_session(runtime, monkeypatch)
    backup = await create_backup(organization_id=runtime.client.group_id)
    assert backup.success
    original = json.dumps(asdict(backup.backup_data), sort_keys=True).encode()
    files = {GRAPH_FILENAME: original}
    archive = LoadedArchive(
        source=Path("owned-archive"),
        files=files,
        manifest=build_manifest(
            organization_id=runtime.client.group_id, source_store="surreal", files=files
        ),
    )
    org = "merged-" + uuid4().hex
    result = merge_archives([archive], options=ArchiveMergeOptions(canonical_org_id=org))
    provenance = result.archive.manifest.metadata["merge"]["original_provenance_files"]
    assert len(provenance) == 1
    assert result.archive.files[provenance[0]] == original
    merged = graph_payload_from_archive(result.archive)
    assert merged is not None
    client = SurrealGraphClient(group_id=org, url="memory://")
    try:
        await bootstrap_schema(client)
        destination = GraphRuntime(
            client=client,
            entity_manager=EntityManager(client, group_id=org),
            relationship_manager=RelationshipManager(client, group_id=org),
        )
        monkeypatch.setattr(
            "sibyl_core.services.graph_runtime.get_surreal_graph_runtime",
            AsyncMock(return_value=destination),
        )
        restored = await restore_backup(BackupData(**merged), organization_id=org)
        assert restored.success, restored.errors
        quarantined = {row["source_id"] for row in restored.quarantined}
        assert {source.id, target.id} <= quarantined
        target_row = await destination.entity_manager.get(target.id)
        assert target_row.content == target.content
        assert target_row.derivation_required is True
        assert not await client.execute_query("SELECT * FROM memory_derivations;")
    finally:
        await client.close()
