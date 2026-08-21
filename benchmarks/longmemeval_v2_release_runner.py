"""Execute one sealed LongMemEval-V2 release stage without score stopping."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_evidence as evidence
from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_command import invoke_command as _invoke_command
from benchmarks.longmemeval_v2_release_contract import MAX_WORKERS_CAP
from benchmarks.longmemeval_v2_release_inputs import (
    DOMAINS,
    StagePlanError,
    load_json,
    require_exact_keys,
)
from tools.bench import longmemeval_v2_rig as rig

ROOT = Path(__file__).resolve().parents[1]
DOMAIN_EXIT_SCHEMA_VERSION = "sibyl-longmemeval-v2-release-domain-exit-v1"
EXIT_KEYS = frozenset(
    {
        "schema_version",
        "stage_plan_sha256",
        "arm_id",
        "domain",
        "execution",
        "command_sha256",
        "returncode",
        "status",
        "artifacts",
        "actual_cost_usd",
        "error",
        "started_at",
        "completed_at",
        "exit_sha256",
    }
)


def _run_log_path(plan: dict[str, Any], run: dict[str, Any], domain: str, phase: str) -> Path:
    return Path(plan["output_root"]) / "logs" / phase / run["arm_id"] / f"{domain}.jsonl"


def _exit_path(plan: dict[str, Any], run: dict[str, Any], domain: str) -> Path:
    return Path(plan["output_root"]) / "exits" / run["arm_id"] / f"{domain}.json"


def _write_exit(
    plan: dict[str, Any],
    run: dict[str, Any],
    domain: str,
    *,
    started_at: str,
    returncode: int,
    cost: float | None,
    artifacts: dict[str, Any] | None,
    error: str | None,
    secrets: tuple[str, ...],
) -> dict[str, Any]:
    payload = state.sealed(
        {
            "schema_version": DOMAIN_EXIT_SCHEMA_VERSION,
            "stage_plan_sha256": plan["stage_plan_sha256"],
            "arm_id": run["arm_id"],
            "domain": domain,
            "execution": run["execution"],
            "command_sha256": rig.canonical_sha256(run["domains"][domain]["run_command"]),
            "returncode": returncode,
            "status": "COMPLETE" if error is None else "FAIL",
            "artifacts": artifacts,
            "actual_cost_usd": cost,
            "error": None if error is None else state.redact(error, secrets=secrets),
            "started_at": started_at,
            "completed_at": state.now(),
        },
        "exit_sha256",
    )
    release_io.write_json_atomic(_exit_path(plan, run, domain), payload)
    return payload


def _run_log_binding(plan: dict[str, Any], run: dict[str, Any], domain: str) -> dict[str, Any]:
    return state.require_command_log(
        _run_log_path(plan, run, domain, "runs"),
        command=run["domains"][domain]["run_command"],
        secrets=state.secret_values(plan),
        expected_returncode=0,
        expected_invocations=1,
    )


def require_completed_exit(
    plan: dict[str, Any],
    run: dict[str, Any],
    domain: str,
) -> float:
    raw = load_json(_exit_path(plan, run, domain))
    require_exact_keys(raw, EXIT_KEYS, name="completed domain exit")
    unsigned = {key: value for key, value in raw.items() if key != "exit_sha256"}
    if raw.get("exit_sha256") != rig.canonical_sha256(unsigned):
        raise StagePlanError("completed domain exit digest is invalid")
    expected = {
        "schema_version": DOMAIN_EXIT_SCHEMA_VERSION,
        "stage_plan_sha256": plan["stage_plan_sha256"],
        "arm_id": run["arm_id"],
        "domain": domain,
        "execution": run["execution"],
        "command_sha256": rig.canonical_sha256(run["domains"][domain]["run_command"]),
        "returncode": 0,
        "status": "COMPLETE",
        "error": None,
    }
    if any(raw.get(key) != value for key, value in expected.items()):
        raise StagePlanError("completed domain exit differs from its sealed run")
    actual_cost, artifacts = evidence.require_completed_domain(plan, run, domain)
    artifacts["runner_log"] = _run_log_binding(plan, run, domain)
    if raw.get("artifacts") != artifacts or raw.get("actual_cost_usd") != actual_cost:
        raise StagePlanError("completed domain artifacts or cost changed after execution")
    return actual_cost


def _existing_domains(
    plan: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    fresh: bool,
    completed: dict[str, float],
    resumed: list[str],
) -> None:
    for run in runs:
        for domain in DOMAINS:
            key = f"{run['arm_id']}:{domain}"
            output_dir = Path(run["domains"][domain]["output_dir"])
            exit_path = _exit_path(plan, run, domain)
            run_log = _run_log_path(plan, run, domain, "runs")
            existing = [path for path in (output_dir, exit_path, run_log) if path.exists()]
            if not existing:
                continue
            if fresh or not output_dir.is_dir() or not exit_path.is_file() or not run_log.is_file():
                raise StagePlanError(f"partial domain output requires a fresh root: {key}")
            completed[key] = require_completed_exit(plan, run, domain)
            resumed.append(key)


def _preflight(
    plan: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    secrets: tuple[str, ...],
) -> None:
    for run in runs:
        for domain in DOMAINS:
            domain_run = run["domains"][domain]
            returncode = _invoke_command(
                domain_run["plan_command"],
                log_path=_run_log_path(plan, run, domain, "planning"),
                secrets=secrets,
            )
            if returncode != 0:
                raise StagePlanError(
                    f"official plan-only command failed for {run['arm_id']}:{domain}"
                )
            evidence.require_planning_output(plan, run, domain)


def _attest_future_memory(
    plan: dict[str, Any], run: dict[str, Any], domain: str, *, secrets: tuple[str, ...]
) -> None:
    domain_run = run["domains"][domain]
    planning_memory = domain_run["planning_memory_dir"]
    assert planning_memory is not None
    if not Path(planning_memory, "memory_manifest.json").is_file():
        raise StagePlanError("dependent wave memory is not complete")
    returncode = _invoke_command(
        domain_run["plan_command"],
        log_path=_run_log_path(plan, run, domain, "planning"),
        secrets=secrets,
    )
    if returncode != 0:
        raise StagePlanError("dependent wave remote memory attestation failed")
    evidence.require_planning_output(plan, run, domain)


def _execute_domain(
    plan: dict[str, Any], run: dict[str, Any], domain: str, *, secrets: tuple[str, ...]
) -> tuple[float | None, dict[str, Any] | None, str, dict[str, Any] | None]:
    key = f"{run['arm_id']}:{domain}"
    started_at = state.now()
    returncode = -1
    try:
        returncode = _invoke_command(
            run["domains"][domain]["run_command"],
            log_path=_run_log_path(plan, run, domain, "runs"),
            secrets=secrets,
        )
        _require_command_success(returncode, name="official paid command")
        cost, artifacts = evidence.require_completed_domain(plan, run, domain)
    except Exception as exc:
        failure = {
            "run": key,
            "error": state.redact(exc, secrets=secrets),
            "returncode": returncode,
        }
        _write_exit(
            plan,
            run,
            domain,
            started_at=started_at,
            returncode=returncode,
            cost=None,
            artifacts=None,
            error=failure["error"],
            secrets=secrets,
        )
        return None, None, started_at, failure
    return cost, artifacts, started_at, None


def _require_command_success(returncode: int, *, name: str) -> None:
    if returncode != 0:
        raise StagePlanError(f"{name} exited {returncode}")


def require_arm_costs(
    plan: dict[str, Any], runs: list[dict[str, Any]], costs: dict[str, float]
) -> None:
    for run in runs:
        keys = [f"{run['arm_id']}:{domain}" for domain in DOMAINS]
        if all(key in costs for key in keys):
            actual = sum(costs[key] for key in keys)
            if actual > run["spend_reservation"]["max_spend_usd_total"]:
                raise StagePlanError(f"arm {run['arm_id']} exceeded its sealed total reservation")


def _require_worker_count(plan: dict[str, Any], max_workers: int) -> None:
    if (
        isinstance(max_workers, bool)
        or not isinstance(max_workers, int)
        or not 1 <= max_workers <= MAX_WORKERS_CAP
    ):
        raise StagePlanError("release runner workers must be within the temporary 1..4 cap")
    if max_workers > plan.get("max_workers_cap", 0):
        raise StagePlanError("release runner workers exceed the sealed stage cap")


def require_executed_status(
    prior_status: dict[str, Any],
    *,
    runs: list[dict[str, Any]],
    costs: dict[str, float],
) -> dict[str, Any]:
    expected_keys = sorted(f"{run['arm_id']}:{domain}" for run in runs for domain in DOMAINS)
    if (
        sorted(costs) != expected_keys
        or prior_status["completed_domains"] != expected_keys
        or prior_status["failures"]
        or prior_status["actual_cost_usd"] != sum(costs.values())
    ):
        raise StagePlanError("executed stage status has inconsistent domain evidence")
    return prior_status


def _pending_wave(
    wave: list[str],
    *,
    runs_by_id: dict[str, dict[str, Any]],
    costs: dict[str, float],
) -> list[tuple[dict[str, Any], str]]:
    return [
        (runs_by_id[arm_id], domain)
        for arm_id in wave
        for domain in DOMAINS
        if f"{arm_id}:{domain}" not in costs
    ]


def _require_prior_domains(plan: dict[str, Any], costs: dict[str, float]) -> None:
    for run in plan["runs"]:
        for domain in DOMAINS:
            key = f"{run['arm_id']}:{domain}"
            if key in costs and require_completed_exit(plan, run, domain) != costs[key]:
                raise StagePlanError("prior completed domain cost changed")


def require_planning_barrier(plan: dict[str, Any], *, secrets: tuple[str, ...]) -> dict[str, Any]:
    bindings: dict[str, Any] = {}
    for run in plan["runs"]:
        for domain in DOMAINS:
            key = f"{run['arm_id']}:{domain}"
            bindings[key] = {
                "plan": evidence.require_planning_output(plan, run, domain),
                "log": state.require_command_log(
                    _run_log_path(plan, run, domain, "planning"),
                    command=run["domains"][domain]["plan_command"],
                    secrets=secrets,
                    expected_returncode=0,
                ),
            }
    return bindings


def _require_current_domains(
    plan: dict[str, Any],
    successes: list[tuple[dict[str, Any], str, float, dict[str, Any], str]],
) -> None:
    for run, domain, cost, artifacts, _started_at in successes:
        current_cost, current_artifacts = evidence.require_completed_domain(plan, run, domain)
        if current_cost != cost or current_artifacts != artifacts:
            raise StagePlanError("current wave evidence changed before completion")


def _execute_wave(
    plan: dict[str, Any],
    pending: list[tuple[dict[str, Any], str]],
    *,
    max_workers: int,
    secrets: tuple[str, ...],
    costs: dict[str, float],
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    successes: list[tuple[dict[str, Any], str, float, dict[str, Any], str]] = []
    _require_prior_domains(plan, costs)
    planning = require_planning_barrier(plan, secrets=secrets)
    control = state.stage_control_snapshot(plan)
    with ThreadPoolExecutor(max_workers=min(max_workers, len(pending))) as executor:
        futures = {
            executor.submit(_execute_domain, plan, run, domain, secrets=secrets): (run, domain)
            for run, domain in pending
        }
        for future in as_completed(futures):
            run, domain = futures[future]
            cost, artifacts, started_at, failure = future.result()
            if failure is not None:
                failures.append(failure)
            elif cost is not None and artifacts is not None:
                successes.append((run, domain, cost, artifacts, started_at))
    state.require_stage_control(plan, control)
    _require_prior_domains(plan, costs)
    if require_planning_barrier(plan, secrets=secrets) != planning:
        raise StagePlanError("paid wave changed sealed planning evidence")
    state.require_claimed_output_tree(plan)
    _require_current_domains(plan, successes)
    for run, domain, _cost, artifacts, _started_at in successes:
        artifacts["runner_log"] = _run_log_binding(plan, run, domain)
    for run, domain, cost, artifacts, started_at in successes:
        _write_exit(
            plan,
            run,
            domain,
            started_at=started_at,
            returncode=0,
            cost=cost,
            artifacts=artifacts,
            error=None,
            secrets=secrets,
        )
        costs[f"{run['arm_id']}:{domain}"] = cost
    return failures


def _attest_wave_memories(
    plan: dict[str, Any],
    pending: list[tuple[dict[str, Any], str]],
    *,
    max_workers: int,
    secrets: tuple[str, ...],
) -> None:
    dependent = [
        (run, domain)
        for run, domain in pending
        if run["domains"][domain]["planning_memory_dir"] is not None
        and "--checkpoint-dir" in run["domains"][domain]["plan_command"]
    ]
    if not dependent:
        return
    with ThreadPoolExecutor(max_workers=min(max_workers, len(dependent))) as executor:
        futures = [
            executor.submit(
                _attest_future_memory,
                plan,
                run,
                domain,
                secrets=secrets,
            )
            for run, domain in dependent
        ]
        for future in as_completed(futures):
            future.result()


def _execute_waves(
    plan: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    max_workers: int,
    secrets: tuple[str, ...],
    costs: dict[str, float],
    resumed: list[str],
) -> list[dict[str, Any]]:
    runs_by_id = {run["arm_id"]: run for run in runs}
    for wave in plan["waves"]:
        pending = _pending_wave(wave, runs_by_id=runs_by_id, costs=costs)
        if not pending:
            continue
        _attest_wave_memories(
            plan,
            pending,
            max_workers=max_workers,
            secrets=secrets,
        )
        state.write_status(
            plan,
            status="RUNNING",
            max_workers=max_workers,
            completed=list(costs),
            resumed=resumed,
            cost=sum(costs.values()),
        )
        failures = _execute_wave(
            plan,
            pending,
            max_workers=max_workers,
            secrets=secrets,
            costs=costs,
        )
        require_arm_costs(plan, runs, costs)
        if failures:
            return failures
    return []


def _final_status(
    plan: dict[str, Any],
    runs: list[dict[str, Any]],
    *,
    max_workers: int,
    costs: dict[str, float],
    resumed: list[str],
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    if failures or len(costs) != len(runs) * len(DOMAINS):
        if not failures:
            failures.append({"error": "stage ended without every declared domain"})
        status = "FAIL"
    else:
        status = "EXECUTED"
    return state.write_status(
        plan,
        status=status,
        max_workers=max_workers,
        completed=list(costs),
        resumed=resumed,
        failures=failures,
        cost=sum(costs.values()),
    )


def _run_claimed_stage(
    plan: dict[str, Any],
    *,
    max_workers: int,
    fresh: bool,
    secrets: tuple[str, ...],
    costs: dict[str, float],
    resumed: list[str],
) -> dict[str, Any]:
    runs = plan["runs"]
    _existing_domains(
        plan,
        runs,
        fresh=fresh,
        completed=costs,
        resumed=resumed,
    )
    prior_status = state.require_status(plan)
    if prior_status["status"] == "EXECUTED":
        return require_executed_status(prior_status, runs=runs, costs=costs)
    _preflight(plan, runs, secrets=secrets)
    state.write_status(
        plan,
        status="PREFLIGHT_COMPLETE",
        max_workers=max_workers,
        completed=list(costs),
        resumed=resumed,
        cost=sum(costs.values()),
    )
    failures = _execute_waves(
        plan,
        runs,
        max_workers=max_workers,
        secrets=secrets,
        costs=costs,
        resumed=resumed,
    )
    return _final_status(
        plan,
        runs,
        max_workers=max_workers,
        costs=costs,
        resumed=resumed,
        failures=failures,
    )


def run_stage_plan(plan: dict[str, Any], max_workers: int = MAX_WORKERS_CAP) -> dict[str, Any]:
    """Execute every fixed wave and stop only for invalid evidence or failure."""

    _require_worker_count(plan, max_workers)
    fresh = not Path(str(plan.get("output_root", ""))).exists()
    if fresh:
        state.claim_stage(plan, max_workers=max_workers)
    else:
        state.require_claimed_stage_plan(plan)
    output_root = Path(plan["output_root"])
    secrets = state.secret_values(plan)
    with state.stage_lock(output_root):
        state.require_claimed_stage_plan(plan)
        costs: dict[str, float] = {}
        resumed: list[str] = []
        try:
            return _run_claimed_stage(
                plan,
                max_workers=max_workers,
                fresh=fresh,
                secrets=secrets,
                costs=costs,
                resumed=resumed,
            )
        except Exception as exc:
            failure = {"error": state.redact(exc, secrets=secrets)}
            state.write_status(
                plan,
                status="FAIL",
                max_workers=max_workers,
                completed=list(costs),
                resumed=resumed,
                failures=[failure],
                cost=sum(costs.values()),
            )
            raise
