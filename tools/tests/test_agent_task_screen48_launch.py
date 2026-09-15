"""Materialization, claim, execution and observation checks for the 48-cell screen."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from benchmarks.agent_tasks.manifest import (
    ManifestError,
    digest,
    identity,
    load_manifest,
    runtime_identity,
)
from benchmarks.agent_tasks.screen48 import launcher, observation
from benchmarks.agent_tasks.screen48.materialize import materialize
from benchmarks.agent_tasks.transfer_report import IDENTITY_FIELDS

REPO_SCHEDULE = (
    Path(__file__).resolve().parents[2] / "benchmarks/agent_tasks/screen48/schedule/schedule.json"
)
FROZEN_SCHEDULE = Path(
    "/Users/bliss/dev/eval-artifacts/sibyl"
    "/full-cohort-screen48-schedule-b21d33fdf886433cb188cb4414e435e1/schedule.json"
)
NAMESPACE = "b21d33fdf886433cb188cb4414e435e1"
FROZEN_CELLS = 48
MINI_CELLS = 12
CATALOG = digest(b"mini-source-catalog")
TASKS = ("venue-capacity-report", "hex-stream-journal")
ARMS = ("native", "raw_retrieval", "no_memory")
ORACLE = json.dumps(
    {
        "schema_version": "sibyl-json-cli-cases-v1",
        "cases": [{"id": "one", "input": {"a": 1}, "expected": {"b": 2}}],
    },
    sort_keys=True,
)


def frozen_schedule() -> dict:
    """Prefer the in-repo frozen copy; fall back to the retained evidence path."""
    for path in (REPO_SCHEDULE, FROZEN_SCHEDULE):
        if path.is_file():
            return json.loads(path.read_text())
    pytest.skip("the frozen 48-cell schedule is not available")


def mini_cells() -> list[dict]:
    cells, ordinal = [], 0
    for checkpoint in (0, 1):
        for task in TASKS:
            for arm in ARMS:
                cells.append(
                    {
                        "attempt_id": f"{ordinal:032x}",
                        "ordinal": ordinal,
                        "checkpoint": checkpoint,
                        "task": task,
                        "arm": arm,
                        "family": "interval_capacity",
                        "category": (
                            "related_transfer" if task == TASKS[0] else "applicability_contrast"
                        ),
                        "repetition": 0,
                    }
                )
                ordinal += 1
    return cells


def mini_schedule(sources: list[dict]) -> dict:
    cells = mini_cells()
    return {
        "schema": "sibyl-screen48-unarmed-schedule-proposal-v1",
        "experiment_namespace": NAMESPACE,
        "checkpoints": [0, 1],
        "arms": list(ARMS),
        "denominator": len(cells),
        "cells": cells,
        "original_sources": sources,
        "source_catalog_sha256": CATALOG,
        "source_commit": "0" * 40,
        "tasks_policy_order": list(TASKS),
    }


def write_tasks(root: Path, *, leak_oracle: bool = False) -> Path:
    for task in TASKS:
        directory = root / "tasks" / task
        (directory / "workspace").mkdir(parents=True)
        (directory / "prompt.md").write_text(f"# {task}\n\nRepair the resolver.\n")
        (directory / "oracle.json").write_text(ORACLE)
        (directory / "workspace" / "app.py").write_text(
            ORACLE if leak_oracle else f"# {task} entry point\n"
        )
        (directory / "workspace" / "resolver.py").write_text(f"# {task} resolver\n")
    return root


def build_template(root: Path, count: int = 3) -> tuple[dict, list[dict]]:
    """Create the template bindings plus the matching schedule source rows."""
    (root / "learning").mkdir(parents=True)
    lock = root / "uv.lock"
    lock.write_bytes(b"# frozen dependency lock\n")
    controller = root / "controller.py"
    controller.write_bytes(b"import sys\n\nsys.stdout.write('{}')\n")
    experiences, sources = [], []
    for index in range(count):
        source_id = f"source-{index:04d}"
        content = json.dumps({"source": source_id}, sort_keys=True).encode()
        (root / "learning" / f"{source_id}.json").write_bytes(content)
        family = f"training-family-{index % 3}"
        experiences.append(
            {
                "id": source_id,
                "family_id": family,
                "split": "learning",
                "revision": "1",
                "artifact": {
                    "path": f"learning/{source_id}.json",
                    "sha256": digest(content),
                },
            }
        )
        sources.append(
            {
                "id": source_id,
                "training_family": family,
                "source_sha256": digest(content),
                "observation": {"revision": 1},
            }
        )
    template = {
        "artifact_root": str(root),
        "runtime_sha256": identity(runtime_identity()),
        "dependency_lock": {"path": "uv.lock", "sha256": digest(lock.read_bytes())},
        "seed": 0,
        "controller": {
            "script": {"path": "controller.py", "sha256": digest(controller.read_bytes())},
            "args": [],
        },
        "controller_api_key_env": None,
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
    return template, sources


def write_packs(root: Path, cells: list[dict], *, absent: set[str] = frozenset()) -> Path:
    """Write one preparation receipt per cell, omitting the file for absent arms."""
    for cell in cells:
        if cell["attempt_id"] in absent:
            continue
        directory = root / "packs" / f"cp{cell['checkpoint']}" / cell["task"]
        directory.mkdir(parents=True, exist_ok=True)
        memory = (
            ""
            if cell["arm"] == "no_memory"
            else f"memory for {cell['task']} {cell['arm']} at cp{cell['checkpoint']}\n"
        )
        (directory / f"{cell['arm']}.txt").write_text(memory)
        (directory / f"{cell['arm']}.json").write_text(
            json.dumps(
                {
                    "status": "prepared",
                    "reason": None,
                    "checkpoint": cell["checkpoint"],
                    "task": cell["task"],
                    "arm": cell["arm"],
                    "memory": memory,
                    "counts": {"memory_sha256": digest(memory.encode()), "fits": True},
                    "catalog_sha256": CATALOG,
                },
                sort_keys=True,
            )
        )
    return root


@pytest.fixture
def mini(tmp_path):
    """A complete, fully prepared miniature of the screen: 2 checkpoints, 2 tasks, 3 arms."""
    template, sources = build_template(tmp_path / "template")
    schedule = mini_schedule(sources)
    return {
        "schedule": schedule,
        "template": template,
        "tasks_root": write_tasks(tmp_path / "material"),
        "packs_root": write_packs(tmp_path / "prepared", schedule["cells"]),
        "output": tmp_path / "materialized",
        "root": tmp_path / "run",
    }


def run_materialize(world, **overrides):
    return materialize(
        schedule=overrides.get("schedule", world["schedule"]),
        packs_root=overrides.get("packs_root", world["packs_root"]),
        tasks_root=overrides.get("tasks_root", world["tasks_root"]),
        template=overrides.get("template", world["template"]),
        output=overrides.get("output", world["output"]),
    )


def test_materialize_emits_one_loadable_manifest_per_checkpoint_and_task(mini):
    report = run_materialize(mini)

    assert report["denominator"] == len(mini["schedule"]["cells"])
    assert report["prepared_cells"] == len(mini["schedule"]["cells"])
    assert report["manifest_count"] == len(TASKS) * 2 <= MINI_CELLS
    assert [cell["attempt_id"] for cell in report["cells"]] == [
        cell["attempt_id"] for cell in mini["schedule"]["cells"]
    ]
    for entry in report["manifests"]:
        manifest, _ = load_manifest(mini["output"] / entry["path"])
        assert [arm.id for arm in manifest.arms] == entry["arms"]
        assert manifest.tasks[0].id == entry["task"]
        assert manifest.tasks[0].split == "development"
        assert manifest.tasks[0].family_id == "interval_capacity"
        assert digest((mini["output"] / entry["path"]).read_bytes()) == entry["sha256"]
    for cell in report["cells"]:
        assert set(cell["expected_identity"]) == set(IDENTITY_FIELDS)
    no_memory = next(cell for cell in report["cells"] if cell["arm"] == "no_memory")
    assert no_memory["pack_sha256"] == digest(b"")


def test_a_missing_arm_stays_unprepared_and_never_gets_an_invented_pack(mini):
    absent = mini["schedule"]["cells"][1]
    packs_root = write_packs(
        mini["packs_root"].parent / "partial",
        mini["schedule"]["cells"],
        absent={absent["attempt_id"]},
    )

    report = run_materialize(mini, packs_root=packs_root)

    unprepared = next(
        cell for cell in report["cells"] if cell["attempt_id"] == absent["attempt_id"]
    )
    assert unprepared["prepared"] is False
    assert unprepared["reason"] == "no_pack_receipt"
    assert unprepared["manifest"] is None
    assert unprepared["expected_identity"] is None
    assert report["prepared_cells"] == len(mini["schedule"]["cells"]) - 1
    entry = next(
        row
        for row in report["manifests"]
        if (row["checkpoint"], row["task"]) == (absent["checkpoint"], absent["task"])
    )
    manifest, _ = load_manifest(mini["output"] / entry["path"])
    assert absent["arm"] not in {arm.id for arm in manifest.arms}
    packs = (mini["output"] / entry["path"]).parent / "packs"
    assert not (packs / f"{absent['arm']}.txt").exists()
    assert {path.name for path in packs.iterdir()} == {f"{arm.id}.txt" for arm in manifest.arms}


def test_a_task_whose_workspace_leaks_the_oracle_is_refused(mini, tmp_path):
    leaking = write_tasks(tmp_path / "leaking", leak_oracle=True)

    with pytest.raises(ManifestError, match="private oracle"):
        run_materialize(mini, tasks_root=leaking)

    assert not mini["output"].exists()


def test_claim_refuses_a_reservation_above_the_ceiling(mini):
    report = run_materialize(mini)

    with pytest.raises(ManifestError, match="exceeds the accepted ceiling"):
        launcher.claim(
            mini["root"],
            schedule=mini["schedule"],
            materialization=report,
            ceiling_usd=Decimal("10"),
            prior_reserved_usd=Decimal("0"),
            per_cell_usd=Decimal("2"),
        )

    assert not (mini["root"] / "claim.json").exists()


def test_a_second_claim_against_the_same_root_is_refused(mini):
    report = run_materialize(mini)
    amounts = {
        "ceiling_usd": Decimal("100"),
        "prior_reserved_usd": Decimal("7.5"),
        "per_cell_usd": Decimal("2"),
    }

    first = launcher.claim(
        mini["root"], schedule=mini["schedule"], materialization=report, **amounts
    )

    assert first["reserved_usd"] == "24"
    assert first["cumulative_reserved_usd"] == "31.5"
    assert first["prepared_cells"] == MINI_CELLS
    with pytest.raises(ManifestError, match="single-use"):
        launcher.claim(mini["root"], schedule=mini["schedule"], materialization=report, **amounts)


def claimed(world, **amounts):
    report = run_materialize(world)
    launcher.claim(
        world["root"],
        schedule=world["schedule"],
        materialization=report,
        ceiling_usd=amounts.get("ceiling_usd", Decimal("1000")),
        prior_reserved_usd=amounts.get("prior_reserved_usd", Decimal("0")),
        per_cell_usd=amounts.get("per_cell_usd", Decimal("2")),
    )
    return report


def test_dry_run_writes_one_outcome_per_cell_in_schedule_order(mini):
    claimed(mini)

    record = launcher.execute(mini["root"], dry_run=True)

    assert record["dispatched"] == len(mini["schedule"]["cells"])
    assert [row["attempt_id"] for row in record["outcomes"]] == [
        cell["attempt_id"] for cell in mini["schedule"]["cells"]
    ]
    assert {row["status"] for row in record["outcomes"]} == {"dry_run"}
    for cell in mini["schedule"]["cells"]:
        directory = mini["root"] / "cells" / cell["attempt_id"]
        assert (directory / "begin.json").is_file()
        assert (directory / "outcome.json").is_file()
        assert not (directory / "attempt").exists()


def test_resume_skips_completed_cells_and_never_replays_an_unknown_one(mini):
    claimed(mini)
    launcher.execute(mini["root"], dry_run=True)
    crashed = mini["schedule"]["cells"][3]["attempt_id"]
    outcome = mini["root"] / "cells" / crashed / "outcome.json"
    began = json.loads((mini["root"] / "cells" / crashed / "begin.json").read_text())
    outcome.unlink()

    resumed = launcher.execute(mini["root"], dry_run=True)

    assert resumed["dispatched"] == 0
    assert resumed["unknown_not_replayed"] == [crashed]
    assert len(resumed["skipped_complete"]) == len(mini["schedule"]["cells"]) - 1
    assert not outcome.exists()
    assert json.loads((mini["root"] / "cells" / crashed / "begin.json").read_text()) == began
    report = launcher.terminal(mini["root"])
    row = next(row for row in report["rows"] if row["attempt_id"] == crashed)
    assert row["dispatch_state"] == "unknown"
    assert row["task_passed"] is None


def test_the_frozen_forty_eight_cell_schedule_always_reports_forty_eight_rows(tmp_path):
    schedule = frozen_schedule()
    assert len(schedule["cells"]) == FROZEN_CELLS
    template, _ = build_template(tmp_path / "template")
    template["experiences"] = [
        {
            "id": row["id"],
            "family_id": row["training_family"],
            "split": "learning",
            "revision": str(row["observation"]["revision"]),
            "artifact": {"path": f"learning/{row['id']}.json", "sha256": row["source_sha256"]},
        }
        for row in schedule["original_sources"]
    ]
    report = materialize(
        schedule=schedule,
        packs_root=tmp_path / "unprepared",
        tasks_root=write_tasks(tmp_path / "material"),
        template=template,
        output=tmp_path / "materialized",
    )
    assert report["manifest_count"] == 0
    assert report["prepared_cells"] == 0

    launcher.claim(
        tmp_path / "run",
        schedule=schedule,
        materialization=report,
        ceiling_usd=Decimal("762.4700928"),
        prior_reserved_usd=Decimal("707.2186368"),
        per_cell_usd=Decimal("2"),
    )
    launcher.execute(tmp_path / "run", dry_run=True)
    terminal = launcher.terminal(tmp_path / "run")

    assert terminal["denominator"] == FROZEN_CELLS
    assert len(terminal["rows"]) == FROZEN_CELLS
    assert terminal["totals"]["unprepared"] == FROZEN_CELLS
    assert sum(group["denominator"] for group in terminal["groups"]) == FROZEN_CELLS
    assert {group["denominator"] for group in terminal["groups"]} == {6}
    observed = observation.observe(schedule, terminal)
    assert observed["denominator"] == FROZEN_CELLS
    assert len(observed["unprepared_attempt_ids"]) == FROZEN_CELLS
    assert observed["observed"] == {"unprepared": FROZEN_CELLS}


def test_execute_refuses_to_dispatch_without_the_declared_credential(mini, monkeypatch):
    mini["template"]["controller_api_key_env"] = "OPENROUTER_API_KEY"
    claimed(mini)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(ManifestError, match="OPENROUTER_API_KEY is not set"):
        launcher.execute(mini["root"])

    assert list((mini["root"] / "cells").iterdir()) == []
    assert launcher.execute(mini["root"], dry_run=True)["dispatched"] == MINI_CELLS


def test_a_real_dispatch_records_the_scheduled_runner_identity(mini):
    """The fixture controller returns an invalid protocol object, so no checker runs.

    That is enough to drive the whole launcher path for real: the runner is
    called, a durable receipt comes back, and its frozen identity fields must be
    exactly the ones materialization predicted for that cell.
    """
    report = claimed(mini)

    record = launcher.execute(mini["root"], workers=2)

    assert record["dispatched"] == len(mini["schedule"]["cells"])
    assert {row["status"] for row in record["outcomes"]} == {"controller_protocol_invalid"}
    for outcome, cell in zip(record["outcomes"], report["cells"], strict=True):
        assert outcome["identity"] == cell["expected_identity"]
        assert outcome["receipt_sha256"] is not None
        attempt = mini["root"] / "cells" / outcome["attempt_id"] / "attempt"
        assert digest((attempt / "receipt.json").read_bytes()) == outcome["receipt_file_sha256"]
    terminal = launcher.terminal(mini["root"])
    assert terminal["dispatch_states"]["complete"] == len(mini["schedule"]["cells"])
    observed = observation.observe(mini["schedule"], terminal)
    assert observed["observed"] == {"unknown": len(mini["schedule"]["cells"])}
    assert all(
        row["reason"] == "receipt_status:controller_protocol_invalid"
        for row in observed["untrusted_rows"]
    )


def test_a_runner_exception_seals_an_outcome_instead_of_aborting_the_ledger(mini, monkeypatch):
    """A cell that raises anything must still end as a sealed runner_error.

    An escaping exception leaves begin.json with no outcome.json, which resume
    reads as unknown and never replays, and it takes the whole execute() call
    down before the ledger is published for any of the other cells.
    """
    claimed(mini)

    def explode(*_args, **_kwargs):
        raise RuntimeError("controller socket closed")

    monkeypatch.setattr(launcher.runner, "run_task", explode)

    record = launcher.execute(mini["root"], workers=2)

    assert record["dispatched"] == len(mini["schedule"]["cells"])
    assert {row["status"] for row in record["outcomes"]} == {"runner_error"}
    assert {row["error_type"] for row in record["outcomes"]} == {"RuntimeError"}
    for outcome in record["outcomes"]:
        assert outcome["error"] == "RuntimeError: controller socket closed"
        assert outcome["success"] is False
        cell_root = mini["root"] / "cells" / outcome["attempt_id"]
        assert json.loads((cell_root / "outcome.json").read_text())["status"] == "runner_error"
    terminal = launcher.terminal(mini["root"])
    assert terminal["dispatch_states"]["complete"] == len(mini["schedule"]["cells"])
    assert terminal["dispatch_states"]["unknown"] == 0


def trusted_terminal(world) -> dict:
    """Promote a dry-run terminal into one where every prepared cell returned cleanly."""
    claimed(world)
    launcher.execute(world["root"], dry_run=True)
    terminal = launcher.terminal(world["root"])
    for row in terminal["rows"]:
        row.update(
            dispatch_state="complete",
            receipt_status="passed",
            task_passed=True,
            receipt_identity=dict(row["expected_identity"]),
            usage={"input_tokens": 10, "output_tokens": 5, "tool_calls": 1, "cost_usd": 0.25},
        )
    return terminal


def test_observe_reads_a_clean_screen_as_facts_without_a_verdict(mini):
    terminal = trusted_terminal(mini)

    observed = observation.observe(mini["schedule"], terminal)

    assert observed["untrusted_rows"] == []
    assert observed["observed"] == {"passed": len(mini["schedule"]["cells"])}
    assert observed["learning_benefit_established"] is False
    assert {group["denominator"] for group in observed["arm_checkpoint"]} == {2}
    paired = {(row["task"], row["arm"]): row for row in observed["paired_checkpoints"]}
    assert len(paired) == len(TASKS) * len(ARMS)
    assert paired[(TASKS[0], "native")]["treatment_changed"] is True
    assert paired[(TASKS[0], "no_memory")]["treatment_changed"] is False
    assert {row["category"] for row in observed["categories"]} == {
        "related_transfer",
        "applicability_contrast",
    }


def test_observe_flags_a_row_whose_receipt_carries_a_swapped_pack(mini):
    terminal = trusted_terminal(mini)
    swapped, donor = (
        next(row for row in terminal["rows"] if row["arm"] == arm)
        for arm in ("native", "no_memory")
    )
    swapped["receipt_identity"]["memory_pack_sha256"] = donor["expected_identity"][
        "memory_pack_sha256"
    ]

    observed = observation.observe(mini["schedule"], terminal)

    flagged = observed["untrusted_rows"]
    assert [row["attempt_id"] for row in flagged] == [swapped["attempt_id"]]
    assert "memory_pack_sha256" in flagged[0]["reason"]
    assert flagged[0]["observed"] == "unknown"
    assert swapped["attempt_id"] in observed["unknown_attempt_ids"]
    assert observed["complete"] is False


def test_observe_refuses_a_terminal_report_from_another_schedule(mini):
    terminal = trusted_terminal(mini)
    terminal["rows"][0]["task"] = "some-other-task"

    with pytest.raises(ManifestError, match="outside its cell"):
        observation.observe(mini["schedule"], terminal)
