"""No-memory headroom screen: task selection, manifest shape, cells and bands."""

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
from benchmarks.agent_tasks.screen48 import headroom
from benchmarks.agent_tasks.screen48.contract import TASKS as SCREEN_TASKS

MATERIAL_ROOT = Path(__file__).resolve().parents[2] / "benchmarks/agent_tasks/transfer_material"
CATALOGUED = 24
EXPECTED_HEADROOM_TASKS = 18
TASKS = ("alpha-task", "beta-task", "gamma-task")
ATTEMPT_HEX = 32
CELL_COST_USD = 0.02
CELL_TOOL_CALLS = 4.0
CELL_ELAPSED_SECONDS = 12.5
HALF = 0.5
FAILED_ELAPSED_SECONDS = 3.0
MIXED_MEAN_ELAPSED = 7.75
PER_TASK_CELLS = 2
ORACLE = json.dumps(
    {
        "schema_version": "sibyl-json-cli-cases-v1",
        "cases": [{"id": "one", "input": {"a": 1}, "expected": {"b": 2}}],
    },
    sort_keys=True,
)


def write_material(root: Path) -> Path:
    """Write a catalog and one workspace per task, in the frozen material's shape."""
    for task in TASKS:
        directory = root / "tasks" / task
        (directory / "workspace").mkdir(parents=True)
        (directory / "prompt.md").write_text(f"# {task}\n\nRepair the resolver.\n")
        (directory / "oracle.json").write_text(ORACLE)
        (directory / "workspace" / "app.py").write_text(f"# {task} entry point\n")
        (directory / "workspace" / "resolver.py").write_text(f"# {task} resolver\n")
    catalog = {
        "schema_version": "sibyl-transfer-task-material-v1",
        "tasks": [
            {"id": task, "family_id": f"transfer-{task}", "split": "development"} for task in TASKS
        ],
    }
    (root / headroom.CATALOG_NAME).write_text(json.dumps(catalog, indent=2, sort_keys=True))
    return root


def write_template(root: Path, *, api_key_env: str | None = None) -> dict[str, Any]:
    """Build the template shape ``materialize`` consumes, with no experience at all."""
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "uv.lock"
    lock.write_bytes(b"# frozen dependency lock\n")
    controller = root / "controller.py"
    controller.write_bytes(b"import sys\n\nsys.stdout.write('{}')\n")
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
    }


def receipt(*, passed: bool | None, attempt_id: str, cost: float = CELL_COST_USD) -> dict[str, Any]:
    """A receipt in the runner's shape; ``passed=None`` is a controller failure."""
    if passed is None:
        return {
            "attempt_id": attempt_id,
            "status": "controller_failed",
            "success": False,
            "controller": {"elapsed_seconds": FAILED_ELAPSED_SECONDS},
            "usage": dict.fromkeys(headroom.USAGE_FIELDS),
            "receipt_sha256": digest(attempt_id.encode()),
        }
    return {
        "attempt_id": attempt_id,
        "status": "passed" if passed else "task_failed",
        "success": passed,
        "outcome": {"passed": passed, "status": "passed" if passed else "task_failed"},
        "controller": {"elapsed_seconds": CELL_ELAPSED_SECONDS},
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 200,
            "tool_calls": CELL_TOOL_CALLS,
            "cost_usd": cost,
            "complete": True,
        },
        "receipt_sha256": digest(attempt_id.encode()),
    }


class FakeRunner:
    """Stand in for ``runner.run_task``: no docker, no provider, recorded calls."""

    def __init__(
        self, outcomes: dict[str, list[bool | None]], *, raises: frozenset[str] = frozenset()
    ):
        self.outcomes = {task: list(values) for task, values in outcomes.items()}
        self.raises = raises
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
            {
                "manifest": manifest_path,
                "task": task_id,
                "arm": arm_id,
                "output": output,
                "attempt_id": attempt_id,
            }
        )
        if task_id in self.raises:
            raise RuntimeError(f"docker is unreachable for {task_id}")
        output.mkdir(parents=True)
        result = receipt(passed=self.outcomes[task_id].pop(0), attempt_id=str(attempt_id))
        (output / "receipt.json").write_text(json.dumps(result, sort_keys=True))
        return result


def run_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    outcomes: dict[str, list[bool | None]],
    repetitions: int = 2,
    raises: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any], FakeRunner]:
    fake = FakeRunner(outcomes, raises=raises)
    monkeypatch.setattr(runner, "run_task", fake)
    report = headroom.screen(
        tasks_root=write_material(tmp_path / "material"),
        template=write_template(tmp_path / "artifacts"),
        task_ids=list(TASKS),
        repetitions=repetitions,
        output=tmp_path / "out",
        workers=2,
    )
    return report, fake


def test_default_task_ids_are_the_eighteen_unscreened_tasks() -> None:
    families = headroom.catalog_families(MATERIAL_ROOT)
    tasks = headroom.default_task_ids(MATERIAL_ROOT)
    assert len(families) == CATALOGUED
    assert len(tasks) == EXPECTED_HEADROOM_TASKS
    assert set(tasks) == set(families) - set(SCREEN_TASKS)
    assert not set(tasks) & set(SCREEN_TASKS)


def test_one_arm_manifest_is_emitted_and_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, fake = run_screen(
        tmp_path, monkeypatch, outcomes={task: [True, True] for task in TASKS}, repetitions=2
    )
    path = Path(report["root"]) / "manifests" / TASKS[0] / "manifest.json"
    manifest, inputs = load_manifest(path)
    assert [arm.id for arm in manifest.arms] == [headroom.ARM]
    assert manifest.arms[0].learning_source_ids == []
    assert manifest.arms[0].memory_pack.sha256 == digest(b"")
    assert inputs[manifest.arms[0].memory_pack.path] == b""
    assert manifest.experiences == []
    assert [task.id for task in manifest.tasks] == [TASKS[0]]
    assert manifest.tasks[0].family_id == f"transfer-{TASKS[0]}"
    assert manifest.experiment_id == f"screen48-{headroom.NAMESPACE}-c0-{TASKS[0]}"
    assert {call["arm"] for call in fake.calls} == {headroom.ARM}


def test_repetitions_each_get_a_fresh_attempt_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, fake = run_screen(
        tmp_path,
        monkeypatch,
        outcomes={task: [True, False, True] for task in TASKS},
        repetitions=3,
    )
    attempts = [call["attempt_id"] for call in fake.calls]
    assert len(attempts) == len(TASKS) * 3
    assert len(set(attempts)) == len(attempts)
    assert all(len(value) == ATTEMPT_HEX for value in attempts)
    cells = report["cells"]
    assert len({cell["attempt_id"] for cell in cells}) == len(cells)
    assert sorted(cell["repetition"] for cell in cells if cell["task"] == TASKS[0]) == [0, 1, 2]
    for cell in cells:
        assert (Path(report["root"]) / cell["receipt"]).is_file()


def test_a_raising_runner_becomes_one_bad_cell_not_a_dead_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _ = run_screen(
        tmp_path,
        monkeypatch,
        outcomes={TASKS[0]: [True, True], TASKS[1]: [True, False], TASKS[2]: []},
        raises=frozenset({TASKS[2]}),
    )
    rows = {row["task"]: row for row in report["tasks"]}
    assert rows[TASKS[2]]["unknown"] == PER_TASK_CELLS
    assert rows[TASKS[2]]["pass_rate"] == 0.0
    assert rows[TASKS[2]]["headroom_band"] == headroom.FLOOR
    assert rows[TASKS[0]]["passes"] == PER_TASK_CELLS
    assert rows[TASKS[1]]["passes"] == 1
    broken = [cell for cell in report["cells"] if cell["task"] == TASKS[2]]
    assert {cell["status"] for cell in broken} == {"runner_error"}
    assert all("docker is unreachable" in cell["error"] for cell in broken)
    assert report["totals"] == {
        "denominator": 6,
        "passes": 3,
        "failures": 1,
        "unknown": 2,
        "pass_rate": 0.5,
    }


def test_bands_split_at_the_declared_thresholds() -> None:
    assert headroom.band(1.0) == headroom.SATURATED
    assert headroom.band(headroom.SATURATED_AT) == headroom.SATURATED
    assert headroom.band(headroom.SATURATED_AT - 0.01) == headroom.HEADROOM
    assert headroom.band(headroom.FLOOR_BELOW) == headroom.HEADROOM
    assert headroom.band(headroom.FLOOR_BELOW - 0.01) == headroom.FLOOR
    assert headroom.band(0.0) == headroom.FLOOR


def test_bands_and_cost_follow_the_measured_rates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _ = run_screen(
        tmp_path,
        monkeypatch,
        outcomes={TASKS[0]: [True, True], TASKS[1]: [True, False], TASKS[2]: [False, None]},
    )
    rows = {row["task"]: row for row in report["tasks"]}
    assert rows[TASKS[0]]["headroom_band"] == headroom.SATURATED
    assert rows[TASKS[1]]["headroom_band"] == headroom.HEADROOM
    assert rows[TASKS[2]]["headroom_band"] == headroom.FLOOR
    assert report["bands"] == {
        headroom.SATURATED: [TASKS[0]],
        headroom.HEADROOM: [TASKS[1]],
        headroom.FLOOR: [TASKS[2]],
    }
    assert rows[TASKS[1]]["pass_rate"] == HALF
    assert rows[TASKS[2]]["unknown"] == 1
    # A controller failure reports no usage, so it stays outside the usage
    # means, but it did take wall time and that time is still averaged in.
    assert rows[TASKS[2]]["mean_tool_calls"] == CELL_TOOL_CALLS
    assert rows[TASKS[2]]["mean_cost_usd"] == CELL_COST_USD
    assert rows[TASKS[2]]["mean_elapsed_seconds"] == MIXED_MEAN_ELAPSED
    assert report["total_cost_usd"] == pytest.approx(0.1)
    assert report["memory_established"] is False
    assert report["learning_benefit_established"] is False
    assert json.loads((Path(report["root"]) / "headroom.json").read_bytes()) == report
    rendered = headroom.table(report)
    assert TASKS[0] in rendered
    assert f"{headroom.SATURATED}: {TASKS[0]}" in rendered


def test_a_task_outside_the_catalog_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "run_task", FakeRunner({}))
    with pytest.raises(ManifestError, match="outside the material catalog"):
        headroom.screen(
            tasks_root=write_material(tmp_path / "material"),
            template=write_template(tmp_path / "artifacts"),
            task_ids=["not-a-task"],
            repetitions=1,
            output=tmp_path / "out",
        )


def test_a_declared_credential_must_be_populated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "run_task", FakeRunner({}))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ManifestError, match="is not set"):
        headroom.screen(
            tasks_root=write_material(tmp_path / "material"),
            template=write_template(tmp_path / "artifacts", api_key_env="OPENROUTER_API_KEY"),
            task_ids=[TASKS[0]],
            repetitions=1,
            output=tmp_path / "out",
        )
