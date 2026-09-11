"""Procedure critique shares the synthesis view while retaining original authority."""

import hashlib
import json
from dataclasses import replace

import pytest

from sibyl_core.tasks.consolidation import DraftConditionalProcedure
from sibyl_core.tasks.episode_evidence import (
    EvidenceCitation,
    encode_episode_views,
    project_episode,
)
from sibyl_core.tasks.memory_validation import (
    OriginalValidationEvidence,
    prepare_procedure_validation,
)
from tests.test_episode_evidence import _episode
from tests.test_tasks_consolidation import group as group
from tests.test_tasks_consolidation import procedure as procedure


def prepare(procedure, *, evidence=None, citations=None):
    if evidence is None:
        raw = json.dumps(_episode(), ensure_ascii=False).encode()
        evidence = [
            OriginalValidationEvidence(k, raw, "a" * 64, "signed") for k in ("success", "failure")
        ]
    projections = [
        project_episode(e.source_id, e.content, prefix=f"s{i}") for i, e in enumerate(evidence)
    ]
    if citations is None:
        citations = {k: v for p in projections for k, v in p.citations.items()}
    value = procedure.model_dump(mode="json")

    def supports(item):
        if isinstance(item, dict):
            for support in item.get("support", []):
                source = next(e for e in evidence if e.source_id == support["episode_id"])
                support.update(start_byte=0, end_byte=len(source.content))
            for child in item.values():
                supports(child)
        elif isinstance(item, list):
            for child in item:
                supports(child)

    supports(value)
    prepared = prepare_procedure_validation(
        DraftConditionalProcedure.model_validate(value),
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
        evidence=evidence,
        citations=citations,
    )
    return prepared, evidence, citations, projections


def test_validation_projection_preserves_original_hashes_ranges_and_view(procedure):
    prepared, evidence, citations, projections = prepare(procedure)
    payload = json.loads(prepared.payload_json)
    assert payload["evidence_view"] == encode_episode_views(projections)
    assert payload["evidence_representation"] == "controller_episode_projection_v1"
    for source in evidence:
        row = payload["sources"][source.source_id]
        assert "text" not in row
        assert row["sha256"] == hashlib.sha256(source.content).hexdigest()
        assert row["observation_sha256"] == source.observation_sha256
        assert row["provenance"] == "signed"
    for key, citation in citations.items():
        assert payload["citations"][key] == {
            "source_id": citation.episode_id,
            "ranges": [list(r) for r in citation.ranges],
        }


def test_validation_projection_audit_only_change_still_changes_identity(procedure):
    before, evidence, _, _ = prepare(procedure)
    value = json.loads(evidence[0].content)
    value["assurance"] = {"audit": "different retained transport identity"}
    changed = [
        replace(evidence[0], content=json.dumps(value, ensure_ascii=False).encode()),
        evidence[1],
    ]
    after, _, _, _ = prepare(procedure, evidence=changed)
    assert (
        json.loads(before.payload_json)["evidence_view"]
        == json.loads(after.payload_json)["evidence_view"]
    )
    assert before.input_sha256 != after.input_sha256


def test_validation_projection_observation_change_changes_identity(procedure):
    before, evidence, _, _ = prepare(procedure)
    after, _, _, _ = prepare(
        procedure, evidence=[replace(evidence[0], observation_sha256="d" * 64), evidence[1]]
    )
    assert before.input_sha256 != after.input_sha256


@pytest.mark.parametrize("change", ["range", "alias", "source"])
def test_validation_projection_rejects_altered_citation_namespace(procedure, change):
    _, evidence, citations, _ = prepare(procedure)
    key = next(iter(citations))
    original = citations[key]
    if change == "range":
        citations[key] = EvidenceCitation(original.episode_id, ((0, 1),))
    elif change == "alias":
        citations["invented"] = citations.pop(key)
    else:
        citations[key] = EvidenceCitation("not-authorized", original.ranges)
    with pytest.raises(ValueError, match="citations differ"):
        prepare(procedure, evidence=evidence, citations=citations)


def test_validation_projection_noncontroller_fallback_keeps_full_text(procedure, group):
    evidence = [
        OriginalValidationEvidence(e.episode_id, e.artifact, "a" * 64, "reported")
        for e in group.episodes
    ]
    citations = {
        str(i): EvidenceCitation(e.source_id, ((0, len(e.content)),))
        for i, e in enumerate(evidence)
    }
    prepared = prepare_procedure_validation(
        procedure,
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
        evidence=evidence,
        citations=citations,
    )
    payload = json.loads(prepared.payload_json)
    assert "evidence_representation" not in payload
    assert "evidence_view" not in payload
    for source in evidence:
        assert payload["sources"][source.source_id]["text"] == source.content.decode()


def test_validation_projection_malformed_controller_does_not_fallback(procedure):
    _, evidence, citations, _ = prepare(procedure)
    malformed = json.loads(evidence[0].content)
    malformed["trace"][1]["payload"]["body_sha256"] = "0" * 64
    evidence[0] = replace(evidence[0], content=json.dumps(malformed).encode())
    with pytest.raises(ValueError):
        prepare(procedure, evidence=evidence, citations=citations)
