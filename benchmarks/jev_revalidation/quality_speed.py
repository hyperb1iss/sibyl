"""Prospective synthetic validation-path comparison; no publication authority."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import random
import shutil
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from sibyl_core.ai.decisions import DecisionRequest
from sibyl_core.ai.openrouter_decisions import (
    DECISIONS_ENDPOINT,
    OpenRouterDecisionProvider,
    OpenRouterDecisionRoute,
)
from sibyl_core.tasks._evidence_json import canonical, read_json_value
from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

from . import critic_pair, quality_speed_analysis, support_inputs
from . import support_fallback_policy as policy
from .quality_speed_analysis import summarize
from .runner import _sha, _write

VERSION = "jev-quality-speed-v1"
ARMS = ("critic", "jev_then_critic")
THRESHOLD = 0.99
CONCURRENCY = 8
REPEATS = 2
SEED = 20260920


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def wire_request(request: Any) -> dict[str, Any]:
    route = OpenRouterDecisionRoute()
    return {
        "model": route.requested_model_id,
        "state": request.state,
        "questions": {
            question.question_id: {
                "type": "choice",
                "instructions": question.instructions,
                "criteria": {option.label: option.description for option in question.options},
            }
            for question in request.questions
        },
        "provider": route.provider_preferences,
    }


def entries(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rng = random.Random(SEED)  # noqa: S311 - reproducible experiment order
    pairs = [(index, repeat) for index in range(len(cases)) for repeat in range(REPEATS)]
    rng.shuffle(pairs)
    result = []
    for index, repeat in pairs:
        case = cases[index]
        run_id = f"{VERSION}:case-{index}:repeat-{repeat}"
        prepared = critic_pair.prepare_case(case, run_id)
        decision = support_inputs.make_request(case, run_id)
        arms = list(ARMS)
        rng.shuffle(arms)
        for arm in arms:
            result.append(
                {
                    "id": f"case-{index}-repeat-{repeat}-{arm}",
                    "case_id": case["id"],
                    "case_index": index,
                    "repeat": repeat,
                    "arm": arm,
                    "run_id": run_id,
                    "prepared_payload": prepared.payload_json,
                    "critic_request": critic_pair.critic_request(prepared),
                    "jev_request": decision.model_dump(mode="json"),
                    "jev_wire": wire_request(decision),
                }
            )
    return result


def manifest(cases_path: Path) -> dict[str, Any]:
    cases = support_inputs.load_cases(cases_path)
    if any(c.get("expected_action") not in {"accept", "flag", "abstain"} for c in cases):
        raise ValueError("full-critic gold is required")
    for case in cases:
        allowed = case.get("acceptable_actions", [case["expected_action"]])
        if (
            not isinstance(allowed, list)
            or not allowed
            or set(allowed) - {"accept", "flag", "abstain"}
            or case["expected_action"] not in allowed
            or ("accept" in allowed and allowed != ["accept"])
        ):
            raise ValueError("invalid predeclared action rubric")
    hashes = critic_pair.code_hashes()
    hashes["benchmark/quality_speed.py"] = _sha(Path(__file__))
    hashes["benchmark/quality_speed_analysis.py"] = _sha(Path(quality_speed_analysis.__file__))
    hashes["benchmark/support_fallback_policy.py"] = _sha(Path(policy.__file__))
    return {
        "version": VERSION,
        "cases_sha256": _sha(cases_path),
        "code_hashes": hashes,
        "schedule_sha256": digest(entries(cases)),
        "case_count": len(cases),
        "repeats": REPEATS,
        "seed": SEED,
        "concurrency": CONCURRENCY,
        "threshold": THRESHOLD,
        "numeric_guard": True,
        "critic_controls": critic_pair.CONTROLS,
        "jev_route": OpenRouterDecisionRoute().model_dump(mode="json"),
        "jev_policy_sha256": OpenRouterDecisionRoute().policy_sha256,
        "gate": {
            "false_accepts": 0,
            "coverage_loss": 0,
            "rubric_action_loss": 0,
            "median_service_ratio_max": 0.8,
            "p95_service_ratio_max": 1.0,
            "cost_ratio_max": 1.0,
            "unknown_cost_calls": 0,
        },
        "scope": "synthetic validation and evidence availability; not QA or interactive recall",
        "timing": "prepared evidence to final action; route/fallback/validation included; acquisition and queue excluded",
    }


async def send(
    client: httpx.AsyncClient, endpoint: str, request: dict[str, Any], path: Path
) -> dict[str, Any]:
    raw = {
        "endpoint": endpoint,
        "request": request,
        "request_sha256": digest(request),
        "http_status": None,
        "response_body": None,
        "response_body_base64": None,
        "dispatch_started_at": critic_pair._now(),
        "completed_at": None,
        "elapsed_ms": None,
        "error_code": None,
    }
    _write(path.with_suffix(".dispatch.json"), raw)
    started = time.monotonic()
    try:
        response = await client.post(endpoint, json=request)
        raw.update(
            http_status=response.status_code,
            response_body=response.content.decode("utf-8", errors="replace"),
            response_body_base64=base64.b64encode(response.content).decode("ascii"),
        )
    except asyncio.CancelledError:
        raw["error_code"] = "cancelled"
        raise
    except Exception:
        raw["error_code"] = "transport_error"
    finally:
        raw.update(completed_at=critic_pair._now(), elapsed_ms=(time.monotonic() - started) * 1000)
        _write(path, raw)
    return raw


def raw_receipt(
    archive: Path, identifier: str, stage: str, endpoint: str, request: dict[str, Any]
) -> dict[str, Any]:
    path = archive / "raw" / f"{identifier}.{stage}.json"
    raw = read_json_value(path.read_bytes())
    if (
        raw.get("endpoint") != endpoint
        or raw.get("request") != request
        or raw.get("request_sha256") != digest(request)
    ):
        raise ValueError("raw request binding mismatch")
    dispatch = read_json_value(path.with_suffix(".dispatch.json").read_bytes())
    expected = {
        **raw,
        "http_status": None,
        "response_body": None,
        "response_body_base64": None,
        "completed_at": None,
        "elapsed_ms": None,
        "error_code": None,
    }
    if dispatch != expected:
        raise ValueError("dispatch binding mismatch")
    return raw


def critic_action(critic: dict[str, Any]) -> str:
    validation = critic["result"]
    if validation is None or validation["reason"] == "critic_output_failed_mechanical_validation":
        return "error"
    return {"no_findings": "accept", "reconsider": "flag", "abstain": "abstain"}[
        validation["status"]
    ]


def stage_costs(
    stages: list[dict[str, Any]], decision: dict[str, Any] | None, critic: dict[str, Any] | None
) -> list[float | None]:
    return [
        decision["observed_cost_usd"]
        if stage["stage"] == "jev" and decision is not None
        else critic["usage"]["observed_cost_usd"]
        if stage["stage"] == "critic" and critic is not None
        else None
        for stage in stages
    ]


def copy_archive(archive: Path, out: Path) -> None:
    for directory in ("raw", "paths"):
        for source in (archive / directory).iterdir():
            shutil.copyfile(source, out / directory / source.name)


async def execute(
    entry: dict[str, Any],
    case: dict[str, Any],
    *,
    client: httpx.AsyncClient | None,
    out: Path | None,
    archive: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    started_at = critic_pair._now()
    prepared = PreparedMemoryValidation(entry["prepared_payload"])
    if critic_pair.critic_request(prepared) != entry["critic_request"]:
        raise ValueError("prepared critic changed")
    stages: list[dict[str, Any]] = []

    async def observe(stage: str, endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        stages.append({"stage": stage})
        if archive is not None:
            raw = raw_receipt(archive, entry["id"], stage, endpoint, request)
        else:
            assert client is not None
            assert out is not None
            raw = await send(client, endpoint, request, out / "raw" / f"{entry['id']}.{stage}.json")
        return raw

    route, reason = "fallback", "baseline"
    decision_dump = None
    if entry["arm"] == "jev_then_critic":
        decision = DecisionRequest.model_validate_json(canonical(entry["jev_request"]))
        if decision.model_dump(mode="json") != entry["jev_request"]:
            raise ValueError("prepared Jev changed")

        async def forward(request: httpx.Request) -> httpx.Response:
            sent = read_json_value(await request.aread())
            if str(request.url) != DECISIONS_ENDPOINT or sent != entry["jev_wire"]:
                raise ValueError("Jev wire request changed")
            raw = await observe("jev", DECISIONS_ENDPOINT, sent)
            if raw["http_status"] is None:
                if raw["error_code"] == "cancelled":
                    raise httpx.ReadTimeout("recorded deadline")
                raise httpx.ConnectError("recorded transport error")
            body = base64.b64decode(raw["response_body_base64"], validate=True)
            if body.decode("utf-8", errors="replace") != raw["response_body"]:
                raise ValueError("raw response bytes mismatch")
            return httpx.Response(raw["http_status"], content=body)

        async with OpenRouterDecisionProvider(
            "recording-transport", transport=httpx.MockTransport(forward)
        ) as provider:
            observation = await provider.decide(decision)
        observation.validate_for(
            decision, expected_model_id=OpenRouterDecisionRoute().resolved_model_id
        )
        decision_dump = observation.model_dump(mode="json")
        # Adapter wall time is not replay-stable; raw and outer timing are retained separately.
        decision_dump.pop("elapsed_ms")
        call = {
            **decision_dump,
            "case_id": case["id"],
            "repeat": entry["repeat"],
            "arm": "grouped",
            "question_ids": [q.question_id for q in decision.questions],
        }
        route, reason = policy.route(policy.candidate_row(case, entry["repeat"], [call]), THRESHOLD)
    critic = None
    if route == "fallback":
        request = entry["critic_request"]
        raw = await observe("critic", str(critic_pair.CONTROLS["endpoint"]), request)
        critic = await critic_pair.interpret(
            {"prepared_payload": entry["prepared_payload"]}, {**raw, "id": entry["id"]}
        )
        action = critic_action(critic)
    else:
        action = "accept"
    costs = stage_costs(stages, decision_dump, critic)
    return {
        "id": entry["id"],
        "case_id": case["id"],
        "repeat": entry["repeat"],
        "arm": entry["arm"],
        "route": route,
        "route_reason": reason,
        "action": action,
        "jev_observation": decision_dump,
        "critic": critic,
        "stages": [s["stage"] for s in stages],
        "known_cost_usd": str(sum((Decimal(str(c)) for c in costs if c is not None), Decimal(0))),
        "unknown_cost_calls": sum(c is None for c in costs),
        "service_ms": (time.monotonic() - started) * 1000,
        "started_at": started_at,
        "completed_at": critic_pair._now(),
    }


async def replay_rows(
    replay: Path,
    current: dict[str, Any],
    schedule: list[dict[str, Any]],
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if (
        read_json_value((replay / "manifest.json").read_bytes()) != current
        or read_json_value((replay / "schedule.json").read_bytes()) != schedule
    ):
        raise ValueError("replay input mismatch")
    prior = read_json_value((replay / "rows.json").read_bytes())
    if len(prior) != len(schedule) or [r["id"] for r in prior] != [e["id"] for e in schedule]:
        raise ValueError("replay coverage mismatch")
    rows = []
    names = set()
    for entry, old in zip(schedule, prior, strict=True):
        if read_json_value((replay / "paths" / f"{entry['id']}.json").read_bytes()) != old:
            raise ValueError("path timing receipt mismatch")
        row = await execute(
            entry, cases[entry["case_index"]], client=None, out=None, archive=replay
        )
        for field in ("service_ms", "started_at", "completed_at", "queue_ms", "total_ms"):
            row[field] = old[field]
        if row != old:
            raise ValueError("replay derived path mismatch")
        for stage in row["stages"]:
            names.update({f"{row['id']}.{stage}.json", f"{row['id']}.{stage}.dispatch.json"})
        stage_ms = sum(
            raw_receipt(
                replay,
                entry["id"],
                stage,
                DECISIONS_ENDPOINT if stage == "jev" else str(critic_pair.CONTROLS["endpoint"]),
                entry["jev_wire"] if stage == "jev" else entry["critic_request"],
            )["elapsed_ms"]
            for stage in row["stages"]
        )
        if (
            row["service_ms"] + 1 < stage_ms
            or row["total_ms"] + 1 < row["queue_ms"] + row["service_ms"]
        ):
            raise ValueError("path timing is shorter than its stages")
        rows.append(row)
    if {p.name for p in (replay / "paths").iterdir()} != {f"{e['id']}.json" for e in schedule}:
        raise ValueError("replay path artifact set mismatch")
    if {p.name for p in (replay / "raw").iterdir()} != names:
        raise ValueError("replay raw artifact set mismatch")
    if summarize(cases, schedule, rows) != read_json_value((replay / "summary.json").read_bytes()):
        raise ValueError("replay summary mismatch")
    return rows


async def run(
    cases_path: Path,
    out: Path,
    *,
    freeze: Path | None = None,
    live: bool = False,
    replay: Path | None = None,
) -> dict[str, Any] | None:
    if live and replay is not None:
        raise ValueError("exclusive execution modes")
    cases = support_inputs.load_cases(cases_path)
    current = manifest(cases_path)
    schedule = entries(cases)
    if live and freeze is None:
        raise ValueError("live execution requires pre-call freeze")
    if freeze is not None and read_json_value(_read_bytes(freeze)) != current:
        raise ValueError("frozen experiment changed")
    rows = None
    if replay is not None:
        rows = await replay_rows(replay, current, schedule, cases)
    key = os.environ.get("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY") if live else None
    if live and not key:
        raise ValueError("authorized synthetic credential required")
    out.mkdir(parents=True, exist_ok=False)  # noqa: ASYNC240 - exclusive pre-dispatch setup
    (out / "raw").mkdir()
    (out / "paths").mkdir()
    _write(out / "manifest.json", current)
    _write(out / "schedule.json", schedule)
    if live:
        semaphore = asyncio.Semaphore(CONCURRENCY)
        records: dict[str, dict[str, Any]] = {}
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {key}"},
            timeout=180,
            transport=httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
            trust_env=False,
        ) as client:

            async def one(entry: dict[str, Any]) -> None:
                queued = time.monotonic()
                async with semaphore:
                    queue_ms = (time.monotonic() - queued) * 1000
                    row = await execute(entry, cases[entry["case_index"]], client=client, out=out)
                    row.update(queue_ms=queue_ms, total_ms=(time.monotonic() - queued) * 1000)
                    records[entry["id"]] = row
                    _write(out / "paths" / f"{entry['id']}.json", row)

            tasks = [asyncio.create_task(one(entry)) for entry in schedule]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                _write(
                    out / "partial.json",
                    {
                        "scheduled": len(schedule),
                        "recorded": list(records),
                        "missing": [e["id"] for e in schedule if e["id"] not in records],
                    },
                )
            rows = [records[e["id"]] for e in schedule]
    if replay is not None:
        copy_archive(replay, out)
    if rows is not None:
        summary = summarize(cases, schedule, rows)
        _write(out / "rows.json", rows)
        _write(out / "summary.json", summary)
        _write(out / "completion.json", {"paths": len(rows), "mode": "live" if live else "replay"})
        return summary
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--freeze", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", type=Path)
    args = parser.parse_args()
    asyncio.run(
        run(args.cases, args.output_dir, freeze=args.freeze, live=args.live, replay=args.replay)
    )


if __name__ == "__main__":
    main()
