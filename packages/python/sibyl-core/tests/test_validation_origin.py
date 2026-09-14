"""Execution origins survive archival history without allowing legacy downgrades."""

import json
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

import pytest

from sibyl_core.migrate.validation_receipt_archive import prepare_payload
from sibyl_core.services.validation_origin import validate_origin_row
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_review import review_digest
from tests.test_ordinary_cohort import (
    cohort_sources as cohort_sources,
)
from tests.test_ordinary_cohort import (
    content_store as content_store,
)
from tests.test_ordinary_cohort import (
    install_proposal,
)


def origin_records(*, purged=False):
    request = {
        "kind": "sibyl-ordinary-partial-proposal-v1",
        "org": "org",
        "principal": "owner",
        "parent": "source",
        "source_bindings": [{"source_id": "source", "incarnation": "epoch", "generation": 1}],
        "policy": "{}",
        "input": "a" * 64,
        "evidence_packet": {"manifest": "test-shape-only"},
    }
    identity = review_digest(request)
    assertion = {
        "statement": "Observe source context",
        "label": "inferred",
        "support": [{"episode_id": "source", "start_byte": 0, "end_byte": 1}],
    }
    value = {
        "status": "ordinary_cohort_proposal",
        "input_sha256": "a" * 64,
        "proposal": {
            "procedure": {"kind": "pattern", "goal": assertion},
            "abstention_reason": None,
        },
        "usage": {"requests": 1},
        "validation_error": None,
    }
    from sibyl_core.ai.llm.extractor import ExtractionUsage

    value["usage"] = ExtractionUsage(
        requests=1, input_tokens=1, output_tokens=1, total_tokens=2
    ).model_dump(mode="json")
    row = {
        "uuid": identity,
        "request_sha256": identity,
        "request_json": canonical(request),
        "organization_id": "org",
        "principal_id": "owner",
        "parent_id": "source",
        "source_ids": ["source"],
        "policy_json": "{}",
        "state": "returned",
        "purged": purged,
        "result_json": None if purged else canonical(value),
        "usage_json": canonical(value["usage"]),
        "dependency_ids": [],
    }
    target = str(uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + identity))
    derivation = {
        "organization_id": "org",
        "principal_id": "owner",
        "target_id": target,
        "origin_execution_id": identity,
    }
    return derivation, row


@pytest.mark.parametrize(
    "mutation", ["missing", "tenant", "principal", "target", "request", "hash", "failed"]
)
def test_validation_origin_rejects_changed_identity(mutation):
    derivation, row = origin_records()
    if mutation == "missing":
        row = None
    elif mutation in {"tenant", "principal"}:
        row["organization_id" if mutation == "tenant" else "principal_id"] = "other"
    elif mutation == "target":
        derivation["target_id"] = "different"
    elif mutation == "request":
        request = json.loads(row["request_json"])
        request["evidence_packet"] = {"another": "packet"}
        row["request_json"] = canonical(request)
    elif mutation == "hash":
        row["request_sha256"] = "0" * 64
    else:
        row["state"] = "failed"
    with pytest.raises(ValueError):
        validate_origin_row(derivation, row)


def payload(derivation, row):
    from sibyl_core.migrate.validation_receipt_archive import capture

    return {
        "version": "2.3",
        "tables": {"memory_validation_executions": [row]},
        "validation_receipts": capture([row]),
        "source_integrity": {
            "derivations": [derivation],
            "source_rows": [{"record": {"uuid": derivation["target_id"]}}],
        },
    }


def test_validation_origin_purged_history_remains_archivable_but_unavailable_live():
    derivation, row = origin_records(purged=True)
    validate_origin_row(derivation, row, historical=True)
    with pytest.raises(ValueError):
        validate_origin_row(derivation, row)
    assert prepare_payload(payload(derivation, row)) == []


@pytest.mark.parametrize("mutation", ["omit", "wrong", "dangling", "foreign", "legacy"])
def test_validation_origin_archive_cannot_forge_or_remove_packet_lineage(mutation):
    derivation, row = origin_records()
    archive = payload(derivation, row)
    if mutation == "omit":
        del derivation["origin_execution_id"]
    elif mutation == "wrong":
        derivation["origin_execution_id"] = "0" * 64
    elif mutation == "dangling":
        archive["tables"]["memory_validation_executions"] = []
    elif mutation == "foreign":
        derivation["organization_id"] = "foreign"
    else:
        archive["version"] = "2.2"
        archive.pop("validation_receipts")
    with pytest.raises(ValueError):
        prepare_payload(deepcopy(archive))


async def test_validation_origin_atomic_candidate_write_and_immutable_reference(
    cohort_sources, content_store, monkeypatch
):
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from sibyl_core.ai.llm.extractor import Extractor
    from sibyl_core.services import ordinary_cohort, validation_candidate
    from sibyl_core.services.memory_source_validation import SourceReadAuthority

    install_proposal(monkeypatch, cohort_sources)
    captured = {}
    insert = validation_candidate.insert_validation_candidate
    extract = Extractor.extract_with_usage
    sends = []

    async def tracked_extract(self, prompt):
        sends.append(prompt)
        return await extract(self, prompt)

    monkeypatch.setattr(Extractor, "extract_with_usage", tracked_extract)

    async def stop_before_insert(client, row, write, derivation):
        captured.update(client=client, row=row, write=write, derivation=derivation)
        raise ValueError("test interruption before candidate insert")

    monkeypatch.setattr(validation_candidate, "insert_validation_candidate", stop_before_insert)
    with pytest.raises(ValueError, match="test interruption"):
        await ordinary_cohort.propose_stored_cohort(
            "org",
            "owner",
            [source.id for source in cohort_sources],
            AsyncMock(return_value=SourceReadAuthority("owner")),
            authorize=AsyncMock(),
        )
    write = captured["write"]
    assert len(sends) == 1
    before = await content_store.execute_query("SELECT * FROM source_states ORDER BY source_id;")
    with pytest.raises(Exception, match="test atomic failure"):
        await insert(
            captured["client"],
            captured["row"],
            replace(write, source_guard=write.source_guard + "THROW 'test atomic failure';"),
            captured["derivation"],
        )
    assert (
        await content_store.execute_query("SELECT * FROM source_states ORDER BY source_id;")
        == before
    )
    assert (
        await content_store.execute_query("SELECT * FROM raw_captures WHERE uuid=$id;", id=write.id)
        == []
    )
    assert (
        await content_store.execute_query(
            "SELECT * FROM memory_derivations WHERE target_id=$id;", id=write.id
        )
        == []
    )
    monkeypatch.setattr(validation_candidate, "insert_validation_candidate", insert)
    candidate, replayed = await ordinary_cohort.propose_stored_cohort(
        "org",
        "owner",
        [source.id for source in cohort_sources],
        AsyncMock(return_value=SourceReadAuthority("owner")),
        authorize=AsyncMock(),
    )
    assert candidate.id == write.id and replayed == write.execution_id and len(sends) == 1
    rows = await content_store.execute_query(
        "SELECT * FROM memory_derivations WHERE target_id=$id;", id=write.id
    )
    assert rows[0]["origin_execution_id"] == write.execution_id
    for assignment in ("NONE", "'other'"):
        with pytest.raises(Exception, match="Derivation origin is immutable"):
            await content_store.execute_query(
                f"UPDATE memory_derivations SET origin_execution_id={assignment} WHERE target_id=$id;",
                id=write.id,
            )
