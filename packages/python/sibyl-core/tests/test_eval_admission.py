"""Real embedded-store qualification of permanent eval admission."""

import asyncio
import base64
import hashlib

import pytest

from sibyl_core.services import content_client, eval_admission
from sibyl_core.services.eval_admission import (
    EvalAdmissionConflict,
    admit_eval_outcome,
    get_registered_eval_assignment,
    register_eval_assignment,
)
from tests.test_eval_receipts import evidence as evidence
from tests.test_reflection_identity import content_store as content_store


def admission(evidence, **overrides):
    _, assignment, _, materials, issue, params = evidence
    return (
        dict(
            organization_id=assignment.organization_id,
            experiment_id=assignment.experiment_id,
            attempt_id=assignment.attempt_id,
            principal_id=assignment.owner_principal_id,
            issuer_id="oracle-1",
            trusted_public_key=params["trusted_public_key"],
            expected_controller_policy_sha256=assignment.controller_policy_sha256,
            receipt_bytes=issue(),
            **materials,
        )
        | overrides
    )


async def register(evidence):
    return await register_eval_assignment(organization_id="org", assignment=evidence[1])


async def rows(table):
    async with content_client.surreal_content_client() as client:
        return await content_client.select_many(client, f"SELECT * FROM {table};")


async def test_registration_is_immutable_and_tenant_scoped(content_store, evidence):
    assignment = await register(evidence)
    assert await register(evidence) == assignment
    assert len(await rows("eval_attempts")) == 1
    assert (
        await get_registered_eval_assignment(
            organization_id="other", experiment_id="experiment", attempt_id=assignment.attempt_id
        )
        is None
    )
    with pytest.raises(EvalAdmissionConflict):
        await register_eval_assignment(
            organization_id="org", assignment=assignment.model_copy(update={"seed": 9})
        )
    with pytest.raises(EvalAdmissionConflict):
        await register_eval_assignment(organization_id="other", assignment=assignment)


async def test_admission_preserves_bytes_and_permanent_binding(content_store, evidence):
    await register(evidence)
    params = admission(evidence)
    first = await admit_eval_outcome(**params)
    second = await admit_eval_outcome(**params)
    assert first == second
    assert first.memory.raw_content.encode() == params["episode_bytes"]
    assert first.memory.principal_id == "owner"
    assert first.memory.memory_scope == "private"
    assert first.memory.revision == 1
    assert len(await rows("raw_captures")) == 1
    ledger = (await rows("eval_attempts"))[0]
    assert base64.b64decode(ledger["receipt_base64"]) == params["receipt_bytes"]
    for kind in ("receipt", "outcome", "transcript", "episode"):
        assert ledger[f"{kind}_sha256"] == hashlib.sha256(params[f"{kind}_bytes"]).hexdigest()


async def test_concurrent_identical_admission_creates_once(content_store, evidence):
    await register(evidence)
    params = admission(evidence)
    results = await asyncio.gather(*(admit_eval_outcome(**params) for _ in range(8)))
    assert all(item == results[0] for item in results)
    assert len(await rows("raw_captures")) == 1


async def test_concurrent_conflicting_admission_has_one_winner(content_store, evidence):
    await register(evidence)
    data = evidence[3] | {"episode_bytes": b"Different episode"}
    other = admission(evidence, **data, receipt_bytes=evidence[4](data=data))
    results = await asyncio.gather(
        admit_eval_outcome(**admission(evidence)),
        admit_eval_outcome(**other),
        return_exceptions=True,
    )
    assert sum(isinstance(item, EvalAdmissionConflict) for item in results) == 1
    assert len(await rows("raw_captures")) == 1


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE raw_captures;",
        "UPDATE raw_captures SET deleted_at = time::now();",
        "UPDATE raw_captures SET raw_content = ''; ",
    ],
)
async def test_retry_never_resurrects_deleted_or_changed_capture(content_store, evidence, mutation):
    await register(evidence)
    params = admission(evidence)
    await admit_eval_outcome(**params)
    async with content_client.surreal_content_client() as client:
        await client.execute_query(mutation)
    before = await rows("raw_captures")
    with pytest.raises(EvalAdmissionConflict):
        await admit_eval_outcome(**params)
    assert await rows("raw_captures") == before
    assert (await rows("eval_attempts"))[0]["receipt_sha256"]


async def test_transaction_failure_rolls_back_capture_and_admission(
    content_store, evidence, monkeypatch
):
    await register(evidence)
    original = eval_admission._ADMIT
    monkeypatch.setattr(
        eval_admission,
        "_ADMIT",
        original.replace("RETURN $memory;", "THROW 'injected storage failure';"),
    )
    with pytest.raises(Exception, match="injected storage failure"):
        await admit_eval_outcome(**admission(evidence))
    assert await rows("raw_captures") == []
    assert not (await rows("eval_attempts"))[0].get("receipt_sha256")
    monkeypatch.setattr(eval_admission, "_ADMIT", original)
    assert (await admit_eval_outcome(**admission(evidence))).memory.revision == 1


async def test_unregistered_and_wrong_principal_cannot_write(content_store, evidence):
    with pytest.raises(LookupError):
        await admit_eval_outcome(**admission(evidence))
    await register(evidence)
    with pytest.raises(PermissionError):
        await admit_eval_outcome(**admission(evidence, principal_id="other"))
    with pytest.raises(LookupError):
        await admit_eval_outcome(**admission(evidence, organization_id="other"))
    assert await rows("raw_captures") == []


async def test_assignment_is_checked_again_inside_transaction(content_store, evidence, monkeypatch):
    await register(evidence)
    original = eval_admission._transaction

    async def changed_registry(client, query, **params):
        await client.execute_query("UPDATE eval_attempts SET assignment_sha256 = 'changed';")
        return await original(client, query, **params)

    monkeypatch.setattr(eval_admission, "_transaction", changed_registry)
    with pytest.raises(EvalAdmissionConflict, match="assignment changed"):
        await admit_eval_outcome(**admission(evidence))
    assert await rows("raw_captures") == []


async def test_public_capture_cannot_stamp_admission_metadata(content_store):
    from sibyl_core.services.content_raw_persistence import remember_raw_memory

    authored = {
        "eval_admission": {"admission_id": "forged"},
        "details": {"eval_admission": "authored"},
    }
    memory = await remember_raw_memory(
        organization_id="org",
        principal_id="owner",
        source_id="public",
        raw_content="Ordinary authored capture",
        metadata=authored,
        embedding_provider=None,
    )
    assert "eval_admission" not in memory.metadata
    assert memory.metadata["details"] == {"eval_admission": "authored"}
    assert authored["eval_admission"] == {"admission_id": "forged"}


@pytest.mark.parametrize("split", ["development", "sealed"])
async def test_nonlearning_assignment_cannot_admit(content_store, evidence, split):
    from sibyl_core.tasks.eval_receipts import ReceiptError

    task = evidence[1].model_copy(update={"split": split})
    await register_eval_assignment(organization_id="org", assignment=task)
    with pytest.raises(ReceiptError):
        await admit_eval_outcome(**admission(evidence, receipt_bytes=evidence[4](task=task)))
    assert await rows("raw_captures") == []


async def test_operational_failure_cannot_admit(content_store, evidence):
    import json

    from sibyl_core.tasks.eval_receipts import ReceiptError

    await register(evidence)
    artifact = evidence[2] | {"status": "oracle_runtime_error", "passed": False}
    data = evidence[3] | {"outcome_bytes": json.dumps(artifact).encode()}
    with pytest.raises(ReceiptError):
        await admit_eval_outcome(
            **admission(evidence, **data, receipt_bytes=evidence[4](data=data))
        )
    assert await rows("raw_captures") == []


async def test_concurrent_registration_is_idempotent_and_conflicts(content_store, evidence):
    task = evidence[1]
    results = await asyncio.gather(*(register(evidence) for _ in range(6)))
    assert all(item == task for item in results)
    changed = task.model_copy(update={"seed": 8})
    results = await asyncio.gather(
        register(evidence),
        register_eval_assignment(organization_id="org", assignment=changed),
        return_exceptions=True,
    )
    assert results[0] == task
    assert isinstance(results[1], EvalAdmissionConflict)
    assert len(await rows("eval_attempts")) == 1


async def test_wrong_signature_and_assignment_leave_registry_unadmitted(content_store, evidence):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from sibyl_core.tasks.eval_receipts import ReceiptError

    await register(evidence)
    with pytest.raises(ReceiptError):
        await admit_eval_outcome(
            **admission(evidence, trusted_public_key=Ed25519PrivateKey.generate().public_key())
        )
    changed = evidence[1].model_copy(update={"seed": 9})
    with pytest.raises(ReceiptError):
        await admit_eval_outcome(**admission(evidence, receipt_bytes=evidence[4](task=changed)))
    assert await rows("raw_captures") == []
    assert not (await rows("eval_attempts"))[0].get("receipt_sha256")


async def test_migration_reapplication_preserves_registered_evidence(content_store, evidence):
    from sibyl_core.backends.surreal.content_schema import (
        CONTENT_EVAL_ATTEMPTS_MIGRATION_DEFINITIONS,
    )

    await register(evidence)
    params = admission(evidence)
    admitted = await admit_eval_outcome(**params)
    ledger = await rows("eval_attempts")
    async with content_client.surreal_content_client() as client:
        await client.execute_query(CONTENT_EVAL_ATTEMPTS_MIGRATION_DEFINITIONS)
    assert await rows("eval_attempts") == ledger
    assert await admit_eval_outcome(**params) == admitted


async def test_non_utf8_signed_episode_is_rejected_without_writes(content_store, evidence):
    from sibyl_core.tasks.eval_receipts import ReceiptError

    await register(evidence)
    data = evidence[3] | {"episode_bytes": b"\xff"}
    with pytest.raises(ReceiptError, match="UTF-8"):
        await admit_eval_outcome(
            **admission(evidence, **data, receipt_bytes=evidence[4](data=data))
        )
    assert await rows("raw_captures") == []
    assert not (await rows("eval_attempts"))[0].get("receipt_sha256")


@pytest.mark.parametrize("operation", ["register", "admit"])
async def test_checked_client_replays_transaction_conflicts(
    content_store, evidence, monkeypatch, operation
):
    if operation == "admit":
        await register(evidence)
    async with content_client.surreal_content_client() as client:
        original = client._send_query
        attempts = 0

        async def conflict_once(connection, query, **kwargs):
            nonlocal attempts
            if query.lstrip().startswith("BEGIN TRANSACTION"):
                attempts += 1
                if attempts == 1:
                    return {
                        "result": [
                            {
                                "status": "ERR",
                                "result": "The query was not executed due to a failed transaction",
                            },
                            {
                                "status": "ERR",
                                "result": "Cannot COMMIT: Transaction conflict: Write conflict, retry the transaction. This transaction can be retried",
                            },
                        ]
                    }
            return await original(connection, query, **kwargs)

        monkeypatch.setattr(client, "_send_query", conflict_once)
        if operation == "register":
            assert await register(evidence) == evidence[1]
        else:
            assert (await admit_eval_outcome(**admission(evidence))).memory.revision == 1
        assert attempts == 2
    assert len(await rows("eval_attempts")) == 1
