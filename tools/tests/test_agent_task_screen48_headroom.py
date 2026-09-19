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
FIVE = headroom.MINIMUM_REPETITIONS
#: Wilson 95% bounds at n=5, to six places, checked against a hand calculation.
WILSON_0_OF_5_UPPER = 0.434482
WILSON_3_OF_5 = (0.230724, 0.882379)
WILSON_5_OF_5_LOWER = 0.565518
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
    # Nothing is known about a task whose every cell broke before the checker:
    # no rate, no interval, no band.
    assert rows[TASKS[2]]["known"] == 0
    assert rows[TASKS[2]]["pass_rate"] is None
    assert rows[TASKS[2]]["interval"] is None
    assert rows[TASKS[2]]["point_band"] is None
    assert rows[TASKS[2]]["headroom_band"] == headroom.UNDETERMINED
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
        "known": 4,
        "pass_rate": 0.75,
    }


def test_point_bands_split_at_the_declared_thresholds() -> None:
    assert headroom.point_band(1.0) == headroom.SATURATED
    assert headroom.point_band(headroom.SATURATED_AT) == headroom.SATURATED
    assert headroom.point_band(headroom.SATURATED_AT - 0.01) == headroom.HEADROOM
    assert headroom.point_band(headroom.FLOOR_BELOW) == headroom.HEADROOM
    assert headroom.point_band(headroom.FLOOR_BELOW - 0.01) == headroom.FLOOR
    assert headroom.point_band(0.0) == headroom.FLOOR


def test_the_wilson_interval_matches_the_hand_calculation() -> None:
    assert headroom.wilson_interval(0, 0) is None
    zero = headroom.wilson_interval(0, FIVE)
    assert zero is not None
    assert (zero["lower"], zero["upper"]) == (0.0, WILSON_0_OF_5_UPPER)
    assert zero["method"] == "wilson"
    assert zero["confidence"] == headroom.CONFIDENCE
    three = headroom.wilson_interval(3, FIVE)
    assert three is not None
    assert (three["lower"], three["upper"]) == WILSON_3_OF_5
    full = headroom.wilson_interval(FIVE, FIVE)
    assert full is not None
    assert (full["lower"], full["upper"]) == (WILSON_5_OF_5_LOWER, 1.0)
    # Symmetric around one half, and narrowing as trials accumulate.
    lower = headroom.wilson_interval(5, 10)
    upper = headroom.wilson_interval(50, 100)
    assert lower is not None
    assert upper is not None
    assert lower["lower"] == round(1 - lower["upper"], 6)
    assert upper["upper"] - upper["lower"] < lower["upper"] - lower["lower"]
    with pytest.raises(ManifestError, match="impossible outcome count"):
        headroom.wilson_interval(6, FIVE)
    with pytest.raises(ManifestError, match="impossible outcome count"):
        headroom.wilson_interval(-1, FIVE)


def test_no_band_is_assigned_below_the_minimum_repetitions() -> None:
    """The coin flip that started this: 0/2 is not a floor, it is unmeasured."""
    for known in range(FIVE):
        for passes in range(known + 1):
            assert headroom.band(passes, known) == headroom.UNDETERMINED


def test_bands_are_read_from_the_interval_not_the_rate() -> None:
    # 0/5 excludes saturation but not the floor: floor. 3/5 is a 60% point rate
    # the old rule called headroom, but its interval reaches down to 0.23 and
    # up to 0.88, so it is floor too: the solver does not pass reliably and
    # the floor cannot be excluded.
    assert headroom.band(0, FIVE) == headroom.FLOOR
    assert headroom.band(3, FIVE) == headroom.FLOOR
    assert headroom.point_band(3 / FIVE) == headroom.HEADROOM
    # 4/5 and 5/5 reach 0.96 and 1.0: saturation is not excluded, and the
    # lower bound is nowhere near 0.9, so neither is anything else.
    assert headroom.band(4, FIVE) == headroom.UNDETERMINED
    assert headroom.band(FIVE, FIVE) == headroom.UNDETERMINED
    assert headroom.point_band(1.0) == headroom.SATURATED
    # Headroom needs the interval clear of both thresholds: 6/10 sits at
    # [0.31, 0.83]. Saturated needs the whole interval above 0.9, which
    # thirty-five straight passes is the first count to reach.
    assert headroom.band(6, 10) == headroom.HEADROOM
    assert headroom.band(30, 30) == headroom.UNDETERMINED
    assert headroom.band(35, 35) == headroom.SATURATED
    assert headroom.band(0, 10) == headroom.FLOOR
    assert headroom.band(9, 10) == headroom.UNDETERMINED


def test_bands_and_cost_follow_the_measured_rates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report, _ = run_screen(
        tmp_path,
        monkeypatch,
        outcomes={
            TASKS[0]: [True] * FIVE,
            TASKS[1]: [True, False, True, False, True, False],
            TASKS[2]: [False] * FIVE + [None],
        },
        repetitions=FIVE + 1,
    )
    rows = {row["task"]: row for row in report["tasks"]}
    assert rows[TASKS[0]]["headroom_band"] == headroom.UNDETERMINED
    assert rows[TASKS[0]]["point_band"] == headroom.SATURATED
    assert rows[TASKS[1]]["headroom_band"] == headroom.FLOOR
    assert rows[TASKS[1]]["point_band"] == headroom.HEADROOM
    assert rows[TASKS[2]]["headroom_band"] == headroom.FLOOR
    assert rows[TASKS[2]]["point_band"] == headroom.FLOOR
    assert report["bands"] == {
        headroom.SATURATED: [],
        headroom.HEADROOM: [],
        headroom.FLOOR: [TASKS[1], TASKS[2]],
        headroom.UNDETERMINED: [TASKS[0]],
    }
    assert report["schema"] == "sibyl-screen48-headroom-v2"
    assert report["minimum_repetitions"] == FIVE
    assert report["interval"] == {"method": "wilson", "confidence": headroom.CONFIDENCE}
    assert rows[TASKS[1]]["pass_rate"] == HALF
    assert rows[TASKS[1]]["repetitions"] == FIVE + 1
    assert rows[TASKS[1]]["known"] == FIVE + 1
    assert rows[TASKS[1]]["minimum_repetitions"] == FIVE
    assert rows[TASKS[1]]["interval"] == headroom.wilson_interval(3, FIVE + 1)
    for field in headroom.BAND_FIELDS:
        assert field in rows[TASKS[1]]
    # The controller failure is a repetition that produced no outcome: it is
    # counted, but it is outside the rate and the interval.
    assert rows[TASKS[2]]["unknown"] == 1
    assert rows[TASKS[2]]["repetitions"] == FIVE + 1
    assert rows[TASKS[2]]["known"] == FIVE
    assert rows[TASKS[2]]["pass_rate"] == 0.0
    assert rows[TASKS[2]]["interval"] == headroom.wilson_interval(0, FIVE)
    # A controller failure reports no usage, so it stays outside the usage
    # means, but it did take wall time and that time is still averaged in.
    assert rows[TASKS[2]]["mean_tool_calls"] == CELL_TOOL_CALLS
    assert rows[TASKS[2]]["mean_cost_usd"] == CELL_COST_USD
    assert rows[TASKS[2]]["mean_elapsed_seconds"] == pytest.approx(
        (FIVE * CELL_ELAPSED_SECONDS + FAILED_ELAPSED_SECONDS) / (FIVE + 1)
    )
    assert report["total_cost_usd"] == pytest.approx(CELL_COST_USD * (3 * FIVE + 1))
    assert report["memory_established"] is False
    assert report["learning_benefit_established"] is False
    assert json.loads((Path(report["root"]) / "headroom.json").read_bytes()) == report
    rendered = headroom.table(report)
    assert TASKS[0] in rendered
    assert f"{headroom.UNDETERMINED}: {TASKS[0]}" in rendered
    assert f"{headroom.FLOOR}: {TASKS[1]}, {TASKS[2]}" in rendered
    assert f"{headroom.SATURATED}: none" in rendered
    assert f"at least {FIVE} known outcomes" in rendered
    assert "0.00-0.43" in rendered


def test_the_cli_defaults_to_the_minimum_repetitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner({task: [True] * FIVE for task in TASKS})
    monkeypatch.setattr(runner, "run_task", fake)
    template = write_template(tmp_path / "artifacts")
    path = tmp_path / "template.json"
    path.write_text(json.dumps(template, sort_keys=True))
    exit_code = headroom.main(
        [
            "--tasks-root",
            str(write_material(tmp_path / "material")),
            "--template",
            str(path),
            "--output",
            str(tmp_path / "out"),
            "--workers",
            "2",
        ]
    )
    assert exit_code == 0
    assert len(fake.calls) == len(TASKS) * FIVE


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
