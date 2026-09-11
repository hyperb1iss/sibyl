"""Progress critiques preserve partial repairs and immutable legacy identities."""

import json
from dataclasses import asdict

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.tasks.memory_progress import (
    PROGRESS_VERSION,
    ProgressCriticOutput,
    prepare_progress_validation,
)
from sibyl_core.tasks.memory_validation import (
    VALIDATION_INSTRUCTIONS,
    VALIDATION_VERSION,
    CriticOutput,
    prepare_reflection_validation,
    run_memory_validation,
)
from sibyl_core.tasks.procedure_review import ReviewSubmission
from tests.test_memory_validation import candidate as candidate
from tests.test_memory_validation import citations as citations
from tests.test_memory_validation import finding
from tests.test_memory_validation import prepared as prepared
from tests.test_memory_validation import sources as sources
from tests.test_tasks_consolidation import group as group
from tests.test_tasks_consolidation import procedure as procedure


@pytest.fixture
def progress(prepared, sources, citations):
    prepared = prepare_reflection_validation(
        ReflectionCandidate(
            "pattern",
            "Validation",
            "All ten cases failed, so retries always work.",
            "contrast",
            0.9,
        ),
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
        evidence=sources,
        citations=citations,
    )
    current = prepare_reflection_validation(
        ReflectionCandidate(
            "pattern", "Validation", "Three cases passed, so retries always work.", "contrast", 0.9
        ),
        parent_operation_id="d" * 64,
        parent_candidate_sha256="e" * 64,
        evidence=sources,
        citations=citations,
    )
    payload = json.loads(prepared.payload_json)
    concern = finding(prepared)
    concern["critique"] = "The reported count is three passed, and retry causality is unsupported."
    review = ReviewSubmission(
        parent_operation_id=payload["parent_operation_id"],
        parent_candidate_sha256=payload["parent_candidate_sha256"],
        findings=[concern],
    )
    return prepare_progress_validation(prepared, current, review)


def output(prepared, disposition="partially_resolved"):
    payload = json.loads(prepared.payload_json)
    return {
        "findings": [finding(prepared)],
        "prior_assessments": [
            {
                "finding_id": payload["prior_progress"]["finding_ids"][0],
                "disposition": disposition,
                "supported_reduction": "The count now matches three passed."
                if disposition in ("resolved", "partially_resolved")
                else None,
                "remaining_concern": None
                if disposition == "resolved"
                else "Retry causality remains unsupported.",
                "evidence_refs": [{"evidence_id": "observed"}],
            }
        ],
    }


async def execute(prepared, value):
    extractor = Extractor(
        ProgressCriticOutput,
        agent=Agent(TestModel(custom_output_args=value), output_type=ProgressCriticOutput),
    )
    return await run_memory_validation(prepared, extractor)


async def test_progress_partial_compound_content_can_advance(progress):
    result = await execute(progress, output(progress))
    assert result.status == "reconsider" and result.progress == "advance"
    assert result.prior_assessments[0].disposition == "partially_resolved"
    assert result.usage.requests == 1 and result.version == PROGRESS_VERSION


async def test_progress_remaining_old_finding_is_not_automatic_no_progress(progress):
    result = await execute(progress, output(progress, "unresolved"))
    assert result.progress == "unresolved" and result.status == "reconsider"


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "finding", "evidence", "reduction", "remaining", "accepted"]
)
async def test_progress_mechanical_refusal_preserves_usage(progress, change):
    value = output(progress)
    if change == "missing":
        value["prior_assessments"] = []
    elif change == "duplicate":
        value["prior_assessments"] *= 2
    elif change == "finding":
        value["prior_assessments"][0]["finding_id"] = "a" * 64
    elif change == "evidence":
        value["prior_assessments"][0]["evidence_refs"] = [{"evidence_id": "invented"}]
    elif change == "reduction":
        value["prior_assessments"][0]["supported_reduction"] = None
    elif change == "remaining":
        value["prior_assessments"][0]["remaining_concern"] = None
    elif change == "accepted":
        value["findings"] = []
    result = await execute(progress, value)
    assert result.status == "abstain" and result.progress == "abstain"
    assert (
        result.reason == "critic_output_failed_mechanical_validation" and result.usage.requests == 1
    )


async def test_progress_no_findings_requires_complete_resolved_assessments(progress):
    value = output(progress, "resolved")
    value["findings"] = []
    result = await execute(progress, value)
    assert result.status == "no_findings" and result.progress == "accepted"


async def test_progress_unchanged_candidate_cannot_advance(prepared):
    p = json.loads(prepared.payload_json)
    review = ReviewSubmission(
        parent_operation_id=p["parent_operation_id"],
        parent_candidate_sha256=p["parent_candidate_sha256"],
        findings=[finding(prepared)],
    )
    current = prepare_progress_validation(prepared, prepared, review)
    result = await execute(current, output(current))
    assert result.progress == "no_progress"


async def test_progress_ancestor_oscillation_cannot_advance(progress):
    from sibyl_core.tasks.memory_progress import semantic_candidate_digest
    from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

    p = json.loads(progress.payload_json)
    p["prior_progress"]["ancestor_candidate_digests"] = [semantic_candidate_digest(p)]
    current = PreparedMemoryValidation(json.dumps(p))
    assert (await execute(current, output(current))).progress == "no_progress"


def test_progress_context_detaches_and_binds_prior_identity(prepared, progress):
    assert progress.input_sha256 != prepared.input_sha256
    assert (
        json.loads(progress.payload_json)["prior_progress"]["previous_parent_candidate_sha256"]
        == "c" * 64
    )
    assert json.loads(progress.payload_json)["parent_candidate_sha256"] == "e" * 64
    assert "partially repaired" in progress.prompt


async def test_progress_legacy_prompt_schema_and_result_remain_v1(prepared):
    assert prepared.prompt == VALIDATION_INSTRUCTIONS + "\n\n" + prepared.payload_json
    extractor = Extractor(
        CriticOutput,
        agent=Agent(TestModel(custom_output_args={"findings": []}), output_type=CriticOutput),
    )
    result = await run_memory_validation(prepared, extractor)
    assert result.version == VALIDATION_VERSION
    assert set(asdict(result)) == {
        "status",
        "submission",
        "reason",
        "input_sha256",
        "schema_sha256",
        "usage",
        "configured_policy_json",
        "version",
    }
    assert "prior_assessments" not in CriticOutput.model_json_schema()["properties"]


@pytest.mark.parametrize("change", ["parent", "claim", "source", "observation", "citation"])
def test_progress_rejects_changed_prior_binding(prepared, progress, change):
    from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

    before = json.loads(prepared.payload_json)
    after = json.loads(progress.payload_json)
    item = finding(prepared)
    operation = before["parent_operation_id"]
    if change == "parent":
        operation = "f" * 64
    elif change == "claim":
        item["claim_sha256"] = "f" * 64
    elif change == "source":
        after["sources"]["raw"]["sha256"] = "f" * 64
    elif change == "observation":
        after["sources"]["raw"]["observation_sha256"] = "f" * 64
    else:
        after["citations"]["observed"]["ranges"] = [[0, 1]]
    review = ReviewSubmission(
        parent_operation_id=operation,
        parent_candidate_sha256=before["parent_candidate_sha256"],
        findings=[item],
    )
    with pytest.raises(ValueError):
        prepare_progress_validation(prepared, PreparedMemoryValidation(json.dumps(after)), review)


async def test_progress_actual_extractor_detaches_caller_review(prepared, progress, monkeypatch):
    before = json.loads(prepared.payload_json)
    review = ReviewSubmission(
        parent_operation_id=before["parent_operation_id"],
        parent_candidate_sha256=before["parent_candidate_sha256"],
        findings=[finding(prepared)],
    )
    current = prepare_progress_validation(prepared, progress, review)
    digest = current.input_sha256
    method = Extractor.extract_with_usage

    async def mutate(extractor, *args, **kwargs):
        review.findings.clear()
        return await method(extractor, *args, **kwargs)

    monkeypatch.setattr(Extractor, "extract_with_usage", mutate)
    result = await execute(current, output(current))
    assert result.progress == "advance" and result.input_sha256 == digest
    assert len(result.prior_assessments) == 1


async def test_progress_post_return_failure_retains_actual_usage(progress, monkeypatch):
    import sibyl_core.tasks.memory_progress as module

    error = RuntimeError("progress bookkeeping failed")

    def fail(*args):
        raise error

    monkeypatch.setattr(module, "assess_progress", fail)
    with pytest.raises(RuntimeError) as caught:
        await execute(progress, output(progress))
    assert caught.value is error and error.extraction_usage.requests == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_progress_extraction_failure_keeps_original_exception(progress, monkeypatch, cancel):
    import asyncio

    error = asyncio.CancelledError() if cancel else RuntimeError("physical outcome unknown")
    error.extraction_usage = {"requests": 1, "transport_usage_complete": False}

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(Extractor, "extract_with_usage", fail)
    with pytest.raises(type(error)) as caught:
        await execute(progress, output(progress))
    assert caught.value is error and error.extraction_usage["requests"] == 1


@pytest.mark.parametrize("change", ["missing", "finding_ids", "parent", "assertion", "evidence"])
async def test_progress_context_tamper_denied_before_extraction(progress, monkeypatch, change):
    from unittest.mock import AsyncMock

    from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

    payload = json.loads(progress.payload_json)
    context = payload["prior_progress"]
    if change == "missing":
        del payload["prior_progress"]
    elif change == "finding_ids":
        context["finding_ids"] = ["f" * 64]
    elif change == "parent":
        context["previous_parent_operation_id"] = "f" * 64
    elif change == "assertion":
        context["previous_assertions"]["/content"]["statement"] = "changed"
    else:
        payload["sources"]["raw"]["observation_sha256"] = "f" * 64
    call = AsyncMock(side_effect=AssertionError("unexpected physical extraction"))
    monkeypatch.setattr(Extractor, "extract_with_usage", call)
    with pytest.raises(ValueError):
        await execute(PreparedMemoryValidation(json.dumps(payload)), output(progress))
    call.assert_not_awaited()


async def test_progress_signed_adapter_preserves_original_hashes_and_claim_view(procedure, group):
    from sibyl_core.tasks.episode_evidence import EvidenceCitation
    from sibyl_core.tasks.memory_validation import (
        OriginalValidationEvidence,
        prepare_procedure_validation,
    )
    from sibyl_core.tasks.procedure_review import review_digest

    source_group = group
    parent = procedure
    sources = [
        OriginalValidationEvidence(e.episode_id, e.artifact, "a" * 64, "signed")
        for e in source_group.episodes
    ]
    refs = {
        e.episode_id: EvidenceCitation(e.episode_id, ((0, len(e.artifact)),))
        for e in source_group.episodes
    }
    before = prepare_procedure_validation(
        parent,
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
        evidence=sources,
        citations=refs,
    )
    child = parent.model_copy(
        update={"goal": parent.goal.model_copy(update={"statement": "Narrower source-bound goal"})},
        deep=True,
    )
    after = prepare_procedure_validation(
        child,
        parent_operation_id="d" * 64,
        parent_candidate_sha256="e" * 64,
        evidence=sources,
        citations=refs,
    )
    b = json.loads(before.payload_json)
    prior = {
        "claim_path": "/goal",
        "claim_sha256": b["assertion_hashes"]["/goal"],
        "evidence_refs": [{"evidence_id": sources[0].source_id}],
        "basis": "unsupported_generalization",
        "disposition": "qualify",
        "critique": "Narrow the goal.",
    }
    review = ReviewSubmission(
        parent_operation_id="b" * 64, parent_candidate_sha256="c" * 64, findings=[prior]
    )
    prepared = prepare_progress_validation(before, after, review)
    payload = json.loads(prepared.payload_json)
    assert payload["sources"] == b["sources"]
    assert payload["candidate_view_sha256"] == review_digest(child.model_dump(mode="json"))
    assert payload["parent_candidate_sha256"] != payload["candidate_view_sha256"]
    result = await execute(
        prepared,
        {
            "findings": [],
            "prior_assessments": [
                {
                    "finding_id": review.finding_ids()[0],
                    "disposition": "resolved",
                    "supported_reduction": "The goal is narrower.",
                    "remaining_concern": None,
                    "evidence_refs": [{"evidence_id": sources[0].source_id}],
                }
            ],
        },
    )
    assert result.progress == "accepted"


@pytest.mark.parametrize(
    "change",
    ["missing_view", "missing_input", "invalid_input", "different_view", "different_view_rehashed"],
)
async def test_progress_previous_view_integrity_before_dispatch(progress, monkeypatch, change):
    from unittest.mock import AsyncMock

    from sibyl_core.tasks.memory_validation import PreparedMemoryValidation
    from sibyl_core.tasks.procedure_review import review_digest

    payload = json.loads(progress.payload_json)
    context = payload["prior_progress"]
    if change == "missing_view":
        del context["previous_candidate"]
    elif change == "missing_input":
        del context["previous_input_sha256"]
    elif change == "invalid_input":
        context["previous_input_sha256"] = "not-a-digest"
    else:
        context["previous_candidate"]["content"] = "An unrelated previous assertion."
        if change == "different_view_rehashed":
            context["previous_candidate_view_sha256"] = review_digest(context["previous_candidate"])
    call = AsyncMock(side_effect=AssertionError("unexpected physical extraction"))
    monkeypatch.setattr(Extractor, "extract_with_usage", call)
    with pytest.raises(ValueError):
        await execute(PreparedMemoryValidation(json.dumps(payload)), output(progress))
    call.assert_not_awaited()


async def test_progress_false_positive_can_resolve_without_candidate_rewrite(sources, citations):
    prepared = prepare_reflection_validation(
        ReflectionCandidate(
            "pattern", "Validation", "The report says three cases passed.", "contrast", 0.9
        ),
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
        evidence=sources,
        citations=citations,
    )
    before = json.loads(prepared.payload_json)
    review = ReviewSubmission(
        parent_operation_id=before["parent_operation_id"],
        parent_candidate_sha256=before["parent_candidate_sha256"],
        findings=[finding(prepared)],
    )
    unchanged = prepare_progress_validation(prepared, prepared, review)
    value = output(unchanged, "resolved")
    value["findings"] = []
    value["prior_assessments"][0]["supported_reduction"] = (
        "Original evidence refutes the previous criticism."
    )
    result = await execute(unchanged, value)
    assert result.status == "no_findings" and result.progress == "accepted"
