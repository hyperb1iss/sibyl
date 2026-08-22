"""Score-blind package claim and executed-stage handoff validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_outcomes as outcomes
from benchmarks import longmemeval_v2_release_runner as runner
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_handoff import ExecutedDomain, ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    bind_artifact,
    require_artifact,
)
from tools.bench import longmemeval_v2_rig as rig


def sealed(payload: dict[str, Any], field: str) -> dict[str, Any]:
    """Add the canonical digest field to a public package payload."""

    result = dict(payload)
    result[field] = rig.canonical_sha256(result)
    return result


def build_package_claim(
    executed: ExecutedStage,
    official_arms: tuple[outcomes.OfficialArm, ...],
    *,
    official_packages_root: Path,
    template: dict[str, Any] | None,
) -> dict[str, Any]:
    """Seal the exact immutable arm and paid-evidence handoff."""

    controls = {name: deepcopy(binding) for name, binding in executed.control_artifacts}
    domains = [
        {
            "arm_id": domain.arm_id,
            "domain": domain.domain,
            "actual_cost_usd": domain.actual_cost_usd,
            "exit_artifact": deepcopy(domain.exit_artifact),
            "artifacts": {name: deepcopy(binding) for name, binding in domain.artifacts},
        }
        for domain in executed.domains
    ]
    arm_claims = {
        arm.arm_id: {
            "publication": deepcopy(arm.authority),
            "authority_artifact": deepcopy(arm.authority_artifact),
            "object_artifact": deepcopy(arm.object_artifact),
        }
        for arm in official_arms
    }
    return sealed(
        {
            "schema_version": outcomes.PACKAGE_CLAIM_SCHEMA_VERSION,
            "stage_plan_sha256": executed.plan["stage_plan_sha256"],
            "official_packages_root": str(official_packages_root),
            "preregistration_template": template,
            "executed_status": deepcopy(executed.status_receipt),
            "control_artifacts": controls,
            "domains": domains,
            "official_arms": arm_claims,
        },
        "package_claim_sha256",
    )


def ledger(status: dict[str, Any]) -> dict[str, Any]:
    """Project the durable paid-work accounting into status writer fields."""

    return {
        "max_workers": status["max_workers"],
        "completed": list(status["completed_domains"]),
        "resumed": list(status["resumed_domains"]),
        "cost": float(status["actual_cost_usd"]),
    }


def require_packaging_handoff(
    plan: dict[str, Any],
    claim: dict[str, Any],
    live_status: dict[str, Any],
) -> ExecutedStage:
    """Rebuild the score-blind executed stage from live bound evidence."""

    original = state.validate_status_receipt(plan, claim["executed_status"])
    controls = deepcopy(claim["control_artifacts"])
    if (
        live_status["package_claim"] != claim
        or live_status["executed_status_artifact"] != controls.get("runner_status")
        or any(
            live_status[key] != original[key]
            for key in (
                "max_workers",
                "completed_domains",
                "resumed_domains",
                "actual_cost_usd",
            )
        )
    ):
        raise StagePlanError("package lifecycle changed the executed ledger")
    runs = state.require_claimed_stage_plan(plan)
    planning = runner.require_planning_barrier(plan, secrets=state.secret_values(plan))
    for key, bindings in planning.items():
        if (
            controls.get(f"planning:{key}") != bindings["plan"]
            or controls.get(f"planning-log:{key}") != bindings["log"]
        ):
            raise StagePlanError("package lifecycle planning evidence changed")
    domains: list[ExecutedDomain] = []
    costs: dict[str, float] = {}
    by_key = {f"{row['arm_id']}:{row['domain']}": row for row in claim["domains"]}
    runs_by_id = {str(run["arm_id"]): run for run in runs}
    for key, row in sorted(by_key.items()):
        run = runs_by_id[str(row["arm_id"])]
        cost = runner.require_completed_exit(plan, run, str(row["domain"]))
        if cost != row["actual_cost_usd"]:
            raise StagePlanError("package lifecycle domain cost changed")
        costs[key] = cost
        domains.append(
            ExecutedDomain(
                arm_id=str(row["arm_id"]),
                domain=str(row["domain"]),
                actual_cost_usd=cost,
                exit_artifact=deepcopy(row["exit_artifact"]),
                artifacts=tuple(
                    (name, deepcopy(binding)) for name, binding in sorted(row["artifacts"].items())
                ),
            )
        )
    runner.require_arm_costs(plan, runs, costs)
    runner.require_executed_status(original, runs=runs, costs=costs)
    template = claim["preregistration_template"]
    if (
        template is not None
        and require_artifact(
            template,
            name="preregistration template",
        )
        != template
    ):
        raise StagePlanError("preregistration template changed during packaging")
    return ExecutedStage(
        plan=deepcopy(plan),
        runs=tuple(deepcopy(runs)),
        domains=tuple(domains),
        status_receipt=deepcopy(original),
        control_artifacts=tuple(
            (name, deepcopy(binding)) for name, binding in sorted(controls.items())
        ),
    )


def require_official_arms(
    executed: ExecutedStage,
    *,
    official_packages_root: Path,
) -> tuple[outcomes.OfficialArm, ...]:
    """Consume every preexisting official arm through its strict authority."""

    return outcomes.require_official_arms(executed, packages_root=official_packages_root)


def expected_template(plan: dict[str, Any], path: Path | None) -> dict[str, Any] | None:
    """Bind the exact preregistration template when the stage issues one."""

    required = plan["spec"]["stage"] in {"anchor", "race"}
    if required is not (path is not None):
        raise StagePlanError("stage preregistration template applicability is invalid")
    return None if path is None else bind_artifact(path, name="preregistration template")
