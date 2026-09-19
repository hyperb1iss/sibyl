"""Synthetic revalidation experiments; no graph access or publication writes."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

from sibyl_core.ai.decisions import DecisionObservation, DecisionRequest, ReplayDecisionProvider
from sibyl_core.ai.openrouter_decisions import OpenRouterDecisionProvider, OpenRouterDecisionRoute
from sibyl_core.models.reflection import ReflectionCandidate
from sibyl_core.services.reflection import apply_reflection_lifecycle_decisions
from sibyl_core.tasks._evidence_json import read_json_value

from . import prompts

RELATIONS = frozenset(
    {
        "supported",
        "contradicted",
        "superseded",
        "compatible",
        "unrelated",
        "non_evidence",
        "uncertain",
    }
)
_RETAIN = frozenset({"supported", "compatible", "unrelated", "non_evidence"})
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$")


def timestamp(value: object) -> datetime | None:
    """Only complete timezone-aware ISO timestamps can establish event order."""
    if not isinstance(value, str) or not _ISO.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC)


def policy_action(relation: str, case: dict[str, Any]) -> str:
    if relation in _RETAIN:
        return "retain"
    if relation not in {"contradicted", "superseded"}:
        return "review"
    memory_time = timestamp(case.get("memory_effective_at"))
    event_time = timestamp(case.get("event_effective_at"))
    if memory_time is not None and event_time is not None:
        if event_time < memory_time:
            return "retain"
        if event_time > memory_time and case.get("source_authority") == "authoritative":
            return "retire"
    return "review"


@dataclass
class _PriorMemory:
    raw_content: str
    id: str = "prior-source"
    metadata: dict[str, Any] = field(default_factory=dict)
    review_state: str = "accepted"


def baseline_prediction(case: dict[str, Any]) -> tuple[str, str, str, list[str]]:
    candidate = ReflectionCandidate(
        kind="claim",
        title="Synthetic event",
        content=case["event"],
        reason="Synthetic lifecycle baseline",
        confidence=1.0,
        raw_source_ids=["event"],
    )
    result = apply_reflection_lifecycle_decisions(
        [candidate], prior_memories=[_PriorMemory(raw_content=case["memory"])]
    )[0]
    kinds = [str(finding.kind) for finding in result.reflection_findings]
    for kind, relation, proposal in (
        ("supersession", "superseded", "propose_supersession"),
        ("stale", "contradicted", "propose_stale"),
        ("contradiction", "contradicted", "review_only"),
        ("duplicate", "supported", "duplicate_new_candidate"),
    ):
        if kind in kinds:
            action = "review" if kind == "contradiction" else policy_action(relation, case)
            return relation, action, proposal, kinds
    return "uncertain", "retain", "no_signal", kinds


def _row(case: dict[str, Any], *, arm: str, repeat: int, **values: Any) -> dict[str, Any]:
    return {
        "case_id": case["id"],
        "category": case["category"],
        "arm": arm,
        "repeat": repeat,
        "expected_relation": case["expected_relation"],
        "expected_action": case["expected_action"],
        **values,
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def summarize(rows: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    """All scheduled cases remain in accuracy and recall denominators on failure."""
    total = len(rows)
    gold_retire = sum(row["expected_action"] == "retire" for row in rows)
    completed = sum(row["status"] == "completed" for row in rows)
    latencies = [call["elapsed_ms"] for call in calls if call["attempt_count"] > 0]
    result: dict[str, Any] = {
        "cases": total,
        "completed_cases": completed,
        "failed_cases": total - completed,
        "relation_correct": sum(row.get("relation") == row["expected_relation"] for row in rows),
        "action_correct": sum(row.get("action") == row["expected_action"] for row in rows),
        "gold_retire_cases": gold_retire,
        "gold_retire_captured": sum(
            row.get("action") == "retire" and row["expected_action"] == "retire" for row in rows
        ),
        "false_retire_count": sum(
            row.get("action") == "retire" and row["expected_action"] != "retire" for row in rows
        ),
        "review_count": sum(row.get("action") == "review" for row in rows),
        "transport_failures": sum(call["execution_status"] != "completed" for call in calls),
        "calls": len(calls),
        "physical_attempts": sum(call["attempt_count"] for call in calls),
        "latency_p50_ms": statistics.median(latencies) if latencies else None,
        "latency_p95_ms": _percentile(latencies, 0.95),
    }
    for count, rate in (
        ("relation_correct", "relation_accuracy"),
        ("action_correct", "action_accuracy"),
        ("review_count", "review_rate"),
    ):
        result[rate] = result[count] / total if total else None
    result["gold_retire_recall"] = (
        result["gold_retire_captured"] / gold_retire if gold_retire else None
    )
    for key in ("input_tokens", "output_tokens", "observed_cost_usd"):
        known = [call[key] for call in calls if call.get(key) is not None]
        result[key] = sum(known) if known else None
        result[f"unknown_{key}_calls"] = len(calls) - len(known)
    if rows and all(row["arm"] == "baseline" for row in rows):
        result["relation_accuracy"] = None
        result["relation_correct"] = None
        result["proposal_counts"] = {
            proposal: sum(row.get("proposal") == proposal for row in rows)
            for proposal in sorted({row.get("proposal", "unknown") for row in rows})
        }
        retirement_proposals = {"propose_supersession", "propose_stale"}
        result["retirement_proposal_count"] = sum(
            row.get("proposal") in retirement_proposals for row in rows
        )
        result["false_retirement_proposals"] = sum(
            row.get("proposal") in retirement_proposals and row["expected_action"] != "retire"
            for row in rows
        )
        result["gold_retire_proposed"] = sum(
            row.get("proposal") in retirement_proposals and row["expected_action"] == "retire"
            for row in rows
        )
    return result


def report(rows: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, Any] = {}
    for arm in sorted({row["arm"] for row in rows}):
        arm_rows = [row for row in rows if row["arm"] == arm]
        arm_calls = [call for call in calls if call["arm"] == arm]
        groups[arm] = {
            "pooled": summarize(arm_rows, arm_calls),
            "repeats": {
                str(repeat): summarize(
                    [row for row in arm_rows if row["repeat"] == repeat],
                    [call for call in arm_calls if call["repeat"] == repeat],
                )
                for repeat in sorted({row["repeat"] for row in arm_rows})
            },
            "categories": {
                category: summarize([row for row in arm_rows if row["category"] == category], [])
                for category in sorted({row["category"] for row in arm_rows})
            },
        }
    return {
        "arms": groups,
        "baseline_caveat": "Existing heuristic findings are proposals, not actual retirement. "
        "The same authority/time gate evaluates retirement proposals; contradiction is review-only. "
        "No signal retains memory. Baseline relation accuracy is undefined.",
        "category_accounting": "Latency and usage belong to calls; mixed-category batches are not apportioned.",
        "failure_denominators": "Failed cases count as incorrect; no failed case is excluded.",
    }


def _write(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    # Every caller supplies literal inspection arguments; no shell or user command input.
    return subprocess.check_output(["git", *args], text=True).strip()  # noqa: S603, S607


def _failed(
    request: DecisionRequest, category: str, *, attempts: int = 0, elapsed_ms: float = 0.0
) -> DecisionObservation:
    return DecisionObservation(
        semantic_input_sha256=request.semantic_input_sha256,
        request_digest=request.request_digest,
        execution_status="unavailable",
        error_category=category,
        attempt_count=attempts,
        elapsed_ms=elapsed_ms,
    )


async def replay_observation(
    path: Path, request: DecisionRequest, model: str
) -> DecisionObservation:
    payload = read_json_value(await asyncio.to_thread(path.read_bytes))
    if not isinstance(payload, dict) or set(payload) != {"request", "observation"}:
        raise ValueError("invalid replay envelope")
    stored_request = DecisionRequest.model_validate_json(json.dumps(payload["request"]))
    if stored_request.request_digest != request.request_digest:
        raise ValueError("replay request mismatch")
    provider = ReplayDecisionProvider(
        {request.request_digest: json.dumps(payload["observation"]).encode()},
        expected_model_id=model,
    )
    return await provider.decide(request)


def _load_cases(path: Path) -> list[dict[str, Any]]:
    payload = read_json_value(path.read_bytes())
    cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(cases, list) or not cases:
        raise ValueError("cases must be a nonempty list")
    required = {
        "id",
        "category",
        "memory",
        "event",
        "memory_effective_at",
        "event_effective_at",
        "source_authority",
        "expected_relation",
        "expected_action",
        "rationale",
    }
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or not set(case) >= required:
            raise ValueError("invalid case schema")
        if not isinstance(case["id"], str) or case["id"] in seen:
            raise ValueError("case IDs must be unique strings")
        if case["expected_relation"] not in RELATIONS or case["expected_action"] not in {
            "retain",
            "review",
            "retire",
        }:
            raise ValueError("invalid gold label")
        if any(
            not isinstance(case[key], str)
            for key in ("category", "memory", "event", "source_authority")
        ):
            raise ValueError("case evidence must be text")
        seen.add(case["id"])
    return cases


def select_prompts(version: str) -> ModuleType:
    """Select an explicit frozen prompt program without changing historical v1."""
    names = {"v1": ".prompts", "v2": ".prompts_v2"}
    if version not in names:
        raise ValueError("unknown prompt version")
    return import_module(names[version], package=__package__)


def _validate_replay_manifest(current: dict[str, Any], original: dict[str, Any]) -> None:
    if current["prompt_version"] != original.get("prompt_version", "v1"):
        raise ValueError("replay manifest mismatch: prompt_version")
    if current["prompt_dependencies_sha256"] != original.get("prompt_dependencies_sha256", {}):
        raise ValueError("replay manifest mismatch: prompt_dependencies_sha256")
    for key in (
        "cases_sha256",
        "prompts_sha256",
        "arms",
        "repeats",
        "batch_size",
        "route_policy_sha256",
    ):
        if current[key] != original[key]:
            raise ValueError(f"replay manifest mismatch: {key}")


def _prepare_run(
    args: argparse.Namespace,
) -> tuple[
    list[dict[str, Any]], list[str], str | None, dict[str, Any], OpenRouterDecisionRoute, ModuleType
]:
    cases = _load_cases(args.cases)
    arms = args.arms.split(",")
    if not arms or len(set(arms)) != len(arms) or not set(arms) <= {"direct", "decomposed"}:
        raise ValueError("arms must contain direct and/or decomposed once")
    if min(args.batch_size, args.repeats, args.concurrency) < 1:
        raise ValueError("batch size, repeats, and concurrency must be positive")
    api_key = os.environ.get("SIBYL_DECISION_OPENROUTER_API_KEY") if args.live else None
    if args.live and not api_key:
        raise ValueError("live execution requires SIBYL_DECISION_OPENROUTER_API_KEY")
    replay_manifest = (
        read_json_value((args.replay / "manifest.json").read_bytes()) if args.replay else None
    )
    run_id = replay_manifest["run_id"] if replay_manifest else uuid.uuid4().hex
    route = OpenRouterDecisionRoute()
    prompt_version = getattr(args, "prompt_version", "v1")
    program = select_prompts(prompt_version)
    prompt_path = Path(str(program.__file__))
    manifest = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "mode": "live" if args.live else "replay" if args.replay else "prepare",
        "cases_sha256": _sha(args.cases),
        "prompts_sha256": _sha(prompt_path),
        "prompt_version": prompt_version,
        "prompt_dependencies_sha256": {"prompts.py": _sha(Path(prompts.__file__))}
        if prompt_version == "v2"
        else {},
        "runner_sha256": _sha(Path(__file__)),
        "base_git_sha": _git("rev-parse", "HEAD"),
        "dirty_diff_sha256": hashlib.sha256(_git("diff", "HEAD").encode()).hexdigest(),
        "git_status": _git("status", "--short"),
        "arms": arms,
        "repeats": args.repeats,
        "batch_size": args.batch_size,
        "concurrency": args.concurrency,
        "case_count": len(cases),
        "route": route.model_dump(mode="json"),
        "route_policy_sha256": route.policy_sha256,
        "synthetic_only": True,
    }
    if replay_manifest:
        _validate_replay_manifest(manifest, replay_manifest)
    args.out.mkdir(parents=True, exist_ok=False)
    (args.out / "receipts").mkdir()
    _write(args.out / "manifest.json", manifest)
    return cases, arms, api_key, manifest, route, program


def _initial_rows(
    cases: list[dict[str, Any]], arms: list[str], repeats: int
) -> tuple[list[dict[str, Any]], dict[tuple[str, int, str], dict[str, Any]]]:
    rows = []
    for case in cases:
        relation, action, proposal, findings = baseline_prediction(case)
        rows.append(
            _row(
                case,
                arm="baseline",
                repeat=0,
                status="completed",
                relation=relation,
                action=action,
                proposal=proposal,
                finding_kinds=findings,
            )
        )
    pending_rows = {}
    for arm in arms:
        for repeat in range(repeats):
            for case in cases:
                row = _row(
                    case, arm=arm, repeat=repeat, status="not_executed", relation=None, action=None
                )
                rows.append(row)
                pending_rows[arm, repeat, case["id"]] = row
    return rows, pending_rows


def _predict(program: ModuleType, answers: dict[str, str], index: int, arm: str) -> str:
    relation = program.predict(answers, index, arm)
    if relation not in RELATIONS:
        raise ValueError("unknown relation")
    return relation


@dataclass
class _Experiment:
    args: argparse.Namespace
    cases: list[dict[str, Any]]
    run_id: str
    route: OpenRouterDecisionRoute
    provider: OpenRouterDecisionProvider | None
    pending_rows: dict[tuple[str, int, str], dict[str, Any]]
    calls: list[dict[str, Any]]
    semaphore: asyncio.Semaphore
    program: ModuleType

    async def batch(self, arm: str, repeat: int, offset: int) -> None:
        args, cases, run_id = self.args, self.cases, self.run_id
        semaphore, provider, route = self.semaphore, self.provider, self.route
        pending_rows, calls = self.pending_rows, self.calls
        selected = cases[offset : offset + args.batch_size]
        key = f"{arm}-{repeat}-{offset}"
        request = self.program.make_request(selected, arm, f"{run_id}:{key}")
        async with semaphore:
            started = time.monotonic()
            if provider is not None:
                try:
                    observation = await provider.decide(request)
                except asyncio.CancelledError:
                    observation = _failed(
                        request,
                        "cancelled_usage_unknown",
                        attempts=1,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                    _write(
                        args.out / "receipts" / f"{key}.json",
                        {
                            "request": request.model_dump(mode="json"),
                            "observation": observation.model_dump(mode="json"),
                        },
                    )
                    calls.append(
                        {
                            "arm": arm,
                            "repeat": repeat,
                            "batch": key,
                            **observation.model_dump(mode="json"),
                        }
                    )
                    for case in selected:
                        pending_rows[arm, repeat, case["id"]].update(status="cancelled", batch=key)
                    raise
                except Exception:
                    observation = _failed(
                        request,
                        "provider_exception_usage_unknown",
                        attempts=1,
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
            elif args.replay:
                try:
                    observation = await replay_observation(
                        args.replay / "receipts" / f"{key}.json", request, route.resolved_model_id
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    observation = _failed(request, "replay_invalid_or_missing")
            else:
                observation = _failed(request, "not_executed")
        _write(
            args.out / "receipts" / f"{key}.json",
            {
                "request": request.model_dump(mode="json"),
                "observation": observation.model_dump(mode="json"),
            },
        )
        calls.append(
            {"arm": arm, "repeat": repeat, "batch": key, **observation.model_dump(mode="json")}
        )
        answers = {answer.question_id: answer.value for answer in observation.answers}
        for index, case in enumerate(selected):
            relation = None
            action = None
            status: str = observation.execution_status
            if status == "completed":
                try:
                    relation = _predict(self.program, answers, index, arm)
                    action = policy_action(relation, case)
                except (KeyError, ValueError, TypeError):
                    status = "invalid_prediction"
                    relation = None
            pending_rows[arm, repeat, case["id"]].update(
                status=status, relation=relation, action=action, batch=key
            )


def _finish_run(
    args: argparse.Namespace,
    rows: list[dict[str, Any]],
    calls: list[dict[str, Any]],
    scheduled: int,
) -> dict[str, Any]:
    _write(
        args.out / "predictions.json",
        sorted(rows, key=lambda row: (row["arm"], row["repeat"], row["case_id"])),
    )
    _write(args.out / "calls.json", calls)
    summary = report(rows, calls)
    _write(args.out / "summary.json", summary)
    _write(
        args.out / "completion.json",
        {
            "ended_at": datetime.now(UTC).isoformat(),
            "scheduled_model_cases": scheduled,
            "recorded_model_cases": sum(row["arm"] != "baseline" for row in rows),
        },
    )
    return summary


async def run(args: argparse.Namespace) -> dict[str, Any]:
    cases, arms, api_key, manifest, route, program = await asyncio.to_thread(_prepare_run, args)
    rows, pending_rows = _initial_rows(cases, arms, args.repeats)
    calls: list[dict[str, Any]] = []
    provider = OpenRouterDecisionProvider(api_key) if api_key else None
    experiment = _Experiment(
        args,
        cases,
        manifest["run_id"],
        route,
        provider,
        pending_rows,
        calls,
        asyncio.Semaphore(args.concurrency),
        program,
    )
    tasks = [
        asyncio.create_task(experiment.batch(arm, repeat, offset))
        for arm in arms
        for repeat in range(args.repeats)
        for offset in range(0, len(cases), args.batch_size)
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        if provider is not None:
            await provider.aclose()
        summary = _finish_run(args, rows, calls, len(cases) * len(arms) * args.repeats)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--arms", default="direct,decomposed")
    parser.add_argument("--prompt-version", choices=("v1", "v2"), default="v1")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=8)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", type=Path)
    args = parser.parse_args()
    summary = asyncio.run(run(args))
    sys.stdout.write(json.dumps(summary, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
