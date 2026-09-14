"""Complete multisource evidence survives the real ordinary stage owners."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from pydantic_ai import Agent

from sibyl_core.ai.llm.extractor import Extractor
from sibyl_core.config import settings
from sibyl_core.services import ordinary_cohort, procedure_validation
from sibyl_core.services.content_raw_persistence import remember_raw_memory, save_raw_memory
from sibyl_core.services.memory_source_validation import SourceReadAuthority
from sibyl_core.services.reflection_validation import (
    prepare_stored_reflection,
    validate_reflection_stage,
)
from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.consolidation import ConsolidationInputBudgetExceeded
from sibyl_core.tasks.memory_validation import CriticOutput, projection_critic_input_chars
from sibyl_core.tasks.ordinary_projection import VERSION, reconstruct_ordinary_projection
from sibyl_core.tasks.ordinary_proposals import (
    QUALIFICATION,
    PartialCohort,
    PartialProposal,
    _projection_for_cohort,
)
from tests.test_episode_evidence import _encoded, _episode
from tests.test_ordinary_cohort import content_store as content_store
from tests.test_ordinary_packet_correction import install


@pytest.fixture
async def complete_sources(content_store):
    episode = _episode()
    request = episode["trace"][1]["payload"]
    request["body"]["messages"][0]["content"] = "Shared source instruction. " * 40
    request["body_base64"], request["body_sha256"] = _encoded(request["body"])
    return [
        await remember_raw_memory(
            organization_id="org",
            principal_id="owner",
            source_id=f"controller-{index}",
            raw_content=canonical({**episode, "goal": f"preserve evidence {index}"}),
            embedding_provider=None,
        )
        for index in range(2)
    ]


async def prepare(sources, **kwargs):
    return await ordinary_cohort.prepare_stored_cohort(
        "org",
        "owner",
        [source.id for source in sources],
        AsyncMock(return_value=SourceReadAuthority("owner")),
        **kwargs,
    )


def proposal(prepared, statement="Preserve the reported goal"):
    group = PartialCohort.model_validate_json(prepared.input_json)
    projection = _projection_for_cohort(group, prepared.projection_json)
    start, end = projection.citations["s0.goal"].ranges[0]
    return {
        "procedure": {
            "kind": "pattern",
            "goal": {
                "statement": statement,
                "label": "inferred",
                "support": [
                    {
                        "episode_id": projection.citations["s0.goal"].episode_id,
                        "start_byte": start,
                        "end_byte": end,
                    }
                ],
            },
        },
        "abstention_reason": None,
    }


async def test_complete_projection_shared_values_mode_and_audit_boundaries(complete_sources):
    current = await prepare(complete_sources)
    raw = await prepare(complete_sources, evidence_mode="raw_v1")
    assert current.snapshot_sha256 == raw.snapshot_sha256
    assert current.prepared.input_sha256 != raw.prepared.input_sha256
    assert raw.prepared.projection_json is None
    group = PartialCohort.model_validate_json(current.prepared.input_json)
    projection = _projection_for_cohort(group, current.prepared.projection_json)
    payload = json.loads(projection.payload_json)
    assert payload["version"] == VERSION
    assert len(payload["evidence_view"]["sources"]) == 2
    assert payload["evidence_view"]["values"]
    for binding in (
        dict(projection.binding, version="raw_v1"),
        dict(projection.binding, source_observations=[]),
    ):
        with pytest.raises(ValueError):
            reconstruct_ordinary_projection(
                [(e.episode_id, e.artifact) for e in group.episodes], binding
            )
    value = proposal(current.prepared)
    source = next(
        e
        for e in group.episodes
        if e.episode_id == value["procedure"]["goal"]["support"][0]["episode_id"]
    )
    audit = source.artifact.index(b'"usage"')
    value["procedure"]["goal"]["support"][0].update(start_byte=audit, end_byte=audit + 7)
    with pytest.raises(ValueError, match="outside the complete evidence projection"):
        current.prepared.render(PartialProposal.model_validate(value))
    broken = json.loads(source.artifact)
    broken["trace"][1]["payload"]["body_sha256"] = "0" * 64
    stored = next(s for s in complete_sources if s.id == source.episode_id)
    await save_raw_memory(
        replace(stored, raw_content=canonical(broken)), expected_revision=stored.revision
    )
    with pytest.raises(ValueError):
        await prepare(complete_sources)


async def test_complete_projection_actual_sdk_stages_and_protected_replay(
    complete_sources, content_store, monkeypatch
):
    from openai import AsyncOpenAI, _base_client
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from sibyl_core.ai.transport import RecordingOpenAIClient

    httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx
    monkeypatch.setattr(settings, "consolidation_max_input_chars", 40_000)
    original = await prepare(complete_sources)
    output = proposal(original.prepared)
    projection = _projection_for_cohort(
        PartialCohort.model_validate_json(original.prepared.input_json),
        original.prepared.projection_json,
    )
    sent = []

    async def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        tool = body["tools"][0]
        value = output if "procedure" in tool["parameters"]["properties"] else {"findings": []}
        return httpx.Response(
            200,
            json={
                "id": "resp_offline",
                "object": "response",
                "created_at": 0,
                "status": "completed",
                "model": "offline",
                "output": [
                    {
                        "id": "fc_offline",
                        "type": "function_call",
                        "call_id": "call_offline",
                        "name": tool["name"],
                        "arguments": canonical(value),
                        "status": "completed",
                    }
                ],
                "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
            },
        )

    async with AsyncOpenAI(
        api_key="offline-test-key",
        max_retries=0,
        http_client=RecordingOpenAIClient(transport=httpx.MockTransport(respond)),
    ) as client:
        model = OpenAIResponsesModel("offline", provider=OpenAIProvider(openai_client=client))
        reader = Extractor(
            CriticOutput, agent=Agent(model, output_type=CriticOutput), output_retries=0
        )
        monkeypatch.setattr(
            procedure_validation,
            "validation_extractor",
            AsyncMock(return_value=(reader, '{"model":"offline","transport_retries":0}')),
        )
        resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
        ids = [source.id for source in complete_sources]
        assert await ordinary_cohort.partition_stored_cohort("org", "owner", ids, resolver) == [
            sorted(ids)
        ]
        candidate, origin = await ordinary_cohort.propose_stored_cohort(
            "org", "owner", ids, resolver, authorize=AsyncMock()
        )
        assert QUALIFICATION in candidate.raw_content
        prepared = await prepare_stored_reflection("org", "owner", candidate.id, resolver)
        payload = json.loads(prepared.prepared.payload_json)
        assert prepared.evidence == projection
        assert payload["evidence_projection"] == json.loads(projection.payload_json)
        assert set(payload["sources"]) == set(ids)
        assert all(
            "text" not in source and source["provenance"] == "reported"
            for source in payload["sources"].values()
        )
        assert (await validate_reflection_stage(prepared, resolver))["status"] == "no_findings"
        assert len(sent) == 2
        for body in sent:
            content = next(item["content"] for item in body["input"] if item.get("role") == "user")
            text = content if isinstance(content, str) else content[0]["text"]
            assert canonical(json.loads(projection.payload_json)["evidence_view"]) in text
        replay, execution = await ordinary_cohort.propose_stored_cohort(
            "org", "owner", ids, resolver, authorize=AsyncMock()
        )
        assert (replay.id, execution) == (candidate.id, origin) and len(sent) == 2
        rows = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
        request = json.loads(next(row for row in rows if row["uuid"] == origin)["request_json"])
        assert request["evidence_projection"] == projection.binding
        assert json.loads(request["policy"])["packing_policy"]["candidate_reserve_chars"] == 10_000
        changed = replace(
            candidate,
            metadata={
                key: value
                for key, value in candidate.metadata.items()
                if key != "ordinary_proposal_receipt"
            },
        )
        await save_raw_memory(changed, expected_revision=changed.revision)
        assert (
            await prepare_stored_reflection("org", "owner", candidate.id, resolver)
        ).evidence == projection
        source = complete_sources[0]
        await save_raw_memory(
            replace(source, title="same bytes, new revision"), expected_revision=source.revision
        )
        with pytest.raises(SourceUnavailableError):
            await prepare_stored_reflection("org", "owner", candidate.id, resolver)
        assert len(sent) == 2


async def test_complete_projection_dual_envelope_partition_and_actual_candidate_guard(
    complete_sources, content_store, monkeypatch
):
    original = await prepare(complete_sources)
    install(monkeypatch, proposal(original.prepared, statement="Reported boundary " * 4000))
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    ids = [source.id for source in complete_sources]
    candidate, _ = await ordinary_cohort.propose_stored_cohort(
        "org", "owner", ids, resolver, authorize=AsyncMock()
    )
    parent = await prepare_stored_reflection("org", "owner", candidate.id, resolver)
    reader = install(monkeypatch, {"findings": []})
    reader.extract_with_usage = AsyncMock(wraps=reader.extract_with_usage)
    with pytest.raises(ConsolidationInputBudgetExceeded):
        await validate_reflection_stage(parent, resolver)
    reader.extract_with_usage.assert_not_awaited()
    rows = await content_store.execute_query("SELECT * FROM memory_validation_executions;")
    assert len(rows) == 1 and rows[0]["state"] == "returned"
    assert json.loads(rows[0]["usage_json"])["requests"] == 1
    assert rows[0]["result_json"]
    owned, _ = await procedure_validation.validation_extractor()
    extractor = await ordinary_cohort._proposal_extractor(owned, original.prepared.system)
    proposal_schema = len(canonical(await extractor.output_schema()))
    critic_schema = len(canonical(await owned.output_schema()))
    # A critic-heavy envelope splits the same source order even when the proposer fits.
    projection = parent.evidence
    proposer_size = len(original.prepared.system) + len(original.prepared.prompt) + proposal_schema
    critic_size = (
        projection_critic_input_chars(projection, candidate_reserve_chars=0) + critic_schema
    )
    limit = max(proposer_size, critic_size) + 100
    monkeypatch.setattr(settings, "consolidation_max_input_chars", limit)
    assert proposer_size < limit < critic_size + limit // 4
    groups = await ordinary_cohort.partition_stored_cohort("org", "owner", ids, resolver)
    assert sorted(identity for group in groups for identity in group) == sorted(ids)
    assert groups == [[identity] for identity in sorted(ids)]
