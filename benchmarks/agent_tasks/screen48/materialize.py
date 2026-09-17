"""Turn prepared packs and frozen task material into one-task screen48 manifests.

The frozen 48-cell schedule owns the cells. This module owns nothing but the
translation: it copies the exact prepared bytes into at most twelve one-task
manifest directories and records, per cell, whether that cell became executable.
A cell without a prepared pack stays in the schedule as unprepared; no empty or
invented pack ever enters a manifest.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks.agent_tasks import coding_controller, json_oracle, runner
from benchmarks.agent_tasks.manifest import (
    SCHEMA_VERSION,
    Artifact,
    Experience,
    Manifest,
    ManifestError,
    canonical_bytes,
    digest,
    identity,
    load_manifest,
    read_artifact,
    relative_path,
    runtime_identity,
    strict_json,
    validate_partitions,
)
from benchmarks.agent_tasks.screen48.contract import schedule_content_sha256
from benchmarks.agent_tasks.transfer_report import IDENTITY_FIELDS

if TYPE_CHECKING:
    from collections.abc import Iterator

SCHEMA = "sibyl-screen48-materialization-v1"
ORACLE_SCHEMA = "sibyl-json-cli-oracle-v1"
CELL_COORDINATES = ("attempt_id", "checkpoint", "task", "arm", "family", "category", "repetition")
PACK_COORDINATES = ("checkpoint", "task", "arm")
PACK_STATUSES = ("prepared", "missing_pack")
NO_MEMORY_ARM = "no_memory"
CHECKER_RUNTIME_PATH = "checker/runtime.py"
CHECKER_EVALUATOR_PATH = "checker/evaluator.py"
WORKSPACE_MODE = 420
ARTIFACT_MODE = 0o444
TEMPLATE_KEYS = frozenset(
    {
        "artifact_root",
        "runtime_sha256",
        "dependency_lock",
        "seed",
        "controller",
        "controller_api_key_env",
        "controller_model",
        "controller_tools",
        "controller_budget",
        "controller_timeout_seconds",
        "checker_timeout_seconds",
        "checker",
        "experiences",
    }
)
CHECKER_KEYS = frozenset({"image", "docker", "docker_host", "argv", "memory_mb", "timeout_seconds"})


class Retainer:
    """Collect the exact bytes of one manifest directory, refusing path conflicts."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def retain(self, path: str, content: bytes) -> str:
        relative_path(path)
        if self.files.setdefault(path, content) != content:
            raise ManifestError(f"conflicting materialized artifact path: {path}")
        return digest(content)

    def artifact(self, path: str, content: bytes) -> dict[str, str]:
        return {"path": path, "sha256": self.retain(path, content)}


def _regular_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ManifestError(f"expected a regular file: {path}")
    return path.read_bytes()


def _validate_template(template: dict[str, Any]) -> None:
    if set(template) != TEMPLATE_KEYS:
        missing = sorted(TEMPLATE_KEYS - set(template))
        extra = sorted(set(template) - TEMPLATE_KEYS)
        raise ManifestError(f"template keys differ (missing {missing}, unexpected {extra})")
    if set(template["checker"]) != CHECKER_KEYS:
        raise ManifestError("checker template keys differ")
    if template["runtime_sha256"] != identity(runtime_identity()):
        raise ManifestError("this interpreter is not the runtime the template was frozen against")


def _validate_experiences(schedule: dict[str, Any], template: dict[str, Any]) -> None:
    """Bind every declared experience to one original source row of the schedule."""
    originals = {row["id"]: row for row in schedule["original_sources"]}
    if len(originals) != len(schedule["original_sources"]):
        raise ManifestError("the schedule repeats an original source ID")
    experiences = [Experience.model_validate(row) for row in template["experiences"]]
    if len(experiences) != len(originals) or {row.id for row in experiences} != set(originals):
        raise ManifestError("manifest experiences must be exactly the declared original sources")
    for row in experiences:
        source = originals[row.id]
        declared = (row.family_id, row.split, row.revision, row.artifact.sha256)
        expected = (
            source["training_family"],
            "learning",
            str(source["observation"]["revision"]),
            source["source_sha256"],
        )
        if declared != expected:
            raise ManifestError(f"experience binding differs from its original source: {row.id}")


def _read_pack(
    packs_root: Path,
    schedule: dict[str, Any],
    cell: dict[str, Any],
    *,
    content_sha256: str | None = None,
) -> dict[str, Any]:
    """Read one preparation receipt, returning an unprepared verdict when it is absent.

    The receipt is bound to the schedule by content, not by database lifetime.
    Restoring the study database from its cold copy remints every observation
    incarnation, so a receipt prepared after a restore carries a different
    ``catalog_sha256`` than the one the schedule was frozen with while naming
    the identical 233 captures. What must agree is ``catalog_content_sha256``.
    Lifetime agreement is still required, but between receipts rather than
    against the schedule: see ``_bind_lifetime``.
    """
    base = packs_root / "packs" / f"cp{cell['checkpoint']}" / cell["task"]
    receipt_path = base / f"{cell['arm']}.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return {"status": "missing_pack", "reason": "no_pack_receipt", "memory": None}
    receipt = strict_json(_regular_file(receipt_path))
    if not isinstance(receipt, dict):
        raise ManifestError(f"pack receipt is not an object: {receipt_path}")
    if any(receipt.get(key) != cell[key] for key in PACK_COORDINATES):
        raise ManifestError(f"pack receipt is outside its scheduled cell: {receipt_path}")
    if receipt.get("status") not in PACK_STATUSES:
        raise ManifestError(f"unknown pack preparation status: {receipt_path}")
    expected = schedule_content_sha256(schedule) if content_sha256 is None else content_sha256
    if receipt.get("catalog_content_sha256") != expected:
        raise ManifestError(f"pack receipt used another source catalog: {receipt_path}")
    if not isinstance(receipt.get("catalog_sha256"), str) or not receipt["catalog_sha256"]:
        raise ManifestError(f"pack receipt names no database lifetime: {receipt_path}")
    reason = receipt.get("reason")
    if receipt["status"] == "missing_pack":
        if receipt.get("memory") is not None:
            raise ManifestError(f"unprepared pack carries memory: {receipt_path}")
        return {"status": "missing_pack", "reason": reason or "not_prepared", "receipt": receipt}
    memory = receipt.get("memory")
    if not isinstance(memory, str):
        raise ManifestError(f"prepared pack has no memory text: {receipt_path}")
    if cell["arm"] == NO_MEMORY_ARM and memory != "":
        raise ManifestError(f"the no-memory arm carries memory: {receipt_path}")
    content = _regular_file(base / f"{cell['arm']}.txt")
    if content != memory.encode():
        raise ManifestError(f"prepared pack bytes differ from the receipt: {receipt_path}")
    return {"status": "prepared", "reason": reason, "receipt": receipt, "memory": content}


def _bind_lifetime(schedule: dict[str, Any], packs: dict[str, dict[str, Any]]) -> str | None:
    """Refuse a materialization whose receipts span more than one database lifetime.

    One checkpoint is prepared in one phase against one restored database, and
    checkpoint 1 continues the very cycle checkpoint 0 began: its two reusing
    arms are handed the checkpoint-0 packs. A restore between those phases
    remints every observation incarnation, so the lifetime digests part company
    even though the study content did not. That is a broken cycle, not a
    reusable pair of checkpoints, and it is refused here rather than executed.

    Returns the single lifetime digest every receipt agreed on, or ``None`` when
    no receipt was present at all.
    """
    lifetimes: dict[int, set[str]] = {}
    for cell in schedule["cells"]:
        receipt = packs[cell["attempt_id"]].get("receipt")
        if isinstance(receipt, dict):
            lifetimes.setdefault(cell["checkpoint"], set()).add(receipt["catalog_sha256"])
    for checkpoint, digests in sorted(lifetimes.items()):
        if len(digests) > 1:
            raise ManifestError(
                f"checkpoint {checkpoint} pack receipts span {len(digests)} database "
                "lifetimes; one checkpoint is prepared against one restored database"
            )
    across = {next(iter(digests)) for digests in lifetimes.values()}
    if len(across) > 1:
        raise ManifestError(
            f"pack receipts span {len(across)} database lifetimes across the checkpoints; "
            "a restore between checkpoint 0 and checkpoint 1 breaks the cycle they share"
        )
    return next(iter(across), None)


def _task_value(
    retainer: Retainer, tasks_root: Path, template: dict[str, Any], task_id: str, family: str
) -> dict[str, Any]:
    """Copy one frozen task, binding its private oracle beside the public material."""
    source = tasks_root / "tasks" / task_id
    workspace_root = source / "workspace"
    workspace = []
    for path in sorted(workspace_root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        destination = path.relative_to(workspace_root).as_posix()
        artifact = retainer.artifact(
            f"tasks/{task_id}/workspace/{destination}", _regular_file(path)
        )
        workspace.append({"artifact": artifact, "destination": destination, "mode": WORKSPACE_MODE})
    if not workspace:
        raise ManifestError(f"task workspace is empty: {task_id}")
    checker = template["checker"]
    return {
        "id": task_id,
        "family_id": family,
        "split": "development",
        "prompt": retainer.artifact(
            f"tasks/{task_id}/prompt.md", _regular_file(source / "prompt.md")
        ),
        "workspace": workspace,
        "checker": {
            "schema_version": ORACLE_SCHEMA,
            "oracle": retainer.artifact(
                f"tasks/{task_id}/oracle.json", _regular_file(source / "oracle.json")
            ),
            "runtime": retainer.artifact(
                CHECKER_RUNTIME_PATH, _regular_file(Path(coding_controller.__file__))
            ),
            "evaluator": retainer.artifact(
                CHECKER_EVALUATOR_PATH, _regular_file(Path(json_oracle.__file__))
            ),
            "argv": list(checker["argv"]),
            "image": checker["image"],
            "docker": checker["docker"],
            "docker_host": checker["docker_host"],
            "memory_mb": int(checker["memory_mb"]),
            "timeout_seconds": float(checker["timeout_seconds"]),
        },
    }


def _grouped_cells(schedule: dict[str, Any]) -> Iterator[tuple[tuple[int, str], list[dict]]]:
    """Walk (checkpoint, task) groups in the order the frozen schedule lists them."""
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for cell in schedule["cells"]:
        groups.setdefault((cell["checkpoint"], cell["task"]), []).append(cell)
    yield from groups.items()


def _build_manifest(
    *,
    schedule: dict[str, Any],
    template: dict[str, Any],
    tasks_root: Path,
    task_id: str,
    checkpoint: int,
    prepared: list[tuple[dict[str, Any], bytes]],
) -> tuple[Manifest, dict[str, bytes]]:
    retainer = Retainer()
    artifact_root = Path(template["artifact_root"]).resolve(strict=True)
    for row in [
        template["dependency_lock"],
        template["controller"]["script"],
        *(row["artifact"] for row in template["experiences"]),
    ]:
        retainer.retain(row["path"], read_artifact(artifact_root, Artifact.model_validate(row)))
    families = {cell["family"] for cell, _ in prepared}
    if len(families) != 1:
        raise ManifestError(f"scheduled cells disagree on the task family: {task_id}")
    task = _task_value(retainer, tasks_root, template, task_id, families.pop())
    learning_ids = [row["id"] for row in template["experiences"]]
    arms = []
    for cell, memory in prepared:
        arm_id = cell["arm"]
        arms.append(
            {
                "id": arm_id,
                "memory_pack": retainer.artifact(f"packs/{arm_id}.txt", memory),
                "learning_source_ids": [] if arm_id == NO_MEMORY_ARM else learning_ids,
            }
        )
    namespace = schedule["experiment_namespace"]
    manifest = Manifest.model_validate(
        {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": f"screen48-{namespace}-c{checkpoint}-{task_id}",
            "purpose": "trusted_development",
            "runtime_sha256": template["runtime_sha256"],
            "dependency_lock": template["dependency_lock"],
            "seed": int(template["seed"]),
            "controller": template["controller"],
            "controller_api_key_env": template["controller_api_key_env"],
            "controller_model": template["controller_model"],
            "controller_tools": list(template["controller_tools"]),
            "controller_budget": template["controller_budget"],
            "controller_timeout_seconds": float(template["controller_timeout_seconds"]),
            "checker_timeout_seconds": float(template["checker_timeout_seconds"]),
            "experiences": template["experiences"],
            "tasks": [task],
            "arms": arms,
        }
    )
    validate_partitions(manifest)
    return manifest, retainer.files


def _expected_identity(manifest: Manifest, arm_id: str, inputs: dict[str, bytes]) -> dict[str, Any]:
    task = manifest.tasks[0]
    arm = next(arm for arm in manifest.arms if arm.id == arm_id)
    prototype = runner._receipt(manifest, task, arm, inputs)
    return {field: prototype[field] for field in IDENTITY_FIELDS}


def _emit(output: Path, relative: str, content: bytes) -> None:
    path = output / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    runner._put(path, content, ARTIFACT_MODE)


def materialize(
    *,
    schedule: dict[str, Any],
    packs_root: Path,
    tasks_root: Path,
    template: dict[str, Any],
    output: Path,
) -> dict[str, Any]:
    """Emit at most twelve validated one-task manifests and the 48-row cell ledger.

    ``packs_root`` holds ``packs/cp{checkpoint}/{task}/{arm}.json`` preparation
    receipts beside the exact ``{arm}.txt`` memory bytes; ``tasks_root`` holds
    ``tasks/{task}/`` with ``prompt.md``, ``oracle.json`` and ``workspace/``.
    The template supplies every binding the schedule does not: runtime identity,
    dependency lock, controller, budgets, checker runtime and the full original
    experience set. Every emitted manifest is re-read through ``load_manifest``
    before this function returns, so a directory that exists is executable.
    """
    _validate_template(template)
    _validate_experiences(schedule, template)
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ManifestError("materialization output already exists")
    content_sha256 = schedule_content_sha256(schedule)
    packs = {
        cell["attempt_id"]: _read_pack(packs_root, schedule, cell, content_sha256=content_sha256)
        for cell in schedule["cells"]
    }
    pack_catalog_sha256 = _bind_lifetime(schedule, packs)
    manifests: list[dict[str, Any]] = []
    entries: dict[str, dict[str, Any]] = {}
    pending: list[tuple[str, bytes]] = []
    for (checkpoint, task_id), cells in _grouped_cells(schedule):
        prepared = [
            (cell, packs[cell["attempt_id"]]["memory"])
            for cell in cells
            if packs[cell["attempt_id"]]["status"] == "prepared"
        ]
        if not prepared:
            continue
        manifest, files = _build_manifest(
            schedule=schedule,
            template=template,
            tasks_root=tasks_root,
            task_id=task_id,
            checkpoint=checkpoint,
            prepared=prepared,
        )
        directory = f"cp{checkpoint}/{task_id}"
        body = canonical_bytes(manifest.model_dump(mode="json")) + b"\n"
        pending.extend((f"{directory}/{path}", content) for path, content in files.items())
        pending.append((f"{directory}/manifest.json", body))
        manifests.append(
            {
                "path": f"{directory}/manifest.json",
                "sha256": digest(body),
                "checkpoint": checkpoint,
                "task": task_id,
                "arms": [arm.id for arm in manifest.arms],
                "experiment_id": manifest.experiment_id,
            }
        )
        for cell, memory in prepared:
            entries[cell["attempt_id"]] = {
                "prepared": True,
                "pack_status": "prepared",
                "reason": packs[cell["attempt_id"]]["reason"],
                "pack_sha256": digest(memory),
                "manifest": f"{directory}/manifest.json",
                "manifest_sha256": digest(body),
                "expected_identity": _expected_identity(manifest, cell["arm"], files),
            }
    cells = [
        {
            **{key: cell[key] for key in CELL_COORDINATES},
            "ordinal": cell["ordinal"],
            **entries.get(
                cell["attempt_id"],
                {
                    "prepared": False,
                    "pack_status": packs[cell["attempt_id"]]["status"],
                    "reason": packs[cell["attempt_id"]]["reason"],
                    "pack_sha256": None,
                    "manifest": None,
                    "manifest_sha256": None,
                    "expected_identity": None,
                },
            ),
        }
        for cell in schedule["cells"]
    ]
    report = {
        "schema": SCHEMA,
        "schedule_sha256": identity(schedule),
        "source_commit": schedule.get("source_commit"),
        # The content digest every receipt agreed with, the database lifetime
        # they were all prepared against, and the lifetime the schedule was
        # frozen with. The last two differ after a restore, and the flag says so.
        "catalog_content_sha256": content_sha256,
        "pack_catalog_sha256": pack_catalog_sha256,
        "schedule_source_catalog_sha256": schedule["source_catalog_sha256"],
        "lifetime_catalog_matches_schedule": (
            pack_catalog_sha256 == schedule["source_catalog_sha256"]
        ),
        "root": str(output),
        "denominator": len(cells),
        "prepared_cells": sum(cell["prepared"] for cell in cells),
        "manifest_count": len(manifests),
        "manifests": manifests,
        "cells": cells,
        "learning_benefit_established": False,
    }
    output.mkdir(mode=0o700, parents=True)
    for relative, content in pending:
        _emit(output, relative, content)
    _verify(output, manifests, cells)
    runner._write_json(output / "materialization.json", report)
    return report


def _verify(output: Path, manifests: list[dict[str, Any]], cells: list[dict[str, Any]]) -> None:
    """Re-read every emitted manifest through the owner that will execute it."""
    identities = {
        cell["attempt_id"]: cell for cell in cells if cell["expected_identity"] is not None
    }
    for entry in manifests:
        loaded, inputs = load_manifest(output / entry["path"])
        if loaded.experiment_id != entry["experiment_id"]:
            raise ManifestError(f"emitted manifest differs: {entry['path']}")
        for cell in identities.values():
            if cell["manifest"] != entry["path"]:
                continue
            derived = _expected_identity(loaded, cell["arm"], inputs)
            if identity(derived) != identity(cell["expected_identity"]):
                raise ManifestError(f"emitted runner identity differs: {cell['attempt_id']}")
