"""Claim one whole-schedule budget, then execute the 48 screen48 cells once each.

Claiming and executing are separate so that a crash can never widen the money
already reserved. Every cell keeps two durable files: ``begin.json`` before the
runner is called and ``outcome.json`` after it returns. A cell holding a begin
without an outcome is ``unknown`` forever; it is reported, never replayed, and
never replaced by a fresh attempt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchmarks.agent_tasks import runner
from benchmarks.agent_tasks.manifest import (
    ManifestError,
    canonical_bytes,
    digest,
    identity,
    strict_json,
)
from benchmarks.agent_tasks.screen48.materialize import CELL_COORDINATES
from benchmarks.agent_tasks.screen48.materialize import SCHEMA as MATERIALIZATION

CLAIM_SCHEMA = "sibyl-screen48-claim-v1"
EXECUTION_SCHEMA = "sibyl-screen48-execution-v1"
TERMINAL_SCHEMA = "sibyl-screen48-terminal-v1"
CELL_FIELDS = (
    *CELL_COORDINATES,
    "ordinal",
    "prepared",
    "pack_status",
    "reason",
    "pack_sha256",
    "manifest",
    "manifest_sha256",
    "expected_identity",
)
USAGE_FIELDS = ("input_tokens", "output_tokens", "tool_calls", "cost_usd")
OUTCOME_STATES = ("passed", "failed", "unknown", "unprepared")
NOT_STARTED, BEGUN, COMPLETE, UNKNOWN = "not_started", "begun", "complete", "unknown"


def _decimal(value: Any, label: str) -> Decimal:
    try:
        return Decimal(value if isinstance(value, Decimal | str) else str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ManifestError(f"{label} is not a decimal amount") from exc


def _publish(path: Path, value: Any) -> bytes:
    """Create one durable record exactly once; an existing record is never rewritten."""
    body = canonical_bytes(value) + b"\n"
    with path.open("xb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return body


def _read(path: Path) -> Any:
    if path.is_symlink() or not path.is_file():
        raise ManifestError(f"missing launcher record: {path}")
    return strict_json(path.read_bytes())


def _now() -> str:
    return datetime.now(UTC).isoformat()


def claim(
    root: Path,
    *,
    schedule: dict[str, Any],
    materialization: dict[str, Any],
    ceiling_usd: Decimal,
    prior_reserved_usd: Decimal,
    per_cell_usd: Decimal,
) -> dict[str, Any]:
    """Reserve one amount for every prepared cell, or refuse the whole schedule.

    The reservation covers prepared cells only: an unprepared cell is never
    dispatched, so it never costs anything. Writing ``claim.json`` exclusively
    is what makes the reservation single-use; a second claim against the same
    root is refused rather than silently doubling the held amount.
    """
    if materialization.get("schema") != MATERIALIZATION:
        raise ManifestError("unknown materialization report")
    if materialization.get("schedule_sha256") != identity(schedule):
        raise ManifestError("the materialization was built from another schedule")
    cells = materialization["cells"]
    if len(cells) != len(schedule["cells"]):
        raise ManifestError("the materialization does not cover the whole schedule")
    for cell, scheduled in zip(cells, schedule["cells"], strict=True):
        if any(cell[key] != scheduled[key] for key in CELL_COORDINATES):
            raise ManifestError(f"materialized cell is outside the schedule: {cell['attempt_id']}")
    ceiling = _decimal(ceiling_usd, "ceiling_usd")
    prior = _decimal(prior_reserved_usd, "prior_reserved_usd")
    per_cell = _decimal(per_cell_usd, "per_cell_usd")
    if min(ceiling, prior, per_cell) < 0:
        raise ManifestError("budget amounts must not be negative")
    prepared = [cell for cell in cells if cell["prepared"]]
    reservation = per_cell * len(prepared)
    cumulative = prior + reservation
    if cumulative > ceiling:
        raise ManifestError(
            f"reservation {cumulative} exceeds the accepted ceiling {ceiling}; no claim written"
        )
    record = {
        "schema": CLAIM_SCHEMA,
        "claimed_at": _now(),
        "schedule_sha256": identity(schedule),
        "materialization_sha256": identity(materialization),
        "materialization_root": materialization["root"],
        "denominator": len(cells),
        "prepared_cells": len(prepared),
        "per_cell_usd": str(per_cell),
        "reserved_usd": str(reservation),
        "prior_reserved_usd": str(prior),
        "cumulative_reserved_usd": str(cumulative),
        "ceiling_usd": str(ceiling),
        "release_on_unknown": False,
        "cells": [{key: cell[key] for key in CELL_FIELDS} for cell in cells],
    }
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "cells").mkdir(mode=0o700, exist_ok=True)
    try:
        _publish(root / "claim.json", record)
    except FileExistsError as exc:
        raise ManifestError("this root already holds a claim; reservations are single-use") from exc
    return record


def _dispatch_state(cell_root: Path) -> str:
    if (cell_root / "outcome.json").is_file():
        return COMPLETE
    if (cell_root / "begin.json").is_file():
        return UNKNOWN
    return NOT_STARTED


def _usage(receipt: dict[str, Any]) -> dict[str, Any] | None:
    usage = receipt.get("usage")
    if not isinstance(usage, dict):
        return None
    return {key: usage.get(key) for key in USAGE_FIELDS}


def _check_credentials(
    materialization_root: Path, cells: list[dict[str, Any]], api_key_env: str | None
) -> None:
    """Confirm the declared provider variable is populated before spending anything.

    Only the name is inspected here. The value is read by the runner, from the
    environment, and never passes through this module's records or logs.
    """
    declared = set()
    for relative in sorted({cell["manifest"] for cell in cells if cell["prepared"]}):
        manifest = _read(materialization_root / relative)
        if manifest.get("controller_api_key_env") is not None:
            declared.add(manifest["controller_api_key_env"])
    if not declared:
        return
    if declared != {api_key_env}:
        raise ManifestError(
            f"the manifests declare {sorted(declared)} as the controller credential variable"
        )
    if not os.environ.get(api_key_env):
        raise ManifestError(f"the controller credential {api_key_env} is not set")


def _run_cell(
    cell: dict[str, Any], *, cells_root: Path, materialization_root: Path, dry_run: bool
) -> dict[str, Any]:
    """Own one cell end to end: claim it with a begin record, then seal an outcome."""
    attempt_id = cell["attempt_id"]
    cell_root = cells_root / attempt_id
    cell_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    begin = {
        "attempt_id": attempt_id,
        **{key: cell[key] for key in ("checkpoint", "task", "arm", "family", "category")},
        "state": BEGUN,
        "dry_run": dry_run,
        "prepared": cell["prepared"],
        "manifest": cell["manifest"],
        "manifest_sha256": cell["manifest_sha256"],
        "pack_sha256": cell["pack_sha256"],
        "began_at": _now(),
    }
    outcome: dict[str, Any] = {
        "attempt_id": attempt_id,
        **{key: cell[key] for key in ("checkpoint", "task", "arm", "family", "category")},
        "prepared": cell["prepared"],
        "dry_run": dry_run,
        "status": "dry_run" if dry_run else "runner_error",
        "success": False,
        "task_outcome": None,
        "usage": None,
        "receipt_sha256": None,
        "receipt_file_sha256": None,
        "identity": None,
    }
    if not cell["prepared"]:
        outcome.update(status="unprepared", reason=cell["reason"])
        _publish(cell_root / "outcome.json", {**outcome, "completed_at": _now()})
        return outcome
    _publish(cell_root / "begin.json", begin)
    if not dry_run:
        try:
            receipt = runner.run_task(
                materialization_root / cell["manifest"],
                task_id=cell["task"],
                arm_id=cell["arm"],
                output=cell_root / "attempt",
                attempt_id=attempt_id,
            )
        except (OSError, ValueError) as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        else:
            receipt_path = cell_root / "attempt" / "receipt.json"
            outcome.update(
                status=receipt.get("status"),
                success=bool(receipt.get("success")),
                task_outcome=receipt.get("outcome"),
                usage=_usage(receipt),
                receipt_sha256=receipt.get("receipt_sha256"),
                receipt_file_sha256=digest(receipt_path.read_bytes()),
                identity={field: receipt.get(field) for field in cell["expected_identity"] or {}},
                budget_status=receipt.get("budget_status"),
            )
    _publish(cell_root / "outcome.json", {**outcome, "completed_at": _now()})
    return outcome


def execute(
    root: Path,
    *,
    workers: int = 1,
    api_key_env: str | None = "OPENROUTER_API_KEY",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run every not-yet-outcome cell once, in schedule order, and resume safely.

    The controller credential is read by the runner from the environment named
    in each manifest. This function only checks that the variable is populated
    before spending anything; it never reads, logs or stores the value.
    """
    if workers < 1:
        raise ManifestError("at least one worker is required")
    claim_record = _read(root / "claim.json")
    if claim_record.get("schema") != CLAIM_SCHEMA:
        raise ManifestError("this root holds no screen48 claim")
    materialization_root = Path(claim_record["materialization_root"])
    materialization = _read(materialization_root / "materialization.json")
    if identity(materialization) != claim_record["materialization_sha256"]:
        raise ManifestError("the materialization changed after the claim was written")
    cells = claim_record["cells"]
    if not dry_run:
        _check_credentials(materialization_root, cells, api_key_env)
    cells_root = root / "cells"
    cells_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    pending, skipped, unknown = [], [], []
    for cell in cells:
        state = _dispatch_state(cells_root / cell["attempt_id"])
        if state == COMPLETE:
            skipped.append(cell["attempt_id"])
        elif state == UNKNOWN:
            unknown.append(cell["attempt_id"])
        else:
            pending.append(cell)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                _run_cell,
                cell,
                cells_root=cells_root,
                materialization_root=materialization_root,
                dry_run=dry_run,
            )
            for cell in pending
        ]
        outcomes = [future.result() for future in futures]
    record = {
        "schema": EXECUTION_SCHEMA,
        "root": str(root),
        "dry_run": dry_run,
        "workers": workers,
        "denominator": len(cells),
        "dispatched": len(outcomes),
        "skipped_complete": skipped,
        "unknown_not_replayed": unknown,
        "outcomes": outcomes,
        "completed_at": _now(),
    }
    executions = root / "executions"
    executions.mkdir(mode=0o700, exist_ok=True)
    _publish(executions / f"{uuid4().hex}.json", record)
    return record


def _row(cell: dict[str, Any], cells_root: Path) -> dict[str, Any]:
    state = _dispatch_state(cells_root / cell["attempt_id"])
    row = {
        **{key: cell[key] for key in CELL_COORDINATES},
        "ordinal": cell["ordinal"],
        "prepared": cell["prepared"],
        "dispatch_state": state,
        "receipt_status": None,
        "task_passed": None,
        "usage": None,
        "pack_sha256": cell["pack_sha256"],
        "manifest_sha256": cell["manifest_sha256"],
        "expected_identity": cell["expected_identity"],
        "receipt_identity": None,
        "reason": cell["reason"],
    }
    if state != COMPLETE:
        return row
    outcome = _read(cells_root / cell["attempt_id"] / "outcome.json")
    task_outcome = outcome.get("task_outcome")
    row.update(
        receipt_status=outcome.get("status"),
        usage=outcome.get("usage"),
        receipt_identity=outcome.get("identity"),
        receipt_sha256=outcome.get("receipt_sha256"),
        budget_status=outcome.get("budget_status"),
    )
    if isinstance(task_outcome, dict) and isinstance(task_outcome.get("passed"), bool):
        row["task_passed"] = task_outcome["passed"]
    return row


def _outcome_state(row: dict[str, Any]) -> str:
    if not row["prepared"]:
        return "unprepared"
    if row["task_passed"] is True:
        return "passed"
    if row["task_passed"] is False:
        return "failed"
    return "unknown"


def terminal(root: Path) -> dict[str, Any]:
    """Report every scheduled cell, executed or not, from the durable records alone."""
    claim_record = _read(root / "claim.json")
    cells_root = root / "cells"
    rows = [_row(cell, cells_root) for cell in claim_record["cells"]]
    groups = []
    for checkpoint in sorted({row["checkpoint"] for row in rows}):
        for arm in sorted({row["arm"] for row in rows}):
            selected = [row for row in rows if (row["checkpoint"], row["arm"]) == (checkpoint, arm)]
            counts = dict.fromkeys(OUTCOME_STATES, 0)
            for row in selected:
                counts[_outcome_state(row)] += 1
            groups.append(
                {
                    "checkpoint": checkpoint,
                    "arm": arm,
                    "denominator": len(selected),
                    **counts,
                }
            )
    totals = {state: sum(group[state] for group in groups) for state in OUTCOME_STATES}
    report = {
        "schema": TERMINAL_SCHEMA,
        "root": str(root),
        "schedule_sha256": claim_record["schedule_sha256"],
        "materialization_sha256": claim_record["materialization_sha256"],
        "denominator": len(rows),
        "reserved_usd": claim_record["reserved_usd"],
        "cumulative_reserved_usd": claim_record["cumulative_reserved_usd"],
        "rows": rows,
        "groups": groups,
        "totals": {**totals, "denominator": len(rows)},
        "dispatch_states": {
            state: sum(row["dispatch_state"] == state for row in rows)
            for state in (NOT_STARTED, BEGUN, COMPLETE, UNKNOWN)
        },
        "learning_benefit_established": False,
        "generated_at": _now(),
    }
    runner._write_json(root / "terminal.json", report)
    return report


def _load(path: str) -> dict[str, Any]:
    return strict_json(Path(path).read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screen48-launcher", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    claim_parser = commands.add_parser("claim", help="reserve the prepared cells")
    claim_parser.add_argument("--root", required=True, type=Path)
    claim_parser.add_argument("--schedule", required=True)
    claim_parser.add_argument("--materialization", required=True)
    claim_parser.add_argument("--ceiling-usd", required=True)
    claim_parser.add_argument("--prior-reserved-usd", required=True)
    claim_parser.add_argument("--per-cell-usd", required=True)
    execute_parser = commands.add_parser("execute", help="run the claimed cells once each")
    execute_parser.add_argument("--root", required=True, type=Path)
    execute_parser.add_argument("--workers", type=int, default=1)
    execute_parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    execute_parser.add_argument("--dry-run", action="store_true")
    terminal_parser = commands.add_parser("terminal", help="report all scheduled cells")
    terminal_parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "claim":
        record = claim(
            args.root,
            schedule=_load(args.schedule),
            materialization=_load(args.materialization),
            ceiling_usd=_decimal(args.ceiling_usd, "ceiling_usd"),
            prior_reserved_usd=_decimal(args.prior_reserved_usd, "prior_reserved_usd"),
            per_cell_usd=_decimal(args.per_cell_usd, "per_cell_usd"),
        )
    elif args.command == "execute":
        record = execute(
            args.root,
            workers=args.workers,
            api_key_env=args.api_key_env,
            dry_run=args.dry_run,
        )
    else:
        record = terminal(args.root)
    json.dump(record, sys.stdout, sort_keys=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
