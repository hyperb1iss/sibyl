"""Frozen synthetic paired critic calls, with raw receipts and offline validation."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import time
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx
from pydantic import TypeAdapter

from sibyl_core.ai.llm.config import LLMSurface
from sibyl_core.ai.llm.extractor import ExtractionResult, ExtractionUsage, Extractor
from sibyl_core.memory_pipeline.observations import SourceIdentity, SourceKind, SourceObservation
from sibyl_core.models.reflection import ClaimRecord, ReflectionCandidate
from sibyl_core.tasks._evidence_json import canonical, read_json_value
from sibyl_core.tasks.episode_evidence import EvidenceCitation
from sibyl_core.tasks.memory_validation import (
    CriticOutput,
    OriginalValidationEvidence,
    PreparedMemoryValidation,
    prepare_reflection_validation,
    run_memory_validation,
)
from sibyl_core.tasks.procedure_review import review_digest

from .runner import _sha, _write
from .support_inputs import ORG, make_request
from .support_study import _code_hashes

CONTROLS: dict[str, Any] = {
    "model": "anthropic/claude-opus-5",
    "observed_models": ["anthropic/claude-opus-5", "anthropic/claude-opus-5-20260723"],
    "provider": {"only": ["anthropic"], "allow_fallbacks": False, "require_parameters": True},
    "observed_provider": "Anthropic",
    "max_tokens": 4096,
    "temperature": "omitted_provider_default",
    "concurrency": 8,
    "retries": 0,
    "timeout_seconds": 180,
    "endpoint": "https://openrouter.ai/api/v1/chat/completions",
}
VERSION = "jev-critic-pair-v1"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def prepare_case(case: dict[str, Any], run_id: str) -> PreparedMemoryValidation:
    """Reconstruct the unchanged support adapter's original critic preparation."""
    evidence = []
    for source in case["sources"]:
        content = source["text"].encode()
        observation = SourceObservation(
            SourceIdentity(ORG, SourceKind.RAW_CAPTURE, source["id"]),
            generation=1,
            content_sha256=hashlib.sha256(content).hexdigest(),
            revision=1,
            durable=True,
            incarnation="synthetic-first",
        )
        evidence.append(
            OriginalValidationEvidence(
                source["id"], content, review_digest(asdict(observation)), source["provenance"]
            )
        )
    source_ids = [source["id"] for source in case["sources"]]
    candidate = ReflectionCandidate(
        "claim",
        "Synthetic candidate",
        case["content"],
        "Source-support qualification",
        0.5,
        raw_source_ids=source_ids,
        claim_records=[
            ClaimRecord(
                text, source_ids, 0.5, id=f"claim-{index}", created_at="2026-01-01T00:00:00+00:00"
            )
            for index, text in enumerate(case["claims"])
        ],
    )
    prepared = prepare_reflection_validation(
        candidate,
        parent_operation_id=review_digest(run_id),
        parent_candidate_sha256=review_digest(candidate.to_dict()),
        evidence=evidence,
        citations={
            f"s{index}": EvidenceCitation(item.source_id, ((0, len(item.content)),))
            for index, item in enumerate(evidence)
        },
    )
    semantic = json.loads(prepared.payload_json)
    semantic.pop("parent_operation_id")
    semantic.pop("parent_candidate_sha256")
    for source in semantic["sources"].values():
        source.pop("observation_sha256")
    if canonical(semantic) != make_request(case, run_id).state:
        raise ValueError("critic preparation differs from original Jev semantic state")
    return prepared


def critic_request(prepared: PreparedMemoryValidation) -> dict[str, Any]:
    """Pin a single forced tool; generation temperature uses provider default."""
    return {
        "model": CONTROLS["model"],
        "provider": deepcopy(CONTROLS["provider"]),
        "max_tokens": CONTROLS["max_tokens"],
        "messages": [{"role": "user", "content": prepared.prompt}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "CriticOutput",
                    "description": "Return the source-bound critique.",
                    "parameters": CriticOutput.model_json_schema(),
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "CriticOutput"}},
    }


def _now() -> str:
    return datetime.now(UTC).isoformat()


def code_hashes() -> dict[str, str]:
    from . import critic_pair_analysis  # noqa: PLC0415 - shared CLI owners import each other

    return {
        **_code_hashes(),
        "benchmark/critic_pair.py": _sha(Path(__file__)),
        "benchmark/critic_pair_analysis.py": _sha(Path(critic_pair_analysis.__file__)),
    }


def prepare_entries(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Recheck every frozen request before creating output or reading a provider key."""
    if plan["controls"] != CONTROLS or plan["version"] != VERSION:
        raise ValueError("critic controls differ from frozen plan")
    entries = []
    seen = set()
    for call in plan["calls"]:
        identifier = call["id"]
        if (
            not isinstance(identifier, str)
            or not identifier
            or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for c in identifier
            )
            or identifier in seen
        ):
            raise ValueError("invalid or duplicate call id")
        seen.add(identifier)
        prepared = prepare_case(call["case"], call["preparation_run_id"])
        request = critic_request(prepared)
        expected = {
            "expected_jev_state_sha256": digest(
                make_request(call["case"], call["preparation_run_id"]).state
            ),
            "prepared_payload_sha256": digest(prepared.payload_json),
            "prompt_sha256": digest(prepared.prompt),
            "request_sha256": digest(canonical(request)),
        }
        if any(call.get(key) != value for key, value in expected.items()):
            raise ValueError("frozen critic request binding mismatch")
        entries.append(
            {
                "id": identifier,
                "request": request,
                "request_sha256": expected["request_sha256"],
                "prepared_payload": prepared.payload_json,
            }
        )
    if not entries:
        raise ValueError("empty critic plan")
    return entries


def _usage(body: Any, *, dispatched: bool, responded: bool) -> dict[str, Any]:
    source = body.get("usage", {}) if isinstance(body, dict) else {}
    source = source if isinstance(source, dict) else {}
    tokens = {
        key: source.get(field)
        for key, field in (
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
        )
    }
    tokens = {
        key: value if type(value) is int and value >= 0 else None for key, value in tokens.items()
    }
    cost = source.get("cost")
    cost = (
        cost
        if isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(cost)
        and cost >= 0
        else None
    )
    return {
        **tokens,
        "observed_cost_usd": cost,
        "attempt_count": 1 if responded else (None if dispatched else 0),
        "attempt_count_known": responded or not dispatched,
        "usage_complete": cost is not None and all(v is not None for v in tokens.values()),
        "model": body.get("model") if isinstance(body, dict) else None,
        "provider": body.get("provider") if isinstance(body, dict) else None,
    }


class _RecordedExtractor(Extractor[CriticOutput]):
    def __init__(self, prompt: str, output: CriticOutput, usage: ExtractionUsage) -> None:
        super().__init__(
            CriticOutput,
            surface=LLMSurface.MEMORY,
            model_override=str(CONTROLS["model"]),
            output_retries=0,
            max_tokens=4096,
            openrouter_provider="anthropic",
        )
        self.prompt = prompt
        self.recorded = ExtractionResult(output, usage)

    async def extract_with_usage(self, prompt: str) -> ExtractionResult[CriticOutput]:
        if prompt != self.prompt:
            raise ValueError("offline critic prompt changed")
        return self.recorded


def _response_body(raw: dict[str, Any]) -> Any:
    if raw["response_body"] is None:
        return None
    body_bytes = base64.b64decode(raw["response_body_base64"], validate=True)
    if body_bytes.decode("utf-8", errors="replace") != raw["response_body"]:
        raise ValueError("response text differs from raw bytes")
    return read_json_value(body_bytes)


def _output(body: Any, status: int | None, usage: dict[str, Any]) -> CriticOutput:
    if status != HTTPStatus.OK:
        raise ValueError("http_error")
    if not isinstance(body, dict):
        raise TypeError("invalid_response_json")
    if body.get("model") not in CONTROLS["observed_models"] or body.get("provider") != "Anthropic":
        raise ValueError("route_mismatch")
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("invalid_choice_count")
    choice = choices[0]
    if not isinstance(choice, dict) or choice.get("finish_reason") != "tool_calls":
        raise ValueError("incomplete_tool_output")
    tool_calls = choice.get("message", {}).get("tool_calls")
    if not isinstance(tool_calls, list) or len(tool_calls) != 1:
        raise ValueError("invalid_tool_count")
    tool = tool_calls[0]
    if tool.get("type") != "function" or tool.get("function", {}).get("name") != "CriticOutput":
        raise ValueError("invalid_tool_name")
    output = CriticOutput.model_validate(read_json_value(tool["function"]["arguments"].encode()))
    if any(usage[key] is None for key in ("input_tokens", "output_tokens", "total_tokens")):
        raise ValueError("missing_token_usage")
    return output


async def interpret(entry: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    """Retain independent accounting even when route, syntax or mechanics fail."""
    body = None
    error = raw["error_code"]
    try:
        body = _response_body(raw)
    except (ValueError, UnicodeError):
        error = "invalid_response_json"
    usage = _usage(
        body,
        dispatched=raw["dispatch_started_at"] is not None,
        responded=raw["http_status"] is not None,
    )
    result = None
    if error is None:
        try:
            output = _output(body, raw["http_status"], usage)
            recorded_usage = ExtractionUsage(
                provider=usage["provider"],
                model=usage["model"],
                requests=1,
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                total_tokens=usage["total_tokens"],
                cost_usd=usage["observed_cost_usd"],
                cost_complete=usage["observed_cost_usd"] is not None,
            )
            prepared = PreparedMemoryValidation(entry["prepared_payload"])
            validation = await run_memory_validation(
                prepared, _RecordedExtractor(prepared.prompt, output, recorded_usage)
            )
            result = TypeAdapter(type(validation)).dump_python(validation, mode="json")
        except Exception as exc:
            # Exception text may contain provider content; only fixed codes may leave here.
            allowed = {
                "http_error",
                "invalid_response_json",
                "route_mismatch",
                "invalid_choice_count",
                "incomplete_tool_output",
                "invalid_tool_count",
                "invalid_tool_name",
                "missing_token_usage",
            }
            error = (
                str(exc)
                if type(exc) is ValueError and str(exc) in allowed
                else "invalid_critic_output"
            )
    return {
        **raw,
        "execution_status": "completed" if result is not None else "failed",
        "error_code": error,
        "result": result,
        "usage": usage,
    }


async def _dispatch(client: httpx.AsyncClient, entry: dict[str, Any], out: Path) -> dict[str, Any]:
    raw = {
        "id": entry["id"],
        "request": entry["request"],
        "request_sha256": entry["request_sha256"],
        "http_status": None,
        "response_body": None,
        "response_body_base64": None,
        "dispatch_started_at": _now(),
        "completed_at": None,
        "elapsed_ms": None,
        "error_code": None,
    }
    _write(out / "dispatch" / f"{entry['id']}.json", raw)
    started = time.monotonic()
    try:
        response = await client.post(str(CONTROLS["endpoint"]), json=entry["request"])
        raw.update(
            http_status=response.status_code,
            response_body=response.content.decode("utf-8", errors="replace"),
            response_body_base64=base64.b64encode(response.content).decode("ascii"),
        )
    except asyncio.CancelledError:
        raw["error_code"] = "cancelled"
    except Exception:
        raw["error_code"] = "transport_error"
    raw.update(completed_at=_now(), elapsed_ms=(time.monotonic() - started) * 1000)
    _write(out / "receipts" / f"{entry['id']}.json", raw)
    return await interpret(entry, raw)


async def replay_calls(entries: list[dict[str, Any]], archive: Path) -> list[dict[str, Any]]:
    """Fail closed on any missing, corrupted or accounting-divergent receipt."""
    archived = read_json_value((archive / "calls.json").read_bytes())
    if not isinstance(archived, list) or len(archived) != len(entries):
        raise ValueError("incomplete archived critic calls")
    names = {f"{entry['id']}.json" for entry in entries}
    for directory in ("receipts", "dispatch"):
        if {path.name for path in (archive / directory).iterdir()} != names:
            raise ValueError("archived critic artifact set mismatch")
    replayed = []
    for entry, prior in zip(entries, archived, strict=True):
        raw = read_json_value((archive / "receipts" / f"{entry['id']}.json").read_bytes())
        if (
            not isinstance(raw, dict)
            or raw.get("id") != entry["id"]
            or raw.get("request") != entry["request"]
            or raw.get("request_sha256") != entry["request_sha256"]
        ):
            raise ValueError("archived critic request mismatch")
        dispatch = read_json_value((archive / "dispatch" / f"{entry['id']}.json").read_bytes())
        expected_dispatch = {
            **raw,
            "http_status": None,
            "response_body": None,
            "response_body_base64": None,
            "completed_at": None,
            "elapsed_ms": None,
            "error_code": None,
        }
        if dispatch != expected_dispatch:
            raise ValueError("archived critic dispatch mismatch")
        call = await interpret(entry, raw)
        if call != prior:
            raise ValueError("archived critic receipt differs from derived call")
        replayed.append(call)
    return replayed


async def _live(entries: list[dict[str, Any]], out: Path, key: str) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(8)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {key}"},
        timeout=180,
        transport=httpx.AsyncHTTPTransport(retries=0),
        follow_redirects=False,
    ) as client:

        async def observe(entry: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await _dispatch(client, entry, out)

        tasks = [asyncio.create_task(observe(entry)) for entry in entries]
        try:
            calls = await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await asyncio.gather(*tasks, return_exceptions=True)
            calls = []
            for entry, task in zip(entries, tasks, strict=True):
                if not task.cancelled() and task.exception() is None:
                    calls.append(task.result())
                    continue
                receipt = out / "receipts" / f"{entry['id']}.json"
                dispatch = out / "dispatch" / f"{entry['id']}.json"
                if receipt.exists():
                    raw = read_json_value(receipt.read_bytes())
                else:
                    raw = (
                        read_json_value(dispatch.read_bytes())
                        if dispatch.exists()
                        else {
                            "id": entry["id"],
                            "request": entry["request"],
                            "request_sha256": entry["request_sha256"],
                            "http_status": None,
                            "response_body": None,
                            "response_body_base64": None,
                            "dispatch_started_at": None,
                            "completed_at": None,
                            "elapsed_ms": None,
                            "error_code": None,
                        }
                    )
                    if not dispatch.exists():
                        _write(dispatch, raw)
                    raw.update(error_code="cancelled", completed_at=_now())
                    _write(receipt, raw)
                calls.append(await interpret(entry, raw))
    return calls


async def run(
    plan: dict[str, Any], out: Path, *, live: bool = False, replay: Path | None = None
) -> list[dict[str, Any]]:
    from .critic_pair_analysis import summarize, validate_plan  # noqa: PLC0415 - shared CLI owners

    validate_plan(plan)
    entries = prepare_entries(plan)
    manifest = {
        "version": VERSION,
        "plan_sha256": plan["plan_sha256"],
        "controls": CONTROLS,
        "code_hashes": code_hashes(),
        "scheduled_calls": len(entries),
    }
    calls = None
    if replay is not None:
        if read_json_value((replay / "manifest.json").read_bytes()) != manifest:
            raise ValueError("critic replay manifest mismatch")
        if read_json_value((replay / "requests.json").read_bytes()) != entries:
            raise ValueError("critic replay request mismatch")
        calls = await replay_calls(entries, replay)
    if live and replay is not None:
        raise ValueError("live and replay are mutually exclusive")
    key = os.environ.get("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY") if live else None
    if live and not key:
        raise ValueError("synthetic critic key is required")
    out.mkdir(parents=True, exist_ok=False)  # noqa: ASYNC240 - exclusive local receipt setup
    _write(out / "manifest.json", manifest)
    _write(out / "plan.json", plan)
    _write(out / "requests.json", entries)
    (out / "receipts").mkdir()
    (out / "dispatch").mkdir()
    if live:
        calls = await _live(entries, out, str(key))
    elif replay is not None:
        for entry in entries:
            for directory in ("receipts", "dispatch"):
                _write(
                    out / directory / f"{entry['id']}.json",
                    read_json_value((replay / directory / f"{entry['id']}.json").read_bytes()),
                )
    if calls is not None:
        _write(out / "calls.json", calls)
        _write(out / "summary.json", summarize(plan, calls))
    _write(
        out / "completion.json",
        {
            "mode": "live" if live else "replay" if replay is not None else "prepare",
            "scheduled": len(entries),
            "completed": sum(call["execution_status"] == "completed" for call in calls or []),
            "failed": sum(call["execution_status"] != "completed" for call in calls or []),
            "dispatched": calls is not None,
        },
    )
    return calls if calls is not None else []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", type=Path)
    args = parser.parse_args()
    asyncio.run(
        run(
            read_json_value(args.plan.read_bytes()),
            args.output_dir,
            live=args.live,
            replay=args.replay,
        )
    )


if __name__ == "__main__":
    main()
