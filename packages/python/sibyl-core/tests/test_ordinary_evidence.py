"""Ordinary reports stay source-bound without acquiring evaluator authority."""

import json

import pytest

from sibyl_core.models.experience import OperationalExperience
from sibyl_core.tasks import consolidation as c
from sibyl_core.tasks import ordinary_evidence as o
from tests.test_tasks_consolidation import group as group
from tests.test_tasks_consolidation import procedure as procedure


def episode(name="first", outcome="unknown"):
    body = (
        f"python-3.13\n{name}\n{outcome if outcome is not None else 'no outcome recorded'}".encode()
    )
    start = len(f"python-3.13\n{name}\n".encode())
    return o.OrdinaryEpisode(
        episode_id=name,
        artifact=body,
        source=o.OrdinarySource(
            source_id=f"capture-{name}",
            incarnation=f"incarnation-{name}",
            generation=1,
            observed_revision=1,
            content_sha256=c._digest(body),
        ),
        outcome=o.ReportedOutcome(
            value=outcome,
            source=None if outcome is None else o.SourceBytes(start_byte=start, end_byte=len(body)),
        ),
        environment={
            "runtime": o.EnvironmentFact(
                value="python-3.13", source=o.SourceBytes(start_byte=0, end_byte=11)
            )
        },
    )


def cohort(*episodes):
    return o.OrdinaryCohort(
        group_id="ordinary",
        mechanism="scoped indexing",
        organization_id="org",
        owner_principal_id="owner",
        memory_scope="private",
        environment_compatibility_keys=("runtime",),
        episodes=episodes,
    )


@pytest.mark.parametrize(
    "value", [None, "unknown", "passed", "task_failed", "completed successfully"]
)
def test_outcome_text_does_not_become_a_task_receipt(value):
    prepared = o.prepare_ordinary_evidence(
        cohort(episode(outcome=value), episode("other", "failed"))
    )
    outcome = json.loads(prepared.input_json)["episodes"][0]["outcome"]
    assert outcome["basis"] == "source_report"
    assert outcome["value"] == value
    assert not {"status", "success", "task_id", "attempt_id", "receipt_sha256"} & outcome.keys()
    assert "not verified successes" in prepared.prompt


@pytest.mark.parametrize(
    "field,value", [("basis", "signed"), ("status", "passed"), ("receipt_sha256", "a" * 64)]
)
def test_forged_signed_outcome_fields_are_rejected(field, value):
    data = episode().model_dump()
    data["outcome"][field] = value
    with pytest.raises(ValueError):
        o.OrdinaryEpisode.model_validate(data)


@pytest.mark.parametrize("target", ["outcome", "environment", "hash", "utf8"])
def test_source_binding_rejects_fabricated_facts(target):
    data = episode().model_dump()
    if target == "outcome":
        data["outcome"]["value"] = "passed"
    elif target == "environment":
        data["environment"]["runtime"]["value"] = "python-4"
    elif target == "hash":
        data["source"]["content_sha256"] = "0" * 64
    else:
        data["artifact"] = b"\xff"
        data["source"]["content_sha256"] = c._digest(b"\xff")
    with pytest.raises(ValueError):
        o.OrdinaryEpisode.model_validate(data)


def test_mixed_authority_preserves_declared_reference(group):
    mixed = cohort(episode(), group.episodes[0])
    data = json.loads(o.prepare_ordinary_evidence(mixed).input_json)
    assert data["episodes"][0]["outcome"]["basis"] == "source_report"
    assert data["episodes"][1]["outcome"] == group.episodes[0].outcome.model_dump()


def test_task_only_cannot_bypass_existing_contrast(group):
    with pytest.raises(ValueError, match="existing contrast"):
        cohort(*group.episodes)
    data = group.model_dump()
    data["episodes"][1]["outcome"].update(status="passed", success=True)
    with pytest.raises(ValueError, match="both passed"):
        c.ConsolidationGroup.model_validate(data)


@pytest.mark.parametrize("kind", ["source", "bytes", "episode"])
def test_duplicate_lineage_is_not_independent_support(kind):
    first, second = episode(), episode("other")
    data = second.model_dump()
    if kind == "source":
        data["source"]["source_id"] = first.source.source_id
    elif kind == "bytes":
        data = first.model_dump()
        data["episode_id"] = "copy"
        data["source"]["source_id"] = "copy"
    else:
        data["episode_id"] = first.episode_id
    with pytest.raises(ValueError):
        cohort(first, o.OrdinaryEpisode.model_validate(data))


def test_preparation_detaches_and_binds_original_inputs():
    value = cohort(episode(), episode("other"))
    prepared = o.prepare_ordinary_evidence(value)
    value.episodes[0].environment.clear()
    assert "python-3.13" in prepared.input_json
    with pytest.raises(ValueError):
        o.prepare_ordinary_evidence(value)
    changed = cohort(episode(outcome="failed"), episode("other"))
    assert o.prepare_ordinary_evidence(changed).input_sha256 != prepared.input_sha256
    assert o.prepare_ordinary_evidence(changed).prompt_sha256 != prepared.prompt_sha256


def test_shared_assertion_checks_use_original_utf8_ranges(group, procedure):
    first = episode("success")
    second = episode("failure")
    prepared = o.prepare_ordinary_evidence(cohort(first, second))
    draft = procedure.model_dump()
    for assertion in [
        draft["goal"],
        draft["expected_result"],
        *draft["environment"],
        *draft["preconditions"],
        *draft["required_tools"],
        *draft["failure_modes"],
        *draft["abstain_when"],
        draft["actions"][0]["action"],
        draft["actions"][0]["success_criteria"],
    ]:
        for ref in assertion["support"]:
            ref["end_byte"] = 11
    checked = c.DraftConditionalProcedure.model_validate(draft)
    assert len(prepared.validate_support(checked)) == 2
    draft["actions"][0]["action"]["support"][0]["episode_id"] = "foreign"
    with pytest.raises(ValueError, match="outside the group"):
        prepared.validate_support(c.DraftConditionalProcedure.model_validate(draft))


def test_legacy_prompt_bytes_and_outcome_support_are_unchanged(group, procedure):
    header = group.model_dump(
        mode="json",
        exclude={
            "organization_id": True,
            "owner_principal_id": True,
            "episodes": {"__all__": {"artifact"}},
        },
    )
    lines = ["Declared contrast group:", c._canonical(header).decode(), "Evidence byte ranges:"]
    for item in group.episodes:
        offset = 0
        for line in item.artifact.splitlines(keepends=True):
            lines.append(
                c._canonical(
                    {
                        "episode_id": item.episode_id,
                        "start_byte": offset,
                        "end_byte": offset + len(line),
                        "text": line.decode(),
                    }
                ).decode()
            )
            offset += len(line)
    expected = "\n".join([*lines, "", c.RETROSPECTIVE_REQUEST])
    assert c._prompt(group).encode() == expected.encode()
    assert c._extraction_input(group).prompt == expected
    assert c._extraction_input(group).system == c.SYSTEM_PROMPT
    assert len(c._spans(group, procedure)) == 2
    draft = procedure.model_dump()
    draft["failure_modes"][0]["support"] = draft["goal"]["support"]
    with pytest.raises(ValueError, match="task_failed"):
        c._spans(group, c.DraftConditionalProcedure.model_validate(draft))


def test_mixed_signed_reference_preserves_authority_without_authenticating_it(group):
    data = group.episodes[0].model_dump()
    data["outcome"] = c.AdmittedTaskOutcome(
        receipt_schema_version="sibyl-signed-eval-outcome-v1",
        task_id="task",
        attempt_id="attempt",
        status="passed",
        success=True,
        snapshot_sha256="a" * 64,
        receipt_sha256="b" * 64,
        admission_id="c" * 64,
        assignment_sha256="d" * 64,
        outcome_sha256="e" * 64,
        transcript_sha256="f" * 64,
    ).model_dump()
    admitted = c.ConsolidationEpisode.model_validate(data)
    prepared = o.prepare_ordinary_evidence(cohort(episode(outcome="passed"), admitted))
    assert json.loads(prepared.input_json)["episodes"][1]["outcome"] == data["outcome"]
    assert "requires a separate server ledger verification" in prepared.system


def test_duplicate_task_receipt_is_rejected_in_a_mixed_cohort(group):
    data = group.episodes[1].model_dump()
    data["outcome"]["receipt_sha256"] = group.episodes[0].outcome.receipt_sha256
    with pytest.raises(ValueError, match="task evidence identity"):
        cohort(episode(), group.episodes[0], c.ConsolidationEpisode.model_validate(data))


def test_unretained_alias_cannot_claim_ordinary_source_authority():
    data = episode().model_dump()
    data["source"]["source_id"] = "reflection:input:example"
    with pytest.raises(ValueError, match="unretained"):
        o.OrdinaryEpisode.model_validate(data)


def test_present_empty_plain_text_outcome_remains_present():
    data = episode().model_dump()
    data["outcome"] = {
        "value": "",
        "source": {"start_byte": len(data["artifact"]), "end_byte": len(data["artifact"])},
    }
    value = o.OrdinaryEpisode.model_validate(data)
    assert value.outcome.value == "" and value.outcome.source is not None
    missing = episode(outcome=None)
    assert missing.outcome.value is None and missing.outcome.source is None


@pytest.mark.parametrize(
    "outcome", ["", "unknown", 'said "done"\nnext', "café\\path", "\U0001f49c"]
)
def test_encoded_json_fields_preserve_decoded_value_and_original_bytes(outcome):
    raw = {
        "source_id": "raw-json",
        "goal": "scope",
        "outcome": outcome,
        "observations": [
            {
                "id": "observation",
                "ordinal": 0,
                "action": "inspect",
                "evidence": [{"id": "part", "content": "observed"}],
            }
        ],
    }
    # The retained source uses JSON encoding; source spans cover complete lexemes.
    # Constructing through the product model prevents a plain-text-only fixture.
    experience = OperationalExperience.model_validate(raw)
    environment = 'runtime "quoted"\\path'
    payload = experience.model_dump(mode="json")
    payload["metadata"] = {"runtime": environment}
    artifact = json.dumps(payload, ensure_ascii=True).encode()

    def ref(value):
        encoded = json.dumps(value, ensure_ascii=True).encode()
        start = artifact.index(encoded, artifact.index(b'"outcome"') if value == outcome else 0)
        return o.SourceBytes(
            start_byte=start, end_byte=start + len(encoded), encoding="json_string"
        )

    value = o.OrdinaryEpisode(
        episode_id="json",
        artifact=artifact,
        source=o.OrdinarySource(
            source_id="json",
            incarnation="inc",
            generation=1,
            observed_revision=1,
            content_sha256=c._digest(artifact),
        ),
        outcome=o.ReportedOutcome(value=outcome, source=ref(outcome)),
        environment={"runtime": o.EnvironmentFact(value=environment, source=ref(environment))},
    )
    assert value.outcome.source.read(artifact) == outcome
    assert value.artifact == artifact
    assert value.environment["runtime"].source.read(artifact) == environment


@pytest.mark.parametrize(
    "encoded", [b"null", b"false", b"123", b"[]", b"{}", b'"x" "y"', b'"\\ud800"', b""]
)
def test_json_field_decoder_rejects_nonstring_or_invalid_values(encoded):
    with pytest.raises(ValueError):
        o.SourceBytes(start_byte=0, end_byte=len(encoded), encoding="json_string").read(encoded)


def test_field_empty_support_does_not_relax_assertion_spans(group, procedure):
    data = procedure.model_dump()
    data["goal"]["support"][0]["end_byte"] = 0
    with pytest.raises(ValueError):
        c._spans(group, c.DraftConditionalProcedure.model_validate(data))
