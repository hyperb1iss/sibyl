"""Floor probe: task material, manifests, missing packs, rows, lift and CLI.

No provider and no database. The packs these tests run over are written to disk
in the layout ``prepare_packs`` seals, and ``runner.run_task`` is a fake, so
every assertion is about this module's own arithmetic and file handling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks.agent_tasks import runner
from benchmarks.agent_tasks.manifest import (
    ManifestError,
    digest,
    identity,
    load_manifest,
    runtime_identity,
)
from benchmarks.agent_tasks.screen48 import contract, headroom, probe
from benchmarks.agent_tasks.screen48.devbox import run_phase

TASKS = ("alpha-task", "beta-task")
ARMS = contract.ARMS
NESTED = "pkg/resolver.py"
REPETITIONS = 2
PER_ROW_CELLS = 2
ARMS_PER_TASK = 4
WORKSPACE_FILES = 3
NATIVE_LIFT = 1.0
SUMMARY_LIFT = 0.5
MEMORY_TEXT = 'Historical complete controller views.\n<source id="one"/>\n'
CELL_COST_USD = 0.02
ORACLE = json.dumps(
    {
        "schema_version": "sibyl-json-cli-cases-v1",
        "cases": [{"id": "one", "input": {"a": 1}, "expected": {"b": 2}}],
    },
    sort_keys=True,
)


# ---------------------------------------------------------------------------
# Material, templates and prepared packs
# ---------------------------------------------------------------------------


def write_material(root: Path, *, tasks: tuple[str, ...] = TASKS) -> Path:
    """A catalog and one task tree per task, in the frozen material's shape."""
    for task in tasks:
        directory = root / "tasks" / task
        (directory / "workspace" / "pkg").mkdir(parents=True, exist_ok=True)
        (directory / "prompt.md").write_text(f"# {task}\n\nRepair the resolver.\n")
        (directory / "oracle.json").write_text(ORACLE)
        (directory / "workspace" / "app.py").write_text(f"# {task} entry point\n")
        (directory / "workspace" / "application.py").write_text(f"# {task} application\n")
        (directory / "workspace" / NESTED).write_text(f"# {task} resolver\n")
    catalog = {
        "schema_version": "sibyl-transfer-task-material-v1",
        "tasks": [
            {
                "id": task,
                "family_id": f"transfer-{task}",
                "learning_family": "decoded-root-routing",
                "split": "development",
            }
            for task in tasks
        ],
    }
    (root / headroom.CATALOG_NAME).write_text(json.dumps(catalog, indent=2, sort_keys=True))
    return root


def write_template(root: Path, *, api_key_env: str | None = None) -> dict[str, Any]:
    """The template ``materialize`` consumes, with two declared learning sources."""
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "uv.lock"
    lock.write_bytes(b"# frozen dependency lock\n")
    controller = root / "controller.py"
    controller.write_bytes(b"import sys\n\nsys.stdout.write('{}')\n")
    experiences = []
    for index in range(2):
        path = root / f"experience-{index}.json"
        path.write_bytes(f'{{"episode": {index}}}\n'.encode())
        experiences.append(
            {
                "id": f"source-{index}",
                "family_id": f"learning-family-{index}",
                "split": "learning",
                "revision": "1",
                "artifact": {
                    "path": f"experience-{index}.json",
                    "sha256": digest(path.read_bytes()),
                },
            }
        )
    return {
        "artifact_root": str(root),
        "runtime_sha256": identity(runtime_identity()),
        "dependency_lock": {"path": "uv.lock", "sha256": digest(lock.read_bytes())},
        "seed": 0,
        "controller": {
            "script": {"path": "controller.py", "sha256": digest(controller.read_bytes())},
            "args": [],
        },
        "controller_api_key_env": api_key_env,
        "controller_model": "qwen/qwen3-coder-next",
        "controller_tools": ["shell"],
        "controller_budget": {
            "input_tokens": 2000000,
            "output_tokens": 8000,
            "tool_calls": 20,
            "cost_usd": 2.0,
        },
        "controller_timeout_seconds": 600.0,
        "checker_timeout_seconds": 120.0,
        "checker": {
            "image": "sha256:" + "0" * 64,
            "docker": "/usr/bin/docker",
            "docker_host": "unix:///var/run/docker.sock",
            "argv": ["python", "-B", "app.py"],
            "memory_mb": 256,
            "timeout_seconds": 10.0,
        },
        "experiences": experiences,
    }


def write_preparation(
    output: Path,
    *,
    tasks: tuple[str, ...] = TASKS,
    arms: tuple[str, ...] = ARMS,
    missing: frozenset[tuple[str, str]] = frozenset(),
    checkpoint: int = 1,
) -> dict[str, Any]:
    """Seal pack receipts on disk and return the preparation record over them.

    The layout is exactly what ``prepare_packs`` writes, so ``run_cells`` reads
    the same files here that it would read after a real preparation.
    """
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    cells = []
    for task in tasks:
        for arm in arms:
            prepared = (task, arm) not in missing
            memory = "" if arm == probe.NO_MEMORY_ARM else f"{MEMORY_TEXT}{task}\n"
            document = {
                "schema": "sibyl-unarmed-whole-item-pack-v3",
                "checkpoint": checkpoint,
                "task": task,
                "arm": arm,
                "status": "prepared" if prepared else "missing_pack",
                "reason": None if prepared else "raw_required_lane_incomplete",
                "memory": memory if prepared else None,
                "counts": {"memory_tokens": len(memory) // 4, "fits": True} if prepared else None,
            }
            paths = _write_pack(output, document)
            cells.append(
                {
                    "checkpoint": checkpoint,
                    "task": task,
                    "arm": arm,
                    "status": document["status"],
                    "reason": document["reason"],
                    "memory_sha256": contract.sha(memory.encode()) if prepared else None,
                    "memory_bytes": len(memory.encode()) if prepared else None,
                    "memory_tokens": (document["counts"] or {}).get("memory_tokens"),
                    **paths,
                }
            )
    return {
        "schema": probe.PREPARATION_SCHEMA,
        "checkpoint": checkpoint,
        "tasks": list(tasks),
        "arms": list(arms),
        "families": {task: f"transfer-{task}" for task in tasks},
        "denominator": len(tasks) * len(arms),
        "prepared": sum(cell["status"] == "prepared" for cell in cells),
        "cells": cells,
    }


def _write_pack(output: Path, document: dict[str, Any]) -> dict[str, str | None]:
    directory = output / "packs" / f"cp{document['checkpoint']}" / document["task"]
    directory.mkdir(parents=True, exist_ok=True)
    receipt = directory / f"{document['arm']}.json"
    receipt.write_text(json.dumps(document, indent=2, sort_keys=True))
    memory_path: Path | None = None
    if document["status"] == "prepared":
        memory_path = directory / f"{document['arm']}.txt"
        memory_path.write_bytes(document["memory"].encode())
    return {
        "receipt_path": str(receipt.relative_to(output)),
        "memory_path": str(memory_path.relative_to(output)) if memory_path else None,
    }


class FakeRunner:
    """Stand in for ``runner.run_task``: no docker, no provider, recorded calls."""

    def __init__(self, outcomes: dict[tuple[str, str], list[bool | None]]):
        self.outcomes = {key: list(values) for key, values in outcomes.items()}
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        manifest_path: Path,
        *,
        task_id: str,
        arm_id: str,
        output: Path,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {"manifest": manifest_path, "task": task_id, "arm": arm_id, "attempt_id": attempt_id}
        )
        output.mkdir(parents=True)
        passed = self.outcomes[(task_id, arm_id)].pop(0)
        result: dict[str, Any] = {
            "attempt_id": attempt_id,
            "status": "passed" if passed else "task_failed",
            "success": bool(passed),
            "outcome": {"passed": passed, "status": "passed" if passed else "task_failed"},
            "controller": {"elapsed_seconds": 12.5},
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "tool_calls": 4.0,
                "cost_usd": CELL_COST_USD,
                "complete": True,
            },
            "receipt_sha256": digest(str(attempt_id).encode()),
        }
        (output / "receipt.json").write_text(json.dumps(result, sort_keys=True))
        return result


def all_pass(tasks: tuple[str, ...] = TASKS, *, repetitions: int = REPETITIONS) -> dict:
    return {(task, arm): [True] * repetitions for task in tasks for arm in ARMS}


# ---------------------------------------------------------------------------
# The task source
# ---------------------------------------------------------------------------


def test_the_material_task_source_reads_the_authored_prompt_and_workspace(tmp_path: Path) -> None:
    root = write_material(tmp_path / "material")
    source = probe.material_task_source(root)

    prompt, workspace = source(TASKS[0], Path("/not/a/policy/root"))

    assert prompt == f"# {TASKS[0]}\n\nRepair the resolver.\n"
    assert list(workspace) == sorted(["app.py", "application.py", NESTED])
    assert len(workspace) == WORKSPACE_FILES
    assert workspace[NESTED] == f"# {TASKS[0]} resolver\n".encode()
    assert all(isinstance(value, bytes) for value in workspace.values())


def test_the_material_task_source_refuses_an_absent_prompt_or_empty_workspace(
    tmp_path: Path,
) -> None:
    root = write_material(tmp_path / "material")
    source = probe.material_task_source(root)

    with pytest.raises(ManifestError, match="prompt is absent"):
        source("not-a-task", contract.POLICY_ROOT)

    for path in sorted((root / "tasks" / TASKS[1] / "workspace").rglob("*.py")):
        path.unlink()
    with pytest.raises(ManifestError, match="workspace is empty"):
        source(TASKS[1], contract.POLICY_ROOT)


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------


def test_a_memory_arm_cites_every_declared_experience_and_no_memory_cites_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "run_task", FakeRunner(all_pass()))
    output = tmp_path / "out"
    preparation = write_preparation(output)
    template = write_template(tmp_path / "artifacts")

    report = probe.run_cells(
        preparation=preparation,
        template=template,
        tasks_root=write_material(tmp_path / "material"),
        output=output,
        repetitions=REPETITIONS,
        workers=2,
    )

    manifest, inputs = load_manifest(output / "manifests" / TASKS[0] / "manifest.json")
    declared = [row["id"] for row in template["experiences"]]
    arms = {arm.id: arm for arm in manifest.arms}
    assert sorted(arms) == sorted(ARMS)
    assert len(arms) == ARMS_PER_TASK
    for arm_id, arm in arms.items():
        expected = [] if arm_id == probe.NO_MEMORY_ARM else declared
        assert arm.learning_source_ids == expected, arm_id
    assert inputs[arms[probe.NO_MEMORY_ARM].memory_pack.path] == b""
    assert inputs[arms["native"].memory_pack.path] == f"{MEMORY_TEXT}{TASKS[0]}\n".encode()
    assert [row.id for row in manifest.experiences] == declared
    assert manifest.experiment_id == f"screen48-{probe.NAMESPACE}-c1-{TASKS[0]}"
    assert report["checkpoint"] == preparation["checkpoint"]


def test_the_manifest_carries_the_bytes_that_were_sealed_not_a_caller_s_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A receipt rewritten after preparation is what the manifest cites."""
    monkeypatch.setattr(runner, "run_task", FakeRunner(all_pass()))
    output = tmp_path / "out"
    preparation = write_preparation(output)
    receipt_path = output / preparation["cells"][0]["receipt_path"]
    sealed = json.loads(receipt_path.read_bytes())
    sealed["memory"] = "resealed memory\n"
    receipt_path.write_text(json.dumps(sealed, sort_keys=True))

    probe.run_cells(
        preparation=preparation,
        template=write_template(tmp_path / "artifacts"),
        tasks_root=write_material(tmp_path / "material"),
        output=output,
        repetitions=1,
        workers=1,
    )

    manifest, inputs = load_manifest(output / "manifests" / TASKS[0] / "manifest.json")
    arm = next(arm for arm in manifest.arms if arm.id == preparation["cells"][0]["arm"])
    assert inputs[arm.memory_pack.path] == b"resealed memory\n"


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------


def test_a_missing_pack_is_recorded_once_per_repetition_and_never_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner(all_pass())
    monkeypatch.setattr(runner, "run_task", fake)
    output = tmp_path / "out"
    missing = frozenset({(TASKS[0], "raw_retrieval")})
    preparation = write_preparation(output, missing=missing)

    report = probe.run_cells(
        preparation=preparation,
        template=write_template(tmp_path / "artifacts"),
        tasks_root=write_material(tmp_path / "material"),
        output=output,
        repetitions=REPETITIONS,
        workers=2,
    )

    unprepared = [
        cell
        for cell in report["cells"]
        if (cell["task"], cell["arm"]) == (TASKS[0], "raw_retrieval")
    ]
    assert len(unprepared) == REPETITIONS
    assert {cell["status"] for cell in unprepared} == {"missing_pack"}
    assert {cell["reason"] for cell in unprepared} == {"raw_required_lane_incomplete"}
    assert all(cell["attempt_id"] is None and cell["passed"] is None for cell in unprepared)
    assert (TASKS[0], "raw_retrieval") not in {(call["task"], call["arm"]) for call in fake.calls}
    assert len(fake.calls) == (len(TASKS) * len(ARMS) - 1) * REPETITIONS
    row = next(
        row for row in report["rows"] if (row["task"], row["arm"]) == (TASKS[0], "raw_retrieval")
    )
    assert row["unknown"] == REPETITIONS
    assert row["statuses"] == {"missing_pack": REPETITIONS}
    assert row["pass_rate"] == 0.0
    # The three prepared arms of that task still ran, on one shared manifest.
    assert (output / "manifests" / TASKS[0] / "manifest.json").is_file()


def test_rows_and_lift_follow_the_measured_rates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes = all_pass()
    outcomes[(TASKS[0], probe.NO_MEMORY_ARM)] = [False, False]
    outcomes[(TASKS[0], "strong_summary")] = [True, False]
    outcomes[(TASKS[1], probe.NO_MEMORY_ARM)] = [True, True]
    monkeypatch.setattr(runner, "run_task", FakeRunner(outcomes))
    output = tmp_path / "out"

    report = probe.run_cells(
        preparation=write_preparation(output),
        template=write_template(tmp_path / "artifacts"),
        tasks_root=write_material(tmp_path / "material"),
        output=output,
        repetitions=REPETITIONS,
        workers=3,
    )

    rows = {(row["task"], row["arm"]): row for row in report["rows"]}
    assert len(rows) == len(TASKS) * len(ARMS)
    assert rows[(TASKS[0], "native")]["passes"] == PER_ROW_CELLS
    assert rows[(TASKS[0], "native")]["pass_rate"] == NATIVE_LIFT
    assert rows[(TASKS[0], probe.NO_MEMORY_ARM)]["failures"] == PER_ROW_CELLS
    assert rows[(TASKS[0], "strong_summary")]["pass_rate"] == SUMMARY_LIFT
    assert rows[(TASKS[0], "native")]["family_id"] == f"transfer-{TASKS[0]}"
    assert rows[(TASKS[0], "native")]["statuses"] == {"passed": PER_ROW_CELLS}

    lift = {(row["task"], row["arm"]): row for row in report["lift"]}
    assert len(lift) == len(TASKS) * (len(ARMS) - 1)
    assert lift[(TASKS[0], "native")]["lift"] == NATIVE_LIFT
    assert lift[(TASKS[0], "strong_summary")]["lift"] == SUMMARY_LIFT
    assert lift[(TASKS[0], "raw_retrieval")]["no_memory_pass_rate"] == 0.0
    # The second task is saturated without memory, so no arm can lift it.
    assert lift[(TASKS[1], "native")]["lift"] == 0.0
    assert probe.NO_MEMORY_ARM not in {arm for _, arm in lift}

    assert report["totals"]["denominator"] == len(TASKS) * len(ARMS) * REPETITIONS
    assert report["learning_benefit_established"] is False
    assert report["memory_established"] is False
    assert json.loads((output / probe.REPORT_NAME).read_bytes()) == report

    rendered = probe.table(report)
    assert TASKS[0] in rendered
    assert "lift" in rendered
    assert rendered.count("lift ") == len(lift)


def test_run_cells_refuses_a_repetition_or_worker_count_below_one(tmp_path: Path) -> None:
    output = tmp_path / "out"
    preparation = write_preparation(output)
    template = write_template(tmp_path / "artifacts")
    material = write_material(tmp_path / "material")
    with pytest.raises(ManifestError, match="one repetition"):
        probe.run_cells(
            preparation=preparation,
            template=template,
            tasks_root=material,
            output=output,
            repetitions=0,
            workers=1,
        )
    with pytest.raises(ManifestError, match="one worker"):
        probe.run_cells(
            preparation=preparation,
            template=template,
            tasks_root=material,
            output=output,
            repetitions=1,
            workers=0,
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cli(tmp_path: Path, *, output: Path, extra: list[str]) -> list[str]:
    """The flags every CLI case shares, with the template written to disk."""
    template = write_template(tmp_path / "artifacts")
    path = tmp_path / "template.json"
    path.write_text(json.dumps(template, sort_keys=True))
    return [
        "--tasks-root",
        str(write_material(tmp_path / "material")),
        "--template",
        str(path),
        "--tokenizer-assets",
        str(tmp_path / "tokenizer"),
        "--output",
        str(output),
        *extra,
    ]


def test_the_cli_refuses_an_output_that_already_exists(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.mkdir()
    with pytest.raises(ManifestError, match="already exists"):
        probe.main(cli(tmp_path, output=output, extra=["--task-ids", TASKS[0]]))


def test_the_cli_refuses_an_unknown_arm_or_task_before_reading_anything(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="outside the study contract"):
        probe.main(
            cli(
                tmp_path,
                output=tmp_path / "arm-out",
                extra=["--task-ids", TASKS[0], "--arms", "native", "telepathy"],
            )
        )
    with pytest.raises(ManifestError, match="outside the material catalog"):
        probe.main(cli(tmp_path, output=tmp_path / "task-out", extra=["--task-ids", "not-a-task"]))


def test_the_parser_defaults_to_all_four_arms_at_checkpoint_one(tmp_path: Path) -> None:
    args = probe.build_parser().parse_args(
        cli(tmp_path, output=tmp_path / "out", extra=["--task-ids", *TASKS])
    )
    assert args.arms == list(contract.ARMS)
    assert args.checkpoint == probe.DEFAULT_CHECKPOINT
    assert args.repetitions == probe.DEFAULT_REPETITIONS
    assert args.workers == probe.DEFAULT_WORKERS
    assert args.api_key_env == "OPENROUTER_API_KEY"
    assert args.task_ids == list(TASKS)


def test_the_devbox_phase_hands_its_own_flags_and_output_to_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(probe, "main", fake_main)
    output = tmp_path / "out"

    exit_code = run_phase.PHASES["probe"](output, ["--task-ids", TASKS[0]], {})

    assert exit_code == 0
    assert seen == [["--task-ids", TASKS[0], "--output", str(output)]]
    # The probe reads the live database for its packs, so the phase keeps it.
    assert "probe" not in run_phase.NO_DATABASE_PHASES


def test_the_probe_refuses_a_repeated_task_or_an_unknown_checkpoint(tmp_path: Path) -> None:
    root = write_material(tmp_path / "material")
    with pytest.raises(ManifestError, match="repeats a task"):
        probe._validated(checkpoint=1, tasks=[TASKS[0], TASKS[0]], arms=list(ARMS), tasks_root=root)
    with pytest.raises(ManifestError, match="repeats an arm"):
        probe._validated(
            checkpoint=1, tasks=list(TASKS), arms=["native", "native"], tasks_root=root
        )
    with pytest.raises(ManifestError, match="unknown checkpoint"):
        probe._validated(checkpoint=7, tasks=list(TASKS), arms=list(ARMS), tasks_root=root)
    assert probe._validated(checkpoint=1, tasks=list(TASKS), arms=list(ARMS), tasks_root=root) == {
        task: f"transfer-{task}" for task in TASKS
    }
