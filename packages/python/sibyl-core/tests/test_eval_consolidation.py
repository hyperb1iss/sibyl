"""Admitted outcomes use real stored sources, never caller-declared revisions."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from sibyl_core.services import content_client, eval_consolidation
from sibyl_core.services.eval_admission import admit_eval_outcome, register_eval_assignment
from sibyl_core.tasks import consolidation as c
from sibyl_core.tasks.eval_receipts import ReceiptError
from tests.test_eval_receipts import evidence as evidence
from tests.test_reflection_identity import content_store as content_store


@pytest.fixture
async def admitted_pair(content_store, evidence):
    key, assignment, artifact, materials, issue, _ = evidence
    attempts = []
    for index, status in enumerate(("passed", "task_failed")):
        task = assignment.model_copy(update={"attempt_id": str(index) * 32})
        outcome = artifact | {"attempt_id": task.attempt_id, "status": status, "passed": index == 0}
        data = materials | {
            "outcome_bytes": json.dumps(outcome).encode(),
            "episode_bytes": f"Observed {status} episode.\n".encode(),
        }
        await register_eval_assignment(organization_id="org", assignment=task)
        result = await admit_eval_outcome(
            organization_id="org",
            experiment_id=task.experiment_id,
            attempt_id=task.attempt_id,
            principal_id="owner",
            issuer_id="oracle-1",
            trusted_public_key=key.public_key(),
            expected_controller_policy_sha256=task.controller_policy_sha256,
            receipt_bytes=issue(task=task, data=data),
            **data,
        )
        attempts.append((task, result))
    return dict(
        organization_id="org",
        principal_id="owner",
        experiment_id=assignment.experiment_id,
        experiment_revision=assignment.experiment_revision,
        arm_id="raw",
        through_checkpoint=0,
        attempt_ids=tuple(task.attempt_id for task, _ in attempts),
        group_id="contrast",
        mechanism="verify observed output",
        trusted_issuer_id="oracle-1",
        trusted_public_key=key.public_key(),
        expected_controller_policy_sha256=assignment.controller_policy_sha256,
    ), attempts


async def test_admitted_group_uses_exact_episode_and_current_revision(admitted_pair):
    params, attempts = admitted_pair
    async with content_client.surreal_content_client() as client:
        await client.execute_query("UPDATE raw_captures SET revision = 7;")
    group = await eval_consolidation.load_admitted_consolidation_group(**params)
    assert {e.outcome.status for e in group.episodes} == {"passed", "task_failed"}
    assert all(isinstance(e.outcome, c.AdmittedTaskOutcome) for e in group.episodes)
    assert {e.stored_sources[0].source_id for e in group.episodes} == {
        result.memory.id for _, result in attempts
    }
    assert all(e.stored_sources[0].observed_revision == 7 for e in group.episodes)
    assert group.episodes[0].artifact == b"Observed passed episode.\n"


@pytest.mark.parametrize(
    "changes",
    [
        {"organization_id": "other"},
        {"principal_id": "other"},
        {"arm_id": "other"},
        {"experiment_revision": "other"},
        {"trusted_issuer_id": "other"},
    ],
)
async def test_cohort_rejects_unauthorized_inputs(admitted_pair, changes):
    params, _ = admitted_pair
    with pytest.raises(ReceiptError):
        await eval_consolidation.load_admitted_consolidation_group(**(params | changes))


@pytest.mark.parametrize(
    "query",
    [
        "DELETE raw_captures;",
        "UPDATE raw_captures SET deleted_at = time::now();",
        "UPDATE raw_captures SET raw_content = 'changed';",
        "UPDATE raw_captures SET metadata.eval_admission.receipt_sha256 = 'changed';",
        "UPDATE eval_attempts SET episode_sha256 = 'changed';",
    ],
)
async def test_changed_or_deleted_admission_cannot_support_proposal(admitted_pair, query):
    params, _ = admitted_pair
    async with content_client.surreal_content_client() as client:
        await client.execute_query(query)
    with pytest.raises(ReceiptError):
        await eval_consolidation.load_admitted_consolidation_group(**params)


async def test_offline_proposal_uses_signed_sources_and_rechecks_revision(
    admitted_pair, monkeypatch
):
    params, _ = admitted_pair
    group = await eval_consolidation.load_admitted_consolidation_group(**params)

    def assertion(index):
        source = group.episodes[index]
        return c.ConditionalAssertion(
            statement="Use the observed output check",
            label="inferred",
            support=[
                c.SupportRef(
                    episode_id=source.episode_id, start_byte=0, end_byte=len(source.artifact)
                )
            ],
        )

    draft = c.DraftConditionalProcedure(
        goal=assertion(0),
        environment=[assertion(0)],
        preconditions=[assertion(0)],
        actions=[c.ConditionalAction(order=1, action=assertion(0), success_criteria=assertion(0))],
        expected_result=assertion(0),
        failure_modes=[assertion(1)],
        abstain_when=[assertion(1)],
    )

    async def extract(_self, _prompt):
        return SimpleNamespace(
            output=c.ProcedureProposal(procedure=draft),
            usage=SimpleNamespace(model_dump=lambda **_: {}),
        )

    monkeypatch.setattr(c.Extractor, "extract_with_usage", extract)
    result = await eval_consolidation.propose_admitted_procedure(**params)
    assert result.proposal.candidate.review_state == "pending"
    assert c.validate_candidate_content_agreement(result.proposal.candidate, group=group) == []
    assert result.source_join == "authenticated_admission_ledger"

    async def changed_extract(*args):
        async with content_client.surreal_content_client() as client:
            await client.execute_query("UPDATE raw_captures SET revision += 1;")
        return await extract(*args)

    monkeypatch.setattr(c.Extractor, "extract_with_usage", changed_extract)
    with pytest.raises(ReceiptError, match="changed during consolidation"):
        await eval_consolidation.propose_admitted_procedure(**params)


async def test_signature_verification_keeps_request_event_loop_responsive(
    admitted_pair, monkeypatch
):
    params, _ = admitted_pair
    started = threading.Event()
    release = threading.Event()
    request_thread = threading.get_ident()
    original = eval_consolidation.verify_outcome_receipt

    def verify(*args, **kwargs):
        assert threading.get_ident() != request_thread
        started.set()
        assert release.wait(timeout=5), "request loop did not release validation"
        return original(*args, **kwargs)

    monkeypatch.setattr(eval_consolidation, "verify_outcome_receipt", verify)
    task = asyncio.create_task(eval_consolidation.load_admitted_consolidation_group(**params))
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=5)
        assert not task.done()
        # This callback must run while signature verification is still occupied.
        progressed = asyncio.Event()
        asyncio.get_running_loop().call_soon(progressed.set)
        await asyncio.wait_for(progressed.wait(), timeout=5)
    finally:
        release.set()
    group = await task
    assert len(group.episodes) == 2
