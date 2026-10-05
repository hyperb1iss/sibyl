"""Abstained correction chains retire without orphaning retained drafts."""

from unittest.mock import AsyncMock

import pytest

from sibyl.jobs import reflection
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.reflection_validation import prepare_stored_reflection
from sibyl_core.services.surreal_content import remember_raw_memory
from sibyl_core.tasks.memory_progress import ProgressCriticOutput
from tests.test_dream_source_checkpoints import dream_store as dream_store  # noqa: PLC0414


@pytest.fixture
async def corrected_chain(dream_store, monkeypatch):
    import json

    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from sibyl_core.ai.llm.extractor import Extractor
    from sibyl_core.services import procedure_validation
    from sibyl_core.services.automatic_reflection import _persist_corrected
    from sibyl_core.services.reflection_validation import validate_reflection_stage
    from sibyl_core.tasks.memory_validation import CriticOutput
    from sibyl_core.tasks.procedure_review import ReviewSubmission, review_digest

    await remember_raw_memory(
        organization_id="dream-org",
        principal_id="owner",
        source_id="session",
        raw_content="Decision: validate inputs before parsing.",
        embedding_provider=None,
    )
    await reflection.run_reflection_dream_cycle({}, "dream-org", candidate_limit=0)
    rows = await dream_store.execute_query(
        "SELECT * FROM raw_captures WHERE capture_surface='reflection_candidate';"
    )
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    parent = await prepare_stored_reflection("dream-org", "owner", rows[0]["uuid"], resolver)
    assertion = json.loads(parent.prepared.payload_json)["assertions"]["/content"]
    review = ReviewSubmission(
        parent_operation_id=json.loads(parent.prepared.payload_json)["parent_operation_id"],
        parent_candidate_sha256=json.loads(parent.prepared.payload_json)["parent_candidate_sha256"],
        findings=[
            {
                "claim_path": "/content",
                "claim_sha256": review_digest(assertion),
                "evidence_refs": [{"evidence_id": "s0"}],
                "basis": "factual_contradiction",
                "disposition": "reconsider",
                "critique": "Preserve the observed ordering.",
            }
        ],
    )
    output = {
        "content": "Validate inputs before parsing.",
        "abstention_reason": None,
        "assessments": [
            {
                "finding_id": review.finding_ids()[0],
                "disposition": "accepted",
                "explanation": "The original decision specifies ordering.",
                "evidence_refs": [{"evidence_id": "s0"}],
            }
        ],
    }
    extractor = Extractor(
        CriticOutput, agent=Agent(TestModel(custom_output_args=output), output_type=CriticOutput)
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(return_value=(extractor, '{"max_input_chars":40000,"model":"offline"}')),
    )
    outcome = await validate_reflection_stage(parent, resolver, review)
    assert outcome["status"] == "corrected"

    first = await _persist_corrected(parent, resolver, outcome)
    replay = await _persist_corrected(parent, resolver, outcome)
    assert first.id == replay.id
    assert first.revision == replay.revision
    child = await prepare_stored_reflection("dream-org", "owner", first.id, resolver)
    assert child.candidate.content == output["content"]
    critic = Extractor(
        CriticOutput,
        agent=Agent(
            TestModel(
                custom_output_args={"findings": [review.findings[0].model_dump(mode="json")]}
            ),
            output_type=CriticOutput,
        ),
    )
    recheck = Extractor(
        ProgressCriticOutput,
        agent=Agent(
            TestModel(
                custom_output_args={
                    "findings": [],
                    "abstention_reason": "Evidence supports no further useful correction.",
                    "prior_assessments": [
                        {
                            "finding_id": review.finding_ids()[0],
                            "disposition": "resolved",
                            "supported_reduction": "The corrected ordering agrees with the original decision.",
                            "remaining_concern": None,
                            "evidence_refs": [{"evidence_id": "s0"}],
                        }
                    ],
                }
            ),
            output_type=ProgressCriticOutput,
        ),
    )
    monkeypatch.setattr(
        procedure_validation,
        "_validation_extractor",
        AsyncMock(return_value=(recheck, '{"max_input_chars":40000,"model":"offline"}')),
    )
    monkeypatch.setattr(
        procedure_validation,
        "validation_extractor",
        AsyncMock(
            side_effect=[
                (critic, '{"max_input_chars":40000,"model":"offline"}'),
                (extractor, '{"max_input_chars":40000,"model":"offline"}'),
                (recheck, '{"max_input_chars":40000,"model":"offline"}'),
            ]
        ),
    )
    return parent, resolver, first


@pytest.mark.parametrize(
    "case",
    [
        "live",
        "drain",
        "historical",
        "revoked",
        "source_changed",
        "missing_origin",
        "metadata_ignored",
        "promoted",
        "concurrent_child",
        "concurrent_parent",
        "concurrent_source",
    ],
)
async def test_abstained_correction_chain_retires(corrected_chain, monkeypatch, case):
    parent, resolver, first = corrected_chain
    from sibyl_core.services import procedure_validation
    from sibyl_core.services.automatic_reflection import automatically_review_reflection
    from sibyl_core.services.surreal_content import get_raw_memory

    if case not in {"live", "drain"}:
        # Retain the state produced when the old root-first loop was interrupted.
        from sibyl_core.services.automatic_reflection import _abstain

        await _abstain(parent, resolver, "evidence_abstention", [])
    from dataclasses import replace

    from sibyl_core.services import reflection_retirement
    from sibyl_core.services.content_raw_persistence import save_raw_memory
    from sibyl_core.services.source_observations import SourceUnavailableError

    if case == "revoked":
        resolver.return_value = None
    elif case == "source_changed":
        source = parent.sources[0]
        await save_raw_memory(
            replace(source, raw_content="New source decision."), expected_revision=source.revision
        )
    elif case == "missing_origin":
        monkeypatch.setattr(
            reflection_retirement, "load_raw_derivation", AsyncMock(return_value=None)
        )
    elif case == "metadata_ignored":
        first = await save_raw_memory(
            replace(
                first, metadata={**first.metadata, "automatic_correction": {"parent_id": "forged"}}
            ),
            expected_revision=first.revision,
        )
    elif case == "promoted":
        first = await save_raw_memory(
            replace(first, review_state="promoted"), expected_revision=first.revision
        )
    elif case.startswith("concurrent_"):

        async def race(memory, **kwargs):
            target_id = {
                "concurrent_child": first.id,
                "concurrent_parent": parent.memory.id,
                "concurrent_source": parent.sources[0].id,
            }[case]
            target = await get_raw_memory(organization_id="dream-org", memory_id=target_id)
            assert target is not None
            await save_raw_memory(
                replace(target, title="Concurrent edit"), expected_revision=target.revision
            )
            return await save_raw_memory(memory, **kwargs)

        monkeypatch.setattr(reflection_retirement, "save_raw_memory", race)

    procedure_validation.validation_extractor.reset_mock()
    procedure_validation._validation_extractor.reset_mock()

    async def resume():
        return await automatically_review_reflection(
            "dream-org", "owner", parent.memory.id if case == "live" else first.id, resolver
        )

    if case in {"revoked", "source_changed", "missing_origin", "promoted"}:
        with pytest.raises(SourceUnavailableError):
            await resume()
    elif case.startswith("concurrent_"):
        with pytest.raises(Exception, match="publication source changed before commit"):
            await resume()
    elif case == "drain":
        drained = await reflection.run_reflection_dream_cycle(
            {}, "dream-org", source_limit=0, candidate_limit=50
        )
        assert drained["failed"] == 0
        assert drained["archived"] == 1
    else:
        resumed = await resume()
        assert resumed.status == "abstained"
    if case not in {"live", "drain"}:
        procedure_validation.validation_extractor.assert_not_called()
        procedure_validation._validation_extractor.assert_not_called()
    if case not in {"live", "drain", "historical", "metadata_ignored"}:
        current = await get_raw_memory(organization_id="dream-org", memory_id=first.id)
        assert current is not None
        assert current.review_state == ("promoted" if case == "promoted" else "pending")
        return
    for candidate_id in (parent.memory.id, first.id):
        archived = await get_raw_memory(organization_id="dream-org", memory_id=candidate_id)
        assert archived is not None
        assert archived.review_state == "archived"
    repeat = await reflection.run_reflection_dream_cycle(
        {}, "dream-org", source_limit=0, candidate_limit=50
    )
    assert repeat["failed"] == 0
    assert repeat["candidates_scanned"] == 0
