"""Ordinary packet evidence remains complete, reported, and tied to original bytes."""

import base64
import hashlib
import json
import os
import tarfile
from copy import deepcopy
from dataclasses import replace
from functools import lru_cache
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
from sibyl_core.tasks.episode_evidence import _tool_text, encode_episode_views, project_episode
from sibyl_core.tasks.memory_validation import CriticOutput
from sibyl_core.tasks.ordinary_packets import (
    QUALIFICATION,
    prepare_ordinary_packets,
    reconstruct_ordinary_packet,
)
from tests.test_episode_evidence import _encoded, _episode
from tests.test_ordinary_cohort import content_store as content_store


@lru_cache
def oversized_episode() -> bytes:
    """Reproduce the retained failures' 21 model rounds and 20 tool exchanges."""
    episode = _episode()
    episode["outcome"] = {"status": "task_failed", "reported_check": "boundary mismatch"}
    events = [episode["trace"][0]]
    history = [{"role": "system", "content": "Inspect exact boundaries before changing code."}]
    for index in range(21):
        body = {
            "messages": [history[0], {"role": "user", "content": f"round {index}"}, *history[1:]],
            "model": "test",
        }
        encoded, digest = _encoded(body)
        payloads = [
            (
                "model_request",
                {"body": deepcopy(body), "body_base64": encoded, "body_sha256": digest},
            )
        ]
        message = {
            "role": "assistant",
            "content": "\n".join(
                f"Observation {index}:{line}: preserve half-open boundaries and exact UTF-8 lengths."
                for line in range(18)
            ),
        }
        raw = {"choices": [{"message": message}], "usage": {"tokens": 1}}
        encoded, digest = _encoded(raw)
        payloads.append(
            (
                "model_response",
                {"raw": raw, "body_base64": encoded, "body_sha256": digest, "status_code": 200},
            )
        )
        history.append(message)
        if index < 20:
            call = f"call-{index}"
            payloads.append(
                (
                    "tool_call",
                    {
                        "index": index,
                        "tool_call_id": call,
                        "name": "shell",
                        "command": f"check-boundary --round {index}",
                    },
                )
            )
            stdout = "\n".join(
                f"Case {index}:{line}: actual end differs from expected exclusive end."
                for line in range(22)
            )
            outcome = {
                "index": index,
                "tool_call_id": call,
                "status": "complete",
                "returncode": 1,
                "carried_back": False,
                "stdout": stdout,
                "stderr": "",
            }
            for key in ("stdout", "stderr"):
                value = outcome[key].encode()
                outcome[key + "_base64"] = base64.b64encode(value).decode()
                outcome[key + "_sha256"] = hashlib.sha256(value).hexdigest()
            payloads.append(("tool_result", outcome))
            history.append({"role": "tool", "tool_call_id": call, "content": _tool_text(outcome)})
        for kind, payload in payloads:
            events.append(
                {**episode["trace"][0], "index": len(events), "kind": kind, "payload": payload}
            )
    events.append({**episode["trace"][-1], "index": len(events)})
    episode["trace"] = events
    return canonical(episode).encode()


def test_packet_manifest_covers_complete_exchanges_and_denies_forgery():
    artifact = oversized_episode()
    projection = project_episode("source", artifact, prefix="s0")
    assert len(projection.view["events"]) == 84
    assert len(artifact) > 700_000
    assert len(canonical(encode_episode_views([projection]))) > 40_000
    packets = prepare_ordinary_packets(
        "source", artifact, input_chars=lambda p: len(p.payload_json), max_input_chars=24_000
    )
    assert len(packets) > 1
    manifest = packets[0].binding["manifest"]
    assert manifest["pages"][0]["event_start"] == 0
    assert manifest["pages"][-1]["event_end"] == 84
    for packet in packets:
        assert reconstruct_ordinary_packet("source", artifact, packet.binding) == packet
        assert packet.binding["manifest"] == manifest
        assert packet.citations["s0.goal"].episode_id == "source"
        assert packet.citations["s0.outcome"].episode_id == "source"
    forged = deepcopy(packets[0].binding)
    forged["manifest"]["pages"].pop()
    with pytest.raises(ValueError, match="incomplete"):
        reconstruct_ordinary_packet("source", artifact, forged)
    forged = deepcopy(packets[0].binding)
    forged["manifest"]["pages"][0]["view_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="view differs"):
        reconstruct_ordinary_packet("source", artifact, forged)
    with pytest.raises(ValueError):
        reconstruct_ordinary_packet("other", artifact, packets[0].binding)
    assert not packets[0].permits("source", 0, len(artifact))
    for citation in packets[-1].citations.values():
        if citation not in packets[0].citations.values():
            assert not packets[0].permits("source", *citation.ranges[0])


@lru_cache
def retained_episode(task: str) -> bytes:
    path = os.environ.get("SIBYL_PACKET_RETAINED_ARCHIVE")
    if path is None:
        pytest.skip("Retained source archive is an optional offline acceptance input")
    with tarfile.open(path, "r|*") as archive:
        for member in archive:
            if member.name == f"attempts/{task}/signed-episode.bin":
                source = archive.extractfile(member)
                assert source is not None
                artifact = source.read()
                assert (
                    hashlib.sha256(artifact).hexdigest()
                    == {
                        "half-open-capacity-sweep-111": "11b17282f560a3b72dd43ee39a4b5f5494c9c9f963e15f5c2f02e8d5ed0f4862",
                        "utf8-record-framing-111": "16dcaadf7a3ad67a6419c8079e96231de9ffd66072a81f2bf62b4a570d0a44ba",
                    }[task]
                )
                return artifact
    raise AssertionError("Retained source episode absent")


@pytest.mark.parametrize(
    "source_case", ["geometry", "half-open-capacity-sweep-111", "utf8-record-framing-111"]
)
async def test_packet_default_budget_actual_sdk_proposer_critic_and_replay(
    content_store, monkeypatch, source_case, record_property
):
    from openai import AsyncOpenAI, _base_client
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from sibyl_core.ai.transport import RecordingOpenAIClient
    from sibyl_core.services import content_models

    httpx = getattr(_base_client, "httpx2", None) or _base_client.httpx
    monkeypatch.setattr(settings, "consolidation_max_input_chars", 40_000)
    monkeypatch.setattr(content_models, "configured_raw_memory_embedding_provider", lambda: None)
    artifact = oversized_episode() if source_case == "geometry" else retained_episode(source_case)
    source = await remember_raw_memory(
        organization_id="org",
        principal_id="owner",
        source_id=source_case,
        raw_content=artifact.decode(),
        embedding_provider=None,
    )
    resolver = AsyncMock(return_value=SourceReadAuthority("owner"))
    sent = []

    async def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        tool = body["tools"][0]
        if "procedure" in tool["parameters"].get("properties", {}):
            content = next(item["content"] for item in body["input"] if item.get("role") == "user")
            text = content if isinstance(content, str) else content[0]["text"]
            packet = json.JSONDecoder().raw_decode(text.split("Evidence packet:\n", 1)[1])[0]
            start, end = packet["citations"]["s0.goal"]["ranges"][0]
            output = {
                "procedure": {
                    "kind": "pattern",
                    "goal": {
                        "statement": "Preserve source goal and packet scope",
                        "label": "inferred",
                        "support": [
                            {"episode_id": source.id, "start_byte": start, "end_byte": end}
                        ],
                    },
                },
                "abstention_reason": None,
            }
        else:
            output = {"findings": []}
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
                        "arguments": canonical(output),
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
        packets = await ordinary_cohort.prepare_stored_source_packets(
            "org", "owner", source.id, resolver
        )
        record_property("source_sha256", hashlib.sha256(artifact).hexdigest())
        record_property("source_chars", len(artifact.decode()))
        record_property("packet_count", len(packets))
        record_property("packet_payload_chars", sum(len(packet.payload_json) for packet in packets))
        assert len(packets) > 1 and not sent
        candidates = []
        for packet in packets:
            candidate, _ = await ordinary_cohort.propose_stored_cohort(
                "org",
                "owner",
                [source.id],
                resolver,
                authorize=AsyncMock(),
                packet_binding=packet.binding,
            )
            assert candidate is not None and QUALIFICATION in candidate.raw_content
            candidates.append(candidate)
            prepared = await prepare_stored_reflection("org", "owner", candidate.id, resolver)
            assert prepared.packet == packet
            assert len(prepared.sources) == 1 and prepared.sources[0].id == source.id
            payload = json.loads(prepared.prepared.payload_json)
            assert payload["sources"][source.id]["provenance"] == "reported"
            assert "text" not in payload["sources"][source.id]
            assert len(prepared.prepared.prompt) < 40_000
            result = await validate_reflection_stage(prepared, resolver)
            assert result["status"] == "no_findings"
        count = len(sent)
        assert count == len(packets) * 2
        record_property("offline_sdk_calls", count)
        record_property("total_wire_request_chars", sum(len(canonical(body)) for body in sent))
        replay, _ = await ordinary_cohort.propose_stored_cohort(
            "org",
            "owner",
            [source.id],
            resolver,
            authorize=AsyncMock(),
            packet_binding=packets[0].binding,
        )
        assert replay.id == candidates[0].id and len(sent) == count
        # Removing the entire mutable receipt cannot change the critic's packet.
        changed = replace(
            candidates[0],
            metadata={
                k: v for k, v in candidates[0].metadata.items() if k != "ordinary_proposal_receipt"
            },
        )
        await save_raw_memory(changed, expected_revision=changed.revision)
        current = await prepare_stored_reflection("org", "owner", changed.id, resolver)
        assert current.packet == packets[0]
        await save_raw_memory(
            replace(source, title="same bytes, new revision"), expected_revision=source.revision
        )
        with pytest.raises(SourceUnavailableError):
            await prepare_stored_reflection("org", "owner", changed.id, resolver)
        assert len(sent) == count
