"""Measure what the solver reaches on the transfer material with no memory at all.

The 48-cell screen compares memory arms against each other on six preselected
tasks. This screen asks the prior question about the remaining material: does a
task leave room for memory to matter? A task the solver already passes without
memory cannot show a benefit, and one it never passes cannot either. Only the
band in between is worth spending a memory arm on.

So every cell here is the ``no_memory`` arm: one empty memory pack, no declared
learning source, no database, no graph read, one solver call per repetition.
The output classifies each task as saturated, headroom, floor or undetermined
and reports the cost of having asked. No cell establishes any learning benefit;
a band is a statement about the task, not about memory.

A band is a statement about a rate, and a rate from two repetitions is a coin
flip: the first screen called a task floor on 0/2 and the floor probe then
passed it 3/3 cold. So a band is assigned from a Wilson 95% interval over the
known outcomes, never from the point rate alone, and no band at all is assigned
from fewer than ``MINIMUM_REPETITIONS`` known outcomes. The point-rate band is
still recorded, as ``point_band``, so a reader can see what the old rule would
have said; it decides nothing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from benchmarks.agent_tasks import runner
from benchmarks.agent_tasks.manifest import (
    ManifestError,
    canonical_bytes,
    digest,
    load_manifest,
    strict_json,
)
from benchmarks.agent_tasks.screen48 import materialize
from benchmarks.agent_tasks.screen48.contract import TASKS as SCREEN_TASKS

SCHEMA = "sibyl-screen48-headroom-v2"
#: Namespace for the emitted experiment IDs. These manifests are one-arm
#: diagnostics, never cells of the frozen 48-cell schedule.
NAMESPACE = "headroom"
CHECKPOINT = 0
ARM = materialize.NO_MEMORY_ARM
CATALOG_NAME = "material-catalog.json"
MATERIAL_ROOT = Path(__file__).resolve().parents[1] / "transfer_material"
SATURATED, HEADROOM, FLOOR = "saturated", "headroom", "floor"
#: Too few known outcomes to place the interval, or an interval that straddles
#: a threshold. Not a band: a task here has not been measured yet.
UNDETERMINED = "undetermined"
#: A task the solver passes at or above this rate without memory has no room
#: left to show one; below the floor it has none to show yet.
SATURATED_AT = 0.9
FLOOR_BELOW = 0.3
BANDS = (SATURATED, HEADROOM, FLOOR, UNDETERMINED)
#: Known outcomes (a task pass or a task failure, never a controller failure)
#: a task needs before any band is assigned. This is a policy floor, not a
#: derived minimum: 0/1 already excludes saturation (upper bound 0.79). Five
#: is where 0/5 has an upper bound of 0.43, clear of ``FLOOR_BELOW`` with
#: margin, so a floor band means the solver was given a fair number of tries
#: and not that one attempt happened to fail. Five cannot reach headroom or
#: saturated at all; see ``DEFAULT_REPETITIONS``.
MINIMUM_REPETITIONS = 5
#: What a default run asks for. At five known outcomes only floor and
#: undetermined are reachable (4/5 and 5/5 cannot exclude saturation). Ten is
#: the first round count at which 6/10 and 7/10 read as headroom, so a default
#: run can place a task in the band the memory arms are spent on. Saturated
#: stays out of reach until 35/35; a task that passes every cold attempt at ten
#: reads undetermined, and retiring it as saturated is a larger, separate ask.
DEFAULT_REPETITIONS = 10
#: Two-sided confidence of the Wilson score interval the bands are read from.
CONFIDENCE = 0.95
_Z = 1.959964
INTERVAL_METHOD = "wilson"
#: The row fields a consumer of ``headroom.json`` binds a band to. A report
#: without them was banded by point rate and must not be read as a band.
BAND_FIELDS = ("passes", "failures", "known", "pass_rate", "interval", "headroom_band")
USAGE_FIELDS = ("input_tokens", "output_tokens", "tool_calls", "cost_usd")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def point_band(pass_rate: float) -> str:
    """The band the point rate alone would give. Recorded, never decisive."""
    if pass_rate >= SATURATED_AT:
        return SATURATED
    if pass_rate >= FLOOR_BELOW:
        return HEADROOM
    return FLOOR


def wilson_interval(passes: int, known: int) -> dict[str, Any] | None:
    """Wilson score interval for ``passes`` of ``known`` binary outcomes.

    ``None`` when nothing is known: an interval over zero trials is not [0, 1],
    it is no interval. Bounds are rounded to six places, the report's precision.
    """
    if known < 0 or passes < 0 or passes > known:
        raise ManifestError(f"impossible outcome count: {passes} of {known}")
    if known == 0:
        return None
    z2 = _Z * _Z
    rate = passes / known
    centre = (rate + z2 / (2 * known)) / (1 + z2 / known)
    half = _Z * math.sqrt(rate * (1 - rate) / known + z2 / (4 * known * known)) / (1 + z2 / known)
    return {
        "method": INTERVAL_METHOD,
        "confidence": CONFIDENCE,
        "lower": round(max(0.0, centre - half), 6),
        "upper": round(min(1.0, centre + half), 6),
    }


def band(passes: int, known: int) -> str:
    """Classify one task by where its interval sits, not where its rate sits.

    Saturated needs the whole interval at or above ``SATURATED_AT``: the solver
    reliably passes cold and memory has no room. Headroom needs the interval to
    exclude both saturation and the floor. Floor needs the interval to exclude
    saturation while it cannot exclude the floor: the solver does not pass this
    reliably, and how often it passes at all is what the floor probe measures
    against. Anything else, including every task under ``MINIMUM_REPETITIONS``
    known outcomes, is undetermined: the screen could not place it. That is a
    statement about the measurement, not about eligibility; the floor probe
    accepts any task with enough known outcomes and lets its own rows say
    whether an arm added anything.
    """
    if known < MINIMUM_REPETITIONS:
        return UNDETERMINED
    interval = wilson_interval(passes, known)
    assert interval is not None
    lower, upper = interval["lower"], interval["upper"]
    if lower >= SATURATED_AT:
        return SATURATED
    if upper >= SATURATED_AT:
        return UNDETERMINED
    if lower >= FLOOR_BELOW:
        return HEADROOM
    return FLOOR


def catalog_families(tasks_root: Path) -> dict[str, str]:
    """Read the frozen material catalog as task ID to declared task family."""
    catalog = strict_json((tasks_root / CATALOG_NAME).read_bytes())
    if not isinstance(catalog, dict) or not isinstance(catalog.get("tasks"), list):
        raise ManifestError(f"material catalog names no tasks: {tasks_root / CATALOG_NAME}")
    families = {str(row["id"]): str(row["family_id"]) for row in catalog["tasks"]}
    if len(families) != len(catalog["tasks"]):
        raise ManifestError("the material catalog repeats a task ID")
    return families


def default_task_ids(tasks_root: Path) -> list[str]:
    """Every catalogued task the 48-cell screen does not already own.

    The six screen tasks are measured by the screen itself, against real memory
    arms. Re-measuring them here would spend money on a number that already
    exists, so they are excluded and the rest of the material is the subject.
    """
    return [task for task in catalog_families(tasks_root) if task not in SCREEN_TASKS]


def _emit_manifest(
    *, tasks_root: Path, template: dict[str, Any], task_id: str, family: str, output: Path
) -> Path:
    """Write one validated single-task, single-arm manifest and return its path.

    The arm is handed an empty memory pack and cites no learning source, so the
    manifest declares no experience and the emitted directory holds only the
    task, the checker and the controller.
    """
    manifest, files = materialize.build_manifest(
        namespace=NAMESPACE,
        template=template,
        tasks_root=tasks_root,
        task_id=task_id,
        checkpoint=CHECKPOINT,
        prepared=[({"arm": ARM, "family": family}, b"")],
        experiences=[],
    )
    directory = output / "manifests" / task_id
    body = canonical_bytes(manifest.model_dump(mode="json")) + b"\n"
    for relative, content in files.items():
        materialize.emit(directory, relative, content)
    materialize.emit(directory, "manifest.json", body)
    path = directory / "manifest.json"
    loaded, _ = load_manifest(path)
    if loaded.experiment_id != manifest.experiment_id:
        raise ManifestError(f"emitted manifest differs: {path}")
    return path


def _usage(receipt: dict[str, Any]) -> dict[str, Any]:
    usage = receipt.get("usage")
    if not isinstance(usage, dict):
        return dict.fromkeys(USAGE_FIELDS)
    return {key: usage.get(key) for key in USAGE_FIELDS}


def _elapsed(receipt: dict[str, Any]) -> float | None:
    """The controller's own wall time, when the attempt got far enough to have one."""
    controller = receipt.get("controller")
    if not isinstance(controller, dict):
        return None
    elapsed = controller.get("elapsed_seconds")
    return float(elapsed) if isinstance(elapsed, int | float) else None


TRACE_NAME = "controller-trace.jsonl"


def premature_completion(trace_path: Path) -> bool | None:
    """Did the solver stop on its own without ever changing the workspace?

    The floor probe watched a solver read a rendered historical episode as its
    own earlier turns, run one check, and declare finished work that happened
    in another container. That shape is mechanical: the run ends on the model's
    own stop, and no tool result records a workspace that differs from the one
    before it. ``None`` means the trace was absent or unreadable, which is an
    unknown rather than a clean run.
    """
    try:
        events = [json.loads(line) for line in trace_path.read_text().splitlines() if line]
    except (OSError, ValueError):
        return None
    if not events:
        return None
    terminal = [event for event in events if event.get("kind") == "terminal"]
    if not terminal or terminal[-1].get("payload", {}).get("reason") != "stop":
        return False
    edited = any(
        event.get("kind") == "tool_result"
        and event.get("payload", {}).get("workspace_before")
        != event.get("payload", {}).get("workspace_after")
        for event in events
    )
    return not edited


def _outcome(receipt: dict[str, Any]) -> bool | None:
    outcome = receipt.get("outcome")
    if isinstance(outcome, dict) and isinstance(outcome.get("passed"), bool):
        return outcome["passed"]
    return None


def run_cell(
    task_id: str,
    repetition: int,
    *,
    arm: str,
    manifest_path: Path,
    output: Path,
    relative: str,
) -> dict[str, Any]:
    """Run one repetition of one arm of one task, recording whatever happened.

    A raising runner is the caller's own problem, never the sweep's: the cell
    records what broke and the remaining cells still run. ``relative`` is the
    directory the attempt runs in, under ``output``, and the receipt is
    recorded at that same path so a report names a file a reader can open.

    The arm is a parameter because two diagnostics share this: this screen,
    which only ever runs ``no_memory``, and the floor probe, which runs every
    arm of one task against one another.
    """
    attempt_id = uuid4().hex
    cell: dict[str, Any] = {
        "task": task_id,
        "arm": arm,
        "repetition": repetition,
        "attempt_id": attempt_id,
        "status": "runner_error",
        "success": False,
        "passed": None,
        "premature_completion": None,
        "usage": dict.fromkeys(USAGE_FIELDS),
        "elapsed_seconds": None,
        "receipt": None,
        "began_at": _now(),
    }
    cell_root = output / relative
    cell_root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        receipt = runner.run_task(
            manifest_path,
            task_id=task_id,
            arm_id=arm,
            output=cell_root,
            attempt_id=attempt_id,
        )
    except Exception as exc:  # one broken cell must never end the sweep
        cell.update(error=f"{type(exc).__name__}: {exc}", error_type=type(exc).__name__)
    else:
        cell.update(
            status=receipt.get("status"),
            success=bool(receipt.get("success")),
            passed=_outcome(receipt),
            premature_completion=premature_completion(cell_root / TRACE_NAME),
            usage=_usage(receipt),
            elapsed_seconds=_elapsed(receipt),
            receipt=f"{relative}/receipt.json",
            receipt_sha256=receipt.get("receipt_sha256"),
            budget_status=receipt.get("budget_status"),
        )
    cell["completed_at"] = _now()
    return cell


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _task_row(task_id: str, family: str, cells: list[dict[str, Any]]) -> dict[str, Any]:
    passes = sum(cell["passed"] is True for cell in cells)
    failures = sum(cell["passed"] is False for cell in cells)
    unknown = sum(cell["passed"] is None for cell in cells)
    known = passes + failures
    pass_rate = round(passes / known, 6) if known else None
    costs = [float(cell["usage"]["cost_usd"]) for cell in cells if cell["usage"]["cost_usd"]]
    return {
        "task": task_id,
        "family_id": family,
        "repetitions": len(cells),
        "denominator": len(cells),
        "passes": passes,
        "failures": failures,
        # No task outcome at all: a controller, checker or runner failure, not a
        # statement about the task. Reported apart from a real failure and
        # outside the rate: a cell that never reached the checker says nothing
        # about whether the solver can pass the task.
        "unknown": unknown,
        "known": known,
        "minimum_repetitions": MINIMUM_REPETITIONS,
        "pass_rate": pass_rate,
        "interval": wilson_interval(passes, known),
        "headroom_band": band(passes, known),
        "point_band": point_band(pass_rate) if pass_rate is not None else None,
        "mean_tool_calls": _mean(
            [
                float(cell["usage"]["tool_calls"])
                for cell in cells
                if cell["usage"]["tool_calls"] is not None
            ]
        ),
        "mean_cost_usd": _mean(costs),
        "mean_elapsed_seconds": _mean(
            [cell["elapsed_seconds"] for cell in cells if cell["elapsed_seconds"] is not None]
        ),
        "cost_usd": round(sum(costs), 6),
        "statuses": {
            status: sum(cell["status"] == status for cell in cells)
            for status in sorted({str(cell["status"]) for cell in cells})
        },
    }


def _check_credentials(template: dict[str, Any], api_key_env: str | None) -> None:
    """Confirm the declared provider variable is populated before spending anything.

    Only the name is read here. The value is read by the runner, out of the
    environment, and never enters this module's records, logs or table.
    """
    declared = template.get("controller_api_key_env")
    if declared is None:
        return
    if declared != api_key_env:
        raise ManifestError(f"the template declares {declared} as the controller credential")
    if not os.environ.get(api_key_env or ""):
        raise ManifestError(f"the controller credential {api_key_env} is not set")


def screen(
    *,
    tasks_root: Path,
    template: dict[str, Any],
    task_ids: list[str],
    repetitions: int,
    output: Path,
    workers: int = 6,
    api_key_env: str | None = "OPENROUTER_API_KEY",
) -> dict[str, Any]:
    """Run every task once per repetition with no memory and band the results."""
    if repetitions < 1:
        raise ManifestError("at least one repetition is required")
    if workers < 1:
        raise ManifestError("at least one worker is required")
    if not task_ids:
        raise ManifestError("the screen names no task")
    if len(set(task_ids)) != len(task_ids):
        raise ManifestError("the screen repeats a task")
    materialize.validate_template(template, require_experiences=False)
    _check_credentials(template, api_key_env)
    families = catalog_families(tasks_root)
    if unknown := sorted(set(task_ids) - set(families)):
        raise ManifestError(f"tasks are outside the material catalog: {unknown}")
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ManifestError("headroom output already exists")
    output.mkdir(mode=0o700, parents=True)
    manifests: dict[str, Path] = {}
    refused: dict[str, str] = {}
    for task_id in task_ids:
        try:
            manifests[task_id] = _emit_manifest(
                tasks_root=tasks_root,
                template=template,
                task_id=task_id,
                family=families[task_id],
                output=output,
            )
        except Exception as exc:  # an unbuildable task is a bad cell, not an abort
            refused[task_id] = f"{type(exc).__name__}: {exc}"
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                run_cell,
                task_id,
                repetition,
                arm=ARM,
                manifest_path=manifests[task_id],
                output=output,
                relative=f"cells/{task_id}/{repetition}",
            )
            for task_id in task_ids
            if task_id in manifests
            for repetition in range(repetitions)
        ]
        cells = [future.result() for future in futures]
    for task_id, error in refused.items():
        cells.extend(
            {
                "task": task_id,
                "arm": ARM,
                "repetition": repetition,
                "attempt_id": None,
                "status": "runner_error",
                "success": False,
                "passed": None,
                "usage": dict.fromkeys(USAGE_FIELDS),
                "elapsed_seconds": None,
                "receipt": None,
                "error": error,
                "error_type": "manifest",
                "completed_at": _now(),
            }
            for repetition in range(repetitions)
        )
    rows = [
        _task_row(task_id, families[task_id], [cell for cell in cells if cell["task"] == task_id])
        for task_id in task_ids
    ]
    totals = {
        key: sum(row[key] for row in rows)
        for key in ("denominator", "passes", "failures", "unknown", "known")
    }
    report = {
        "schema": SCHEMA,
        "root": str(output),
        "arm": ARM,
        "tasks_root": str(tasks_root.absolute()),
        "template_sha256": digest(canonical_bytes(template)),
        "controller_model": template["controller_model"],
        "repetitions": repetitions,
        "minimum_repetitions": MINIMUM_REPETITIONS,
        "interval": {"method": INTERVAL_METHOD, "confidence": CONFIDENCE},
        "thresholds": {"saturated_at": SATURATED_AT, "floor_below": FLOOR_BELOW},
        "workers": workers,
        "task_count": len(task_ids),
        "tasks": rows,
        "totals": {
            **totals,
            "pass_rate": (
                round(totals["passes"] / totals["known"], 6) if totals["known"] else None
            ),
        },
        "total_cost_usd": round(sum(row["cost_usd"] for row in rows), 6),
        "bands": {
            name: [row["task"] for row in rows if row["headroom_band"] == name] for name in BANDS
        },
        "memory_established": False,
        "learning_benefit_established": False,
        "cells": cells,
        "generated_at": _now(),
    }
    runner._write_json(output / "headroom.json", report)
    return report


def _rate(value: float | None) -> str:
    return f"{value:>7.2f}" if value is not None else f"{'-':>7}"


def _bounds(interval: dict[str, Any] | None) -> str:
    if interval is None:
        return f"{'-':>13}"
    return f"{interval['lower']:>6.2f}-{interval['upper']:<6.2f}"


def table(report: dict[str, Any]) -> str:
    """Render the per-task bands as one compact fixed-width table."""
    header = (
        f"{'task':<32}{'pass':>5}{'fail':>5}{'unk':>5}{'rate':>7} {'95% wilson':<13} "
        f"{'band':<13}{'cost':>9}"
    )
    lines = [header, "-" * len(header)]
    for row in report["tasks"]:
        lines.append(
            f"{row['task']:<32}{row['passes']:>5}{row['failures']:>5}{row['unknown']:>5}"
            f"{_rate(row['pass_rate'])} {_bounds(row['interval'])} "
            f"{row['headroom_band']:<13}{row['cost_usd']:>9.4f}"
        )
    totals = report["totals"]
    lines.append("-" * len(header))
    lines.append(
        f"{'totals':<32}{totals['passes']:>5}{totals['failures']:>5}{totals['unknown']:>5}"
        f"{_rate(totals['pass_rate'])} {'':<13} {'':<13}{report['total_cost_usd']:>9.4f}"
    )
    lines.append(f"bands need at least {report['minimum_repetitions']} known outcomes per task")
    for name in BANDS:
        lines.append(f"{name}: {', '.join(report['bands'][name]) or 'none'}")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="screen48-headroom", description=__doc__)
    parser.add_argument("--tasks-root", type=Path, default=MATERIAL_ROOT)
    parser.add_argument("--task-ids", nargs="+", default=None)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    args = parser.parse_args(argv)
    report = screen(
        tasks_root=args.tasks_root,
        template=strict_json(args.template.read_bytes()),
        task_ids=args.task_ids or default_task_ids(args.tasks_root),
        repetitions=args.repetitions,
        output=args.output,
        workers=args.workers,
        api_key_env=args.api_key_env,
    )
    sys.stdout.write(table(report))
    sys.stdout.write(f"\nheadroom.json: {report['root']}/headroom.json\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
