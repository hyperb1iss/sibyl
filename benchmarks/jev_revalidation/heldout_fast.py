"""Fresh held-out full critics with actual Jev acquisition and misleading-label stress."""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import random
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from sibyl_core.ai.openrouter_decisions import (
    DECISIONS_ENDPOINT,
    OpenRouterDecisionProvider,
    OpenRouterDecisionRoute,
)
from sibyl_core.tasks._evidence_json import read_json_value
from sibyl_core.tasks.memory_validation import PreparedMemoryValidation

from . import (
    critic_pair,
    fast_critic,
    fast_critic_study,
    heldout_fast_analysis,
    quality_speed,
    support_inputs,
)
from .heldout_fast_analysis import ARMS, PROTOCOL
from .runner import _sha, _write, timestamp

VERSION = "jev-heldout-fast-critic-v1"
REPEATS = 2
CONCURRENCY = 8
SEED = 20260922


def stress_labels(case: dict[str, Any], prepared: PreparedMemoryValidation) -> list[dict[str, str]]:
    hashes = read_json_value(prepared.payload_json.encode())["assertion_hashes"]
    stress = case.get("stress_hints")
    if (
        not isinstance(stress, dict)
        or set(stress) != set(hashes)
        or any(v not in support_inputs.LABELS for v in stress.values())
    ):
        raise ValueError("stress hints must cover every assertion with known labels")
    return [
        {"claim_path": path, "claim_sha256": hashes[path], "value": stress[path]}
        for path in sorted(hashes)
    ]


def entries(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rng = random.Random(SEED)  # noqa: S311 - frozen randomized experiment schedule
    pairs = [(index, repeat) for index in range(len(cases)) for repeat in range(REPEATS)]
    rng.shuffle(pairs)
    schedule = []
    for index, repeat in pairs:
        case = cases[index]
        run_id = f"{VERSION}:case-{index}:repeat-{repeat}"
        prepared = critic_pair.prepare_case(case, run_id)
        decision = support_inputs.make_request(case, run_id)
        direct = fast_critic.critic_request(prepared, [])
        stress = fast_critic.critic_request(prepared, stress_labels(case, prepared))
        arms = list(ARMS)
        rng.shuffle(arms)
        for arm in arms:
            schedule.append(
                {
                    "id": f"case-{index}-repeat-{repeat}-{arm}",
                    "case_id": case["id"],
                    "case_index": index,
                    "repeat": repeat,
                    "arm": arm,
                    "run_id": run_id,
                    "prepared_payload": prepared.payload_json,
                    "direct_request": direct,
                    "stress_request": stress,
                    "jev_request": decision.model_dump(mode="json"),
                    "jev_wire": quality_speed.wire_request(decision),
                }
            )
    return schedule


def _assemble(
    cases_path: Path, rubric_path: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cases = support_inputs.load_cases(cases_path)
    # Reuse the existing full-critic gold/rubric validation without adopting its schedule.
    quality_speed.manifest(cases_path)
    rubric = read_json_value(rubric_path.read_bytes())
    if (
        rubric["cases_sha256"] != _sha(cases_path)
        or {c["case_id"] for c in rubric["cases"]} != {c["id"] for c in cases}
        or len(rubric["cases"]) != len(cases)
    ):
        raise ValueError("rubric must bind and cover the exact cases")
    schedule = entries(cases)
    hashes = critic_pair.code_hashes()
    hashes.update({f"benchmark/{p.name}": _sha(p) for p in Path(__file__).parent.glob("*.py")})
    root = Path(__file__).resolve().parents[2]
    hashes.update({name: _sha(root / name) for name in ("uv.lock", "pyproject.toml")})
    current = {
        "version": VERSION,
        "cases_sha256": _sha(cases_path),
        "rubric_sha256": _sha(rubric_path),
        "schedule_sha256": quality_speed.digest(schedule),
        "case_count": len(cases),
        "paths": len(schedule),
        "critic_calls": len(schedule),
        "jev_calls": len(cases) * REPEATS,
        "repeats": REPEATS,
        "seed": SEED,
        "concurrency": CONCURRENCY,
        "critic_controls": fast_critic.CONTROLS,
        "jev_route": OpenRouterDecisionRoute().model_dump(mode="json"),
        "jev_policy_sha256": OpenRouterDecisionRoute().policy_sha256,
        "protocol": PROTOCOL,
        "code_hashes": hashes,
    }
    return cases, schedule, current


def manifest(cases_path: Path, rubric_path: Path) -> dict[str, Any]:
    return _assemble(cases_path, rubric_path)[2]


async def prepare(
    cases_path: Path, rubric_path: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    return await asyncio.to_thread(_assemble, cases_path, rubric_path)


async def _decision(request: Any, wire: dict[str, Any], observe: Any) -> dict[str, Any]:
    async def forward(sent: httpx.Request) -> httpx.Response:
        body = read_json_value(await sent.aread())
        if str(sent.url) != DECISIONS_ENDPOINT or body != wire:
            raise ValueError("Jev wire request changed")
        raw = await observe("jev", DECISIONS_ENDPOINT, body)
        if raw["http_status"] is None:
            if raw["error_code"] == "cancelled":
                raise httpx.ReadTimeout("recorded deadline")
            raise httpx.ConnectError("recorded transport failure")
        content = base64.b64decode(raw["response_body_base64"], validate=True)
        if content.decode("utf-8", errors="replace") != raw["response_body"]:
            raise ValueError("raw response bytes mismatch")
        return httpx.Response(raw["http_status"], content=content)

    async with OpenRouterDecisionProvider(
        "recording-transport", transport=httpx.MockTransport(forward)
    ) as provider:
        observation = await provider.decide(request)
    observation.validate_for(request, expected_model_id=OpenRouterDecisionRoute().resolved_model_id)
    result = observation.model_dump(mode="json")
    result.pop("elapsed_ms")  # Actual raw transport and outer path timing remain retained.
    return result


async def execute(
    entry: dict[str, Any],
    case: dict[str, Any],
    *,
    client: httpx.AsyncClient | None,
    out: Path | None,
    archive: Path | None = None,
) -> dict[str, Any]:
    start, started_at = time.monotonic(), critic_pair._now()
    # Reconstruct inside the measured path, never reuse the preflight's detached preparation.
    prepared = critic_pair.prepare_case(case, entry["run_id"])
    if prepared.payload_json != entry["prepared_payload"]:
        raise ValueError("candidate preparation changed")
    stages: list[str] = []

    async def observe(stage: str, endpoint: str, request: dict[str, Any]) -> dict[str, Any]:
        stages.append(stage)
        if archive is not None:
            return quality_speed.raw_receipt(archive, entry["id"], stage, endpoint, request)
        assert client is not None
        assert out is not None
        return await quality_speed.send(
            client, endpoint, request, out / "raw" / f"{entry['id']}.{stage}.json"
        )

    hints, observation = [], None
    if entry["arm"] == "live_jev":
        request = support_inputs.make_request(case, entry["run_id"])
        if (
            request.model_dump(mode="json") != entry["jev_request"]
            or quality_speed.wire_request(request) != entry["jev_wire"]
        ):
            raise ValueError("Jev preparation changed")
        observation = await _decision(request, entry["jev_wire"], observe)
        hints = fast_critic_study.hint_labels(entry, {"jev_observation": observation})
    elif entry["arm"] == "misleading":
        hints = stress_labels(case, prepared)
    elif entry["arm"] != "direct":
        raise ValueError("unknown experiment arm")
    wire = fast_critic.critic_request(prepared, hints)
    if (
        entry["arm"] != "live_jev"
        and wire != entry["stress_request" if hints else "direct_request"]
    ):
        raise ValueError("frozen critic request changed")
    invocation = {
        "prepared_payload": prepared.payload_json,
        "hints": hints,
        "request": wire,
        "request_sha256": quality_speed.digest(wire),
    }
    if archive is not None:
        saved = await asyncio.to_thread(
            (archive / "invocations" / f"{entry['id']}.json").read_bytes
        )
        if read_json_value(saved) != invocation:
            raise ValueError("replay invocation binding mismatch")
    else:
        assert out is not None
        _write(out / "invocations" / f"{entry['id']}.json", invocation)
    raw = await observe("critic", fast_critic.CONTROLS["endpoint"], wire)
    critic = await fast_critic.interpret(invocation, raw)
    costs = [critic["usage"]["observed_cost_usd"]]
    if observation is not None:
        costs.append(observation["observed_cost_usd"])
    return {k: entry[k] for k in ("id", "case_id", "repeat", "arm")} | {
        "action": quality_speed.critic_action(critic),
        "hints": hints,
        "jev_observation": observation,
        "critic": critic,
        "stages": stages,
        "known_cost_usd": str(sum((Decimal(str(c)) for c in costs if c is not None), Decimal(0))),
        "unknown_cost_calls": costs.count(None),
        "service_ms": (time.monotonic() - start) * 1000,
        "started_at": started_at,
        "completed_at": critic_pair._now(),
    }


def check_timing(row: dict[str, Any], raws: list[dict[str, Any]]) -> None:
    for raw in raws:
        fast_critic_study.check_timing(row, raw)
    if row["service_ms"] + 1 < sum(r["elapsed_ms"] for r in raws):
        raise ValueError("path timing excludes serial stage work")
    events = [
        timestamp(v) for raw in raws for v in (raw["dispatch_started_at"], raw["completed_at"])
    ]
    present = [v for v in events if v is not None]
    if len(present) != len(events) or present != sorted(present):
        raise ValueError("stage timestamps must be serial")


def _replay_inputs(archive, schedule, current):
    if (
        read_json_value((archive / "manifest.json").read_bytes()) != current
        or read_json_value((archive / "schedule.json").read_bytes()) != schedule
    ):
        raise ValueError("replay inputs changed")
    if (
        read_json_value((archive / "completion.json").read_bytes())
        != {"mode": "live", "paths": len(schedule)}
        or _sha(archive / "rubric.json") != current["rubric_sha256"]
    ):
        raise ValueError("complete bound original archive required")
    prior = read_json_value((archive / "rows.json").read_bytes())
    if [r["id"] for r in prior] != [e["id"] for e in schedule]:
        raise ValueError("replay path coverage mismatch")
    names = {
        f"{e['id']}.{stage}{suffix}.json"
        for e in schedule
        for stage in (["jev", "critic"] if e["arm"] == "live_jev" else ["critic"])
        for suffix in ("", ".dispatch")
    }
    if {p.name for p in (archive / "raw").iterdir()} != names:
        raise ValueError("replay raw artifact set mismatch")
    for directory in ("paths", "invocations"):
        if {p.name for p in (archive / directory).iterdir()} != {
            f"{e['id']}.json" for e in schedule
        }:
            raise ValueError("replay path artifact set mismatch")
    return prior


async def replay(
    archive: Path,
    schedule: list[dict[str, Any]],
    current: dict[str, Any],
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    prior = await asyncio.to_thread(_replay_inputs, archive, schedule, current)
    for entry, old in zip(schedule, prior, strict=True):
        row = await execute(
            entry, cases[entry["case_index"]], client=None, out=None, archive=archive
        )
        for key in ("service_ms", "started_at", "completed_at", "queue_ms", "total_ms"):
            row[key] = old[key]
        raws = [
            read_json_value(
                await asyncio.to_thread(
                    (archive / "raw" / f"{entry['id']}.{stage}.json").read_bytes
                )
            )
            for stage in row["stages"]
        ]
        check_timing(row, raws)
        saved = read_json_value(
            await asyncio.to_thread((archive / "paths" / f"{entry['id']}.json").read_bytes)
        )
        if row != old or saved != old:
            raise ValueError("replay derived row mismatch")
    saved_summary = read_json_value(await asyncio.to_thread((archive / "summary.json").read_bytes))
    if heldout_fast_analysis.summarize(cases, schedule, prior) != saved_summary:
        raise ValueError("replay summary mismatch")
    return prior


async def live_calls(
    cases: list[dict[str, Any]], schedule: list[dict[str, Any]], out: Path, key: str
) -> list[dict[str, Any]]:
    semaphore, records = asyncio.Semaphore(CONCURRENCY), {}
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {key}"},
        timeout=fast_critic.CONTROLS["timeout_seconds"],
        transport=httpx.AsyncHTTPTransport(retries=0),
        follow_redirects=False,
        trust_env=False,
    ) as client:

        async def one(entry):
            queued = time.monotonic()
            async with semaphore:
                queue_ms = (time.monotonic() - queued) * 1000
                row = await execute(entry, cases[entry["case_index"]], client=client, out=out)
                row.update(queue_ms=queue_ms, total_ms=(time.monotonic() - queued) * 1000)
                records[entry["id"]] = row
                _write(out / "paths" / f"{entry['id']}.json", row)

        tasks = [asyncio.create_task(one(e)) for e in schedule]
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
    return [records[e["id"]] for e in schedule]


def _initialize(out, current, schedule, rubric_path, replay_path):
    if replay_path is not None and out.resolve().is_relative_to(replay_path.resolve()):
        raise ValueError("output must be outside immutable archive")
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "manifest.json", current)
    _write(out / "schedule.json", schedule)
    (out / "rubric.json").write_bytes(rubric_path.read_bytes())
    for directory in ("raw", "paths", "invocations"):
        (out / directory).mkdir()


async def run(
    cases_path: Path,
    rubric_path: Path,
    out: Path,
    *,
    freeze: Path | None = None,
    live: bool = False,
    replay_path: Path | None = None,
) -> dict[str, Any] | None:
    if live and replay_path is not None:
        raise ValueError("choose live or replay")
    cases, schedule, current = await prepare(cases_path, rubric_path)
    if live and (
        freeze is None or read_json_value(await asyncio.to_thread(freeze.read_bytes)) != current
    ):
        raise ValueError("live calls require exact frozen manifest")
    rows = await replay(replay_path, schedule, current, cases) if replay_path is not None else None
    key = os.environ.get("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "") if live else ""
    if live and not key:
        raise ValueError("dedicated synthetic key required")
    await asyncio.to_thread(_initialize, out, current, schedule, rubric_path, replay_path)
    if live:
        rows = await live_calls(cases, schedule, out, key)
    if rows is None:
        return None
    summary = heldout_fast_analysis.summarize(cases, schedule, rows)
    _write(out / "rows.json", rows)
    _write(out / "summary.json", summary)
    _write(out / "completion.json", {"mode": "live" if live else "replay", "paths": len(rows)})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("cases", "rubric", "output-dir"):
        parser.add_argument(f"--{flag}", type=Path, required=True)
    parser.add_argument("--freeze", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", type=Path)
    args = parser.parse_args()
    asyncio.run(
        run(
            args.cases,
            args.rubric,
            args.output_dir,
            freeze=args.freeze,
            live=args.live,
            replay_path=args.replay,
        )
    )


if __name__ == "__main__":
    main()
