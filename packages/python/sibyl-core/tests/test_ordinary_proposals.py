"""Partial ordinary knowledge preserves missing coverage and original evidence."""

import json
from dataclasses import replace

import pytest

from sibyl_core.tasks import consolidation as c
from sibyl_core.tasks import ordinary_evidence as o
from sibyl_core.tasks import ordinary_proposals as p
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.episode_evidence import EvidenceCitation
from sibyl_core.tasks.memory_validation import (
    OriginalValidationEvidence,
    prepare_reflection_validation,
)
from sibyl_core.tasks.ordinary_projection import prepare_ordinary_projection
from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest
from sibyl_core.tasks.reflection_correction import prepare_reflection_correction
from tests.test_tasks_consolidation import group as group


def episode(name="first", text=None, environment=None):
    body = (text or f"{name}: inspect the logs before changing config").encode()
    return p.PartialEpisode(
        episode_id=name,
        artifact=body,
        source=o.OrdinarySource(
            source_id=f"capture-{name}",
            incarnation=f"inc-{name}",
            generation=1,
            observed_revision=1,
            content_sha256=c._digest(body),
        ),
        environment=environment or {},
    )


def cohort(*episodes, keys=()):
    return p.PartialCohort(
        group_id="group",
        mechanism="diagnose config",
        organization_id="org",
        owner_principal_id="owner",
        memory_scope="private",
        episodes=episodes,
        environment_compatibility_keys=keys,
    )


def assertion(text="Inspect logs", **ref):
    return c.ConditionalAssertion(
        statement=text,
        label="inferred",
        support=[c.SupportRef(episode_id="first", start_byte=0, end_byte=5, **ref)],
    )


def proposal(kind="pattern"):
    return p.PartialProposal(
        procedure=p.PartialProcedure(
            kind=kind, goal=assertion(), actions=[p.PartialAction(order=1, action=assertion())]
        )
    )


def test_partial_ordinary_freeform_needs_no_json_or_environment():
    prepared = p.prepare_partial_proposal(cohort(episode(), episode("second")))
    assert prepared.output_type is p.PartialProposal
    assert "first: inspect the logs" in prepared.prompt
    assert "second: inspect the logs" in prepared.prompt
    value = json.loads(prepared.input_json)
    assert value["episodes"][0]["outcome"] == {
        "basis": "source_report",
        "source": None,
        "value": None,
    }
    candidate = prepared.render(proposal())
    assert candidate and candidate.kind == "pattern" and candidate.review_state == "pending"
    assert candidate.raw_source_ids == ["capture-first", "capture-second"]
    assert p.QUALIFICATION in candidate.content
    assert "Environment compatibility: unknown" in candidate.content
    receipt = candidate.metadata["ordinary_proposal_receipt"]
    assert "expected_result" in receipt["unspecified_fields"]
    assert {span["path"] for span in receipt["spans"]} == {"/goal", "/actions/0/action"}
    assert "admission" not in receipt and "eval_consolidation" not in candidate.metadata


def test_partial_ordinary_actual_shared_critic_sees_all_qualifiers():
    group = cohort(episode(), episode("second"))
    candidate = p.prepare_partial_proposal(group).render(proposal("procedure"))
    assert candidate
    prepared = prepare_reflection_validation(
        candidate,
        parent_operation_id="b" * 64,
        parent_candidate_sha256=review_digest(candidate.to_dict()),
        evidence=[
            OriginalValidationEvidence(e.source.source_id, e.artifact, "a" * 64, "reported")
            for e in group.episodes
        ],
        citations={
            f"s{i}": EvidenceCitation(episode_id=e.source.source_id, ranges=((0, len(e.artifact)),))
            for i, e in enumerate(group.episodes)
        },
    )
    payload = json.loads(prepared.payload_json)
    assert p.QUALIFICATION in payload["assertions"]["/content"]["statement"]
    assert "actions/0/success_criteria" in payload["candidate"]["content"]
    assert payload["kind"] == "reflection"
    submission = ReviewSubmission.model_validate(
        {
            "parent_operation_id": payload["parent_operation_id"],
            "parent_candidate_sha256": payload["parent_candidate_sha256"],
            "findings": [
                {
                    "claim_path": "/content",
                    "claim_sha256": review_digest(payload["assertions"]["/content"]),
                    "evidence_refs": [{"evidence_id": "s0"}],
                    "basis": "missing_condition",
                    "disposition": "qualify",
                    "critique": "Retain the evidence limitations",
                }
            ],
        }
    )
    correction = prepare_reflection_correction(prepared, submission)
    assert p.QUALIFICATION in correction
    assert "retain supported qualifications" in correction
    assert "unknown" in correction


@pytest.mark.parametrize("keys", [(), ("runtime",)])
def test_partial_ordinary_conflicts_cannot_hide_in_unselected_keys(keys):
    def fact(value):
        return {
            "runtime": o.EnvironmentFact(
                value=value, source=o.SourceBytes(start_byte=0, end_byte=len(value))
            )
        }

    with pytest.raises(ValueError, match=r"incompatible|conflicting"):
        cohort(
            episode(text="python3 first", environment=fact("python3")),
            episode("second", text="python4 second", environment=fact("python4")),
            keys=keys,
        )


def test_partial_ordinary_partial_coverage_is_unknown_not_compatibility():
    fact = {
        "runtime": o.EnvironmentFact(
            value="python3", source=o.SourceBytes(start_byte=0, end_byte=7)
        )
    }
    group = cohort(episode(text="python3 first", environment=fact), episode("second"))
    assert group.common_environment_keys == ()
    with pytest.raises(ValueError, match="missing compatible"):
        cohort(*group.episodes, keys=("runtime",))
    other = episode("second", text="python3 second", environment=fact)
    assert cohort(group.episodes[0], other).common_environment_keys == ("runtime",)


@pytest.mark.parametrize("mutation", ["source", "bytes", "copied", "scope", "spoof"])
def test_partial_ordinary_rejects_invalid_input(mutation):
    data = cohort(episode(), episode("second")).model_dump()
    if mutation == "source":
        data["episodes"][1]["source"]["source_id"] = "capture-first"
    if mutation == "bytes":
        data["episodes"][0]["artifact"] = b"changed"
    if mutation == "copied":
        data["episodes"][1]["artifact"] = data["episodes"][0]["artifact"]
        data["episodes"][1]["source"]["content_sha256"] = data["episodes"][0]["source"][
            "content_sha256"
        ]
    if mutation == "scope":
        data["memory_scope"] = "project"
    if mutation == "spoof":
        data["episodes"][0]["outcome"]["basis"] = "signed"
    with pytest.raises(ValueError):
        p.PartialCohort.model_validate(data)


@pytest.mark.parametrize("mutation", ["empty", "foreign", "bounds", "whitespace", "unicode"])
def test_partial_ordinary_support_stays_nonempty_original_bytes(mutation):
    text = "  é log" if mutation in {"whitespace", "unicode"} else None
    prepared = p.prepare_partial_proposal(cohort(episode(text=text), episode("second")))
    value = proposal()
    ref = value.procedure.goal.support[0]
    bad = {
        "empty": {"start_byte": 1, "end_byte": 1},
        "foreign": {"episode_id": "alien"},
        "bounds": {"end_byte": 9999},
        "whitespace": {"end_byte": 2},
        "unicode": {"end_byte": 3},
    }[mutation]
    value.procedure.goal.support[0] = ref.model_copy(update=bad)
    with pytest.raises(ValueError):
        prepared.render(value)


def test_partial_ordinary_revalidates_mutable_snapshots():
    group = cohort(episode(), episode("second"))
    prepared = p.prepare_partial_proposal(group)
    group.episodes[0].environment["fake"] = o.EnvironmentFact(
        value="missing", source=o.SourceBytes(start_byte=0, end_byte=7)
    )
    with pytest.raises(ValueError):
        p.prepare_partial_proposal(group)
    assert prepared.render(proposal())
    with pytest.raises(ValueError):
        replace(prepared, input_sha256="0" * 64).render(proposal())
    value = proposal()
    value.procedure.actions.append(p.PartialAction(order=9, action=assertion()))
    with pytest.raises(ValueError):
        prepared.render(value)


def test_partial_ordinary_abstention_and_supported_kinds():
    prepared = p.prepare_partial_proposal(cohort(episode(), episode("second")))
    assert prepared.render(p.PartialProposal(abstention_reason="No useful supported claim")) is None
    with pytest.raises(ValueError):
        p.PartialProposal()
    with pytest.raises(ValueError):
        p.PartialProposal(procedure=proposal().procedure, abstention_reason="also")
    with pytest.raises(ValueError):
        p.PartialProcedure.model_validate({**proposal().procedure.model_dump(), "kind": "caution"})


def test_partial_ordinary_does_not_relax_signed_or_complete_contract(group):
    with pytest.raises(ValueError, match="task-only"):
        cohort(*group.episodes)
    with pytest.raises(ValueError):
        c.DraftConditionalProcedure.model_validate(
            proposal().procedure.model_dump(exclude={"kind"})
        )
    with pytest.raises(ValueError):
        o.OrdinaryEpisode.model_validate(
            {**episode().model_dump(), "schema_version": "sibyl-ordinary-episode-v1"}
        )


@pytest.mark.parametrize(
    "field,value", [("prompt", "changed"), ("system", "changed"), ("prompt_sha256", "0" * 64)]
)
def test_partial_ordinary_prepared_policy_identity_is_checked(field, value):
    prepared = p.prepare_partial_proposal(cohort(episode(), episode("second")))
    with pytest.raises(ValueError, match="identity"):
        replace(prepared, **{field: value}).render(proposal())


def test_partial_ordinary_complete_supported_sections_have_no_missing_claims():
    value = proposal("procedure")
    draft = value.procedure.model_copy(
        update={
            "environment": [assertion()],
            "preconditions": [assertion()],
            "required_tools": [assertion()],
            "failure_modes": [assertion()],
            "abstain_when": [assertion()],
            "expected_result": assertion(),
            "actions": [p.PartialAction(order=1, action=assertion(), success_criteria=assertion())],
        }
    )
    prepared = p.prepare_partial_proposal(cohort(episode(), episode("second")))
    candidate = prepared.render(p.PartialProposal(procedure=draft))
    assert candidate
    receipt = candidate.metadata["ordinary_proposal_receipt"]
    assert receipt["unspecified_fields"] == []
    assert len(receipt["spans"]) == 9
    assert p.QUALIFICATION in candidate.content
    assert "sources capture-first" in candidate.content


def test_partial_ordinary_observation_only_pattern_needs_no_invented_action():
    group = cohort(
        episode(text="host-7 appeared in first trace"),
        episode("second", text="host-7 appeared in second trace"),
    )
    value = p.PartialProposal(
        procedure=p.PartialProcedure(
            kind="pattern",
            goal=c.ConditionalAssertion(
                statement="Both traces report host-7",
                label="observed",
                support=[
                    c.SupportRef(episode_id=e.episode_id, start_byte=0, end_byte=6)
                    for e in group.episodes
                ],
            ),
        )
    )
    candidate = p.prepare_partial_proposal(group).render(value)
    assert candidate and candidate.kind == "pattern"
    assert "Both traces report host-7" in candidate.content
    assert "/actions/" not in candidate.content
    spans = candidate.metadata["ordinary_proposal_receipt"]["spans"]
    assert len(spans) == 2 and {r["path"] for r in spans} == {"/goal"}
    with pytest.raises(ValueError, match="requires at least one"):
        p.PartialProcedure.model_validate({**value.procedure.model_dump(), "kind": "procedure"})


@pytest.mark.parametrize(
    "goal",
    [
        None,
        {"statement": "", "label": "observed", "support": []},
        {"statement": "Nothing supplied", "label": "observed", "support": []},
    ],
)
def test_partial_ordinary_content_free_patterns_are_rejected(goal):
    with pytest.raises(ValueError):
        p.PartialProcedure.model_validate({"kind": "pattern", "goal": goal})


# ---------------------------------------------------------------------------
# Complete-projection citations resolve to the ranges the projection listed
# ---------------------------------------------------------------------------


def controller_artifact():
    """A controller episode whose tool call drops one transport field.

    ``container`` is a classified ``tool_call`` payload field that the semantic
    view never selects, so its bytes sit between two listed evidence ranges of
    the same evidence ID.
    """
    from tests.test_episode_evidence import _episode

    value = _episode()
    value["trace"].insert(
        3,
        {
            "schema_version": "sibyl-coding-trace-v1",
            "attempt_id": "one",
            "request_id": "one",
            "index": 3,
            "kind": "tool_call",
            "payload": {
                "argv": ["bash", "-lc", "cat sibyl.log"],
                "command": "cat sibyl.log",
                "container": {"id": "c-1", "image": "sha256:" + "b" * 64},
                "index": 0,
                "name": "shell",
                "tool_call_id": "call-1",
            },
        },
    )
    for index, event in enumerate(value["trace"]):
        event["index"] = index
    return canonical(value).encode()


def controller_cohort(artifact, source_id="controller"):
    source = o.OrdinarySource(
        source_id=source_id,
        incarnation="inc-controller",
        generation=1,
        observed_revision=1,
        content_sha256=c._digest(artifact),
    )
    group = cohort(
        p.PartialEpisode(episode_id=source_id, artifact=artifact, source=source, environment={})
    )
    return group, prepare_ordinary_projection([(source_id, artifact)], [source])


def cited(group, projection, *refs, statement="Reported tool call"):
    prepared = p.prepare_partial_proposal(group, projection=projection)
    value = p.PartialProposal(
        procedure=p.PartialProcedure(
            kind="pattern",
            goal=c.ConditionalAssertion(
                statement=statement,
                label="observed",
                support=[
                    c.SupportRef(
                        episode_id=group.episodes[0].episode_id, start_byte=start, end_byte=end
                    )
                    for start, end in refs
                ],
            ),
        )
    )
    return prepared, value


def test_projection_resolves_a_cited_evidence_extent_to_its_listed_ranges():
    artifact = controller_artifact()
    _, projection = controller_cohort(artifact)
    citation = projection.citations["s0.e3"]
    ranges = sorted(citation.ranges)
    hull = (ranges[0][0], max(right for _, right in ranges))
    dropped = artifact.index(b'"container"')

    resolved = projection.resolve("controller", *hull)

    assert len(ranges) > 1 and tuple(ranges) == resolved
    assert ranges[0][0] < dropped < hull[1]
    assert not any(left <= dropped < right for left, right in resolved)
    assert projection.permits("controller", *hull)


def test_projection_extent_citation_renders_only_visible_bytes():
    artifact = controller_artifact()
    group, projection = controller_cohort(artifact)
    citation = projection.citations["s0.e3"]
    ranges = sorted(citation.ranges)
    hull = (ranges[0][0], max(right for _, right in ranges))
    prepared, value = cited(group, projection, hull)

    candidate = prepared.render(value)

    assert candidate is not None
    excerpt = b"".join(artifact[left:right] for left, right in ranges)
    span = candidate.metadata["ordinary_proposal_receipt"]["spans"][0]
    assert (span["start_byte"], span["end_byte"]) == hull
    assert span["ranges"] == [[left, right] for left, right in ranges]
    assert span["slice_sha256"] == c._digest(excerpt)
    assert b'"container"' not in excerpt and b'"container"' in artifact[hull[0] : hull[1]]
    assert f"bytes {ranges[0][0]}:{ranges[0][1]}, " in candidate.content


def test_projection_single_range_citation_renders_exactly_as_before():
    artifact = controller_artifact()
    group, projection = controller_cohort(artifact)
    start, end = projection.citations["s0.goal"].ranges[0]
    prepared, value = cited(group, projection, (start, end), statement="Reported goal")

    candidate = prepared.render(value)

    assert candidate is not None
    span = candidate.metadata["ordinary_proposal_receipt"]["spans"][0]
    assert (span["start_byte"], span["end_byte"]) == (start, end)
    assert span["ranges"] == [[start, end]]
    assert span["slice_sha256"] == c._digest(artifact[start:end])
    assert f"bytes {start}:{end} " in candidate.content


def test_projection_rejects_spans_across_evidence_ids_and_dropped_regions():
    artifact = controller_artifact()
    group, projection = controller_cohort(artifact)
    call = sorted(projection.citations["s0.e3"].ranges)
    terminal = sorted(projection.citations["s0.e4"].ranges)
    across = (call[0][0], max(right for _, right in terminal))
    dropped = artifact.index(b'"container"')
    inside_dropped = (dropped, artifact.index(b"}", dropped) + 1)

    for span in (across, inside_dropped):
        assert projection.resolve("controller", *span) is None
        prepared, value = cited(group, projection, span)
        with pytest.raises(ValueError, match="outside the complete evidence projection"):
            prepared.render(value)


def test_projection_lists_every_citation_range_in_byte_order():
    """cite() collects ranges in selection order; the prompt must not."""
    artifact = controller_artifact()
    _, projection = controller_cohort(artifact)
    payload = json.loads(projection.payload_json)

    listed = {key: value["ranges"] for key, value in payload["citations"].items()}

    assert any(
        list(citation.ranges) != sorted(citation.ranges)
        for citation in projection.citations.values()
    ), "fixture no longer exercises an unsorted citation"
    for key, ranges in listed.items():
        assert ranges == sorted(ranges) == sorted(map(list, projection.citations[key].ranges))
        first, last = ranges[0][0], ranges[-1][1]
        assert first < last, "the extent of a listed citation must read forwards"


def test_projection_never_repeats_bytes_from_overlapping_ranges():
    artifact = controller_artifact()
    _, projection = controller_cohort(artifact)
    citation = projection.citations["s0.e3"]
    ranges = sorted(citation.ranges)
    nested = EvidenceCitation(
        episode_id="controller", ranges=(*citation.ranges, (ranges[0][0], ranges[-1][1]))
    )
    overlapping = replace(projection, citations={**projection.citations, "s0.e3": nested})

    resolved = overlapping.resolve("controller", ranges[0][0], ranges[-1][1])

    assert resolved == ((ranges[0][0], ranges[-1][1]),)
    excerpt = b"".join(artifact[left:right] for left, right in resolved)
    assert len(excerpt) == ranges[-1][1] - ranges[0][0]
