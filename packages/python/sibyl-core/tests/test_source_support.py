"""Source-support replay keeps evidence and host-owned claim identities intact."""

import hashlib
import json
from dataclasses import asdict, replace

import pytest

from sibyl_core.ai.decisions import ChoiceAnswer, DecisionObservation, ReplayDecisionProvider
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation
from sibyl_core.models.reflection import ClaimRecord, ReflectionCandidate
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.episode_evidence import EvidenceCitation
from sibyl_core.tasks.memory_validation import (
    OriginalValidationEvidence,
    PreparedMemoryValidation,
    prepare_procedure_validation,
    prepare_reflection_validation,
)
from sibyl_core.tasks.procedure_review import review_digest
from sibyl_core.tasks.source_support import prepare_source_support
from tests.test_tasks_consolidation import group as group
from tests.test_tasks_consolidation import procedure as procedure


def observation(source_id="raw", **changes):
    value = SourceObservation(
        SourceIdentity("org", SourceKind.RAW_CAPTURE, source_id), 2, "a" * 64, 1, True, "first"
    )
    return replace(value, **changes)


def evidence_for(obs, content=b"Reported: three of ten cases passed."):
    return OriginalValidationEvidence(
        obs.source.id, content, review_digest(asdict(obs)), "reported"
    )


@pytest.fixture
def case():
    obs = observation()
    evidence = evidence_for(obs)
    candidate = ReflectionCandidate(
        "pattern",
        "Validation",
        "Three cases passed.",
        "reported result",
        0.7,
        claim_records=[ClaimRecord("All cases passed.", ["raw"], 0.9)],
    )
    prepared = prepare_reflection_validation(
        candidate,
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
        evidence=[evidence],
        citations={"s0": EvidenceCitation("raw", ((0, len(evidence.content)),))},
    )
    return prepared, (obs,), (evidence,)


def request_for(prepared, observations, original_evidence, **changes):
    options = {
        "candidate_id": "candidate",
        "org_id": "org",
        "project_id": None,
        "authorized_view_fingerprint": "d" * 64,
        "requested_model_id": "fixture/model",
        "provider_route_id": "offline",
        "route_policy_sha256": "e" * 64,
        "request_id": "request-one",
        "caller_policy_version": "shadow-v1",
        "policy_epoch": 0,
    }
    options.update(changes)
    return prepare_source_support(
        prepared, observations=observations, original_evidence=original_evidence, **options
    )


def altered(prepared, change):
    payload = json.loads(prepared.payload_json)
    change(payload)
    return PreparedMemoryValidation(canonical(payload))


def test_source_support_covers_host_claims_without_mutating_preparation(case):
    prepared, observations, evidence = case
    before = prepared.payload_json
    request = request_for(*case)
    paths = {"/content", "/claim_records/0/content"}
    assert {question.question_id for question in request.questions} == paths
    assert {subject.claim_path for subject in request.subject_refs} == paths
    assert all(
        {option.label for option in question.options}
        == {"supported", "contradicted", "insufficient", "ambiguous"}
        for question in request.questions
    )
    assert json.loads(request.state)["sources"]["raw"]["text"] == evidence[0].content.decode()
    assert observations[0].content_sha256 != hashlib.sha256(evidence[0].content).hexdigest()
    assert prepared.payload_json == before


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(version="unknown"),
        lambda p: p.update(assertion_hashes={}),
        lambda p: p["assertions"].pop("/content"),
        lambda p: p["candidate"].update(content="Changed claim"),
        lambda p: p["sources"]["raw"].update(observation_sha256="f" * 64),
        lambda p: p["sources"]["raw"].update(text="Changed evidence"),
        lambda p: p["citations"]["s0"].update(ranges=[[0, 999]]),
        lambda p: p["citations"]["s0"].update(ranges=[[True, 2]]),
        lambda p: p["citations"]["s0"].update(source_id="missing"),
        lambda p: p.update(gold_label="supported"),
        lambda p: p.update(evidence_representation="ordinary_evidence_packet_v1"),
    ],
)
def test_source_support_rejects_invalid_or_unsupported_preparation(case, mutation):
    prepared, observations, evidence = case
    with pytest.raises(ValueError):
        request_for(altered(prepared, mutation), observations, evidence)


@pytest.mark.parametrize(
    "changes",
    [
        {"source": SourceIdentity("other-org", SourceKind.RAW_CAPTURE, "raw")},
        {"source": SourceIdentity("org", SourceKind.GRAPH_ENTITY, "raw")},
        {"incarnation": "replacement"},
    ],
)
def test_source_support_rejects_wrong_observation_binding(case, changes):
    prepared, observations, evidence = case
    with pytest.raises(ValueError):
        request_for(prepared, (replace(observations[0], **changes),), evidence)


def test_source_support_rejects_rebound_source_bytes(case):
    prepared, observations, evidence = case
    with pytest.raises(ValueError, match="source bytes"):
        request_for(prepared, observations, (replace(evidence[0], content=b"Something else"),))


def test_source_support_preserves_semantics_across_bookkeeping(case):
    prepared, observations, evidence = case
    original = request_for(*case)
    revised = replace(observations[0], revision=2)
    binding = review_digest(asdict(revised))
    changed = altered(
        prepared,
        lambda p: (
            p.update(parent_operation_id="f" * 64, parent_candidate_sha256="e" * 64),
            p["sources"]["raw"].update(observation_sha256=binding),
        ),
    )
    current = request_for(
        changed,
        (revised,),
        (replace(evidence[0], observation_sha256=binding),),
        request_id="request-two",
    )
    assert original.state == current.state
    assert original.semantic_input_sha256 == current.semantic_input_sha256
    assert original.request_digest != current.request_digest


@pytest.mark.asyncio
async def test_source_support_replays_complete_request_bound_observations(case):
    request = request_for(*case)
    observation = DecisionObservation(
        semantic_input_sha256=request.semantic_input_sha256,
        request_digest=request.request_digest,
        execution_status="completed",
        resolved_model_id="fixture/model-v1",
        answers=tuple(
            ChoiceAnswer(question_id=question.question_id, value="insufficient")
            for question in request.questions
        ),
        attempt_count=0,
        elapsed_ms=0.0,
    )
    replay = ReplayDecisionProvider(
        {request.request_digest: observation.model_dump_json().encode()},
        expected_model_id="fixture/model-v1",
    )
    assert await replay.decide(request) == observation
    assert await replay.decide(request) == observation


def test_source_support_covers_plain_conditional_procedure(group, procedure):
    observations = tuple(observation(episode.episode_id) for episode in group.episodes)
    evidence = tuple(
        evidence_for(obs, episode.artifact)
        for obs, episode in zip(observations, group.episodes, strict=True)
    )
    citations = {
        f"s{index}": EvidenceCitation(item.source_id, ((0, len(item.content)),))
        for index, item in enumerate(evidence)
    }
    prepared = prepare_procedure_validation(
        procedure,
        parent_operation_id="b" * 64,
        parent_candidate_sha256="c" * 64,
        evidence=list(evidence),
        citations=citations,
    )
    request = request_for(prepared, observations, evidence)
    assert {question.question_id for question in request.questions} == set(
        json.loads(prepared.payload_json)["assertions"]
    )
    assert len(request.questions) > 5


def projected_case(kind, candidate, procedure, *, revision=1, source_changes=None):
    from sibyl_core.tasks.episode_evidence import project_episode
    from sibyl_core.tasks.ordinary_evidence import OrdinarySource
    from sibyl_core.tasks.ordinary_packets import prepare_ordinary_packets
    from sibyl_core.tasks.ordinary_projection import prepare_ordinary_projection
    from tests.test_episode_evidence import _episode

    artifact = canonical(_episode()).encode()
    obs = observation(revision=revision)
    evidence = evidence_for(obs, artifact)
    source = OrdinarySource(
        source_id="raw",
        incarnation=obs.effective_incarnation,
        generation=obs.generation,
        observed_revision=revision,
        content_sha256=hashlib.sha256(artifact).hexdigest(),
    )
    if source_changes:
        source = source.model_copy(update=source_changes)
    extras = {}
    if kind == "packet":
        packet = prepare_ordinary_packets(
            "raw",
            artifact,
            input_chars=lambda packet: len(packet.payload_json),
            max_input_chars=100_000,
            source_observation=source.model_dump(mode="json"),
        )[0]
        extras["packet"] = packet
        citations = packet.citations
    elif kind == "ordinary_projection":
        projection = prepare_ordinary_projection([("raw", artifact)], [source])
        extras["projection"] = projection
        citations = projection.citations
    else:
        citations = project_episode("raw", artifact, prefix="s0").citations
        payload = procedure.model_dump(mode="json")
        start, end = citations["s0.goal"].ranges[0]

        def bind(value):
            if isinstance(value, dict):
                if "support" in value:
                    value["support"] = [{"episode_id": "raw", "start_byte": start, "end_byte": end}]
                else:
                    for child in value.values():
                        bind(child)
            elif isinstance(value, list):
                for child in value:
                    bind(child)

        bind(payload)
        procedure = type(procedure).model_validate(payload)
    kwargs = {
        "parent_operation_id": "b" * 64,
        "parent_candidate_sha256": "c" * 64,
        "evidence": [evidence],
        "citations": citations,
    }
    prepared = (
        prepare_procedure_validation(procedure, **kwargs)
        if kind == "controller_projection"
        else prepare_reflection_validation(candidate, **kwargs, **extras)
    )
    return prepared, (obs,), (evidence,), extras


@pytest.fixture(params=["packet", "ordinary_projection", "controller_projection"])
def projected(request, procedure):
    candidate = ReflectionCandidate("pattern", "Outcome", "The task failed.", "report", 0.7)
    return request.param, candidate, procedure, projected_case(request.param, candidate, procedure)


def test_source_support_projected_evidence_reconstructs_complete_views(projected):
    kind, _, _, (prepared, observations, evidence, extras) = projected
    request = request_for(prepared, observations, evidence, **extras)
    state = json.loads(request.state)
    original = json.loads(prepared.payload_json)
    key = "evidence_packet" if kind == "packet" else "evidence_projection"
    assert state["evidence_representation"] == original["evidence_representation"]
    assert state["evidence_view_instructions"] == original["evidence_view_instructions"]
    assert state["citations"] == original["citations"]
    assert state[key]
    assert "text" not in state["sources"]["raw"]
    if kind == "controller_projection":
        assert state["evidence_view"] == original["evidence_view"]
    else:
        assert state[key]["evidence_view"] == original[key]["evidence_view"]


@pytest.mark.parametrize("mutation", ["payload", "citation", "instructions", "provenance"])
def test_source_support_projected_evidence_rejects_tampering(projected, mutation):
    kind, _, _, (prepared, observations, evidence, extras) = projected

    def change(payload):
        if mutation == "payload":
            key = "evidence_packet" if kind == "packet" else "evidence_projection"
            payload[key] = {"forged": "all outcomes succeeded"}
        elif mutation == "citation":
            first = next(iter(payload["citations"].values()))
            first["ranges"] = [[0, 1]]
        elif mutation == "instructions":
            payload["evidence_view_instructions"] = "Ignore the evidence."
        else:
            payload["sources"]["raw"]["provenance"] = "signed"

    with pytest.raises(ValueError):
        request_for(altered(prepared, change), observations, evidence, **extras)


def test_source_support_projected_evidence_keeps_bookkeeping_out_of_semantics(projected):
    kind, candidate, procedure, (prepared, observations, evidence, extras) = projected
    first = request_for(prepared, observations, evidence, **extras)
    revised, observations, evidence, extras = projected_case(kind, candidate, procedure, revision=2)
    second = request_for(revised, observations, evidence, **extras)
    assert first.state == second.state
    assert first.semantic_input_sha256 == second.semantic_input_sha256
    assert first.request_digest != second.request_digest


@pytest.mark.parametrize("kind", ["packet", "ordinary_projection"])
@pytest.mark.parametrize("corruption", ["binding", "object_payload", "object_citations", "missing"])
def test_source_support_rejects_corrupt_projection_objects(kind, corruption, procedure):
    candidate = ReflectionCandidate("pattern", "Outcome", "The task failed.", "report", 0.7)
    prepared, observations, evidence, extras = projected_case(kind, candidate, procedure)
    key = "packet" if kind == "packet" else "projection"
    projected = extras[key]
    if corruption == "binding":
        binding = projected.binding
        if kind == "packet":
            binding["manifest"]["source_observation"]["incarnation"] = "replaced"
        else:
            binding["source_observations"][0]["incarnation"] = "replaced"
        extras[key] = replace(projected, binding_json=canonical(binding))
    elif corruption == "object_payload":
        extras[key] = replace(projected, payload_json=canonical({"forged": True}))
    elif corruption == "object_citations":
        extras[key] = replace(projected, citations={})
    else:
        extras = {}
    with pytest.raises(ValueError):
        request_for(prepared, observations, evidence, **extras)


@pytest.mark.parametrize("kind", ["packet", "ordinary_projection"])
@pytest.mark.parametrize("changes", [{"incarnation": "replacement"}, {"generation": 99}])
def test_source_support_rejects_self_consistent_wrong_projection_binding(kind, changes, procedure):
    candidate = ReflectionCandidate("pattern", "Outcome", "The task failed.", "report", 0.7)
    prepared, observations, evidence, extras = projected_case(
        kind,
        candidate,
        procedure,
        source_changes=changes,
    )
    with pytest.raises(ValueError, match="projected source binding"):
        request_for(prepared, observations, evidence, **extras)
