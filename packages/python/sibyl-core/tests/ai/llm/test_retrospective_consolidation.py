"""Historical completion does not replace the current consolidation request."""

import json
from unittest.mock import AsyncMock

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from tests.test_episode_evidence import _contrast_group, _proposal
from tests.test_tasks_consolidation import group as group
from tests.test_tasks_consolidation import procedure as procedure

from sibyl_core.ai.llm import Extractor
from sibyl_core.ai.llm import extractor as extraction
from sibyl_core.ai.transport import RecordingOpenAIClient
from sibyl_core.tasks import consolidation as c


@pytest.mark.parametrize("projected", [False, True])
@pytest.mark.parametrize("outcome", ["procedure", "abstention"])
async def test_native_retrospective_request_preserves_both_valid_outcomes(
    monkeypatch, group, procedure, projected, outcome
):
    monkeypatch.setattr(extraction, "reserve_llm_budget", AsyncMock())
    source = _contrast_group() if projected else group
    # Both fixtures deliberately use one family and compatible environments.
    assert len({episode.family_id for episode in source.episodes}) == 1
    assert all(episode.environment == source.episodes[0].environment for episode in source.episodes)
    reason = "The evidence does not identify an action and check that distinguish the outcomes."
    if projected:
        output = {
            "outcome": {"kind": "procedure", **_proposal()}
            if outcome == "procedure"
            else {"kind": "abstention", "reason": reason}
        }
    else:
        output = c.ProcedureProposal(
            procedure=procedure if outcome == "procedure" else None,
            abstention_reason=reason if outcome == "abstention" else None,
        ).model_dump(mode="json")
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "retrospective",
                "object": "response",
                "created_at": 0,
                "model": "qwen/qwen3-coder-next",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "historical-evidence-assessment",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": json.dumps(output), "annotations": []}
                        ],
                    }
                ],
                "usage": {"input_tokens": 31, "output_tokens": 17, "total_tokens": 48},
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

        async def agent(extractor):
            return Agent(
                model,
                instructions=extractor.system_prompt,
                output_type=NativeOutput(extractor.output_type, strict=True),
                retries={"output": extractor.output_retries},
            )

        monkeypatch.setattr(Extractor, "_get_agent", agent)
        result = await c.propose_conditional_procedure(
            source,
            max_input_chars=800_000,
            max_tokens=8192,
            output_mode="native_strict",
            model_override="qwen/qwen3-coder-next",
        )
    assert len(requests) == 1
    wire = requests[0]
    assert "retrospective memory consolidation" in wire["instructions"]
    assert "not the agent executing any recorded task" in wire["instructions"]
    assert c.RETROSPECTIVE_REQUEST in wire["instructions"]
    assert "unverified conditional candidate" in wire["instructions"]
    assert "neither establish nor disqualify" in wire["instructions"]
    assert "different domains or environments is not required" in wire["instructions"]
    assert "Transfer must be evaluated separately" in wire["instructions"]
    assert "cannot support useful conditions, actions and checks, abstain" in wire["instructions"]
    user_input = next(item["content"] for item in wire["input"] if item.get("role") == "user")
    assert user_input.endswith(c.RETROSPECTIVE_REQUEST)
    assert (
        "future agent" in user_input
        and "Consider both successful and failed outcomes" in user_input
    )
    assert result.prompt == user_input
    assert result.receipt["usage"]["input_tokens"] == 31
    assert result.receipt["usage"]["output_tokens"] == 17
    assert result.receipt["output_retries"] == 2
    assert result.receipt["transfer"] == "not_measured"
    assert result.receipt["entailment"] == "pending"
    if outcome == "abstention":
        assert result.candidate is None and result.receipt["reason"] == reason
        assert result.receipt["status"] == "abstained"
    else:
        assert result.candidate is not None and result.receipt["status"] == "proposed"
        assert not c.validate_candidate_content_agreement(result.candidate, group=source)


def test_retrospective_wrapper_preserves_projected_evidence_and_citations():
    source = _contrast_group()
    evidence = c._extraction_input(source)
    document = evidence.prompt.split("\nEvidence view:\n", 1)[1]
    serialized, footer = document.rsplit("\n\n", 1)
    assert footer == c.RETROSPECTIVE_REQUEST
    assert evidence.projection_receipt["view_sha256"] == c._digest(serialized.encode())
    assert set(evidence.citations) >= {"s0.outcome", "s1.outcome", "s0.e3", "s1.e3"}
    assert c.RETROSPECTIVE_REQUEST in c.SYSTEM_PROMPT
    assert c.RETROSPECTIVE_REQUEST in c.EVIDENCE_SYSTEM_PROMPT
