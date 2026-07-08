from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

EXPECTED_SELECTED_TRAJECTORIES = 2


def _load_probe_module() -> ModuleType:
    path = Path(__file__).parents[2] / "benchmarks" / "longmemeval_v2_probe.py"
    spec = importlib.util.spec_from_file_location("longmemeval_v2_probe", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_longmemeval_v2_probe_prints_json_summary(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_probe_module()
    _write_dataset(tmp_path)

    assert module.main([str(tmp_path), "--limit", "1", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "sibyl-longmemeval-v2-probe-v1"
    assert payload["tier"] == "small"
    assert payload["limit"] == 1
    assert payload["question_count"] == 1
    assert payload["haystack_count"] == 1
    assert payload["domain_counts"] == {"enterprise": 1}
    assert "trajectory_count" not in payload


def test_longmemeval_v2_probe_writes_json_summary(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_probe_module()
    output_path = tmp_path / "summary.json"
    _write_dataset(tmp_path)

    assert module.main([str(tmp_path), "--limit", "1", "--output", str(output_path)]) == 0

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "sibyl-longmemeval-v2-probe-v1"
    assert payload["question_count"] == 1
    assert "LongMemEval-V2 probe" in capsys.readouterr().out


def test_longmemeval_v2_probe_validates_selected_trajectories(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_probe_module()
    _write_dataset(tmp_path)

    assert (
        module.main(
            [
                str(tmp_path),
                "--limit",
                "1",
                "--validate-trajectories",
                "--json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["trajectory_count"] == EXPECTED_SELECTED_TRAJECTORIES
    assert payload["missing_trajectory_count"] == 0


def test_longmemeval_v2_probe_fails_on_missing_trajectory(
    tmp_path: Path,
    capsys,
) -> None:
    module = _load_probe_module()
    _write_dataset(tmp_path, include_second_trajectory=False)

    assert (
        module.main(
            [
                str(tmp_path),
                "--limit",
                "1",
                "--validate-trajectories",
                "--json",
            ]
        )
        == 1
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["trajectory_count"] == 1
    assert payload["missing_trajectory_count"] == 1


def test_longmemeval_v2_workflow_runs_metadata_only_probe() -> None:
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "longmemeval-v2.yml"
    ).read_text(encoding="utf-8")
    probe_job = workflow.split("official-full:", 1)[0]

    assert "matrix:" in probe_job
    assert "tier: [small, medium]" in probe_job
    assert "trajectories.jsonl" not in probe_job
    assert "SIBYL_OPENAI_API_KEY" not in probe_job
    assert "moon run bench-longmemeval-v2-probe" in probe_job
    assert "sha256sum -c -" in probe_job


def test_longmemeval_v2_workflow_gates_official_full_run() -> None:
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "longmemeval-v2.yml"
    ).read_text(encoding="utf-8")

    assert "run_official_full:" in workflow
    assert "if: github.event_name == 'workflow_dispatch' && inputs.run_official_full" in workflow
    assert "'official-full' || 'standard'" in workflow
    assert "github.event_name != 'workflow_dispatch' || !inputs.run_official_full" in workflow
    assert "moon run bench-longmemeval-v2-official-full" in workflow
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in workflow
    assert '--reader-api-key-env "$READER_API_KEY_ENV"' in workflow
    assert "https://openrouter.ai/api/v1" in workflow
    assert "build_submission_step_1_single_operating_point.py" in workflow
    assert "build_submission_step_2_build_package.py" in workflow
    assert "--receipt-only" in workflow
    assert "--profile longmemeval-v2" in workflow


def test_longmemeval_v2_full_moon_task_installs_official_harness_deps() -> None:
    moon = (Path(__file__).parents[2] / "moon.yml").read_text(encoding="utf-8")
    task = moon.split("bench-longmemeval-v2-official-full:", 1)[1].split(
        "bench-live-smoke:",
        1,
    )[0]

    assert "--with openai-agents" in task


def _write_dataset(
    root: Path,
    *,
    include_second_trajectory: bool = True,
) -> None:
    (root / "haystacks").mkdir()
    (root / "questions.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "q1",
                        "domain": "enterprise",
                        "environment": "workarena",
                        "question_type": "dynamic-environment",
                        "question": "Which filter was selected?",
                        "image": None,
                        "answer": "The priority filter.",
                        "eval_function": "exact_match",
                    }
                ),
                json.dumps(
                    {
                        "id": "q2",
                        "domain": "web",
                        "environment": "visualwebarena",
                        "question_type": "procedure",
                        "question": "How did checkout finish?",
                        "image": None,
                        "answer": "It confirmed the order.",
                        "eval_function": "llm_judge",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    (root / "haystacks" / "lme_v2_small.json").write_text(
        json.dumps({"q1": ["t1", "t2"], "q2": ["t3"]}),
        encoding="utf-8",
    )
    trajectory_ids = ["t1", "t3"]
    if include_second_trajectory:
        trajectory_ids.insert(1, "t2")
    (root / "trajectories.jsonl").write_text(
        "\n".join(_trajectory_json(trajectory_id) for trajectory_id in trajectory_ids),
        encoding="utf-8",
    )


def _trajectory_json(trajectory_id: str) -> str:
    return json.dumps(
        {
            "id": trajectory_id,
            "domain": "enterprise",
            "environment": "workarena",
            "goal": "Resolve the assigned incident.",
            "outcome": "success",
            "start_url": "https://example.test/start",
            "states": [
                {
                    "state_index": 0,
                    "step": 0,
                    "url": "https://example.test/start",
                    "action": "click filter",
                    "thought": "Need incidents",
                    "accessibility_tree": "button Priority",
                    "screenshot": f"screenshots/{trajectory_id}/0.png",
                }
            ],
        }
    )
