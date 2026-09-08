"""Actual Ed25519 bindings and fail-closed learning evidence verification."""

import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sibyl_core.tasks.eval_receipts import (
    ReceiptError,
    TaskAssignment,
    sign_outcome,
    verify_learning_evidence,
    verify_outcome,
)


@pytest.fixture
def evidence():
    key = Ed25519PrivateKey.generate()
    assignment = TaskAssignment(
        organization_id="org",
        owner_principal_id="owner",
        experiment_id="experiment",
        experiment_revision="revision-1",
        task_id="task",
        task_revision="task-1",
        task_sha256="a" * 64,
        family_id="family",
        split="learning",
        arm_id="raw",
        checkpoint=0,
        seed=7,
        memory_pack_sha256="b" * 64,
        controller_policy_sha256="c" * 64,
        attempt_id="d" * 32,
        checker_sha256="4" * 64,
        oracle_sha256="e" * 64,
        evaluator_sha256="f" * 64,
        runtime_sha256="1" * 64,
        image="sha256:" + "2" * 64,
    )
    artifact = {
        "schema_version": "sibyl-json-cli-outcome-v1",
        "attempt_id": assignment.attempt_id,
        "snapshot_sha256": "3" * 64,
        "checker_sha256": assignment.checker_sha256,
        "oracle_sha256": assignment.oracle_sha256,
        "evaluator_sha256": assignment.evaluator_sha256,
        "runtime_sha256": assignment.runtime_sha256,
        "image": assignment.image,
        "status": "passed",
        "passed": True,
    }
    materials = {
        "outcome_bytes": json.dumps(artifact).encode(),
        "transcript_bytes": b'{"action":"check"}\n',
        "episode_bytes": b"Observed task behavior",
    }

    def issue(*, task=assignment, data=None):
        return sign_outcome(
            assignment=task, issuer_id="oracle-1", private_key=key, **(data or materials)
        )

    params = {
        "trusted_public_key": key.public_key(),
        "trusted_issuer_id": "oracle-1",
        "expected_assignment": assignment,
        "expected_controller_policy_sha256": assignment.controller_policy_sha256,
        **materials,
    }
    return key, assignment, artifact, materials, issue, params


def test_verified_learning_retains_exact_bytes_and_stable_admission_identity(evidence):
    _, assignment, _, materials, issue, params = evidence
    encoded = issue()
    result = verify_learning_evidence(encoded, **params)
    assert result.outcome.assignment == assignment
    assert result.episode_bytes == materials["episode_bytes"]
    assert result.transcript_bytes == materials["transcript_bytes"]
    assert result.outcome_bytes == materials["outcome_bytes"]
    assert result.outcome.status == "passed"
    # Pure verification does not consume the attempt; persistent admission owns replay.
    assert verify_learning_evidence(encoded, **params).admission_id == result.admission_id


@pytest.mark.parametrize("field", ["episode_bytes", "transcript_bytes", "outcome_bytes"])
def test_changed_artifact_is_rejected(evidence, field):
    *_, issue, params = evidence
    encoded = issue()
    params[field] += b" "
    with pytest.raises(ReceiptError):
        verify_learning_evidence(encoded, **params)


@pytest.mark.parametrize(
    "field",
    [
        "organization_id",
        "owner_principal_id",
        "experiment_revision",
        "task_id",
        "task_revision",
        "family_id",
        "arm_id",
        "memory_pack_sha256",
        "attempt_id",
        "controller_policy_sha256",
        "checkpoint",
        "seed",
    ],
)
def test_registered_assignment_cannot_be_replaced(evidence, field):
    _, assignment, _, _, issue, params = evidence
    old = getattr(assignment, field)
    value = old + 1 if isinstance(old, int) else "0" * len(old)
    params["expected_assignment"] = assignment.model_copy(update={field: value})
    with pytest.raises(ReceiptError, match="registered assignment"):
        verify_learning_evidence(issue(), **params)


def test_wrong_key_and_wrong_issuer_are_rejected(evidence):
    *_, issue, params = evidence
    with pytest.raises(ReceiptError, match="invalid signed"):
        verify_outcome(
            issue(), **{**params, "trusted_public_key": Ed25519PrivateKey.generate().public_key()}
        )
    with pytest.raises(ReceiptError, match="untrusted outcome issuer"):
        verify_outcome(issue(), **{**params, "trusted_issuer_id": "other"})
    with pytest.raises(ReceiptError, match="policy is not approved"):
        verify_outcome(issue(), **{**params, "expected_controller_policy_sha256": "0" * 64})


def test_signature_is_domain_separated_and_detects_payload_changes(evidence):
    key, _, _, _, issue, params = evidence
    document = json.loads(issue())
    canonical = json.dumps(document["payload"], sort_keys=True, separators=(",", ":")).encode()
    document["signature"] = base64.b64encode(key.sign(canonical)).decode()
    with pytest.raises(ReceiptError, match="invalid signed"):
        verify_outcome(json.dumps(document).encode(), **params)
    document = json.loads(issue())
    document["payload"]["snapshot_sha256"] = "0" * 64
    with pytest.raises(ReceiptError, match="invalid signed"):
        verify_outcome(json.dumps(document).encode(), **params)


@pytest.mark.parametrize("split", ["development", "sealed"])
def test_nonlearning_receipts_are_authentic_but_not_admissible(evidence, split):
    _, assignment, _, _, issue, params = evidence
    assignment = assignment.model_copy(update={"split": split})
    encoded = issue(task=assignment)
    params["expected_assignment"] = assignment
    assert verify_outcome(encoded, **params).outcome.assignment.split == split
    with pytest.raises(ReceiptError, match="only learning"):
        verify_learning_evidence(encoded, **params)


@pytest.mark.parametrize(
    "status",
    [
        "task_failed",
        "candidate_failed",
        "candidate_protocol_invalid",
        "candidate_timeout",
        "oracle_runtime_error",
        "oracle_timeout",
    ],
)
def test_task_and_operational_failures_remain_distinct(evidence, status):
    _, _, artifact, materials, issue, params = evidence
    artifact.update(status=status, passed=False)
    materials["outcome_bytes"] = json.dumps(artifact).encode()
    params.update(materials)
    encoded = issue()
    assert verify_outcome(encoded, **params).outcome.status == status
    if status.startswith("oracle_"):
        with pytest.raises(ReceiptError, match="operational failures"):
            verify_learning_evidence(encoded, **params)
    else:
        assert verify_learning_evidence(encoded, **params).outcome.success is False


@pytest.mark.parametrize(
    "field", ["checker_sha256", "oracle_sha256", "evaluator_sha256", "runtime_sha256", "image"]
)
def test_issuer_cannot_choose_different_registered_oracle_policy(evidence, field):
    _, _, artifact, materials, issue, _ = evidence
    artifact[field] = "sha256:" + "0" * 64 if field == "image" else "0" * 64
    materials["outcome_bytes"] = json.dumps(artifact).encode()
    with pytest.raises(ReceiptError, match="registered execution policy"):
        issue()


def test_signing_rejects_disagreeing_status(evidence):
    _, _, artifact, materials, issue, _ = evidence
    artifact["passed"] = False
    materials["outcome_bytes"] = json.dumps(artifact).encode()
    with pytest.raises(ReceiptError, match="status and success disagree"):
        issue()


@pytest.mark.parametrize("invalid", [b'{"x":1,"x":2}', b"1e999", b"NaN", b"deep-json"])
def test_strict_json_failures_do_not_escape_verification(evidence, invalid):
    *_, issue, params = evidence
    if invalid == b"deep-json":
        depth = 100_000
        invalid = b"[" * depth + b"0" + b"]" * depth
    with pytest.raises(ReceiptError):
        verify_outcome(invalid, **params)
    with pytest.raises(ReceiptError):
        issue(
            data={
                **{k: params[k] for k in ("outcome_bytes", "transcript_bytes", "episode_bytes")},
                "outcome_bytes": invalid,
            }
        )


def test_missing_checker_policy_is_rejected(evidence):
    _, _, artifact, materials, issue, _ = evidence
    del artifact["checker_sha256"]
    materials["outcome_bytes"] = json.dumps(artifact).encode()
    with pytest.raises(ReceiptError, match="registered execution policy"):
        issue()


def test_authorized_issuer_rotation_preserves_attempt_admission_identity(evidence):
    _, assignment, _, materials, issue, params = evidence
    original = verify_learning_evidence(issue(), **params)
    rotated_key = Ed25519PrivateKey.generate()
    rotated = sign_outcome(
        assignment=assignment, issuer_id="oracle-2", private_key=rotated_key, **materials
    )
    after_rotation = verify_learning_evidence(
        rotated,
        **{
            **params,
            "trusted_issuer_id": "oracle-2",
            "trusted_public_key": rotated_key.public_key(),
        },
    )
    assert after_rotation.admission_id == original.admission_id
    assert after_rotation.receipt_sha256 != original.receipt_sha256
    changed_materials = {**materials, "episode_bytes": b"Different authenticated episode"}
    different = sign_outcome(
        assignment=assignment, issuer_id="oracle-2", private_key=rotated_key, **changed_materials
    )
    conflicting = verify_learning_evidence(
        different,
        **{
            **params,
            **changed_materials,
            "trusted_issuer_id": "oracle-2",
            "trusted_public_key": rotated_key.public_key(),
        },
    )
    # The persistent ledger must detect the changed evidence at this same identity.
    assert conflicting.admission_id == original.admission_id
    assert conflicting.outcome.episode_sha256 != original.outcome.episode_sha256
