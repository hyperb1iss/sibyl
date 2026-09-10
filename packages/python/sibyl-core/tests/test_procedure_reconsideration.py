"""Critique changes the retained prompt, never the original evidence authority."""

import copy

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm import Extractor
from sibyl_core.tasks import consolidation as c
from sibyl_core.tasks.procedure_evidence import EvidenceRef
from sibyl_core.tasks.procedure_review import ReviewFinding, ReviewSubmission, review_digest
from tests.test_tasks_consolidation import group as group
from tests.test_tasks_consolidation import procedure as procedure


@pytest.fixture
def local_model(monkeypatch, procedure):
    output = {"procedure": procedure.model_dump(mode="json"), "abstention_reason": None}
    calls = []

    async def agent(extractor):
        calls.append(extractor.output_type)
        return Agent(
            TestModel(custom_output_args=copy.deepcopy(output)), output_type=extractor.output_type
        )

    monkeypatch.setattr(Extractor, "_get_agent", agent)
    return output, calls


async def parent_and_review(group, local_model):
    parent = await c.propose_conditional_procedure(group, max_input_chars=100_000)
    artifact = parent.candidate.metadata[c.METADATA_KEY]
    review = ReviewSubmission(
        parent_operation_id="a" * 64,
        parent_candidate_sha256=review_digest(artifact),
        findings=[
            ReviewFinding(
                claim_path="/goal",
                claim_sha256=review_digest(artifact["procedure"]["goal"]),
                evidence_refs=[{"evidence_id": "episode:0"}],
                basis="unsupported_generalization",
                disposition="qualify",
                critique="Limit this goal to the scoped rebuild evidenced in the source.",
            )
        ],
    )
    return artifact, review


def assessment(review):
    return {
        "finding_id": review.finding_ids()[0],
        "disposition": "accepted",
        "explanation": "The source supports the scoped goal.",
        "evidence_refs": [{"evidence_id": "episode:0"}],
    }


async def test_reconsideration_retains_review_and_reconstructs(group, local_model):
    artifact, review = await parent_and_review(group, local_model)
    original = copy.deepcopy(artifact)
    local_model[0]["assessments"] = [assessment(review)]
    result = await c.propose_conditional_procedure(
        group, review=review, parent_artifact=artifact, max_input_chars=100_000
    )
    assert result.candidate is not None
    assert result.candidate.review_state == "pending"
    assert result.receipt["reconsideration"]["submission"] == review.model_dump(mode="json")
    assert result.receipt["assessments"] == [assessment(review)]
    assert review.findings[0].critique in result.prompt
    assert result.receipt["prompt_sha256"] != artifact["build_receipt"]["prompt_sha256"]
    assert not c.validate_candidate_content_agreement(result.candidate, group=group)
    assert artifact == original


@pytest.mark.parametrize("change", ["digest", "claim", "reference"])
async def test_reconsideration_invalid_identity_prevents_extraction(group, local_model, change):
    artifact, review = await parent_and_review(group, local_model)
    if change == "digest":
        review = review.model_copy(update={"parent_candidate_sha256": "b" * 64})
    elif change == "claim":
        review.findings[0] = review.findings[0].model_copy(update={"claim_sha256": "b" * 64})
    else:
        review.findings[0] = review.findings[0].model_copy(
            update={"evidence_refs": [EvidenceRef(evidence_id="foreign")]}
        )
    before = len(local_model[1])
    with pytest.raises(ValueError):
        await c.propose_conditional_procedure(group, review=review, parent_artifact=artifact)
    assert len(local_model[1]) == before


async def test_reconsideration_budget_counts_critique_before_dispatch(group, local_model):
    artifact, review = await parent_and_review(group, local_model)
    review.findings[0] = review.findings[0].model_copy(update={"critique": "x" * 100_001})
    with pytest.raises(c.ConsolidationInputBudgetExceeded):
        await c.propose_conditional_procedure(
            group, review=review, parent_artifact=artifact, max_input_chars=100_000
        )


async def test_reconsideration_missing_assessment_retains_usage(group, local_model):
    artifact, review = await parent_and_review(group, local_model)
    local_model[0]["assessments"] = []
    with pytest.raises(ValueError) as caught:
        await c.propose_conditional_procedure(
            group, review=review, parent_artifact=artifact, max_input_chars=100_000
        )
    assert caught.value.extraction_usage.requests == 1


async def test_reconsideration_abstention_still_assesses_review(group, local_model):
    artifact, review = await parent_and_review(group, local_model)
    local_model[0].update(
        procedure=None, abstention_reason="Insufficient support", assessments=[assessment(review)]
    )
    result = await c.propose_conditional_procedure(
        group, review=review, parent_artifact=artifact, max_input_chars=100_000
    )
    assert result.candidate is None
    assert result.receipt["status"] == "abstained"
    assert result.receipt["assessments"] == [assessment(review)]


async def test_reconsideration_projected_evidence_uses_original_ids(local_model):
    from tests.test_episode_evidence import _contrast_group, _proposal

    group = _contrast_group()
    local_model[0].clear()
    local_model[0].update(outcome={"kind": "procedure", **_proposal()})
    artifact, review = await parent_and_review(group, local_model)
    review.findings[0] = review.findings[0].model_copy(
        update={"evidence_refs": [EvidenceRef(evidence_id="s0.e2")]}
    )
    checked = assessment(review)
    checked["evidence_refs"] = [{"evidence_id": "s0.e2"}]
    local_model[0]["assessments"] = [checked]
    result = await c.propose_conditional_procedure(
        group, review=review, parent_artifact=artifact, max_input_chars=100_000
    )
    assert result.candidate is not None
    assert result.receipt["projection"] == artifact["build_receipt"]["projection"]
    assert not c.validate_candidate_content_agreement(result.candidate, group=group)
    corrupted = copy.deepcopy(result.candidate)
    corrupted.metadata[c.METADATA_KEY]["build_receipt"]["assessments"][0]["explanation"] = "changed"
    assert c.validate_candidate_content_agreement(corrupted, group=group)


async def test_reconsideration_can_revisit_prior_candidate_without_recursive_artifacts(
    group, local_model
):
    artifact, review = await parent_and_review(group, local_model)
    local_model[0]["assessments"] = [assessment(review)]
    first = await c.propose_conditional_procedure(
        group, review=review, parent_artifact=artifact, max_input_chars=100_000
    )
    next_artifact = first.candidate.metadata[c.METADATA_KEY]
    next_review = review.model_copy(
        update={
            "parent_operation_id": "b" * 64,
            "parent_candidate_sha256": review_digest(next_artifact),
            "prior_review_id": review.submission_sha256,
        }
    )
    local_model[0]["assessments"] = [assessment(next_review)]
    second = await c.propose_conditional_procedure(
        group, review=next_review, parent_artifact=next_artifact, max_input_chars=100_000
    )
    assert not c.validate_candidate_content_agreement(second.candidate, group=group)
    assert set(second.receipt["reconsideration"]) == {"submission", "parent_procedure"}
    assert len(str(second.receipt)) < len(str(first.receipt)) + 100
