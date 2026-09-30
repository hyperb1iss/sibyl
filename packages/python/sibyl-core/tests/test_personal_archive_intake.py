from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive import build_manifest, write_archive
from sibyl_core.migrate.personal_archive_intake import (
    ArchiveIntakeBudget,
    ArchiveIntakeCapacityError,
    ArchiveIntakeError,
    parse_personal_archive,
)
from sibyl_core.migrate.source_integrity import build_integrity_archive


@pytest.fixture
def budget():
    # Explicit small fixture resources are not deployment defaults.
    return ArchiveIntakeBudget(
        compressed_bytes=1_000_000,
        inflated_bytes=1_000_000,
        member_bytes=500_000,
        members=16,
        json_depth=30,
        json_scalar_bytes=100_000,
        json_nodes=50_000,
        parsed_rows=10_000,
        encoded_artifact_bytes=1_000_000,
        encoded_plan_bytes=1_000_000,
        metadata_transaction_bytes=2_000_000,
    )


def _graph(org: str) -> dict[str, object]:
    return {
        "version": "2.0",
        "created_at": "2026-09-30T00:00:00+00:00",
        "organization_id": org,
        "entity_count": 1,
        "relationship_count": 0,
        "entities": [{"id": "foreign-entity", "entity_type": "topic", "name": "Synthetic"}],
        "relationships": [],
    }


def _archive(tmp_path: Path, files: dict[str, bytes], org: str) -> Path:
    path = tmp_path / "upload.spool"
    manifest = build_manifest(organization_id=org, source_store="surreal", files=files)
    write_archive(path, manifest=manifest, files=files)
    return path


def _manual_archive(tmp_path: Path, entries: list[tuple[tarfile.TarInfo, bytes]]) -> Path:
    path = tmp_path / "manual.spool"
    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
        for info, encoded in entries:
            tar.addfile(info, io.BytesIO(encoded))
    return path


def _info(name: str, data: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info


def test_personal_archive_staging_accepts_owned_writer_and_measures_actual_bytes(tmp_path, budget):
    org = str(uuid4())
    encoded = json.dumps(_graph(org)).encode()
    path = _archive(tmp_path, {"graph.json": encoded}, org)
    parsed = parse_personal_archive(path, budget)
    inventory = json.loads(parsed.member_inventory_json)
    measurements = json.loads(parsed.measured_sizes_json)
    assert parsed.archive_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert inventory["graph.json"]["sha256"] == hashlib.sha256(encoded).hexdigest()
    assert inventory["graph.json"]["size_bytes"] == len(encoded)
    assert measurements["compressed_bytes"] == path.stat().st_size
    assert measurements["physical_tar_headers"] > measurements["logical_members"]
    assert parsed.graph == _graph(org)
    assert parsed.content is None
    assert parsed.archive.files["graph.json"] == encoded
    assert json.loads(parsed.staged_payload_json).keys() == inventory.keys()


def test_personal_archive_staging_accepts_auth_skipped_api_backup_envelope(tmp_path, budget):
    org = str(uuid4())
    graph = json.dumps(_graph(org)).encode()
    metadata = json.dumps(
        {
            "version": "2.0",
            "created_at": "2026-09-30T00:00:00+00:00",
            "organization_id": org,
            "hostname": "untrusted-declared-host",
            "database_dump_tables": 0,
            "graph_entities": 1,
            "graph_relationships": 0,
            "files": {"graph.json": hashlib.sha256(graph).hexdigest()},
        }
    ).encode()
    path = _manual_archive(
        tmp_path,
        [(_info("metadata.json", metadata), metadata), (_info("graph.json", graph), graph)],
    )
    parsed = parse_personal_archive(path, budget)
    assert parsed.origin.organization_id == org
    assert parsed.graph == _graph(org)


@pytest.mark.parametrize("name", ["auth.json", "../graph.json", "/graph.json", "unknown.json"])
def test_personal_archive_staging_rejects_auth_unsafe_and_unknown_members(tmp_path, budget, name):
    data = b"{}"
    path = _manual_archive(tmp_path, [(_info(name, data), data)])
    with pytest.raises(ArchiveIntakeError, match="unsafe or unsupported"):
        parse_personal_archive(path, budget)


def test_personal_archive_staging_rejects_symlink_and_duplicate_members(tmp_path, budget):
    symlink = tarfile.TarInfo("graph.json")
    symlink.type, symlink.linkname = tarfile.SYMTYPE, "/etc/passwd"
    path = _manual_archive(tmp_path, [(symlink, b"")])
    with pytest.raises(ArchiveIntakeError, match="unsafe or unsupported"):
        parse_personal_archive(path, budget)
    data = b"{}"
    path = _manual_archive(
        tmp_path, [(_info("graph.json", data), data), (_info("graph.json", data), data)]
    )
    with pytest.raises(ArchiveIntakeError, match="unsafe or unsupported"):
        parse_personal_archive(path, budget)


def test_personal_archive_staging_rejects_pax_size_override_before_logical_decode(tmp_path, budget):
    data = b"999999999 size=9999999999999999999\n"
    info = _info("pax", data)
    info.type = tarfile.XHDTYPE
    path = _manual_archive(tmp_path, [(info, data)])
    with pytest.raises(ArchiveIntakeError, match="PAX"):
        parse_personal_archive(path, budget)


@pytest.mark.parametrize("payload", [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'])
def test_personal_archive_staging_rejects_ambiguous_and_nonfinite_json(tmp_path, budget, payload):
    path = _manual_archive(tmp_path, [(_info("graph.json", payload), payload)])
    with pytest.raises(ArchiveIntakeError):
        parse_personal_archive(path, budget)


@pytest.mark.parametrize(
    ("resource", "maximum", "message"),
    [
        ("compressed_bytes", 1, "compressed-byte"),
        ("inflated_bytes", 1, "inflated-byte"),
        ("member_bytes", 1, "member resource"),
        ("members", 1, "member resource"),
        ("json_depth", 1, "nesting"),
        ("json_scalar_bytes", 1, "scalar-byte"),
        ("json_nodes", 1, "node"),
        ("encoded_artifact_bytes", 1, "encoded-artifact"),
    ],
)
def test_personal_archive_staging_enforces_measured_resource_bounds(
    tmp_path, budget, resource, maximum, message
):
    org = str(uuid4())
    path = _archive(tmp_path, {"graph.json": json.dumps(_graph(org)).encode()}, org)
    with pytest.raises(ArchiveIntakeCapacityError, match=message):
        parse_personal_archive(path, replace(budget, **{resource: maximum}))


@pytest.mark.parametrize(
    ("resource", "encoded"),
    [
        ("json_depth", b'{"a":[[[]]]}'),
        ("json_nodes", b'{"a":[1,2,3,4]}'),
        ("json_scalar_bytes", b'{"a":"long-value"}'),
        ("json_scalar_bytes", b'{"a":1234567890}'),
    ],
)
def test_personal_archive_staging_bounds_json_before_eager_parse(
    tmp_path, budget, monkeypatch, resource, encoded
):
    path = _manual_archive(tmp_path, [(_info("graph.json", encoded), encoded)])
    calls = 0

    def forbidden_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("eager JSON parse ran before resource admission")

    monkeypatch.setattr("sibyl_core.migrate.personal_archive_intake.json.loads", forbidden_parse)
    with pytest.raises(ArchiveIntakeCapacityError):
        parse_personal_archive(path, replace(budget, **{resource: 1}))
    assert calls == 0


@pytest.mark.parametrize(
    "mutation", ["unknown-table", "wrong-version", "boolean-count", "wrong-org"]
)
def test_personal_archive_staging_rejects_unsupported_logical_contract(tmp_path, budget, mutation):
    org = str(uuid4())
    content: dict[str, object] = {
        "version": "1.0",
        "organization_id": org,
        "tables": {"raw_captures": []},
        "row_counts": {"raw_captures": 0},
        "total_rows": 0,
    }
    if mutation == "unknown-table":
        content["tables"] = {"unexpected_active_table": []}
    elif mutation == "wrong-version":
        content["version"] = "999.0"
    elif mutation == "boolean-count":
        content["total_rows"] = False
    else:
        content["organization_id"] = str(uuid4())
    path = _archive(tmp_path, {"content.json": json.dumps(content).encode()}, org)
    with pytest.raises(ArchiveIntakeError):
        parse_personal_archive(path, budget)


def test_personal_archive_staging_counts_foreign_ledgers_and_preserves_protection(tmp_path, budget):
    org = str(uuid4())
    source_id = str(uuid4())
    content = {
        "version": "2.2",
        "organization_id": org,
        "tables": {"raw_captures": []},
        "row_counts": {"raw_captures": 0},
        "total_rows": 0,
        "source_integrity": build_integrity_archive(
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org],
            source_rows=[],
            source_states=[
                {
                    "organization_id": org,
                    "source_kind": "raw_capture",
                    "source_id": source_id,
                    "generation": 1,
                    "revision": 0,
                    "deleted": True,
                    "incarnation": str(uuid4()),
                }
            ],
            derivations=[],
        ),
    }
    path = _archive(tmp_path, {"content.json": json.dumps(content).encode()}, org)
    # The only row is a retired foreign ledger, not a live raw capture.
    parsed = parse_personal_archive(path, replace(budget, parsed_rows=1))
    assert json.loads(parsed.measured_sizes_json)["parsed_rows"] == 1
    assert parsed.content["source_integrity"]["source_states"][0]["deleted"] is True
    content["source_integrity"] = build_integrity_archive(
        kind=SourceKind.RAW_CAPTURE,
        organizations=[org],
        source_rows=[],
        source_states=content["source_integrity"]["source_states"] * 1
        + [
            {
                "organization_id": org,
                "source_kind": "raw_capture",
                "source_id": str(uuid4()),
                "generation": 1,
                "revision": 0,
                "deleted": True,
                "incarnation": str(uuid4()),
            }
        ],
        derivations=[],
    )
    path = _archive(tmp_path, {"content.json": json.dumps(content).encode()}, org)
    with pytest.raises(ArchiveIntakeCapacityError, match="parsed-row"):
        parse_personal_archive(path, replace(budget, parsed_rows=1))


def test_personal_archive_staging_rejects_truncated_gzip_and_non_tar_tail(tmp_path, budget):
    org = str(uuid4())
    path = _archive(tmp_path, {"graph.json": json.dumps(_graph(org)).encode()}, org)
    original = path.read_bytes()
    path.write_bytes(original[:-5])
    with pytest.raises(ArchiveIntakeError, match="truncated"):
        parse_personal_archive(path, budget)
    path.write_bytes(gzip.compress(gzip.decompress(original) + b"unexpected-tail"))
    with pytest.raises(ArchiveIntakeError, match="trailing"):
        parse_personal_archive(path, budget)


def test_personal_archive_staging_admits_current_empty_content_receipt_contract(tmp_path, budget):
    org = str(uuid4())
    content = {
        "version": "2.3",
        "organization_id": org,
        "tables": {"raw_captures": [], "memory_validation_executions": []},
        "row_counts": {"raw_captures": 0, "memory_validation_executions": 0},
        "total_rows": 0,
        "source_integrity": build_integrity_archive(
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org],
            source_rows=[],
            source_states=[],
            derivations=[],
        ),
        "validation_receipts": {"version": "1.0", "executions": []},
    }
    path = _archive(tmp_path, {"content.json": json.dumps(content).encode()}, org)
    parsed = parse_personal_archive(path, budget)
    assert parsed.content["validation_receipts"] == content["validation_receipts"]
    assert json.loads(parsed.measured_sizes_json)["parsed_rows"] == 0


def test_personal_archive_staging_bounds_embedded_request_before_provenance_validation(
    tmp_path, budget, monkeypatch
):
    org = str(uuid4())
    content = {
        "version": "1.0",
        "organization_id": org,
        "tables": {
            "memory_validation_executions": [
                {"uuid": str(uuid4()), "request_json": '{"nested":[[[[[[[[]]]]]]]]}'}
            ]
        },
    }
    path = _archive(tmp_path, {"content.json": json.dumps(content).encode()}, org)
    calls = 0

    def forbidden_validation(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("provenance decoder ran before embedded resource admission")

    monkeypatch.setattr(
        "sibyl_core.migrate.personal_archive_intake.validate_archive", forbidden_validation
    )
    with pytest.raises(ArchiveIntakeCapacityError, match="nesting"):
        parse_personal_archive(path, replace(budget, json_depth=6))
    assert calls == 0


def test_personal_archive_staging_bounds_encoded_artifact_before_base64_expansion(
    tmp_path, budget, monkeypatch
):
    org = str(uuid4())
    path = _archive(tmp_path, {"graph.json": json.dumps(_graph(org)).encode()}, org)
    calls = 0

    def forbidden_encoding(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("base64 expansion ran before artifact resource admission")

    monkeypatch.setattr(
        "sibyl_core.migrate.personal_archive_intake.base64.b64encode", forbidden_encoding
    )
    with pytest.raises(ArchiveIntakeCapacityError, match="encoded-artifact"):
        parse_personal_archive(path, replace(budget, encoded_artifact_bytes=1))
    assert calls == 0
