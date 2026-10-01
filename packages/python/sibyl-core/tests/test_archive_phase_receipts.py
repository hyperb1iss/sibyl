"""Strict and immutable archive phase contracts, independent of active writers."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from sibyl_core.migrate.archive_phase_receipts import (
    ArchivePhaseControl,
    ArchivePhaseCounts,
    ArchivePhaseCredential,
    ArchivePhaseKey,
    ArchivePhaseReceipt,
    ArchiveRetirementEvidence,
    ArchiveRunBinding,
    IntroducedArchiveRow,
    phase_receipt_json,
    strict_phase_json,
    verify_phase_receipt,
)
from sibyl_core.services.archive_phase_store import prepare_archive_phase_transaction


def binding() -> ArchiveRunBinding:
    return ArchiveRunBinding(
        organization_id=str(uuid4()),
        actor_id=str(uuid4()),
        run_id=str(uuid4()),
        artifact_id=str(uuid4()),
        archive_sha256="a" * 64,
        artifact_sha256="b" * 64,
        mappings_sha256="c" * 64,
        checked_plan_sha256="d" * 64,
        credential=ArchivePhaseCredential(credential_kind="session"),
    )


def introduced() -> IntroducedArchiveRow:
    return IntroducedArchiveRow(
        kind="raw_capture",
        destination_id=str(uuid4()),
        physical_id="raw_captures:synthetic",
        row_sha256="e" * 64,
        body_sha256="f" * 64,
        audience_sha256="0" * 64,
        revision=1,
        source_incarnation=str(uuid4()),
        source_generation=1,
        source_state_revision=1,
    )


def receipt() -> ArchivePhaseReceipt:
    token = str(uuid4())
    return ArchivePhaseReceipt(
        key=ArchivePhaseKey(binding=binding(), store="content", action="apply", batch_sequence=0),
        token=token,
        previous_token=token,
        previous_revision=0,
        committed_revision=1,
        counts=(ArchivePhaseCounts(kind="raw_capture", created=1),),
        introduced=(introduced(),),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("contract_version", True),
        ("organization_id", str(uuid4()).upper()),
        ("archive_sha256", "A" * 64),
        ("actor_id", 12),
        ("extra", "forbidden"),
    ],
)
def test_archive_phase_binding_rejects_coercion_and_noncanonical_values(field, value):
    payload = binding().model_dump(mode="python")
    payload[field] = value
    with pytest.raises(ValidationError):
        ArchiveRunBinding.model_validate(payload)


@pytest.mark.parametrize("value", [True, "1", 1.0, -1])
def test_archive_phase_counters_are_strict(value):
    with pytest.raises(ValidationError):
        ArchivePhaseCounts(kind="raw_capture", created=value)


def test_archive_phase_credential_preserves_restricted_empty_without_mutable_maps():
    key = str(uuid4())
    empty = ArchivePhaseCredential(
        credential_kind="api_key", api_key_id=key, project_restricted=True, memory_restricted=True
    )
    assert empty.project_ids == empty.memory_scope_keys == ()
    with pytest.raises(ValidationError):
        ArchivePhaseCredential(credential_kind="api_key", api_key_id=key, project_restricted=1)
    with pytest.raises(ValidationError):
        ArchivePhaseCredential(credential_kind="api_key", api_key_id=key, project_ids=["project"])
    model = binding()
    with pytest.raises(ValidationError):
        model.actor_id = str(uuid4())


def test_archive_phase_receipt_counts_are_actual_and_reconcile():
    model = receipt()
    encoded = phase_receipt_json(model)
    assert verify_phase_receipt(encoded) == model
    for change in (
        {"counts": (ArchivePhaseCounts(kind="raw_capture", created=0),)},
        {"counts": ()},
        {"introduced": (model.introduced[0], model.introduced[0])},
        {"committed_revision": 2},
        {"terminal": True},
    ):
        with pytest.raises(ValidationError):
            ArchivePhaseReceipt.model_validate({**model.model_dump(mode="python"), **change})
    duplicate = encoded.replace('"terminal":false', '"terminal":false,"terminal":false')
    with pytest.raises(ValueError, match="duplicate"):
        verify_phase_receipt(duplicate)
    with pytest.raises(ValueError, match="canonical"):
        verify_phase_receipt(json.dumps(json.loads(encoded), indent=2))


def test_archive_phase_relationship_cannot_invent_source_revision():
    model = introduced().model_dump(mode="python")
    model.update(
        kind="graph_relationship",
        physical_id="relates_to:synthetic",
        endpoint_ids=("entity-a", "entity-b"),
        endpoint_state_sha256=("1" * 64, "2" * 64),
        binding_sha256="3" * 64,
    )
    with pytest.raises(ValidationError, match="cannot invent"):
        IntroducedArchiveRow.model_validate(model)
    model.update(
        revision=None, source_incarnation=None, source_generation=None, source_state_revision=None
    )
    assert IntroducedArchiveRow.model_validate(model).revision is None


def test_archive_phase_retirement_requires_advanced_retained_highwater():
    row = introduced()
    values = dict(
        introduced=row,
        absent=True,
        row_sha256=None,
        source_incarnation=row.source_incarnation,
        source_generation=2,
        source_state_sha256="1" * 64,
    )
    assert ArchiveRetirementEvidence(**values).source_generation == 2
    for change in (
        {"source_generation": 1},
        {"source_incarnation": str(uuid4())},
        {"source_state_sha256": None},
    ):
        with pytest.raises(ValidationError, match="high-water"):
            ArchiveRetirementEvidence(**{**values, **change})


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), datetime.now(UTC), uuid4(), {1: "bad"}]
)
def test_archive_phase_json_rejects_lossy_or_nonfinite_values(value):
    with pytest.raises(ValueError):
        strict_phase_json({"value": value})


def test_archive_phase_transaction_snapshots_parameters_and_rejects_reserved_bindings():
    key = receipt().key
    token = str(uuid4())
    parameters = {"record": {"metadata": {"value": [1, 2]}}}
    body = "LET $sibyl_archive_phase_outcomes = [];"
    tx = prepare_archive_phase_transaction(
        url="memory://",
        key=key,
        expected_revision=0,
        expected_token=token,
        writer_statements=body,
        writer_parameters=parameters,
    )
    parameters["record"]["metadata"]["value"].append(3)
    assert tx.parameters["record"]["metadata"]["value"] == [1, 2]
    copy = tx.parameters
    copy["record"]["metadata"]["value"].append(4)
    assert tx.parameters["record"]["metadata"]["value"] == [1, 2]
    for invalid in ({"sibyl_archive_phase_org": "forged"}, {"record": datetime.now(UTC)}):
        with pytest.raises(ValueError):
            prepare_archive_phase_transaction(
                url="memory://",
                key=key,
                expected_revision=0,
                expected_token=token,
                writer_statements=body,
                writer_parameters=invalid,
            )
    for statement in (
        "BEGIN TRANSACTION;",
        "COMMIT;",
        "CANCEL;",
        "DELETE archive_phase_controls;",
        "",
    ):
        with pytest.raises(ValueError):
            prepare_archive_phase_transaction(
                url="memory://",
                key=key,
                expected_revision=0,
                expected_token=token,
                writer_statements=statement,
                writer_parameters={},
            )
    forged = key.binding.model_copy(update={"actor_id": "forged"})
    with pytest.raises(ValidationError):
        prepare_archive_phase_transaction(
            url="memory://",
            key=key.model_copy(update={"binding": forged}),
            expected_revision=0,
            expected_token=token,
            writer_statements=body,
            writer_parameters={},
        )


def test_archive_phase_control_requires_canonical_token_and_boolean_terminal():
    key = receipt().key
    with pytest.raises(ValidationError):
        ArchivePhaseControl(
            binding=key.binding, store="content", revision=True, token=str(uuid4()), state="open"
        )
    with pytest.raises(ValueError):
        prepare_archive_phase_transaction(
            url="memory://",
            key=key,
            expected_revision=0,
            expected_token=str(uuid4()),
            writer_statements="LET $sibyl_archive_phase_outcomes = [];",
            writer_parameters={},
            terminal=1,
        )
