"""Compare complete, paired schedules using existing trusted task receipts."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any, Literal

from benchmarks.agent_tasks.manifest import ManifestError, identity
from benchmarks.longmemeval_v2_reader_replication_report import cluster_bootstrap

ARMS = ("no_memory", "raw_retrieval", "simple_summary", "sibyl_consolidation")


@dataclass(frozen=True)
class ScheduledAttempt:
    """Freeze a runner identity before executing any arm."""

    attempt_id: str
    family: str
    category: Literal["related_transfer", "applicability_contrast"]
    repetition: int
    expected: Mapping[str, Any]


IDENTITY_FIELDS = (
    "experiment_id",
    "manifest_sha256",
    "task_id",
    "task_family_id",
    "task_sha256",
    "arm_id",
    "arm_sha256",
    "memory_pack_sha256",
    "seed",
    "controller_model",
    "controller_tools",
    "controller_budget",
    "runtime",
    "runner_source_sha256",
)
COMMON_FIELDS = (
    "controller_model",
    "controller_tools",
    "controller_budget",
    "runtime",
    "runner_source_sha256",
)


def _validate_schedule(schedule: Sequence[ScheduledAttempt]) -> None:
    if not schedule or len({row.attempt_id for row in schedule}) != len(schedule):
        raise ManifestError("transfer schedule is empty or repeats an attempt")
    paired: dict[tuple[str, str, int], list[ScheduledAttempt]] = defaultdict(list)
    repetitions: dict[tuple[str, str], set[int]] = defaultdict(set)
    common = None
    task_identities: dict[str, tuple[Any, ...]] = {}
    for row in schedule:
        if (
            not row.attempt_id
            or not row.family
            or type(row.repetition) is not int
            or row.repetition < 0
            or row.category not in {"related_transfer", "applicability_contrast"}
            or set(row.expected) != set(IDENTITY_FIELDS)
            or row.expected["arm_id"] not in ARMS
        ):
            raise ManifestError("invalid scheduled transfer identity")
        task_key = row.expected["task_id"]
        task_identity = (
            row.family,
            row.category,
            row.expected["task_sha256"],
            row.expected["task_family_id"],
        )
        if task_key in task_identities and task_identities[task_key] != task_identity:
            raise ManifestError("transfer task identity differs across repetitions")
        task_identities[task_key] = task_identity
        policy = {key: row.expected[key] for key in COMMON_FIELDS}
        if common is not None and policy != common:
            raise ManifestError("transfer solver policy differs between arms")
        common = policy
        paired[(row.family, row.expected["task_id"], row.repetition)].append(row)
        repetitions[(row.family, row.expected["task_id"])].add(row.repetition)
    if len({tuple(sorted(values)) for values in repetitions.values()}) != 1:
        raise ManifestError("transfer tasks have unequal repetition sets")
    for rows in paired.values():
        if Counter(row.expected["arm_id"] for row in rows) != Counter(ARMS):
            raise ManifestError("every task repetition requires exactly four arms")
        for key in ("experiment_id", "task_sha256", "task_family_id", "seed", "manifest_sha256"):
            if len({row.expected[key] for row in rows}) != 1:
                raise ManifestError("paired task or execution identity differs")
        if len({row.category for row in rows}) != 1:
            raise ManifestError("paired task category differs")


def _validate_receipt(row: ScheduledAttempt, receipt: Mapping[str, Any]) -> None:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != identity(unsigned):
        raise ManifestError("transfer receipt digest differs")
    if receipt.get("attempt_id") != row.attempt_id or any(
        receipt.get(key) != row.expected[key] for key in IDENTITY_FIELDS
    ):
        raise ManifestError("transfer receipt is outside its scheduled cell")
    if (
        receipt.get("schema_version") != "sibyl-agent-task-receipt-v1"
        or receipt.get("purpose") != "trusted_development"
        or receipt.get("task_split") != "development"
        or receipt.get("sealed_isolation") is not False
        or not receipt.get("completed_at")
        or receipt.get("status") == "running"
        or type(receipt.get("success")) is not bool
    ):
        raise ManifestError("transfer receipt is not a terminal development attempt")
    if receipt["success"] and (
        receipt.get("status") != "passed"
        or receipt.get("budget_status") not in {"within_reported_budget", "unknown"}
        or receipt.get("outcome", {}).get("passed") is not True
    ):
        raise ManifestError("transfer success lacks a passing budgeted outcome")


def summarize_transfer(
    schedule: Sequence[ScheduledAttempt], receipts: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Keep missing cells in the denominator and resample whole family effects.

    Receipt hashes establish consistency, not hostile-host authentication. The
    caller retains the frozen manifests and oracle evidence with this report.
    Preparation costs belong to the separate memory-pack preparation receipts.
    """
    _validate_schedule(schedule)
    if set(receipts) - {row.attempt_id for row in schedule}:
        raise ManifestError("unscheduled transfer receipts cannot enter the analysis")
    grouped: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    statuses: dict[str, Counter[str]] = {arm: Counter() for arm in ARMS}
    costs = {arm: {"known_usd": 0.0, "unknown_attempts": 0} for arm in ARMS}
    missing = []
    unqualified_passes = []
    budget_statuses: dict[str, Counter[str]] = {arm: Counter() for arm in ARMS}
    for row in schedule:
        arm = row.expected["arm_id"]
        receipt = receipts.get(row.attempt_id)
        success = False
        if receipt is None:
            missing.append(row.attempt_id)
            statuses[arm]["missing"] += 1
            costs[arm]["unknown_attempts"] += 1
        else:
            _validate_receipt(row, receipt)
            budget_status = receipt.get("budget_status", "unknown")
            budget_statuses[arm][budget_status] += 1
            success = receipt["success"] and budget_status == "within_reported_budget"
            if receipt["success"] and not success:
                unqualified_passes.append(row.attempt_id)
            statuses[arm][receipt["status"]] += 1
            cost = receipt.get("usage", {}).get("cost_usd")
            if cost is None:
                costs[arm]["unknown_attempts"] += 1
            elif type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                raise ManifestError("invalid transfer cost")
            else:
                costs[arm]["known_usd"] += cost
        grouped[(row.category, row.family, arm)].append(float(success))
    categories = {}
    for category in sorted({row.category for row in schedule}):
        families = sorted({row.family for row in schedule if row.category == category})
        family_rates = {
            family: {arm: mean(grouped[(category, family, arm)]) for arm in ARMS}
            for family in families
        }
        comparisons = {}
        for control in ARMS[:-1]:
            effects = {
                family: rates["sibyl_consolidation"] - rates[control]
                for family, rates in family_rates.items()
            }
            interval = cluster_bootstrap(list(effects.values()))
            interval["unit"] = "family"
            comparisons[control] = {"family_effects": effects, "uncertainty": interval}
        categories[category] = {
            "family_rates": family_rates,
            "arm_rates": {arm: mean(rates[arm] for rates in family_rates.values()) for arm in ARMS},
            "comparisons": comparisons,
        }
    return {
        "schema_version": "sibyl-transfer-diagnostic-report-v1",
        "schedule_sha256": identity([asdict(row) for row in schedule]),
        "scheduled_attempts": len(schedule),
        "recorded_attempts": len(receipts),
        "receipt_hashes": {key: value["receipt_sha256"] for key, value in receipts.items()},
        "complete": not missing,
        "missing_attempt_ids": missing,
        "unqualified_pass_attempt_ids": unqualified_passes,
        "budget_statuses": {arm: dict(counts) for arm, counts in budget_statuses.items()},
        "statuses": {arm: dict(counts) for arm, counts in statuses.items()},
        "execution_costs": costs,
        "cost_scope": "execution only; preparation and unknown usage are not zero",
        "categories": categories,
        "learning_benefit_established": False,
        "interpretation": "diagnostic paired family effects; independent confirmation required",
    }
