"""Probe the floor: does any memory arm lift a task the solver fails cold?

The no-memory screen bands the transfer material by what the solver reaches
with an empty pack. A task in the floor band is one the solver does not pass
reliably, and the screen cannot say why: the task may be out of the solver's
reach entirely, or it may be exactly the task a memory arm would carry. This
module asks that second question directly.

It asks it only of a task the screen actually measured. The probe binds every
task it is given to the screen's ``headroom.json`` and refuses a task whose band
came from fewer than ``headroom.MINIMUM_REPETITIONS`` known outcomes, or a
report written before bands carried an interval: the first probe was built on
a floor band from two repetitions that turned out to be a coin flip. It prepares the four memory arms for a handful of
catalogued tasks at one checkpoint and runs them against each other, so a lift
over the ``no_memory`` arm is visible if there is one.

This is a diagnostic, not a cell of the frozen 48-cell schedule. Its tasks are
outside the preselected six, its packs are prepared fresh at whatever
checkpoint is asked for rather than carrying checkpoint-0 bytes, and its
manifests live in their own ``probe`` experiment namespace. Nothing it measures
establishes a learning benefit: the schedule owns that claim, this module
reports pass rates and the arithmetic difference between them, and both report
flags say so in as many words.

Two things it does share with the study. The packs come off the live
post-consolidation database through ``RecallAdapter``, the same owner
qualification, native inventory and summary library the checkpoint phase uses,
so a memory arm here is the memory arm the study means. And the task material
is the frozen transfer tree, read through the catalog, so the solver sees the
authored prompt and workspace rather than anything this module composed.

Outputs, all under ``--output``:

``preparation.json``
    Every cell asked for, its status and reason, and the catalog digests the
    packs were prepared against.
``headroom``, inside ``probe.json``
    The screen report the probed tasks were bound to: its path, digest and the
    band row of every probed task.
``native-inventory.json``
    The checkpoint's authorized native universe and its production receipt.
``packs/cp{checkpoint}/{task}/{arm}.json`` and ``{arm}.txt``
    One preparation receipt per cell, beside the exact memory bytes.
``manifests/{task}/manifest.json``
    One validated manifest per task, carrying every prepared arm.
``probe.json``
    Pass rates per task and arm, the lift over ``no_memory``, and every cell.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks.agent_tasks import runner
from benchmarks.agent_tasks.manifest import (
    ManifestError,
    canonical_bytes,
    digest,
    load_manifest,
    strict_json,
)
from benchmarks.agent_tasks.screen48 import checkpoints, contract, cycle, headroom, materialize
from benchmarks.agent_tasks.screen48.recall.native_inventory import NativeCheckpoints
from benchmarks.agent_tasks.screen48.recall.recall_adapter import Reader, RecallAdapter

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

SCHEMA = "sibyl-screen48-probe-v1"
PREPARATION_SCHEMA = "sibyl-screen48-probe-preparation-v1"
#: Namespace for the emitted experiment IDs. These manifests are diagnostics,
#: never cells of the frozen 48-cell schedule.
NAMESPACE = "probe"
MATERIAL_ROOT = headroom.MATERIAL_ROOT
PREPARATION_NAME = "preparation.json"
REPORT_NAME = "probe.json"
NO_MEMORY_ARM = materialize.NO_MEMORY_ARM
USAGE_FIELDS = headroom.USAGE_FIELDS
DEFAULT_CHECKPOINT = 1
DEFAULT_REPETITIONS = 3
DEFAULT_WORKERS = 6

EXIT_OK = 0
#: No pack was prepared at all, so nothing was asked of the solver. The reports
#: are still written: the reasons are the finding.
EXIT_NO_PACKS = 2


def _now() -> str:
    return datetime.now(UTC).isoformat()


def material_task_source(tasks_root: Path) -> Callable[[str, Path], tuple[str, dict[str, bytes]]]:
    """A task reader over the frozen transfer material, for ``RecallAdapter``.

    The study's own reader, ``contract.public_task``, serves the preselected
    six through hash-bound policy inputs, and there are no policy inputs for
    any other task. This reads the same two things out of the catalogued
    material instead: the authored ``prompt.md`` and every regular file under
    ``workspace/``, keyed by its POSIX path relative to that directory.

    The policy root is accepted and ignored, because the adapter hands its
    catalog's root to whatever reader it was given and this reader has no use
    for it. A missing prompt or an empty workspace is refused rather than
    packed: the counter would otherwise measure a request the solver will
    never be asked, and the cell would look prepared.
    """
    root = Path(tasks_root)

    def read(task: str, policy_root: Path) -> tuple[str, dict[str, bytes]]:
        del policy_root  # the frozen policy inputs cover the preselected six only
        directory = root / "tasks" / task
        prompt_path = directory / "prompt.md"
        if prompt_path.is_symlink() or not prompt_path.is_file():
            raise ManifestError(f"task prompt is absent: {prompt_path}")
        workspace_root = directory / "workspace"
        workspace: dict[str, bytes] = {}
        for path in sorted(workspace_root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            workspace[path.relative_to(workspace_root).as_posix()] = path.read_bytes()
        if not workspace:
            raise ManifestError(f"task workspace is empty: {task}")
        return prompt_path.read_text(encoding="utf-8"), dict(sorted(workspace.items()))

    return read


def bound_bands(report: dict[str, Any], tasks: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The screen's band row for every probed task, or a refusal naming why not.

    A band is only as good as the outcomes under it. The report has to be the
    interval-banded schema, every probed task has to be in it, and every one
    has to have reached ``headroom.MINIMUM_REPETITIONS`` known outcomes. The
    stored counts, interval and band are recomputed here rather than trusted:
    a report is a file, and a file can be edited. Which band a task carries is
    not adjudicated: undetermined means the screen could not place the task,
    not that it is ineligible, and a saturated task can be probed too; the
    probe's own rows will show whether an arm added anything.
    """
    if not isinstance(report, dict) or report.get("schema") != headroom.SCHEMA:
        raise ManifestError(
            f"headroom report is not {headroom.SCHEMA}: bands without an interval are not bands"
        )
    if report.get("minimum_repetitions") != headroom.MINIMUM_REPETITIONS:
        raise ManifestError(
            "headroom report declares a different minimum repetition count: "
            f"{report.get('minimum_repetitions')!r} != {headroom.MINIMUM_REPETITIONS}"
        )
    rows = report.get("tasks")
    if not isinstance(rows, list):
        raise ManifestError("headroom report names no tasks")
    by_task: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("task"), str):
            raise ManifestError("headroom report carries a row without a task")
        if missing := [field for field in headroom.BAND_FIELDS if field not in row]:
            raise ManifestError(f"headroom row for {row['task']} lacks {missing}")
        by_task[row["task"]] = row
    bound: dict[str, dict[str, Any]] = {}
    for task in tasks:
        row = by_task.get(task)
        if row is None:
            raise ManifestError(f"task {task} is not in the headroom report")
        bound[task] = _recomputed_band(task, row)
    return bound


def _recomputed_band(task: str, row: dict[str, Any]) -> dict[str, Any]:
    """The band fields of one row, after every derived value is recomputed."""
    known = row["known"]
    if type(known) is not int or known < headroom.MINIMUM_REPETITIONS:
        raise ManifestError(
            f"task {task} was banded from {known!r} known outcomes; the probe needs at "
            f"least {headroom.MINIMUM_REPETITIONS}"
        )
    passes, failures = row["passes"], row["failures"]
    if type(passes) is not int or type(failures) is not int or passes + failures != known:
        raise ManifestError(
            f"task {task} counts do not add up: {passes!r} passes + {failures!r} failures "
            f"!= {known} known"
        )
    if row["pass_rate"] != round(passes / known, 6):
        raise ManifestError(f"task {task} pass rate does not match its counts")
    if row["interval"] != headroom.wilson_interval(passes, known):
        raise ManifestError(f"task {task} interval does not match its counts")
    if row["headroom_band"] != headroom.band(passes, known):
        raise ManifestError(
            f"task {task} band {row['headroom_band']!r} does not match its counts: "
            f"{headroom.band(passes, known)}"
        )
    return {field: row[field] for field in headroom.BAND_FIELDS}


def _validated(
    *, checkpoint: int, tasks: Sequence[str], arms: Sequence[str], tasks_root: Path
) -> dict[str, str]:
    """Refuse an unknown checkpoint, task or arm before anything is read."""
    if type(checkpoint) is not int or checkpoint not in contract.CHECKPOINTS:
        raise ManifestError(f"unknown checkpoint: {checkpoint!r}")
    if not tasks:
        raise ManifestError("the probe names no task")
    if len(set(tasks)) != len(tasks):
        raise ManifestError("the probe repeats a task")
    if not arms:
        raise ManifestError("the probe names no arm")
    if len(set(arms)) != len(arms):
        raise ManifestError("the probe repeats an arm")
    if unknown := sorted(set(arms) - set(contract.ARMS)):
        raise ManifestError(f"arms are outside the study contract: {unknown}")
    families = headroom.catalog_families(tasks_root)
    if unknown := sorted(set(tasks) - set(families)):
        raise ManifestError(f"tasks are outside the material catalog: {unknown}")
    return {task: families[task] for task in tasks}


async def prepare_packs(
    *,
    checkpoint: int,
    tasks: Sequence[str],
    arms: Sequence[str],
    output: Path,
    tokenizer_assets: Path,
    tasks_root: Path,
    group_id: str = contract.ORGANIZATION_ID,
    principal_id: str = contract.PRINCIPAL_ID,
) -> dict[str, Any]:
    """Prepare one cell per task and arm at one checkpoint, sealing a receipt.

    The sequence is the checkpoint phase's, owner for owner: the runtime is
    bootstrapped, the org graph schema is ensured, the 233 retained originals
    are requalified through ``qualify_catalog``, the content digest is checked
    against the frozen schedule, the native universe is snapshotted once, and
    then every cell is asked of ``RecallAdapter`` and written exactly as it
    came back. Nothing here repairs, retries or invents a pack.

    The adapter differs from the study's in three declared ways, all recorded
    in each pack: the tasks it admits are the probe's, the task material comes
    out of the transfer tree rather than the frozen policy inputs, and no cell
    carries checkpoint-0 bytes. The third is not a relaxation of the study's
    contract but the absence of its premise. A probe cell has no checkpoint-0
    twin, so a checkpoint-1 raw or summary cell here is a fresh derivation.
    """
    families = _validated(checkpoint=checkpoint, tasks=tasks, arms=arms, tasks_root=tasks_root)
    # absolute() joins against the working directory; it reads no filesystem.
    output = Path(output).absolute()  # noqa: ASYNC240
    material = Path(tasks_root).absolute()  # noqa: ASYNC240
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    reader = Reader(
        principal_id,
        checkpoints.READER_PROJECT,
        checkpoints.READER_MEMORY_SCOPE,
        checkpoints.READER_SCOPE_KEY,
    )
    receipt: dict[str, Any] = {
        "schema": PREPARATION_SCHEMA,
        "status": checkpoints.STATUS_RUNNING,
        "checkpoint": checkpoint,
        "group_id": group_id,
        "principal": principal_id,
        "reader": asdict(reader),
        "reader_scope_evidence": checkpoints.READER_SCOPE_EVIDENCE,
        "output": str(output),
        "tasks_root": str(material),
        "tokenizer_assets": str(tokenizer_assets),
        "tasks": list(tasks),
        "arms": list(arms),
        "families": families,
        "schedule_source_catalog_sha256": None,
        "schedule_content_sha256": None,
        "catalog_sha256": None,
        "catalog_content_sha256": None,
        "lifetime_catalog_matches_schedule": None,
        "native_inventory": None,
        "tokenizer": None,
        "denominator": len(tasks) * len(arms),
        "prepared": 0,
        "cells": [],
        "missing_reasons": {},
        "errors": [],
        "solver_calls": 0,
        "reservation_changes": 0,
        "learning_claim": False,
        "started_at": _now(),
        "finished_at": None,
    }
    try:
        receipt["schedule_source_catalog_sha256"] = checkpoints.schedule_catalog_sha256()
        receipt["schedule_content_sha256"] = checkpoints.schedule_catalog_content_sha256()
        await cycle.bootstrap_runtime()
        try:
            receipt["graph_schema_bootstrapped"] = await checkpoints.ensure_graph_schema(group_id)
            await _prepare(
                receipt,
                checkpoint=checkpoint,
                tasks=tasks,
                arms=arms,
                output=output,
                tokenizer_assets=Path(tokenizer_assets),
                tasks_root=material,
                group_id=group_id,
                principal_id=principal_id,
                reader=reader,
            )
        finally:
            try:
                await cycle.shutdown_runtime()
            except Exception as exc:  # a drain failure must not eat the receipt
                receipt["errors"].append(f"shutdown:{type(exc).__name__}: {exc}")
    except Exception as exc:
        receipt["errors"].append(f"{type(exc).__name__}: {exc}")
        # A gate that named the refusal keeps its status; only an unnamed
        # failure is recorded as a plain error.
        if receipt["status"] in {
            checkpoints.STATUS_RUNNING,
            checkpoints.STATUS_PREPARED,
            checkpoints.STATUS_PARTIAL,
        }:
            receipt["status"] = checkpoints.STATUS_ERROR
        raise
    finally:
        receipt["finished_at"] = _now()
        checkpoints._write_json(output / PREPARATION_NAME, receipt)
    return receipt


async def _prepare(
    receipt: dict[str, Any],
    *,
    checkpoint: int,
    tasks: Sequence[str],
    arms: Sequence[str],
    output: Path,
    tokenizer_assets: Path,
    tasks_root: Path,
    group_id: str,
    principal_id: str,
    reader: Reader,
) -> None:
    """Qualify the catalog, snapshot the native universe, then walk the cells."""
    catalog, _qualification, resolve_authority, verify_owners = await checkpoints.qualify_catalog(
        group_id=group_id, principal_id=principal_id
    )
    receipt["catalog_sha256"] = catalog.catalog_sha256
    receipt["catalog_content_sha256"] = catalog.catalog_content_sha256
    receipt["lifetime_catalog_matches_schedule"] = (
        catalog.catalog_sha256 == receipt["schedule_source_catalog_sha256"]
    )
    if len(catalog.rows) != contract.SOURCE_COUNT:
        raise ManifestError(f"the qualified catalog holds {len(catalog.rows)} originals")
    # The same gate the checkpoint phase runs: a restore remints every
    # observation incarnation, so the lifetime digest moves while the study's
    # content identity does not. Content is what the schedule can agree with.
    if catalog.catalog_content_sha256 != receipt["schedule_content_sha256"]:
        receipt["status"] = checkpoints.STATUS_CATALOG_DISAGREES
        raise ManifestError(
            "the qualified catalog is not the schedule's: the frozen material and the "
            f"database disagree on source content ({catalog.catalog_content_sha256})"
        )

    natives = NativeCheckpoints(
        catalog=catalog,
        reader=reader,
        resolve_authority=resolve_authority,
        verify_owners=verify_owners,
    )
    items, inventory = await natives.produce(checkpoint)
    shape = checkpoints.inventory_shape(items, inventory)
    receipt["native_inventory"] = shape
    checkpoints._write_json(
        output / checkpoints.INVENTORY_NAME,
        {
            "schema": checkpoints.INVENTORY_SCHEMA,
            "checkpoint": checkpoint,
            "items_sha256": shape["sha256"],
            "receipt": inventory,
            "items": items,
        },
    )
    # The checkpoint phase's gate, mirrored: checkpoint 0 is the state before
    # one consolidation cycle, so a derived item in the authorized universe
    # means the database is past it and no arm here would carry cp0 bytes.
    if checkpoint == 0 and not shape["raw_original_only"]:
        receipt["status"] = checkpoints.STATUS_DERIVED_AT_ZERO
        raise ManifestError(
            "checkpoint 0 is the state before one consolidation cycle, so the native "
            "inventory must hold retained raw originals only; this organization already "
            f"holds derived items (snapshot {shape['derived_source_counts']}, "
            f"{len(shape['derived_items'])} authorized non-raw items)"
        )

    counter = checkpoints.build_counter(tokenizer_assets)
    receipt["tokenizer"] = {
        **counter.tokenizer_receipt(),
        "assets": str(tokenizer_assets),
        "verified": True,
    }
    references, validate_summary_library = checkpoints.summary_library()
    adapter = RecallAdapter(
        catalog=catalog,
        reader=reader,
        counter=counter,
        resolve_authority=resolve_authority,
        verify_owners=verify_owners,
        verify_native_inventory=natives.verify,
        validate_summary_library=validate_summary_library,
        allowed_tasks=tuple(tasks),
        task_source=material_task_source(tasks_root),
        carry_checkpoint_zero=False,
    )
    cells: list[dict[str, Any]] = []
    reasons: dict[str, int] = {}
    prepared = 0
    for task in tasks:
        for arm in arms:
            arguments: dict[str, Any] = {}
            if arm == checkpoints.NATIVE_ARM:
                arguments["native_inventory"] = items
            if arm == checkpoints.SUMMARY_ARM:
                arguments["references"] = references
            pack = await adapter.prepare(checkpoint=checkpoint, task=task, arm=arm, **arguments)
            document = checkpoints.coordinate(
                pack,
                checkpoint=checkpoint,
                task=task,
                arm=arm,
                catalog=catalog.catalog_sha256,
                catalog_content=catalog.catalog_content_sha256,
                schedule_catalog=receipt["schedule_source_catalog_sha256"],
            )
            paths = checkpoints._write_pack(output, document)
            memory = document["memory"] if document["status"] == "prepared" else None
            if memory is None:
                reason = str(document.get("reason") or "not_prepared")
                reasons[reason] = reasons.get(reason, 0) + 1
            else:
                prepared += 1
            counts = document.get("counts") or {}
            cells.append(
                {
                    "checkpoint": checkpoint,
                    "task": task,
                    "arm": arm,
                    "status": document["status"],
                    "reason": document.get("reason"),
                    "pack_receipt_sha256": contract.digest(document),
                    "memory_sha256": contract.sha(memory.encode()) if memory is not None else None,
                    "memory_bytes": len(memory.encode()) if memory is not None else None,
                    "memory_tokens": counts.get("memory_tokens"),
                    "fits": counts.get("fits"),
                    **paths,
                }
            )
    receipt["cells"] = cells
    receipt["prepared"] = prepared
    receipt["missing_reasons"] = dict(sorted(reasons.items()))
    receipt["status"] = (
        checkpoints.STATUS_PREPARED
        if prepared == receipt["denominator"]
        else checkpoints.STATUS_PARTIAL
    )


def _memory_bytes(output: Path, cell: dict[str, Any]) -> bytes:
    """Read one prepared cell's memory out of the receipt that was sealed.

    The bytes handed to the solver are the bytes on disk, read back by the path
    the preparation recorded, so the manifest cites what was sealed rather than
    whatever a caller still holds in memory. Reading them is not enough on its
    own: the preparation recorded a digest for this cell and sealed the same
    bytes beside the receipt, so both are compared here. A receipt edited after
    preparation, or a memory file that drifted from the receipt beside it, is
    refused rather than packed under the cell's recorded digest.
    """
    receipt = strict_json((output / cell["receipt_path"]).read_bytes())
    if not isinstance(receipt, dict):
        raise ManifestError(f"pack receipt is not an object: {cell['receipt_path']}")
    coordinates = ("checkpoint", "task", "arm", "status")
    if any(receipt.get(key) != cell[key] for key in coordinates):
        raise ManifestError(f"pack receipt is outside its cell: {cell['receipt_path']}")
    memory = receipt.get("memory")
    if not isinstance(memory, str):
        raise ManifestError(f"prepared pack has no memory text: {cell['receipt_path']}")
    if cell["arm"] == NO_MEMORY_ARM and memory != "":
        raise ManifestError(f"the no-memory arm carries memory: {cell['receipt_path']}")
    encoded = memory.encode()
    if contract.sha(encoded) != cell["memory_sha256"]:
        raise ManifestError(
            "pack receipt memory is not the digest the preparation recorded: "
            f"{cell['receipt_path']}"
        )
    sealed = cell.get("memory_path")
    if sealed and (output / sealed).read_bytes() != encoded:
        raise ManifestError(f"sealed memory bytes disagree with their receipt: {sealed}")
    return encoded


def _emit_manifest(
    *,
    tasks_root: Path,
    template: dict[str, Any],
    task_id: str,
    checkpoint: int,
    prepared: list[tuple[dict[str, Any], bytes]],
    output: Path,
) -> Path:
    """Write one validated single-task manifest carrying every prepared arm.

    Every arm but ``no_memory`` cites the full declared experience set, exactly
    as the 48-cell materialization does, because a memory arm here is handed
    the same kind of pack the study's arms are handed.
    """
    manifest, files = materialize.build_manifest(
        namespace=NAMESPACE,
        template=template,
        tasks_root=tasks_root,
        task_id=task_id,
        checkpoint=checkpoint,
        prepared=prepared,
        experiences=template["experiences"],
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


def _row(task: str, arm: str, family: str, cells: list[dict[str, Any]]) -> dict[str, Any]:
    """One (task, arm) row: what the arm reached and what it cost to ask."""
    passes = sum(cell["passed"] is True for cell in cells)
    failures = sum(cell["passed"] is False for cell in cells)
    unknown = sum(cell["passed"] is None for cell in cells)
    denominator = len(cells)
    costs = [float(cell["usage"]["cost_usd"]) for cell in cells if cell["usage"]["cost_usd"]]
    return {
        "task": task,
        "arm": arm,
        "family_id": family,
        "denominator": denominator,
        "passes": passes,
        "failures": failures,
        # No task outcome at all: a missing pack, or a controller, checker or
        # runner failure. Reported apart from a real failure.
        "unknown": unknown,
        # Cells whose solver stopped on its own without changing the workspace:
        # the "I already did this" failure the native pack produced on
        # September 19, counted rather than inferred from the transcript.
        "premature_completions": sum(cell.get("premature_completion") is True for cell in cells),
        "pass_rate": round(passes / denominator, 6) if denominator else 0.0,
        "cost_usd": round(sum(costs), 6),
        "mean_tool_calls": headroom._mean(
            [
                float(cell["usage"]["tool_calls"])
                for cell in cells
                if cell["usage"]["tool_calls"] is not None
            ]
        ),
        "mean_elapsed_seconds": headroom._mean(
            [cell["elapsed_seconds"] for cell in cells if cell["elapsed_seconds"] is not None]
        ),
        "statuses": {
            status: sum(cell["status"] == status for cell in cells)
            for status in sorted({str(cell["status"]) for cell in cells})
        },
    }


def _lift(rows: list[dict[str, Any]], tasks: Sequence[str]) -> list[dict[str, Any]]:
    """Each memory arm's pass rate minus the no-memory arm's, per task.

    Arithmetic, not a verdict. A lift is only as good as the two rows under it,
    so an arm whose pack was never prepared still shows a difference and the
    row's ``statuses`` is what says the number means nothing.
    """
    lifts = []
    for task in tasks:
        by_arm = {row["arm"]: row for row in rows if row["task"] == task}
        baseline = by_arm.get(NO_MEMORY_ARM)
        if baseline is None:
            continue
        for arm in contract.ARMS:
            row = by_arm.get(arm)
            if arm == NO_MEMORY_ARM or row is None:
                continue
            lifts.append(
                {
                    "task": task,
                    "arm": arm,
                    "pass_rate": row["pass_rate"],
                    "no_memory_pass_rate": baseline["pass_rate"],
                    "lift": round(row["pass_rate"] - baseline["pass_rate"], 6),
                }
            )
    return lifts


def run_cells(
    *,
    preparation: dict[str, Any],
    template: dict[str, Any],
    tasks_root: Path,
    output: Path,
    repetitions: int,
    workers: int,
    api_key_env: str | None = "OPENROUTER_API_KEY",
    headroom_binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run every prepared cell ``repetitions`` times and report the pass rates.

    One manifest per task carries every arm that has a pack, so all four arms
    of a task run against the same authored material. A cell whose pack is
    missing is recorded once per repetition and never dispatched: there is
    nothing to hand the solver, and an empty pack would quietly become a second
    no-memory arm.
    """
    if repetitions < 1:
        raise ManifestError("at least one repetition is required")
    if workers < 1:
        raise ManifestError("at least one worker is required")
    materialize.validate_template(template)
    headroom._check_credentials(template, api_key_env)
    output = Path(output).absolute()
    checkpoint = preparation["checkpoint"]
    tasks = list(preparation["tasks"])
    arms = list(preparation["arms"])
    families = dict(preparation["families"])
    by_cell = {(cell["task"], cell["arm"]): cell for cell in preparation["cells"]}
    manifests: dict[str, Path] = {}
    refused: dict[str, str] = {}
    for task in tasks:
        if not any(by_cell[(task, arm)]["status"] == "prepared" for arm in arms):
            continue
        try:
            prepared = [
                (
                    {"arm": arm, "family": families[task]},
                    _memory_bytes(output, by_cell[(task, arm)]),
                )
                for arm in arms
                if by_cell[(task, arm)]["status"] == "prepared"
            ]
            manifests[task] = _emit_manifest(
                tasks_root=Path(tasks_root),
                template=template,
                task_id=task,
                checkpoint=checkpoint,
                prepared=prepared,
                output=output,
            )
        except Exception as exc:  # an unbuildable task is a bad cell, not an abort
            refused[task] = f"{type(exc).__name__}: {exc}"
    runnable = [
        (task, arm)
        for task in tasks
        for arm in arms
        if by_cell[(task, arm)]["status"] == "prepared" and task in manifests
    ]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(
                headroom.run_cell,
                task,
                repetition,
                arm=arm,
                manifest_path=manifests[task],
                output=output,
                relative=f"cells/{task}/{arm}/{repetition}",
                seed=headroom.repetition_seed(int(template["seed"]), repetition),
            )
            for task, arm in runnable
            for repetition in range(repetitions)
        ]
        cells = [future.result() for future in futures]
    for task in tasks:
        for arm in arms:
            cell = by_cell[(task, arm)]
            if cell["status"] == "prepared" and task in manifests:
                continue
            status = "missing_pack" if cell["status"] != "prepared" else "runner_error"
            reason = refused.get(task) if cell["status"] == "prepared" else cell["reason"]
            cells.extend(
                {
                    "task": task,
                    "arm": arm,
                    "repetition": repetition,
                    "attempt_id": None,
                    "status": status,
                    "success": False,
                    "passed": None,
                    "premature_completion": None,
                    "usage": dict.fromkeys(USAGE_FIELDS),
                    "elapsed_seconds": None,
                    "receipt": None,
                    "reason": reason or "not_prepared",
                    "completed_at": _now(),
                }
                for repetition in range(repetitions)
            )
    rows = [
        _row(
            task,
            arm,
            families[task],
            [cell for cell in cells if (cell["task"], cell["arm"]) == (task, arm)],
        )
        for task in tasks
        for arm in arms
    ]
    totals = {
        key: sum(row[key] for row in rows)
        for key in ("denominator", "passes", "failures", "unknown")
    }
    report = {
        "schema": SCHEMA,
        "root": str(output),
        "checkpoint": checkpoint,
        "tasks_root": str(Path(tasks_root).absolute()),
        "template_sha256": digest(canonical_bytes(template)),
        "controller_model": template["controller_model"],
        "repetitions": repetitions,
        "workers": workers,
        "tasks": tasks,
        "arms": arms,
        "rows": rows,
        "lift": _lift(rows, tasks),
        "totals": {
            **totals,
            "pass_rate": (
                round(totals["passes"] / totals["denominator"], 6) if totals["denominator"] else 0.0
            ),
        },
        "total_cost_usd": round(sum(row["cost_usd"] for row in rows), 6),
        "prepared_packs": sum(cell["status"] == "prepared" for cell in preparation["cells"]),
        "executed_cells": len(runnable),
        "refused_manifests": refused,
        # The screen report the tasks were bound to, so a reader can check the
        # band each probed task carried and how many outcomes it came from.
        "headroom": headroom_binding,
        # A pass rate is a pass rate. Neither the memory a pack carried nor the
        # difference between two arms is a learning claim; the frozen schedule
        # owns that, and this module is not one of its cells.
        "memory_established": False,
        "learning_benefit_established": False,
        "cells": cells,
        "generated_at": _now(),
    }
    runner._write_json(output / REPORT_NAME, report)
    return report


def table(report: dict[str, Any]) -> str:
    """Render the per-arm rates and the per-task lift as one fixed-width table."""
    header = f"{'task':<28}{'arm':<16}{'pass':>5}{'fail':>5}{'unk':>5}{'rate':>7}{'cost':>9}"
    lines = [header, "-" * len(header)]
    for row in report["rows"]:
        lines.append(
            f"{row['task']:<28}{row['arm']:<16}{row['passes']:>5}{row['failures']:>5}"
            f"{row['unknown']:>5}{row['pass_rate']:>7.2f}{row['cost_usd']:>9.4f}"
        )
    totals = report["totals"]
    lines.append("-" * len(header))
    lines.append(
        f"{'totals':<28}{'':<16}{totals['passes']:>5}{totals['failures']:>5}"
        f"{totals['unknown']:>5}{totals['pass_rate']:>7.2f}{report['total_cost_usd']:>9.4f}"
    )
    for task in report["tasks"]:
        for row in report["lift"]:
            if row["task"] != task:
                continue
            lines.append(
                f"lift {row['task']:<28}{row['arm']:<16}"
                f"{row['pass_rate']:>7.2f} - {row['no_memory_pass_rate']:>5.2f}"
                f" = {row['lift']:>+7.2f}"
            )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="screen48-probe", description=__doc__)
    parser.add_argument("--tasks-root", type=Path, default=MATERIAL_ROOT)
    parser.add_argument("--task-ids", nargs="+", required=True)
    parser.add_argument("--headroom", type=Path, required=True)
    parser.add_argument("--arms", nargs="+", default=list(contract.ARMS))
    parser.add_argument("--checkpoint", type=int, default=DEFAULT_CHECKPOINT)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--tokenizer-assets", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--api-key-env", default="OPENROUTER_API_KEY")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    template = strict_json(args.template.read_bytes())
    materialize.validate_template(template)
    # The credential is confirmed before the database is touched, let alone the
    # provider: only the variable's name is read here, never its value.
    headroom._check_credentials(template, args.api_key_env)
    # Every argument that can be adjudicated on its own is adjudicated before
    # the output directory exists. A typo'd task ID or an absent tokenizer path
    # would otherwise leave a directory behind, and the retry would die on
    # "probe output already exists", which names the wrong problem.
    _validated(
        checkpoint=args.checkpoint,
        tasks=list(args.task_ids),
        arms=list(args.arms),
        tasks_root=args.tasks_root,
    )
    headroom_bytes = Path(args.headroom).read_bytes()
    headroom_binding = {
        "path": str(Path(args.headroom).absolute()),
        "sha256": digest(headroom_bytes),
        "bands": bound_bands(strict_json(headroom_bytes), list(args.task_ids)),
    }
    output = Path(args.output).absolute()
    if output.exists() or output.is_symlink():
        raise ManifestError("probe output already exists")
    tokenizer_assets = Path(args.tokenizer_assets)
    if not tokenizer_assets.is_dir():
        raise ManifestError(f"tokenizer assets are not a directory: {tokenizer_assets}")
    output.mkdir(mode=0o700, parents=True)
    preparation = asyncio.run(
        prepare_packs(
            checkpoint=args.checkpoint,
            tasks=list(args.task_ids),
            arms=list(args.arms),
            output=output,
            tokenizer_assets=tokenizer_assets,
            tasks_root=args.tasks_root,
        )
    )
    report = run_cells(
        preparation=preparation,
        template=template,
        tasks_root=args.tasks_root,
        output=output,
        repetitions=args.repetitions,
        workers=args.workers,
        api_key_env=args.api_key_env,
        headroom_binding=headroom_binding,
    )
    sys.stdout.write(table(report))
    sys.stdout.write(f"\n{REPORT_NAME}: {report['root']}/{REPORT_NAME}\n")
    sys.stdout.write(f"{PREPARATION_NAME}: {report['root']}/{PREPARATION_NAME}\n")
    return EXIT_OK if preparation["prepared"] else EXIT_NO_PACKS


if __name__ == "__main__":
    raise SystemExit(main())
