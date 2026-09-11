"""Semantic proposal validation must run inside model feedback."""

import json
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic import ValidationError
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

from sibyl_core.ai.errors import LLMValidationError
from sibyl_core.ai.llm import Extractor
from sibyl_core.ai.llm import extractor as extraction
from sibyl_core.ai.transport import RecordingOpenAIClient
from sibyl_core.tasks.procedure_evidence import EvidenceProposal


def procedure():
    assertion = {
        "statement": "Synthetic observation",
        "label": "observed",
        "support": [{"evidence_id": "fixture"}],
    }
    return {
        "goal": assertion,
        "environment": [assertion],
        "preconditions": [assertion],
        "required_tools": [],
        "actions": [{"order": 1, "action": assertion, "success_criteria": assertion}],
        "expected_result": assertion,
        "failure_modes": [assertion],
        "abstain_when": [assertion],
    }


@pytest.mark.parametrize("recovery", [None, "procedure", "abstention"])
async def test_semantic_feedback_recovers_or_exhausts_without_repair(monkeypatch, recovery):
    monkeypatch.setattr(extraction, "reserve_llm_budget", AsyncMock())
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        output = {
            "outcome": {
                "kind": "procedure",
                "procedure": procedure(),
                "reason": "No abstention is needed.",
            }
        }
        if recovery and len(requests) > 1:
            output = {
                "outcome": {"kind": "procedure", "procedure": procedure()}
                if recovery == "procedure"
                else {"kind": "abstention", "reason": "Insufficient evidence"}
            }
        return httpx2.Response(
            200,
            json={
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 0,
                "model": "qwen/qwen3-coder-next",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": f"msg_{len(requests)}",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": json.dumps(output), "annotations": []}
                        ],
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
            },
        )

    async with RecordingOpenAIClient(transport=httpx2.MockTransport(respond)) as http:
        client = AsyncOpenAI(
            api_key="fixture",
            base_url="https://openrouter.ai/api/v1",
            http_client=http,
            max_retries=2,
        )
        model = OpenAIResponsesModel(
            "qwen/qwen3-coder-next", provider=OpenAIProvider(openai_client=client)
        )
        agent = Agent(
            model, output_type=NativeOutput(EvidenceProposal, strict=True), retries={"output": 2}
        )
        extractor = Extractor(
            EvidenceProposal, agent=agent, output_mode="native_strict", max_tokens=8192
        )
        if recovery:
            result = await extractor.extract_with_usage("Synthetic evidence")
            assert result.usage.requests == 2
            assert result.usage.input_tokens == 20
            assert len(result.usage.transport_attempts) == 2
            assert result.output.outcome.kind == recovery
        else:
            with pytest.raises(LLMValidationError) as raised:
                await extractor.extract_with_usage("Synthetic evidence")
            assert len(raised.value.details["extraction_usage"]["transport_attempts"]) == 3
    assert "extra_forbidden" in json.dumps(requests[1]["input"])


@pytest.mark.parametrize("payload", [{}, {"procedure": None, "abstention_reason": None}])
def test_neither_outcome_is_rejected(payload):
    with pytest.raises(ValidationError):
        EvidenceProposal.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"outcome": {"kind": "procedure", "procedure": procedure(), "reason": "reason"}},
        {"outcome": {"kind": "abstention", "reason": "reason", "procedure": procedure()}},
        {"outcome": {"kind": "procedure", "reason": "reason"}},
        {"outcome": {"kind": "unknown", "reason": "reason"}},
        {"outcome": {"kind": "abstention", "reason": " "}},
        {"procedure": procedure(), "abstention_reason": "No abstention is needed."},
    ],
)
def test_outcome_shape_rejects_contradictions_without_dropping_fields(payload):
    with pytest.raises(ValidationError):
        EvidenceProposal.model_validate(payload)


@pytest.mark.parametrize("kind", ["procedure", "abstention"])
def test_outcome_resolves_to_unchanged_public_proposal(kind):
    from sibyl_core.tasks.consolidation import ProcedureProposal
    from sibyl_core.tasks.episode_evidence import EvidenceCitation
    from sibyl_core.tasks.procedure_evidence import resolve_evidence_proposal

    outcome = (
        {"kind": "procedure", "procedure": procedure()}
        if kind == "procedure"
        else {"kind": "abstention", "reason": "Insufficient evidence"}
    )
    internal = EvidenceProposal.model_validate({"outcome": outcome})
    public = ProcedureProposal.model_validate(
        resolve_evidence_proposal(
            internal, {"fixture": EvidenceCitation("original-episode", ((7, 19),))}
        )
    )
    assert (public.procedure is not None) == (kind == "procedure")
    if public.procedure is not None:
        assert public.procedure.goal.support[0].model_dump() == {
            "episode_id": "original-episode",
            "start_byte": 7,
            "end_byte": 19,
        }
        assert public.abstention_reason is None
    else:
        assert public.abstention_reason == "Insufficient evidence"
