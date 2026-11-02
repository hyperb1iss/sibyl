"""Streamed archive captures write the bytes the in-memory exports assemble."""

import io
import json
import re
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate import archive_stream
from sibyl_core.migrate.archive_lineage import seal_archive_lineage, seal_lineage
from sibyl_core.migrate.archive_stream import (
    ArchiveSpool,
    Deferred,
    PageReader,
    SourceCapture,
    Streamed,
    StreamedObject,
    capture_snapshot_rows,
    read_source_capture,
    spool_path,
    write_json,
)
from sibyl_core.migrate.legacy_graph_archive import episode_from_payload, save_native_episode
from sibyl_core.migrate.source_integrity import (
    ArchiveDatetime,
    IntegrityRowValidator,
    SectionDigest,
    build_integrity_archive,
)
from sibyl_core.models.entities import Entity, EntityType, Relationship, RelationshipType
from sibyl_core.services import content_client
from sibyl_core.services.source_archive_store import (
    export_source_integrity,
    read_source_archive_snapshot,
)
from sibyl_core.services.surreal_content import remember_raw_memory
from sibyl_core.tools.admin import capture_graph_backup, create_backup
from tests.test_graph_derivation_publication import publish, share_plan
from tests.test_synthesis_source_observations import content_store as content_store
from tests.test_synthesis_source_observations import (
    disable_embeddings_and_bind_runtime as disable_embeddings_and_bind_runtime,
)
from tests.test_synthesis_source_observations import runtime as runtime

STAMP = "2026-09-10T02:47:11.572211763Z"
_RUN_STAMP = re.compile(r'^  "created_at": "[^"]*",$', re.MULTILINE)


def _same_run(text: str) -> str:
    """Blank the top-level capture time, the one field two runs never share."""
    return _RUN_STAMP.sub('  "created_at": "<run>",', text)


def test_streamed_json_matches_indented_dumps() -> None:
    nested = [{"x": [1, 2.5, {"y": "é\nz"}], "z": None, "w": []}, [], [{}], "tail"]
    value = {
        "empty_list": [],
        "empty_object": {},
        "nested": nested,
        "when": datetime(2026, 9, 10, 2, 47, 11, 572211, tzinfo=UTC),
        "later": "computed",
        "lists": [[], [{"k": [[]]}]],
    }
    streamed = StreamedObject(
        [
            ("empty_list", Streamed(iter([]))),
            ("empty_object", StreamedObject([])),
            ("nested", Streamed(iter(nested))),
            ("when", value["when"]),
            ("later", Deferred(lambda: "computed")),
            ("lists", Streamed(iter([Streamed(iter([])), [{"k": [[]]}]]))),
        ]
    )
    buffer = io.StringIO()
    write_json(buffer, streamed)
    assert buffer.getvalue() == json.dumps(value, indent=2, default=str)


def _raw_row(identity: str, revision: int) -> dict:
    return {
        "organization_id": "org-a",
        "uuid": identity,
        "raw_content": f"content {identity}",
        "revision": revision,
        "captured_at": ArchiveDatetime.parse(STAMP),
        "metadata": {"nested": [ArchiveDatetime.parse("2026-01-01T00:00:00Z"), "literal"]},
        "archive_record_key": f"raw_captures:{identity}",
    }


def _state(identity: str, revision: int, *, deleted: bool = False) -> dict:
    return {
        "organization_id": "org-a",
        "source_kind": "raw_capture",
        "source_id": identity,
        "generation": 1,
        "revision": revision,
        "deleted": deleted,
        "incarnation": f"incarnation-{identity}",
    }


def test_section_digest_and_row_validator_match_the_assembled_section(tmp_path) -> None:
    rows = [_raw_row("b", 2), _raw_row("a", 1)]
    states = [_state("a", 1), _state("b", 2), _state("gone", 4, deleted=True)]
    section = build_integrity_archive(
        kind=SourceKind.RAW_CAPTURE,
        organizations=["org-a"],
        source_rows=rows,
        source_states=states,
        derivations=[],
    )
    digest = SectionDigest(kind=SourceKind.RAW_CAPTURE, organizations=["org-a"], derivations=[])
    for row in section["source_rows"]:
        digest.add_row(row)
    assert digest.finish(states) == section["sha256"]

    spool = ArchiveSpool(spool_path(tmp_path, "unit"))
    capture = SourceCapture(SourceKind.RAW_CAPTURE, "org-a", spool)
    capture.begin(list(reversed(states)), [])
    capture.add_rows(rows)
    capture.finish()
    buffer = io.StringIO()
    write_json(buffer, capture.section(set()))
    spool.close()
    ordered = build_integrity_archive(
        kind=SourceKind.RAW_CAPTURE,
        organizations=["org-a"],
        source_rows=sorted(rows, key=lambda row: row["uuid"]),
        source_states=states,
        derivations=[],
    )
    assert buffer.getvalue() == json.dumps(ordered, indent=2, default=str)


@pytest.mark.parametrize(
    ("states", "rows", "message"),
    [
        ([_state("a", 1)], [_raw_row("b", 1)], "archive source has no retained ledger"),
        ([_state("a", 1)], [_raw_row("a", 3)], "revision does not match"),
        ([_state("a", 1), _state("b", 1)], [_raw_row("a", 1)], "live ledger has no source row"),
        ([_state("a", 1)], [_raw_row("a", 1), _raw_row("a", 1)], "duplicate archive source"),
    ],
)
def test_row_validator_rejects_what_the_section_validator_rejects(states, rows, message) -> None:
    with pytest.raises(ValueError, match=message):
        build_integrity_archive(
            kind=SourceKind.RAW_CAPTURE,
            organizations=["org-a"],
            source_rows=rows,
            source_states=states,
            derivations=[],
        )
    validator = IntegrityRowValidator(
        kind=SourceKind.RAW_CAPTURE, organizations=["org-a"], states=states, derivations=[]
    )
    with pytest.raises(ValueError, match=message):
        for row in rows:
            validator.check(row)
        validator.finish()


async def _seed_graph(runtime) -> str:
    """A published derivation, extra pages of entities, and precise companions."""
    org = runtime.client.group_id
    source = await remember_raw_memory(
        organization_id=org,
        principal_id="user_a",
        source_id="stream-archive",
        raw_content="Approved deployment rule",
        embedding_provider=None,
    )
    published = await publish(runtime, await share_plan(runtime, source.id))
    assert published.success
    await runtime.entity_manager.create_direct_bulk(
        [
            Entity(
                id=f"stream-{index}",
                entity_type=EntityType.TOPIC,
                name=f"Stream entity {index}",
                content=f"Body {index}",
            )
            for index in range(5)
        ]
    )
    await runtime.relationship_manager.create_direct_bulk(
        [
            Relationship(
                id=f"stream-edge-{index}",
                source_id=f"stream-{index}",
                target_id=f"stream-{index + 1}",
                relationship_type=RelationshipType.RELATED_TO,
            )
            for index in range(4)
        ]
    )
    # Two edges share a created_at so the uuid tiebreak decides their order.
    await runtime.client.execute_query(
        "UPDATE relates_to SET created_at=d'2026-09-10T02:47:11.572211763Z', "
        "attributes.nested={at: d'2026-09-10T02:47:11.572211763Z', literal: $stamp} "
        "WHERE uuid IN ['stream-edge-1', 'stream-edge-2'];",
        stamp=STAMP,
    )
    for identity in ("stream-episode-a", "stream-episode-b"):
        await save_native_episode(
            runtime.client,
            episode_from_payload(
                {
                    "uuid": identity,
                    "name": identity,
                    "content": "Evidence",
                    "created_at": STAMP,
                    "valid_at": STAMP,
                },
                organization_id=org,
            ),
        )
    await runtime.client.execute_query(
        "LET $src=(SELECT VALUE id FROM episode WHERE uuid='stream-episode-a')[0]; "
        "LET $tgt=(SELECT VALUE id FROM entity WHERE uuid='stream-0')[0]; "
        "RELATE $src->mentions:stream->$tgt SET uuid='stream-mention', group_id=$org, "
        "created_at=d'2026-09-10T02:47:11.572211763Z';",
        org=org,
    )
    return source.id


@pytest.fixture(params=["one_statement", "paged"])
def read_mode(request, monkeypatch) -> str:
    """Read through the embedded fallback, or page through the reader two rows at a time.

    The embedded engine has no client-side transactions, so the paged mode
    hands the reader the client's own executor; a live server runs the same
    statements inside one read transaction.
    """
    if request.param == "paged":

        @asynccontextmanager
        async def pages_without_transaction(self):
            yield self.execute_query

        monkeypatch.setattr(DedicatedSurrealClient, "read_snapshot", pages_without_transaction)
        monkeypatch.setattr(archive_stream, "ARCHIVE_PAGE_SIZE", 2)
    return request.param


async def _raw_capture(client, org: str, read_mode: str, tmp_path) -> SourceCapture:
    capture = SourceCapture(SourceKind.RAW_CAPTURE, org, ArchiveSpool(spool_path(tmp_path, "raw")))
    if read_mode == "paged":
        await read_source_capture(capture, PageReader(client.execute_query))
    else:
        capture_snapshot_rows(
            capture,
            await read_source_archive_snapshot(
                client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=[org]
            ),
        )
    capture.finish()
    return capture


async def _old_graph_json(org: str, content: dict | None) -> tuple[str, list]:
    """graph.json exactly as the in-memory backup assembled it."""
    result = await create_backup(organization_id=org)
    assert result.success, result.message
    graph = json.loads(json.dumps(asdict(result.backup_data), indent=2, default=str))
    sealed, _, report = seal_archive_lineage(graph, content)
    return json.dumps(sealed, indent=2, default=str), report


async def _new_graph_json(org: str, raw: SourceCapture | None, tmp_path) -> tuple[str, list]:
    capture = await capture_graph_backup(organization_id=org, workdir=tmp_path)
    try:
        sections = [capture.lineage, *([raw.lineage] if raw is not None else [])]
        report = seal_lineage(sections, capture.quarantined)
        path = tmp_path / "graph.json"
        capture.write(path, report)
    finally:
        capture.close()
    return path.read_text(), report


@pytest.mark.parametrize("content", ["absent", "matching", "drifted"])
async def test_graph_capture_writes_the_in_memory_graph_json(
    runtime, content_store, read_mode, content, monkeypatch, tmp_path
) -> None:
    del content_store
    org = runtime.client.group_id
    monkeypatch.setattr("sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=runtime))
    source_id = await _seed_graph(runtime)
    raw = None
    old_content = None
    if content != "absent":
        async with content_client.surreal_content_client() as client:
            if content == "drifted":
                await client.execute_query(
                    "UPDATE raw_captures SET raw_content='Revoked rule', revision+=1 "
                    "WHERE uuid=$uuid;",
                    uuid=source_id,
                )
            old_content = {
                "version": "2.3",
                "source_integrity": await export_source_integrity(
                    client.execute_query, kind=SourceKind.RAW_CAPTURE, organizations=[org]
                ),
            }
            raw = await _raw_capture(client, org, read_mode, tmp_path)

    old, old_report = await _old_graph_json(org, old_content)
    try:
        new, new_report = await _new_graph_json(org, raw, tmp_path)
    finally:
        if raw is not None:
            raw.close()

    assert new_report == old_report
    assert _same_run(new) == _same_run(old)
    statuses = {row["status"] for row in new_report}
    expected = {"absent": {"unresolved"}, "matching": set(), "drifted": {"quarantined"}}
    assert statuses == expected[content]
    document = json.loads(new)
    if read_mode == "paged":
        assert len(document["entities"]) > 2 * archive_stream.ARCHIVE_PAGE_SIZE
    assert document["mentions"][0]["created_at"] == STAMP
    assert len(document["episodes"]) == 2


async def test_graph_capture_spool_leaves_no_file_behind(
    runtime, content_store, monkeypatch, tmp_path
) -> None:
    del content_store
    monkeypatch.setattr("sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=runtime))
    capture = await capture_graph_backup(organization_id=runtime.client.group_id, workdir=tmp_path)
    capture.write(tmp_path / "graph.json", seal_lineage([capture.lineage], capture.quarantined))
    capture.close()
    assert [path.name for path in tmp_path.iterdir()] == ["graph.json"]


@pytest.mark.parametrize(
    ("url", "scheme"), [("memory://", "memory"), ("http://127.0.0.1:9/rpc", "http")]
)
async def test_snapshot_reads_off_websocket_have_no_transaction(url, scheme) -> None:
    client = DedicatedSurrealClient(url=url, namespace="snapshot", database="snapshot")
    try:
        async with client.read_snapshot() as execute:
            assert execute is None
        assert client.url_scheme == scheme
    finally:
        await client.close()


async def test_graph_capture_names_the_scheme_it_read_unpaged(
    runtime, content_store, monkeypatch, tmp_path
) -> None:
    del content_store
    monkeypatch.setattr("sibyl_core.tools.admin.get_graph_runtime", AsyncMock(return_value=runtime))
    capture = await capture_graph_backup(organization_id=runtime.client.group_id, workdir=tmp_path)
    capture.close()
    assert capture.unpaged_scheme == "memory"
