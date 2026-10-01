from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from uuid import uuid4

import pytest

from sibyl_core.migrate.personal_archive_artifact import (
    ArchiveArtifactIntegrityError,
    rehydrate_personal_archive,
)
from sibyl_core.migrate.personal_archive_intake import parse_personal_archive
from sibyl_core.migrate.personal_archive_plan import archive_digest, canonical_json
from tests.test_personal_archive_intake import _archive, _graph
from tests.test_personal_archive_intake import budget as budget


def _saved(parsed):
    return {
        "archive_sha256": parsed.archive_sha256,
        "artifact_sha256": parsed.artifact_sha256,
        "member_inventory_json": parsed.member_inventory_json,
        "staged_payload_json": parsed.staged_payload_json,
        "measured_sizes_json": parsed.measured_sizes_json,
    }


def _refresh_members(saved, members):
    inventory = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
        for name, data in members.items()
    }
    saved["member_inventory_json"] = canonical_json(inventory)
    saved["artifact_sha256"] = archive_digest("sibyl-archive-artifact-v1", inventory)
    saved["staged_payload_json"] = canonical_json(
        {name: base64.b64encode(data).decode("ascii") for name, data in members.items()}
    )
    sizes = json.loads(saved["measured_sizes_json"])
    sizes["encoded_artifact_bytes"] = len(saved["staged_payload_json"])
    sizes["logical_members"] = len(members)
    saved["measured_sizes_json"] = canonical_json(sizes)


@pytest.fixture
def admitted(tmp_path, budget):
    org = str(uuid4())
    graph = json.dumps(_graph(org)).encode()
    # Deterministically retain padding for the canonical base64-bit control.
    if len(graph) % 3 == 0:
        graph += b" "
    source = _archive(tmp_path, {"graph.json": graph}, org)
    return parse_personal_archive(source, budget)


def test_saved_members_roundtrip_and_snapshots_are_independent(admitted):
    artifact = rehydrate_personal_archive(**_saved(admitted))
    assert artifact.original_budget == json_budget(admitted)
    first, second = artifact.materialize(), artifact.materialize()
    assert first.graph == second.graph == admitted.graph
    assert first.archive.files == admitted.archive.files
    first.graph["entities"][0]["name"] = "Changed snapshot"
    first.archive.files.clear()
    first.archive.manifest.files.clear()
    assert second.graph == admitted.graph
    assert artifact.materialize().graph == admitted.graph
    assert artifact.members == tuple(sorted(artifact.members))
    with pytest.raises(FrozenInstanceError):
        artifact.graph_json = "{}"


def json_budget(admitted):
    from sibyl_core.migrate.personal_archive_intake import ArchiveIntakeBudget

    return ArchiveIntakeBudget(**json.loads(admitted.measured_sizes_json)["resource_budget"])


def test_rehydration_uses_original_admission_without_a_current_budget(admitted):
    original = json_budget(admitted)
    # A future application's smaller configuration cannot replace this ceiling.
    smaller_current = replace(original, member_bytes=1, encoded_artifact_bytes=1)
    assert smaller_current.member_bytes < len(admitted.archive.files["graph.json"])
    artifact = rehydrate_personal_archive(**_saved(admitted))
    assert artifact.original_budget == original
    assert artifact.materialize().graph == admitted.graph


@pytest.mark.parametrize("field", ["archive_sha256", "artifact_sha256"])
def test_saved_artifact_rejects_invalid_digest_shape(admitted, field):
    saved = _saved(admitted)
    saved[field] = "F" * 64
    with pytest.raises(ArchiveArtifactIntegrityError):
        rehydrate_personal_archive(**saved)


@pytest.mark.parametrize(
    "field", ["staged_payload_json", "member_inventory_json", "measured_sizes_json"]
)
def test_saved_metadata_must_be_canonical_duplicate_free_finite_json(admitted, field):
    saved = _saved(admitted)
    saved[field] = " " + saved[field]
    with pytest.raises(ArchiveArtifactIntegrityError):
        rehydrate_personal_archive(**saved)
    saved = _saved(admitted)
    saved[field] = '{"x":1,"x":2}'
    with pytest.raises(ArchiveArtifactIntegrityError):
        rehydrate_personal_archive(**saved)
    saved[field] = '{"x":NaN}'
    with pytest.raises(ArchiveArtifactIntegrityError):
        rehydrate_personal_archive(**saved)


@pytest.mark.parametrize("mutation", ["bytes", "length", "missing", "extra", "base64"])
def test_saved_artifact_rejects_staging_corruption(admitted, mutation):
    saved = _saved(admitted)
    staged = json.loads(saved["staged_payload_json"])
    inventory = json.loads(saved["member_inventory_json"])
    if mutation == "bytes":
        encoded = staged["graph.json"]
        staged["graph.json"] = ("B" if encoded[0] != "B" else "A") + encoded[1:]
    elif mutation == "length":
        inventory["graph.json"]["size_bytes"] += 1
        saved["member_inventory_json"] = canonical_json(inventory)
        saved["artifact_sha256"] = archive_digest("sibyl-archive-artifact-v1", inventory)
    elif mutation == "missing":
        staged.pop("graph.json")
    elif mutation == "extra":
        staged["auth.json"] = "e30="
    else:
        staged["graph.json"] = "!" + staged["graph.json"][1:]
    saved["staged_payload_json"] = canonical_json(staged)
    with pytest.raises(ArchiveArtifactIntegrityError):
        rehydrate_personal_archive(**saved)


def test_saved_base64_rejects_noncanonical_padding_bits(admitted):
    saved = _saved(admitted)
    staged = json.loads(saved["staged_payload_json"])
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    for name, encoded in staged.items():
        if encoded.endswith("="):
            index = len(encoded.rstrip("=")) - 1
            replacement = alphabet[alphabet.index(encoded[index]) | 1]
            altered = encoded[:index] + replacement + encoded[index + 1 :]
            assert base64.b64decode(altered, validate=True) == base64.b64decode(encoded)
            staged[name] = altered
            break
    else:
        pytest.fail("fixture requires one padded member")
    saved["staged_payload_json"] = canonical_json(staged)
    with pytest.raises(ArchiveArtifactIntegrityError, match="base64"):
        rehydrate_personal_archive(**saved)


@pytest.mark.parametrize(
    "resource", ["member_bytes", "encoded_artifact_bytes", "json_nodes", "parsed_rows"]
)
def test_original_resource_bounds_reject_before_base64_expansion(admitted, resource, monkeypatch):
    saved = _saved(admitted)
    sizes = json.loads(saved["measured_sizes_json"])
    sizes["resource_budget"][resource] = 1
    if resource == "parsed_rows":
        sizes["parsed_rows"] = 2
    saved["measured_sizes_json"] = canonical_json(sizes)
    calls = 0

    def forbidden(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("base64 decoded before original resource admission")

    monkeypatch.setattr("sibyl_core.migrate.personal_archive_artifact.base64.b64decode", forbidden)
    with pytest.raises(ArchiveArtifactIntegrityError):
        rehydrate_personal_archive(**saved)
    assert calls == 0


@pytest.mark.parametrize("measurement", ["logical_members", "json_nodes", "parsed_rows"])
def test_saved_derivable_measurements_are_recomputed(admitted, measurement):
    saved = _saved(admitted)
    sizes = json.loads(saved["measured_sizes_json"])
    sizes[measurement] += 1
    saved["measured_sizes_json"] = canonical_json(sizes)
    with pytest.raises(ArchiveArtifactIntegrityError, match="measurements"):
        rehydrate_personal_archive(**saved)


@pytest.mark.parametrize(
    "mutation", ["unknown-version", "duplicate-json", "unknown-field", "foreign-org", "auth-member"]
)
def test_rehydration_reuses_strict_logical_contract_even_with_consistent_hashes(admitted, mutation):
    saved = _saved(admitted)
    members = {
        name: base64.b64decode(value)
        for name, value in json.loads(saved["staged_payload_json"]).items()
    }
    graph = json.loads(members["graph.json"])
    if mutation == "unknown-version":
        graph["version"] = "999.0"
    elif mutation == "unknown-field":
        graph["active_authority"] = {"allow": True}
    elif mutation == "foreign-org":
        graph["organization_id"] = str(uuid4())
    elif mutation == "auth-member":
        members["auth.json"] = b"{}"
    if mutation == "duplicate-json":
        members["graph.json"] = b'{"version":"2.0","version":"2.0"}'
    else:
        members["graph.json"] = canonical_json(graph).encode()
    manifest = json.loads(members["manifest.json"])
    manifest["files"]["graph.json"]["sha256"] = hashlib.sha256(members["graph.json"]).hexdigest()
    manifest["files"]["graph.json"]["size_bytes"] = len(members["graph.json"])
    members["manifest.json"] = canonical_json(manifest).encode()
    _refresh_members(saved, members)
    with pytest.raises(ArchiveArtifactIntegrityError):
        rehydrate_personal_archive(**saved)


def test_rehydration_retains_foreign_deleted_source_history_inert(tmp_path, budget):
    from sibyl_core.memory_pipeline.observations import SourceKind
    from sibyl_core.migrate.source_integrity import build_integrity_archive

    org, identity = str(uuid4()), str(uuid4())
    content = {
        "version": "2.2",
        "organization_id": org,
        "tables": {"raw_captures": []},
        "source_integrity": build_integrity_archive(
            kind=SourceKind.RAW_CAPTURE,
            organizations=[org],
            source_rows=[],
            source_states=[
                {
                    "organization_id": org,
                    "source_kind": "raw_capture",
                    "source_id": identity,
                    "generation": 3,
                    "revision": 2,
                    "deleted": True,
                    "incarnation": str(uuid4()),
                }
            ],
            derivations=[],
        ),
    }
    source = _archive(tmp_path, {"content.json": json.dumps(content).encode()}, org)
    parsed = parse_personal_archive(source, budget)
    artifact = rehydrate_personal_archive(**_saved(parsed))
    restored = artifact.materialize()
    assert restored.content == parsed.content
    state = restored.content["source_integrity"]["source_states"][0]
    assert state["deleted"] is True
    assert state["generation"] == 3
    assert state["revision"] == 2
    assert restored.content["tables"]["raw_captures"] == []


def test_rehydration_accepts_the_original_auth_skipped_backup_envelope(tmp_path, budget):
    from tests.test_personal_archive_intake import _info, _manual_archive

    org = str(uuid4())
    graph = json.dumps(_graph(org)).encode()
    metadata = json.dumps(
        {
            "version": "2.0",
            "created_at": "2026-09-30T00:00:00+00:00",
            "organization_id": org,
            "hostname": "foreign-declared-host",
            "database_dump_tables": 0,
            "graph_entities": 1,
            "graph_relationships": 0,
            "files": {"graph.json": hashlib.sha256(graph).hexdigest()},
        }
    ).encode()
    source = _manual_archive(
        tmp_path,
        [(_info("metadata.json", metadata), metadata), (_info("graph.json", graph), graph)],
    )
    parsed = parse_personal_archive(source, budget)
    restored = rehydrate_personal_archive(**_saved(parsed)).materialize()
    assert restored.graph == parsed.graph
    assert restored.archive.manifest == parsed.archive.manifest


@pytest.mark.parametrize("field", ["compressed_bytes", "logical_members", "parsed_rows"])
def test_saved_measurements_reject_boolean_counts(admitted, field):
    saved = _saved(admitted)
    sizes = json.loads(saved["measured_sizes_json"])
    sizes[field] = True
    saved["measured_sizes_json"] = canonical_json(sizes)
    with pytest.raises(ArchiveArtifactIntegrityError, match="integers"):
        rehydrate_personal_archive(**saved)
