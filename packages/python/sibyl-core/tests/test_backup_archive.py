"""API backup envelopes feed the existing archive restore contract unchanged."""

import hashlib
import io
import json
import tarfile
from uuid import uuid4

import pytest

from sibyl_core.migrate.archive import load_archive, validate_archive


def backup_parts():
    org = str(uuid4())
    content = json.dumps({"version": "1.0", "organization_id": org, "tables": {}}).encode()
    metadata = {
        "version": "2.0",
        "created_at": "2026-09-10T00:00:00+00:00",
        "organization_id": org,
        "hostname": "owned",
        "database_dump_tables": 0,
        "graph_entities": 0,
        "graph_relationships": 0,
        "files": {"content.json": hashlib.sha256(content).hexdigest()},
        "lineage_validation": [],
    }
    return metadata, {"content.json": content}


def write_bundle(path, metadata, files, *, duplicate=False):
    with tarfile.open(path, "w:gz") as archive:
        for name, data in [("metadata.json", json.dumps(metadata).encode()), *files.items()]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            if duplicate and name == "metadata.json":
                archive.addfile(member, io.BytesIO(data))


def test_backup_envelope_retains_payload_bytes(tmp_path):
    metadata, files = backup_parts()
    path = tmp_path / "backup.tar.gz"
    write_bundle(path, metadata, files)
    archive = load_archive(path)
    assert archive.files == files
    assert archive.manifest.organization_id == metadata["organization_id"]
    assert archive.manifest.metadata["backup_metadata"] == metadata
    assert validate_archive(archive) == []


@pytest.mark.parametrize(
    "mutation",
    [
        "checksum",
        "inventory",
        "version",
        "scope",
        "count",
        "ambiguous",
        "duplicate",
        "global",
        "unknown",
        "row_scope",
        "naive_time",
        "malformed_payload",
    ],
)
def test_backup_envelope_rejects_invalid_contract(tmp_path, mutation):
    metadata, files = backup_parts()
    if mutation == "checksum":
        metadata["files"]["content.json"] = "0" * 64
    elif mutation == "inventory":
        files["extra.json"] = b"{}"
    elif mutation == "version":
        metadata["version"] = "99.0"
    elif mutation == "scope":
        metadata["organization_id"] = str(uuid4())
    elif mutation == "count":
        metadata["graph_entities"] = True
    elif mutation == "ambiguous":
        files["manifest.json"] = b"{}"
    elif mutation == "global":
        metadata["organization_id"] = None
    elif mutation == "unknown":
        metadata["unexpected"] = "not accepted"
    elif mutation == "naive_time":
        metadata["created_at"] = "2026-09-10"
    elif mutation in {"row_scope", "malformed_payload"}:
        payload = json.loads(files["content.json"])
        if mutation == "row_scope":
            payload["tables"] = {"raw_captures": [{"organization_id": str(uuid4())}]}
        else:
            payload = []
        files["content.json"] = json.dumps(payload).encode()
        metadata["files"]["content.json"] = hashlib.sha256(files["content.json"]).hexdigest()
    path = tmp_path / "backup.tar.gz"
    write_bundle(path, metadata, files, duplicate=mutation == "duplicate")
    with pytest.raises((ValueError, TypeError)):
        load_archive(path)


@pytest.mark.parametrize("member", ["metadata.json", "content.json"])
def test_backup_duplicate_json_keys_rejected(tmp_path, member):
    metadata, files = backup_parts()
    path = tmp_path / "backup.tar.gz"
    write_bundle(path, metadata, files)
    with tarfile.open(path) as archive:
        data = {item.name: archive.extractfile(item).read() for item in archive.getmembers()}
    data[member] = data[member].replace(b"{", b'{"version":"2.0",', 1)
    if member == "content.json":
        metadata["files"][member] = hashlib.sha256(data[member]).hexdigest()
        data["metadata.json"] = json.dumps(metadata).encode()
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in data.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(payload)
            archive.addfile(entry, io.BytesIO(payload))
    with pytest.raises(ValueError, match="duplicate keys"):
        load_archive(path)
