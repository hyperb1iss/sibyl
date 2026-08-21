"""Score-blind execution handoff for LongMemEval-V2 release packaging."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_runner as runner
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_inputs import (
    DOMAINS,
    StagePlanError,
    bind_artifact,
    load_json,
)


@dataclass(frozen=True)
class ExecutedDomain:
    """Exact score-blind evidence published for one completed domain."""

    arm_id: str
    domain: str
    actual_cost_usd: float
    exit_artifact: dict[str, Any]
    artifacts: tuple[tuple[str, dict[str, Any]], ...]


@dataclass(frozen=True)
class ExecutedStage:
    """Validated executor-to-packager handoff with no score-bearing content."""

    plan: dict[str, Any]
    runs: tuple[dict[str, Any], ...]
    domains: tuple[ExecutedDomain, ...]
    status_receipt: dict[str, Any]
    control_artifacts: tuple[tuple[str, dict[str, Any]], ...]


def _exit_path(plan: dict[str, Any], run: dict[str, Any], domain: str) -> Path:
    return Path(plan["output_root"]) / "exits" / run["arm_id"] / f"{domain}.json"


def require_executed_stage(plan: dict[str, Any]) -> ExecutedStage:
    """Validate and freeze the score-blind evidence for an executed stage."""

    runs = state.require_claimed_stage_plan(plan)
    output_root = Path(plan["output_root"])
    status_path = output_root / "runner_status.json"
    status_before = bind_artifact(status_path, name="executed stage status")
    status = state.read_status_receipt(plan)
    if status["status"] != "EXECUTED":
        raise StagePlanError("release stage is not ready for score-aware packaging")
    secrets = state.secret_values(plan)
    planning = runner.require_planning_barrier(plan, secrets=secrets)
    controls = {
        "runner_claim": bind_artifact(
            output_root / "runner_claim.json",
            name="executed stage claim",
        ),
        "runner_status": status_before,
    }
    for key, bindings in planning.items():
        controls[f"planning:{key}"] = bindings["plan"]
        controls[f"planning-log:{key}"] = bindings["log"]
    costs: dict[str, float] = {}
    domains: list[ExecutedDomain] = []
    for run in runs:
        for domain in DOMAINS:
            key = f"{run['arm_id']}:{domain}"
            exit_path = _exit_path(plan, run, domain)
            before = bind_artifact(exit_path, name=f"executed domain exit {key}")
            cost = runner.require_completed_exit(plan, run, domain)
            raw = load_json(exit_path)
            after = bind_artifact(exit_path, name=f"executed domain exit {key}")
            if before != after:
                raise StagePlanError("completed domain exit changed during handoff")
            costs[key] = cost
            domains.append(
                ExecutedDomain(
                    arm_id=run["arm_id"],
                    domain=domain,
                    actual_cost_usd=cost,
                    exit_artifact=after,
                    artifacts=tuple(
                        (name, deepcopy(binding))
                        for name, binding in sorted(raw["artifacts"].items())
                    ),
                )
            )
    runner.require_arm_costs(plan, runs, costs)
    runner.require_executed_status(status, runs=runs, costs=costs)
    if (
        state.read_status_receipt(plan) != status
        or bind_artifact(status_path, name="executed stage status") != status_before
    ):
        raise StagePlanError("executed stage status changed during handoff")
    if runner.require_planning_barrier(plan, secrets=secrets) != planning:
        raise StagePlanError("executed stage planning evidence changed during handoff")
    state.require_claimed_output_tree(plan)
    return ExecutedStage(
        plan=deepcopy(plan),
        runs=tuple(deepcopy(runs)),
        domains=tuple(domains),
        status_receipt=deepcopy(status),
        control_artifacts=tuple(
            (name, deepcopy(binding)) for name, binding in sorted(controls.items())
        ),
    )
