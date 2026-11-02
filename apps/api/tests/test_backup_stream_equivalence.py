"""The streamed API backup writes the archive the in-memory exports assembled."""

import json
import re
import tarfile
from contextlib import asynccontextmanager
from dataclasses import asdict
from uuid import uuid4

import pytest

from sibyl.jobs import backup
from sibyl.persistence import content_archive
from sibyl_core.backends.surreal import (
    SurrealAuthClient,
    SurrealContentClient,
    bootstrap_auth_schema,
    bootstrap_content_schema,
)
from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.backends.surreal.schema import bootstrap_schema
from sibyl_core.migrate import archive_stream
from sibyl_core.migrate.archive_lineage import seal_archive_lineage
from sibyl_core.models.entities import Entity, EntityType
from sibyl_core.services.content_raw_persistence import remember_raw_memory
from sibyl_core.services.graph_client import SurrealGraphClient
from sibyl_core.services.graph_entities import EntityManager
from sibyl_core.tools.admin import create_backup
from tests.test_backup_archive_restore import _configure_stores, _seed_graph

_RUN_STAMP = re.compile(r'^  "created_at": "[^"]*",$', re.MULTILINE)


def _same_run(text: str) -> str:
    """Blank the top-level capture time, the one field two runs never share."""
    return _RUN_STAMP.sub('  "created_at": "<run>",', text)


async def _seed(stores, org: str) -> None:
    auth, content, graph = stores["source"]
    await bootstrap_auth_schema(auth)
    await bootstrap_content_schema(content)
    await bootstrap_schema(graph)
    await _seed_graph(graph, org)
    await EntityManager(graph, group_id=org).create_direct_bulk(
        [
            Entity(
                id=f"stream-{index}",
                entity_type=EntityType.TOPIC,
                name=f"Stream entity {index}",
                content=f"Stream body {index}",
            )
            for index in range(3)
        ]
    )
    for index in range(5):
        await remember_raw_memory(
            organization_id=org,
            principal_id="owner",
            source_id=f"stream-source-{index}",
            raw_content=f"Stream evidence {index}",
            metadata={"literal": "2026-09-09T00:00:00.123456789Z"},
            embedding_provider=None,
        )
    for index in range(3):
        await content.execute_query(
            "CREATE eval_consolidations CONTENT $row;",
            row={
                "uuid": f"retained-operation-{index}",
                "organization_id": org,
                "principal_id": "owner",
                "request_sha256": str(index) * 64,
                "result_kind": "abstained",
                "build_receipt_json": "{}",
            },
        )


async def _in_memory_archive(org: str) -> tuple[str, str, list]:
    """graph.json and content.json exactly as the in-memory backup wrote them."""
    graph_result = await create_backup(organization_id=org)
    assert graph_result.success, graph_result.message
    content = await content_archive.export_content_archive_payload(organization_id=org)
    graph, content = (
        json.loads(json.dumps(payload, indent=2, default=str))
        for payload in (asdict(graph_result.backup_data), content)
    )
    sealed_graph, sealed_content, report = seal_archive_lineage(graph, content)
    return (
        json.dumps(sealed_graph, indent=2, default=str),
        json.dumps(sealed_content, indent=2, default=str),
        report,
    )


@pytest.mark.parametrize("read_mode", ["one_statement", "paged"])
async def test_streamed_backup_writes_the_in_memory_archive(monkeypatch, tmp_path, read_mode):
    org = str(uuid4())
    stores = {
        "source": (
            SurrealAuthClient(url="memory://"),
            SurrealContentClient(url="memory://"),
            SurrealGraphClient(group_id=org, url="memory://"),
        )
    }
    closes = _configure_stores(monkeypatch, stores, lambda: "source", org, tmp_path)
    if read_mode == "paged":
        # The embedded engine has no client-side transactions; a server runs
        # these statements inside one. Two rows a page crosses every boundary.
        @asynccontextmanager
        async def pages_without_transaction(self):
            yield self.execute_query

        monkeypatch.setattr(DedicatedSurrealClient, "read_snapshot", pages_without_transaction)
        monkeypatch.setattr(archive_stream, "ARCHIVE_PAGE_SIZE", 2)
    try:
        await _seed(stores, org)
        result = await backup.run_backup({}, org, backup_id="stream_equivalence")
        assert result["success"], result
        with tarfile.open(tmp_path / "sibyl_stream_equivalence.tar.gz", "r:gz") as tar:
            archive = {
                member.name: tar.extractfile(member).read().decode() for member in tar.getmembers()
            }
        expected_graph, expected_content, report = await _in_memory_archive(org)
    finally:
        for close in closes:
            await close()

    assert _same_run(archive["graph.json"]) == _same_run(expected_graph)
    assert _same_run(archive["content.json"]) == _same_run(expected_content)
    assert json.loads(archive["metadata.json"])["lineage_validation"] == report
    graph = json.loads(archive["graph.json"])
    content = json.loads(archive["content.json"])
    assert graph["entity_count"] == 5
    assert content["row_counts"]["raw_captures"] == 5
    assert content["row_counts"]["eval_consolidations"] == 3
    assert not list(tmp_path.rglob("*.spool"))
