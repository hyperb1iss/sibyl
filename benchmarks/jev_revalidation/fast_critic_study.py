"""Paired fast critics with bound archived Jev hints; synthetic experiments only."""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import time
from pathlib import Path
from typing import Any

import httpx

from sibyl_core.ai.decisions import DecisionObservation, DecisionRequest
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionRoute
from sibyl_core.tasks._evidence_json import canonical, read_json_value

from . import (
    background_readiness,
    critic_pair,
    fast_critic,
    fast_critic_analysis,
    quality_speed,
    support_inputs,
)
from .runner import _sha, _write, timestamp

VERSION = "jev-assisted-fast-critic-v1"
SEED = 20260921
CONCURRENCY = 8
PROTOCOL = {
    "intervention": "same full critique and advisory instructions; empty slot versus validated archived Jev labels, paths and hashes",
    "hypotheses": [
        "source-grounded finding quality improves",
        "no new false accepts or unsupported findings or lost safe coverage",
        "median and p95 critic-stage latency do not increase",
    ],
    "semantic_review": "source-derived rubric frozen before calls; output review blinded to arm; same-family agent annotation, not human gold",
    "exclusions": "no post-outcome case selection, confidence hints, original gold, old critic outputs, production changes or new Jev calls",
}


def hint_labels(entry: dict[str, Any], row: dict[str, Any]) -> list[dict[str, str]]:
    request = DecisionRequest.model_validate_json(canonical(entry["jev_request"]))
    observation = DecisionObservation.model_validate_json(
        canonical({**row["jev_observation"], "elapsed_ms": 0})
    )
    observation.validate_for(request, expected_model_id=OpenRouterDecisionRoute().resolved_model_id)
    if observation.execution_status != "completed":
        return []
    subjects = {s.claim_path: s.claim_sha256 for s in request.subject_refs}
    return [
        {"claim_path": a.question_id, "claim_sha256": subjects[a.question_id], "value": a.value}
        for a in observation.answers
    ]


async def prepare(
    cases_path: Path, source: Path, rubric_path: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    cases = await asyncio.to_thread(support_inputs.load_cases, cases_path)
    old_schedule = await asyncio.to_thread(quality_speed.entries, cases)
    old_manifest = await asyncio.to_thread(quality_speed.manifest, cases_path)
    rows = await quality_speed.replay_rows(source, old_manifest, old_schedule, cases)
    return await asyncio.to_thread(
        _assemble, cases_path, source, rubric_path, cases, old_schedule, old_manifest, rows
    )


def _assemble(cases_path, source, rubric_path, cases, old_schedule, old_manifest, rows):
    if read_json_value((source / "completion.json").read_bytes()) != {
        "mode": "live",
        "paths": len(old_schedule),
    }:
        raise ValueError("complete original live archive required")
    rubric = read_json_value(rubric_path.read_bytes())
    if rubric["cases_sha256"] != _sha(cases_path):
        raise ValueError("rubric cases binding changed")
    if {c["case_id"] for c in rubric["cases"]} != {c["id"] for c in cases} or len(
        rubric["cases"]
    ) != len(cases):
        raise ValueError("rubric must cover each case exactly once")
    indexed = {r["id"]: r for r in rows}
    rng = random.Random(SEED)  # noqa: S311 - reproducible paired experiment order
    pairs = [e for e in old_schedule if e["arm"] == "jev_then_critic"]
    rng.shuffle(pairs)
    schedule = []
    for old in pairs:
        row = indexed[old["id"]]
        hints = hint_labels(old, row)
        prepared = critic_pair.prepare_case(cases[old["case_index"]], old["run_id"])
        if prepared.payload_json != old["prepared_payload"]:
            raise ValueError("source or candidate binding changed")
        arms = list(fast_critic_analysis.ARMS)
        rng.shuffle(arms)
        for arm in arms:
            selected = hints if arm == "hinted" else []
            request = fast_critic.critic_request(prepared, selected)
            schedule.append(
                {
                    "id": f"case-{old['case_index']}-repeat-{old['repeat']}-{arm}",
                    "case_id": old["case_id"],
                    "repeat": old["repeat"],
                    "arm": arm,
                    "prepared_payload": prepared.payload_json,
                    "hints": selected,
                    "hint_status": "available" if hints else "unavailable",
                    "request": request,
                    "request_sha256": quality_speed.digest(request),
                    "prior_jev_id": old["id"],
                    "prior_jev_request_digest": row["jev_observation"]["request_digest"],
                    "prior_jev_cost_usd": str(
                        background_readiness._money(row["jev_observation"]["observed_cost_usd"])
                    ),
                }
            )
    manifest = {
        "version": VERSION,
        "cases_sha256": _sha(cases_path),
        "rubric_sha256": _sha(rubric_path),
        "schedule_sha256": quality_speed.digest(schedule),
        "case_count": len(cases),
        "calls": len(schedule),
        "controls": fast_critic.CONTROLS,
        "concurrency": CONCURRENCY,
        "seed": SEED,
        "protocol": PROTOCOL,
        "source_manifest": old_manifest,
        "source_artifact_hashes": {
            str(p.relative_to(source)): _sha(p) for p in sorted(source.rglob("*")) if p.is_file()
        },
        "code_hashes": {
            Path(module.__file__).name: _sha(Path(module.__file__))
            for module in (fast_critic, fast_critic_analysis, background_readiness)
        },
    }
    manifest["code_hashes"][Path(__file__).name] = _sha(Path(__file__))
    return cases, schedule, manifest


async def derive(entry: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    critic = await fast_critic.interpret(entry, raw)
    return {k: entry[k] for k in ("id", "case_id", "repeat", "arm")} | {
        "action": quality_speed.critic_action(critic),
        "critic": critic,
    }


def check_timing(row: dict[str, Any], raw: dict[str, Any]) -> None:
    for value in [row[k] for k in ("service_ms", "queue_ms", "total_ms")] + [raw["elapsed_ms"]]:
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid path timing")
    if (
        row["total_ms"] + 1 < row["service_ms"] + row["queue_ms"]
        or row["service_ms"] + 1 < raw["elapsed_ms"]
    ):
        raise ValueError("path timing does not contain its work")
    values = [
        timestamp(v)
        for v in (
            row["started_at"],
            raw["dispatch_started_at"],
            raw["completed_at"],
            row["completed_at"],
        )
    ]
    if any(v is None for v in values):
        raise ValueError("path timestamps must contain dispatch")
    present = [v for v in values if v is not None]
    if present != sorted(present):
        raise ValueError("path timestamps must contain dispatch")


async def replay(
    archive: Path,
    schedule: list[dict[str, Any]],
    manifest: dict[str, Any],
    cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if (
        read_json_value((archive / "manifest.json").read_bytes()) != manifest
        or read_json_value((archive / "schedule.json").read_bytes()) != schedule
    ):
        raise ValueError("replay inputs changed")
    if read_json_value((archive / "completion.json").read_bytes()) != {
        "mode": "live",
        "paths": len(schedule),
    }:
        raise ValueError("complete fresh critic archive required")
    if _sha(archive / "rubric.json") != manifest["rubric_sha256"]:
        raise ValueError("replay rubric mismatch")
    prior = read_json_value((archive / "rows.json").read_bytes())
    if [r["id"] for r in prior] != [e["id"] for e in schedule]:
        raise ValueError("replay path coverage mismatch")
    names = {f"{e['id']}.critic{suffix}.json" for e in schedule for suffix in ("", ".dispatch")}
    if {p.name for p in (archive / "raw").iterdir()} != names or {
        p.name for p in (archive / "paths").iterdir()
    } != {f"{e['id']}.json" for e in schedule}:
        raise ValueError("replay artifact set mismatch")
    for entry, row in zip(schedule, prior, strict=True):
        raw = quality_speed.raw_receipt(
            archive, entry["id"], "critic", fast_critic.CONTROLS["endpoint"], entry["request"]
        )
        derived = await derive(entry, raw)
        check_timing(row, raw)
        if (
            any(row[k] != value for k, value in derived.items())
            or read_json_value((archive / "paths" / f"{entry['id']}.json").read_bytes()) != row
        ):
            raise ValueError("replay derived row mismatch")
    if fast_critic_analysis.summarize(cases, schedule, prior) != read_json_value(
        (archive / "summary.json").read_bytes()
    ):
        raise ValueError("replay summary mismatch")
    return prior


async def live_calls(schedule: list[dict[str, Any]], out: Path, key: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    semaphore = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {key}"},
        timeout=180,
        transport=httpx.AsyncHTTPTransport(retries=0),
        follow_redirects=False,
    ) as client:

        async def one(entry: dict[str, Any]) -> None:
            queued = time.monotonic()
            async with semaphore:
                queue_ms = (time.monotonic() - queued) * 1000
                start, started_at = time.monotonic(), critic_pair._now()
                raw = await quality_speed.send(
                    client,
                    fast_critic.CONTROLS["endpoint"],
                    entry["request"],
                    out / "raw" / f"{entry['id']}.critic.json",
                )
                row = await derive(entry, raw)
                row.update(
                    started_at=started_at,
                    completed_at=critic_pair._now(),
                    service_ms=(time.monotonic() - start) * 1000,
                    queue_ms=queue_ms,
                    total_ms=(time.monotonic() - queued) * 1000,
                )
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


def _check_output(out: Path, source: Path, replay_path: Path | None) -> None:
    if out.resolve().is_relative_to(source.resolve()) or (
        replay_path is not None and out.resolve().is_relative_to(replay_path.resolve())
    ):
        raise ValueError("output must be outside immutable input archives")


def _initialize(out, manifest, schedule, rubric_path, live):
    out.mkdir(parents=True, exist_ok=False)
    _write(out / "manifest.json", manifest)
    _write(out / "schedule.json", schedule)
    (out / "rubric.json").write_bytes(rubric_path.read_bytes())
    if live:
        (out / "raw").mkdir()
        (out / "paths").mkdir()


async def run(
    cases_path: Path,
    source: Path,
    rubric_path: Path,
    out: Path,
    *,
    freeze: Path | None = None,
    live: bool = False,
    replay_path: Path | None = None,
) -> dict[str, Any] | None:
    if live and replay_path is not None:
        raise ValueError("choose live or replay")
    await asyncio.to_thread(_check_output, out, source, replay_path)
    cases, schedule, manifest = await prepare(cases_path, source, rubric_path)
    if live and (
        freeze is None or read_json_value(await asyncio.to_thread(freeze.read_bytes)) != manifest
    ):
        raise ValueError("live calls require exact frozen manifest")
    rows = await replay(replay_path, schedule, manifest, cases) if replay_path is not None else None
    key = os.environ.get("SIBYL_JEV_CRITIC_OPENROUTER_API_KEY", "") if live else ""
    if live and not key:
        raise ValueError("dedicated synthetic critic key required")
    await asyncio.to_thread(_initialize, out, manifest, schedule, rubric_path, live)
    if live:
        rows = await live_calls(schedule, out, key)
    if rows is None:
        return None
    summary = fast_critic_analysis.summarize(cases, schedule, rows)
    _write(out / "rows.json", rows)
    _write(out / "summary.json", summary)
    _write(out / "completion.json", {"mode": "live" if live else "replay", "paths": len(rows)})
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--freeze", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", type=Path)
    args = parser.parse_args()
    asyncio.run(
        run(
            args.cases,
            args.source_archive,
            args.rubric,
            args.output_dir,
            freeze=args.freeze,
            live=args.live,
            replay_path=args.replay,
        )
    )


if __name__ == "__main__":
    main()
