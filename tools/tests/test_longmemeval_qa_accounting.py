from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from tools.tests.test_longmemeval_live import _load_live_module

from sibyl_core.ai.transport import RecordingOpenAIClient

READER_LIMIT = 128
JUDGE_LIMIT = 64
REPORTED_INPUT = 10
FIRST_JUDGE_CALL = 2


@pytest.mark.parametrize(
    "outcome", ["success", "judge_failure", "cancelled", "retry", "validation_retry"]
)
async def test_native_qa_stage_receipts_survive_terminal_outcome(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str
) -> None:
    live = _load_live_module()
    qa = importlib.import_module("longmemeval_qa")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-fixture")
    requests: list[dict[str, Any]] = []
    receipts: dict[str, Any] = {}
    path = tmp_path / "case.json"

    def retain(stage: dict[str, Any]) -> None:
        receipts[stage["stage"]] = stage
        live._write_report(path, receipts)

    def respond(request: httpx2.Request) -> httpx2.Response:
        wire = json.loads(request.content)
        requests.append(wire)
        judge = bool(wire.get("tools"))
        if judge:
            prior = json.loads(path.read_text())
            assert prior["reader"]["status"] == "completed"
            assert prior["reader"]["output"] == "Blue"
            if outcome == "cancelled":
                raise asyncio.CancelledError
            if outcome == "judge_failure" or (
                outcome == "retry" and len(requests) == FIRST_JUDGE_CALL
            ):
                return httpx2.Response(
                    500, json={"error": {"message": "controlled"}}, headers={"retry-after-ms": "1"}
                )
            output = [
                {
                    "type": "function_call",
                    "id": "fc_fixture",
                    "call_id": "call_fixture",
                    "name": wire["tools"][0]["name"],
                    "arguments": json.dumps(
                        {
                            "correct": True,
                            "score": "invalid"
                            if outcome == "validation_retry" and len(requests) == FIRST_JUDGE_CALL
                            else 1,
                            "rationale": "Supported",
                        }
                    ),
                }
            ]
        else:
            output = [
                {
                    "type": "message",
                    "id": "msg_fixture",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Blue", "annotations": []}],
                }
            ]
        return httpx2.Response(
            200,
            json={
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 0,
                "model": wire["model"],
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
            },
        )

    async with RecordingOpenAIClient(transport=httpx2.MockTransport(respond)) as http:
        client = AsyncOpenAI(api_key="offline-fixture", http_client=http, max_retries=1)
        monkeypatch.setattr(
            qa,
            "build_model",
            lambda config: OpenAIResponsesModel(
                config.model, provider=OpenAIProvider(openai_client=client)
            ),
        )
        config = qa.LongMemEvalQAConfig(
            mode="model",
            reader_max_output_tokens=128,
            judge_max_output_tokens=64,
            transport_max_retries=1,
        )
        entry = {
            "question": "Color?",
            "answer": "Blue",
            "answer_session_ids": ["s1"],
            "haystack_session_ids": ["s1"],
            "haystack_dates": ["2025/01/01"],
            "haystack_sessions": [[{"role": "user", "content": "Blue"}]],
        }
        call = qa.evaluate_longmemeval_case_qa(
            entry,
            ranked_session_ids=["s1"],
            corpus_text_policy="user-and-assistant-turns-v1",
            config=config,
            on_stage=retain,
        )
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            result = await call
            assert result["evaluated"] is (outcome != "judge_failure")
            if outcome == "judge_failure":
                assert result["generated_answer"] == "Blue"
                aggregate, _ = live._aggregate(
                    [
                        {
                            "question_type": "single-session-user",
                            "qa": result,
                            "cross_question_result_count": 0,
                            "created_entity_count": 0,
                            "chunked_session_count": 0,
                            **{
                                f"{name}@5": 0
                                for name in ("hit", "legacy_recall", "recall", "ndcg")
                            },
                        }
                    ],
                    [5],
                )
                assert aggregate["qa_evaluated_count"] == 0
                assert aggregate["qa_attempted_count"] == aggregate["qa_failed_count"] == 1
        await client.close()
    if outcome == "validation_retry":
        assert len(requests) == FIRST_JUDGE_CALL + 1
        assert "score" in json.dumps(requests[-1]["input"])
    retained = json.loads(path.read_text())
    assert retained["reader"]["reported_input_tokens"] == REPORTED_INPUT
    assert retained["reader"]["usage_complete"] is True
    assert retained["reader"]["output"] == "Blue"
    assert requests[0]["max_output_tokens"] == READER_LIMIT
    assert all(wire["max_output_tokens"] == JUDGE_LIMIT for wire in requests[1:])
    assert retained["judge"]["status"] == {"cancelled": "cancelled", "judge_failure": "failed"}.get(
        outcome, "completed"
    )
    if outcome in {"cancelled", "judge_failure", "retry"}:
        assert retained["judge"]["usage_complete"] is False
        assert retained["judge"]["reported_cost_usd"] is None
    assert retained["judge"]["prompt_sha256"] != retained["reader"]["prompt_sha256"]
    assert retained["reader"]["system_prompt_sha256"]
    assert retained["judge"]["output_schema_sha256"]


def test_atomic_report_preserves_prior_receipt_on_serialization_error(tmp_path: Path) -> None:
    live = _load_live_module()
    path = tmp_path / "case.json"
    live._write_report(path, {"reader": "completed"})
    with pytest.raises(TypeError):
        live._write_report(path, {"reader": object()})
    assert json.loads(path.read_text()) == {"reader": "completed"}
    assert not path.with_name("case.json.tmp").exists()


@pytest.mark.parametrize("maximum", [0, -1, True])
def test_output_policy_rejects_invalid_limits(maximum: int) -> None:
    _load_live_module()
    qa = importlib.import_module("longmemeval_qa")
    with pytest.raises(ValueError, match="output limits"):
        qa.LongMemEvalQAConfig(reader_max_output_tokens=maximum)


async def test_live_cancel_checkpoint_references_completed_reader(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live = _load_live_module()
    data = tmp_path / "input.json"
    report = tmp_path / "report.json"
    data.write_text(
        json.dumps([{"question_id": "fixture", "question_type": "single-session-user"}])
    )

    async def cancel_case(entry: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        callback = kwargs["on_qa_stage"]
        callback(
            kwargs["case_index"], {"stage": "reader", "status": "completed", "output": "retained"}
        )
        callback(kwargs["case_index"], {"stage": "judge", "status": "cancelled"})
        raise asyncio.CancelledError

    monkeypatch.setattr(live, "_run_case", cancel_case)
    with pytest.raises(asyncio.CancelledError):
        await live.run_benchmark(
            data, api_url="http://fixture/api", verify_sha256=False, output_path=report
        )
    checkpoint = json.loads(report.read_text())
    reference = checkpoint["qa_stage_receipts"]["0"]["path"]
    stored = json.loads(await asyncio.to_thread(Path(reference).read_text))
    assert stored["stages"]["reader"]["output"] == "retained"
    assert stored["stages"]["judge"]["status"] == "cancelled"
    assert checkpoint["completion_status"] == "partial"
    assert checkpoint["qa_execution_summary"] == {
        "attempted_cases": 1,
        "failed_cases": 0,
        "cancelled_cases": 1,
    }
