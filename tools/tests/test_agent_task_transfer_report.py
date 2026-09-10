"""Paired transfer diagnostics retain failures and reject mismatched receipts."""

from dataclasses import replace

import pytest
from benchmarks.agent_tasks.manifest import ManifestError, identity
from benchmarks.agent_tasks.transfer_report import ARMS, ScheduledAttempt, summarize_transfer


def experiment():
    schedule = []
    receipts = {}
    for family in ("first", "second"):
        for repetition in range(2):
            for arm in ARMS:
                attempt = f"{family}-{repetition}-{arm}"
                expected = {
                    "experiment_id": "pilot",
                    "manifest_sha256": identity([family, repetition]),
                    "task_id": family + "-task",
                    "task_family_id": family,
                    "task_sha256": identity(family),
                    "arm_id": arm,
                    "arm_sha256": identity([family, arm]),
                    "memory_pack_sha256": identity([family, arm, "pack"]),
                    "seed": repetition,
                    "controller_model": "frozen-model",
                    "controller_tools": ["shell"],
                    "controller_budget": {"cost_usd": 1},
                    "runtime": {"image": "frozen-runtime"},
                    "runner_source_sha256": {"runner.py": identity("runner")},
                }
                schedule.append(
                    ScheduledAttempt(attempt, family, "related_transfer", repetition, expected)
                )
                passed = arm == "sibyl_consolidation" and family == "first"
                receipt = {
                    **expected,
                    "attempt_id": attempt,
                    "schema_version": "sibyl-agent-task-receipt-v1",
                    "purpose": "trusted_development",
                    "task_split": "development",
                    "sealed_isolation": False,
                    "completed_at": "2026-09-10T00:00:00Z",
                    "status": "passed" if passed else "task_failed",
                    "success": passed,
                    "budget_status": "within_reported_budget",
                    "outcome": {"passed": passed},
                    "usage": {"cost_usd": 0.1},
                }
                receipt["receipt_sha256"] = identity(receipt)
                receipts[attempt] = receipt
    return schedule, receipts


def test_transfer_family_effects_and_missing_denominator():
    schedule, receipts = experiment()
    missing = "first-0-sibyl_consolidation"
    del receipts[missing]
    report = summarize_transfer(schedule, receipts)
    related = report["categories"]["related_transfer"]
    expected_rate = 0.25
    assert related["arm_rates"]["sibyl_consolidation"] == expected_rate
    assert related["comparisons"]["no_memory"]["family_effects"] == {"first": 0.5, "second": 0.0}
    assert related["comparisons"]["no_memory"]["uncertainty"]["unit"] == "family"
    assert report["scheduled_attempts"] == len(schedule)
    assert report["missing_attempt_ids"] == [missing]
    assert report["complete"] is False
    assert report["execution_costs"]["sibyl_consolidation"]["unknown_attempts"] == 1
    assert report["learning_benefit_established"] is False


@pytest.mark.parametrize("mutation", ["digest", "task", "success", "budget", "running", "cost"])
def test_transfer_rejects_invalid_receipts(mutation):
    schedule, receipts = experiment()
    row = receipts["first-0-sibyl_consolidation"]
    if mutation == "digest":
        row["seed"] = 9
    else:
        if mutation == "task":
            row["task_id"] = "another-task"
        elif mutation == "success":
            row["outcome"]["passed"] = False
        elif mutation == "budget":
            row["budget_status"] = "exceeded"
        elif mutation == "running":
            row["status"] = "running"
        elif mutation == "cost":
            row["usage"]["cost_usd"] = -1
        row["receipt_sha256"] = identity({k: v for k, v in row.items() if k != "receipt_sha256"})
    with pytest.raises(ManifestError):
        summarize_transfer(schedule, receipts)


@pytest.mark.parametrize("mutation", ["missing_arm", "duplicate", "policy", "pair", "repetitions"])
def test_transfer_rejects_unmatched_schedule(mutation):
    schedule, _ = experiment()
    if mutation == "missing_arm":
        schedule.pop()
    elif mutation == "duplicate":
        schedule.append(schedule[0])
    elif mutation in {"policy", "pair"}:
        field = "controller_model" if mutation == "policy" else "task_sha256"
        schedule[0] = replace(schedule[0], expected={**schedule[0].expected, field: "changed"})
    else:
        schedule = [row for row in schedule if not (row.family == "first" and row.repetition == 1)]
    with pytest.raises(ManifestError):
        summarize_transfer(schedule, {})


def test_transfer_unknown_cost_is_retained_and_extra_attempt_is_refused():
    schedule, receipts = experiment()
    row = receipts[schedule[0].attempt_id]
    row["usage"]["cost_usd"] = None
    row["receipt_sha256"] = identity({k: v for k, v in row.items() if k != "receipt_sha256"})
    report = summarize_transfer(schedule, receipts)
    assert report["complete"] is True
    assert report["execution_costs"]["no_memory"]["unknown_attempts"] == 1
    assert len(report["receipt_hashes"]) == len(schedule)
    receipts["unscheduled"] = row
    with pytest.raises(ManifestError, match="unscheduled"):
        summarize_transfer(schedule, receipts)


def test_transfer_unknown_budget_pass_remains_unqualified():
    schedule, receipts = experiment()
    attempt = "first-0-sibyl_consolidation"
    row = receipts[attempt]
    row["budget_status"] = "unknown"
    row["usage"]["cost_usd"] = None
    row["receipt_sha256"] = identity({k: v for k, v in row.items() if k != "receipt_sha256"})
    report = summarize_transfer(schedule, receipts)
    assert report["complete"] is True
    assert report["unqualified_pass_attempt_ids"] == [attempt]
    assert report["budget_statuses"]["sibyl_consolidation"]["unknown"] == 1
    expected_rate = 0.25
    assert (
        report["categories"]["related_transfer"]["arm_rates"]["sibyl_consolidation"]
        == expected_rate
    )


@pytest.mark.parametrize("field", ["task_sha256", "task_family_id", "category"])
def test_transfer_task_identity_cannot_change_between_repetitions(field):
    schedule, _ = experiment()
    for index, row in enumerate(schedule):
        if row.family == "first" and row.repetition == 1:
            schedule[index] = (
                replace(row, category="applicability_contrast")
                if field == "category"
                else replace(row, expected={**row.expected, field: "changed"})
            )
    with pytest.raises(ManifestError, match="identity differs"):
        summarize_transfer(schedule, {})


def test_transfer_paired_experiment_identity_must_agree():
    schedule, _ = experiment()
    schedule[0] = replace(
        schedule[0], expected={**schedule[0].expected, "experiment_id": "different-experiment"}
    )
    with pytest.raises(ManifestError, match="execution identity"):
        summarize_transfer(schedule, {})
