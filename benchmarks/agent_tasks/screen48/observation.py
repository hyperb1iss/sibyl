"""Read the 48-row terminal report as facts, and say plainly what is not trusted.

Nothing here scores an experiment. The report states, per cell, whether the
executed attempt really is the scheduled attempt, what the checker said when it
is, and which rows carry no usable answer at all. Rows that never ran, ran
without a pack, or returned under a different identity stay visible in the
denominator instead of quietly leaving it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from benchmarks.agent_tasks.manifest import ManifestError, identity
from benchmarks.agent_tasks.screen48.materialize import CELL_COORDINATES

# transfer_report owns the identity field list; its arm tuples predate this
# screen (no "native" arm, no checkpoint), so the comparison is made here.
from benchmarks.agent_tasks.transfer_report import IDENTITY_FIELDS

SCHEMA = "sibyl-screen48-observation-v1"
OUTCOMES = ("passed", "failed", "unknown")
TRUSTED_STATUSES = frozenset(
    {"passed", "task_failed", "controller_budget_exceeded", "candidate_failed", "candidate_timeout"}
)
UNTRUSTED_STATUSES = frozenset({"dry_run", "runner_error", "unprepared"})


def _identity_failure(row: dict[str, Any]) -> str | None:
    """Reject any row whose receipt was produced under a different frozen identity."""
    expected, actual = row.get("expected_identity"), row.get("receipt_identity")
    if expected is None:
        return "no_expected_identity"
    if actual is None:
        return "no_receipt_identity"
    if set(expected) != set(IDENTITY_FIELDS) or set(actual) != set(IDENTITY_FIELDS):
        return "identity_fields_differ"
    differing = [
        field for field in IDENTITY_FIELDS if identity(actual[field]) != identity(expected[field])
    ]
    return f"identity_differs:{','.join(differing)}" if differing else None


def _classify(row: dict[str, Any]) -> tuple[str, str | None]:
    """Return the row's observed outcome and, when it is not trusted, the reason."""
    status = row.get("receipt_status")
    if not row["prepared"]:
        return "unprepared", row.get("reason") or "not_prepared"
    if row["dispatch_state"] == "unknown":
        reason = "begun_without_outcome"
    elif row["dispatch_state"] != "complete":
        reason = f"dispatch_state:{row['dispatch_state']}"
    elif status in UNTRUSTED_STATUSES or status not in TRUSTED_STATUSES:
        reason = f"receipt_status:{status}"
    elif failure := _identity_failure(row):
        reason = failure
    elif not isinstance(row.get("task_passed"), bool):
        reason = "no_boolean_checker_outcome"
    else:
        return ("passed" if row["task_passed"] else "failed"), None
    return "unknown", reason


def _validate_rows(schedule: dict[str, Any], terminal: dict[str, Any]) -> list[dict[str, Any]]:
    rows = terminal.get("rows")
    if not isinstance(rows, list) or len(rows) != len(schedule["cells"]):
        raise ManifestError("the terminal report does not cover the whole schedule")
    for row, cell in zip(rows, schedule["cells"], strict=True):
        if any(row.get(key) != cell[key] for key in CELL_COORDINATES):
            raise ManifestError(f"terminal row is outside its cell: {row.get('attempt_id')}")
    return rows


def _table(rows: list[dict[str, Any]], observed: dict[str, str], keys: tuple[str, ...]):
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(tuple(row[key] for key in keys), []).append(row)
    table = []
    for values in sorted(groups, key=str):
        selected = groups[values]
        counts = Counter(observed[row["attempt_id"]] for row in selected)
        table.append(
            {
                **dict(zip(keys, values, strict=True)),
                "denominator": len(selected),
                "trusted": sum(counts[state] for state in OUTCOMES if state != "unknown"),
                **{state: counts.get(state, 0) for state in (*OUTCOMES, "unprepared")},
            }
        )
    return table


def _paired(rows: list[dict[str, Any]], observed: dict[str, str]) -> list[dict[str, Any]]:
    """Put each task's before and after side by side, for one arm at a time."""
    index = {(row["task"], row["arm"], row["checkpoint"]): row for row in rows}
    pairs = []
    for task, arm in sorted({(row["task"], row["arm"]) for row in rows}):
        before, after = index.get((task, arm, 0)), index.get((task, arm, 1))
        if before is None or after is None:
            continue
        both_prepared = before["prepared"] and after["prepared"]
        pairs.append(
            {
                "task": task,
                "arm": arm,
                "family": before["family"],
                "category": before["category"],
                "checkpoint_0": {
                    "attempt_id": before["attempt_id"],
                    "observed": observed[before["attempt_id"]],
                    "pack_sha256": before["pack_sha256"],
                },
                "checkpoint_1": {
                    "attempt_id": after["attempt_id"],
                    "observed": observed[after["attempt_id"]],
                    "pack_sha256": after["pack_sha256"],
                },
                "treatment_changed": (
                    before["pack_sha256"] != after["pack_sha256"] if both_prepared else None
                ),
                "change": (
                    f"{observed[before['attempt_id']]}->{observed[after['attempt_id']]}"
                    if both_prepared
                    else None
                ),
            }
        )
    return pairs


def observe(schedule: dict[str, Any], terminal: dict[str, Any]) -> dict[str, Any]:
    """Summarize the executed screen without inferring any efficacy from it.

    Every prepared row that carries a receipt is re-checked against the identity
    frozen at materialization time, so a swapped pack, task or budget shows up as
    an untrusted row rather than as a result. No bootstrap, no effect estimate
    and no verdict is produced here; the counts are the whole output.
    """
    rows = _validate_rows(schedule, terminal)
    observed: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for row in rows:
        state, reason = _classify(row)
        observed[row["attempt_id"]] = state
        if reason is not None:
            reasons[row["attempt_id"]] = reason
    untrusted = [
        {
            "attempt_id": row["attempt_id"],
            **{key: row[key] for key in ("checkpoint", "task", "arm", "category")},
            "observed": observed[row["attempt_id"]],
            "reason": reasons[row["attempt_id"]],
            "dispatch_state": row["dispatch_state"],
            "receipt_status": row.get("receipt_status"),
        }
        for row in rows
        if row["attempt_id"] in reasons
    ]
    return {
        "schema": SCHEMA,
        "schedule_sha256": terminal.get("schedule_sha256"),
        "denominator": len(rows),
        "arm_checkpoint": _table(rows, observed, ("checkpoint", "arm")),
        "categories": _table(rows, observed, ("checkpoint", "arm", "category")),
        "paired_checkpoints": _paired(rows, observed),
        "observed": dict(Counter(observed.values())),
        "untrusted_rows": untrusted,
        "unprepared_attempt_ids": [
            row["attempt_id"] for row in rows if observed[row["attempt_id"]] == "unprepared"
        ],
        "unknown_attempt_ids": [
            row["attempt_id"] for row in rows if observed[row["attempt_id"]] == "unknown"
        ],
        "complete": not untrusted,
        "learning_benefit_established": False,
        "interpretation": "observed counts only; no effect estimate and no independent acceptance",
    }
