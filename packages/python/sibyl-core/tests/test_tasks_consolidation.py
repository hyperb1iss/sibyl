"""Synthetic proposal checks; no provider or database calls."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from sibyl_core.ai.errors import LLMError
from sibyl_core.ai.llm import Extractor, LLMSurface
from sibyl_core.ai.llm.budget import get_llm_budget_context, llm_budget_context
from sibyl_core.models.entities import Entity, EntityType, Procedure
from sibyl_core.services.graph_records import entity_from_surreal_row
from sibyl_core.services.memory_identity import reflection_entity_id
from sibyl_core.tasks import consolidation as c


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def group():
    episodes = []
    for name, status, content in (
        ("success", "passed", "Run scoped rebuild. Café is searchable.\n"),
        ("failure", "task_failed", "Global rebuild failed. Wrong project.\n"),
    ):
        episodes.append(
            c.ConsolidationEpisode(
                episode_id=name,
                session_id=f"session-{name}",
                family_id="index-recovery",
                split="learning",
                artifact=content.encode(),
                artifact_sha256=digest(content.encode()),
                environment={"runtime": "python-3.13", "repository": "sibyl"},
                stored_sources=(
                    c.StoredSourceRef(source_id=f"capture-{name}", observed_revision=7),
                ),
                outcome=c.DeclaredTaskOutcome(
                    receipt_schema_version="sibyl-agent-task-receipt-v1",
                    task_id=f"task-{name}",
                    attempt_id=f"attempt-{name}",
                    status=status,
                    success=status == "passed",
                    controller_final_snapshot_sha256="a" * 64,
                    checker_input_snapshot_sha256="a" * 64,
                    receipt_sha256=digest(f"receipt-{name}".encode()),
                ),
            )
        )
    return c.ConsolidationGroup(
        group_id="contrast-one",
        mechanism="scope before rebuild",
        organization_id="org-one",
        owner_principal_id="owner-one",
        memory_scope="project",
        scope_key="project-one",
        environment_compatibility_keys=("runtime", "repository"),
        episodes=tuple(episodes),
    )


@pytest.fixture
def procedure(group):
    def assertion(statement, episode="success", label="observed"):
        source = next(e for e in group.episodes if e.episode_id == episode)
        return c.ConditionalAssertion(
            statement=statement,
            label=label,
            support=[c.SupportRef(episode_id=episode, start_byte=0, end_byte=len(source.artifact))],
        )

    return c.DraftConditionalProcedure(
        goal=assertion("Make documents searchable"),
        environment=[assertion("The repository uses scoped indexing")],
        preconditions=[assertion("Resolve the intended project first", label="inferred")],
        required_tools=[assertion("Index CLI")],
        actions=[
            c.ConditionalAction(
                order=1,
                action=assertion("Run scoped rebuild"),
                success_criteria=assertion("Café is searchable"),
            )
        ],
        expected_result=assertion("Documents are searchable"),
        failure_modes=[assertion("Global rebuilding used the wrong project", "failure")],
        abstain_when=[assertion("The project cannot be resolved", "failure", "inferred")],
    )


@pytest.fixture
def model(monkeypatch):
    calls = []
    output = {}

    async def respond(messages, info):
        calls.append(messages)
        context = get_llm_budget_context()
        assert context.organization_id == "org-one"
        assert context.user_id == "owner-one"
        if callback := output.get("callback"):
            callback()
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    output["raw"]
                    if "raw" in output
                    else output["proposal"].model_dump(mode="json"),
                )
            ],
            usage=RequestUsage(input_tokens=321, output_tokens=123),
            model_name="local-proposal-fixture",
            provider_name="function",
        )

    async def local_agent(extractor):
        assert extractor.surface is LLMSurface.MEMORY
        assert extractor.output_retries == 0
        return Agent(
            FunctionModel(respond),
            output_type=c.ProcedureProposal,
            instructions=extractor.system_prompt,
            retries={"output": 0},
        )

    monkeypatch.setattr(Extractor, "_get_agent", local_agent)
    return output, calls


async def propose(group, procedure, model):
    model[0]["proposal"] = c.ProcedureProposal(procedure=procedure)
    return await c.propose_conditional_procedure(group)


def set_value(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("episodes", 0, "artifact_sha256"), "f" * 64),
        (("episodes", 0, "artifact"), b"\xff"),
        (("episodes", 0, "split"), "development"),
        (("episodes", 0, "outcome", "success"), False),
        (("episodes", 0, "outcome", "checker_input_snapshot_sha256"), "c" * 64),
        (("episodes", 0, "outcome", "receipt_sha256"), "not-a-hash"),
        (("episodes", 1, "episode_id"), "success"),
        (("episodes", 1, "session_id"), "session-success"),
        (("episodes", 1, "outcome", "attempt_id"), "attempt-success"),
        (("episodes", 1, "outcome", "receipt_sha256"), digest(b"receipt-success")),
        (("episodes", 0, "stored_sources"), ()),
        (
            ("episodes", 0, "stored_sources"),
            ({"source_id": "reflection:input:0123456789abcdef", "observed_revision": 1},),
        ),
        (("episodes", 0, "stored_sources"), ({"source_id": "capture", "observed_revision": None},)),
        (("episodes", 0, "stored_sources"), ({"source_id": "capture", "observed_revision": True},)),
        (("episodes", 1, "environment"), {"runtime": "python-3.12", "repository": "sibyl"}),
        (("episodes", 1, "environment"), {"runtime": "python-3.13"}),
        (("organization_id",), " "),
        (("scope_key",), None),
    ],
)
def test_invalid_frozen_groups_are_rejected(group, path, value):
    data = group.model_dump()
    set_value(data, path, value)
    with pytest.raises(ValueError):
        c.ConsolidationGroup.model_validate(data)


@pytest.mark.parametrize(
    "status",
    [
        "controller_failed",
        "controller_timeout",
        "checker_failed",
        "checker_protocol_invalid",
        "unsafe_snapshot",
        "controller_budget_exceeded",
    ],
)
def test_operational_failures_are_not_negative_task_evidence(group, status):
    data = group.model_dump()
    data["episodes"][1]["outcome"]["status"] = status
    with pytest.raises(ValidationError):
        c.ConsolidationGroup.model_validate(data)


@pytest.mark.parametrize("status", ["passed", "task_failed"])
def test_one_sided_groups_are_not_contrasts(group, status):
    data = group.model_dump()
    for episode in data["episodes"]:
        episode["outcome"].update(status=status, success=status == "passed")
    with pytest.raises(ValueError, match="both passed and task_failed"):
        c.ConsolidationGroup.model_validate(data)


async def test_invalid_inputs_and_budgets_never_call_the_model(group, model):
    corrupted = group.model_copy(deep=True)
    corrupted.episodes[0].environment.clear()
    for candidate, budget in [(corrupted, 40000), (group, 10), (group, 0), (group, True)]:
        with pytest.raises(ValueError):
            await c.propose_conditional_procedure(candidate, max_input_chars=budget)
    for tokens in (0, True):
        with pytest.raises(ValueError):
            await c.propose_conditional_procedure(group, max_tokens=tokens)
    assert model[1] == []


@pytest.mark.parametrize(
    "defect",
    [
        "outside",
        "past_end",
        "empty_range",
        "utf8",
        "whitespace",
        "negative_only_actions",
        "positive_only_failures",
        "noncontiguous",
    ],
)
async def test_unsupported_proposals_return_rejection_without_retry(
    group, procedure, model, defect
):
    data = procedure.model_dump()
    ref = data["goal"]["support"][0]
    if defect == "outside":
        ref["episode_id"] = "absent"
    elif defect == "past_end":
        ref["end_byte"] = 9999
    elif defect == "empty_range":
        ref.update(start_byte=1, end_byte=1)
    elif defect == "utf8":
        start = group.episodes[0].artifact.index("é".encode())
        ref.update(start_byte=start + 1, end_byte=start + 2)
    elif defect == "whitespace":
        end = len(group.episodes[0].artifact)
        ref.update(start_byte=end - 1, end_byte=end)
    elif defect == "negative_only_actions":
        data["actions"][0]["action"]["support"] = data["failure_modes"][0]["support"]
    elif defect == "positive_only_failures":
        data["failure_modes"][0]["support"] = data["goal"]["support"]
    else:
        data["actions"][0]["order"] = 2
    result = await propose(group, c.DraftConditionalProcedure.model_validate(data), model)
    assert result.candidate is None
    assert result.receipt["status"] == "rejected"
    assert result.receipt["structural"] == "fail"
    assert result.receipt["usage"]["input_tokens"] == 321
    assert len(model[1]) == 1


@pytest.mark.parametrize(
    "field", ["environment", "preconditions", "actions", "failure_modes", "abstain_when"]
)
def test_procedure_requires_every_applicability_section(procedure, field):
    data = procedure.model_dump()
    data[field] = []
    with pytest.raises(ValueError):
        c.DraftConditionalProcedure.model_validate(data)


def test_goal_is_grounded_and_empty_support_is_rejected(procedure):
    data = procedure.model_dump()
    data["goal"]["support"] = []
    with pytest.raises(ValueError):
        c.DraftConditionalProcedure.model_validate(data)


async def test_model_abstention_is_retained(group, model):
    model[0]["proposal"] = c.ProcedureProposal(abstention_reason="No supported reusable procedure")
    result = await c.propose_conditional_procedure(group)
    assert result.candidate is None
    assert result.receipt["status"] == "abstained"
    assert result.receipt["reason"] == model[0]["proposal"].abstention_reason
    assert len(model[1]) == 1


async def test_proposal_preserves_usage_bytes_sources_and_native_steps(group, procedure, model):
    result = await propose(group, procedure, model)
    candidate = result.candidate
    assert candidate.kind == "procedure"
    assert candidate.confidence == 0.0 and candidate.review_state == "pending"
    assert candidate.raw_source_ids == ["capture-failure", "capture-success"]
    assert result.receipt["outcome_join"] == "caller_declared"
    assert result.receipt["source_freshness"] == "not_checked"
    assert result.receipt["entailment"] == "pending"
    assert result.receipt["transfer"] == "not_measured"
    assert result.receipt["usage"]["cost_usd"] is None
    assert result.receipt["usage"]["cost_complete"] is False
    assert result.receipt["usage"]["requests"] == 1
    for key, value in {
        "input": group.model_dump(mode="json"),
        "prompt": {"system": c.SYSTEM_PROMPT, "user": result.prompt},
        "schema": c.ProcedureProposal.model_json_schema(),
        "output": result.proposal.model_dump(mode="json"),
    }.items():
        canonical = json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        assert result.receipt[f"{key}_sha256"] == digest(canonical)
    payload = candidate.metadata[c.METADATA_KEY]
    for span in payload["spans"]:
        episode = next(e for e in group.episodes if e.episode_id == span["episode_id"])
        assert span["slice_sha256"] == digest(
            episode.artifact[span["start_byte"] : span["end_byte"]]
        )
        assert span["artifact_sha256"] == episode.artifact_sha256
    assert "inferred" in candidate.content
    assert "capture-success" in candidate.content
    assert str(len(group.episodes[0].artifact)) in result.prompt
    assert c.validate_candidate_content_agreement(candidate, group=group) == []
    entity = Entity(
        id="proposal-one",
        entity_type=EntityType.PROCEDURE,
        name=candidate.title,
        content=candidate.content,
        organization_id=group.organization_id,
        metadata=candidate.metadata,
    )
    read = entity_from_surreal_row(entity.model_dump(mode="json"))
    assert isinstance(read, Procedure)
    assert read.steps[0].description == procedure.actions[0].action.statement
    assert read.steps[0].success_criteria == procedure.actions[0].success_criteria.statement
    assert read.required_tools == ["Index CLI"]
    assert get_llm_budget_context() is None


@pytest.mark.parametrize(
    "field",
    [
        "content",
        "steps",
        "payload",
        "source",
        "scope",
        "added_identity",
        "confidence",
        "review_state",
    ],
)
async def test_candidate_mirror_tampering_is_detected(group, procedure, model, field):
    candidate = (await propose(group, procedure, model)).candidate
    changed = copy.deepcopy(candidate)
    if field == "content":
        changed = replace(changed, content=changed.content + "unsupported instruction")
    elif field == "steps":
        changed.metadata["steps"][0]["description"] = "Use every project"
    elif field == "payload":
        changed.metadata[c.METADATA_KEY]["group"]["organization_id"] = "other-org"
    elif field == "source":
        changed = replace(changed, raw_source_ids=["capture-success"])
    elif field == "scope":
        changed = replace(changed, suggested_scope_key="other-project")
    elif field == "added_identity":
        changed.metadata["reflection_source"] = True
        changed.metadata["principal_id"] = "other-owner"
    elif field == "confidence":
        changed = replace(changed, confidence=1.0)
    else:
        changed = replace(changed, review_state="approved")
    assert c.validate_candidate_content_agreement(changed, group=group)


async def test_input_snapshot_survives_caller_mutation_during_extraction(group, procedure, model):
    frozen = group.model_copy(deep=True)
    model[0]["callback"] = lambda: group.episodes[0].environment.update(runtime="changed")
    result = await propose(group, procedure, model)
    assert c.validate_candidate_content_agreement(result.candidate, group=frozen) == []
    assert c.validate_candidate_content_agreement(result.candidate, group=result.group) == []
    assert (
        result.candidate.metadata[c.METADATA_KEY]["group"]["episodes"][0]["environment"]["runtime"]
        == "python-3.13"
    )


async def test_supported_fact_changes_native_identity(group, procedure, model):
    first = (await propose(group, procedure, model)).candidate
    repeated = (await propose(group, procedure, model)).candidate
    assert first.content == repeated.content
    data = procedure.model_dump()
    data["preconditions"][0]["statement"] = "Confirm project membership"
    second = (
        await propose(group, c.DraftConditionalProcedure.model_validate(data), model)
    ).candidate

    def entity(candidate):
        return Entity(
            id="draft",
            entity_type=EntityType.PROCEDURE,
            name=candidate.title,
            content=candidate.content,
            organization_id=group.organization_id,
            created_by=group.owner_principal_id,
            metadata=candidate.metadata,
        )

    assert reflection_entity_id(entity(first)) != reflection_entity_id(entity(second))


def test_matching_hash_does_not_make_invalid_utf8_evidence_valid(group):
    data = group.model_dump()
    data["episodes"][0].update(artifact=b"\xff", artifact_sha256=digest(b"\xff"))
    with pytest.raises(ValueError):
        c.ConsolidationGroup.model_validate(data)


def test_shared_source_requires_one_observed_revision(group):
    data = group.model_dump()
    data["episodes"][1]["stored_sources"] = (
        {"source_id": "capture-success", "observed_revision": 8},
    )
    with pytest.raises(ValueError, match="revision"):
        c.ConsolidationGroup.model_validate(data)


async def test_provider_failure_propagates_without_retry_or_task_outcome(group, model):
    def fail():
        raise RuntimeError("provider unavailable")

    model[0]["callback"] = fail
    with pytest.raises(LLMError):
        await c.propose_conditional_procedure(group)
    assert len(model[1]) == 1
    assert get_llm_budget_context() is None


@pytest.mark.parametrize("key", ["input", "prompt", "schema", "output"])
async def test_consistently_rewritten_receipt_hashes_fail_replay(group, procedure, model, key):
    result = await propose(group, procedure, model)
    bad = copy.deepcopy(result.receipt)
    bad[f"{key}_sha256"] = "f" * 64
    changed = c._candidate(group, procedure, bad)
    assert c.validate_candidate_content_agreement(changed, group=group)


async def test_returned_usage_edits_do_not_mutate_candidate(group, procedure, model):
    result = await propose(group, procedure, model)
    result.receipt["usage"]["cost_usd"] = 99
    assert result.candidate.metadata[c.METADATA_KEY]["build_receipt"]["usage"]["cost_usd"] is None
    assert c.validate_candidate_content_agreement(result.candidate, group=result.group) == []


async def test_active_budget_identity_cannot_be_replaced(group, procedure, model):
    with llm_budget_context(user_id="other-owner", organization_id="org-one"):
        with pytest.raises(ValueError, match="budget principal"):
            await c.propose_conditional_procedure(group)
        assert get_llm_budget_context().user_id == "other-owner"
    assert not model[1]
    with llm_budget_context(user_id="owner-one", organization_id="org-one"):
        result = await propose(group, procedure, model)
        assert result.candidate is not None
        assert get_llm_budget_context().user_id == "owner-one"


async def test_schema_invalid_provider_output_is_not_retried(group, model):
    model[0]["raw"] = {"procedure": None, "abstention_reason": None}
    with pytest.raises(LLMError):
        await c.propose_conditional_procedure(group)
    assert len(model[1]) == 1


async def test_declared_cross_family_contrast_can_include_a_preventive_action(
    group, procedure, model
):
    data = group.model_dump()
    data["episodes"][1]["family_id"] = "different-index-recovery"
    group = c.ConsolidationGroup.model_validate(data)
    draft = procedure.model_dump()
    preventive = copy.deepcopy(draft["failure_modes"][0])
    preventive.update(statement="Avoid global rebuilding", label="inferred")
    draft["actions"].append(
        {"order": 2, "action": preventive, "success_criteria": draft["expected_result"]}
    )
    result = await propose(group, c.DraftConditionalProcedure.model_validate(draft), model)
    assert result.candidate is not None
    assert result.receipt["entailment"] == "pending"
