from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

EXPECTED_SELECTED_TRAJECTORIES = 2
EXPECTED_WORKFLOW_OCCURRENCES = 2


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
    assert "official_domain:" in workflow
    assert "inputs.official_domain == 'web'" in workflow
    assert "inputs.official_domain == 'enterprise'" in workflow
    assert "(inputs.official_domain || 'both') == 'both'" in workflow
    assert "if: github.event_name == 'workflow_dispatch' && inputs.run_official_full" in workflow
    assert "inputs.run_official_full && github.run_id || 'standard'" in workflow
    assert "inputs.run_official_full && 'official-full' || 'standard'" not in workflow
    assert "github.event_name != 'workflow_dispatch' || !inputs.run_official_full" in workflow
    assert "moon run bench-longmemeval-v2-official-full" in workflow
    assert "OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}" in workflow
    assert '--reader-api-key-env "$READER_API_KEY_ENV"' in workflow
    assert "official_reader_max_concurrent_requests:" in workflow
    assert "official_reader_retry_attempts:" in workflow
    assert "official_validation_slice:" in workflow
    assert "composition-v1" in workflow
    assert "composition-v2" in workflow
    assert "official_evidence_composition_mode:" in workflow
    assert "official_source_evidence_bundling:" in workflow
    assert '--reader-max-concurrent-requests "$READER_MAX_CONCURRENT_REQUESTS"' in workflow
    assert '--reader-retry-attempts "$READER_RETRY_ATTEMPTS"' in workflow
    assert '--evidence-composition-mode "$EVIDENCE_COMPOSITION_MODE"' in workflow
    assert "args+=(--source-evidence-bundling)" in workflow
    assert "Verify frozen validation slice" in workflow
    assert "Evaluate validation evidence" in workflow
    assert "official_limit cannot be combined with a frozen validation slice." in workflow
    assert "Screenshots are disabled by the frozen validation slice." in workflow
    assert "official_reader_base_url must exactly match the frozen validation slice." in workflow
    assert "jq -e -r '.question_ids_by_domain.web" in workflow
    assert "Frozen validation slice produced no question IDs" in workflow
    assert "Upload frozen validation report" in workflow
    assert (
        workflow.count("ref: 2cc8c540bdb87fe6761629b585e727e1c4704520")
        == EXPECTED_WORKFLOW_OCCURRENCES
    )
    assert (
        workflow.count("if: inputs.official_validation_slice == 'none'")
        == EXPECTED_WORKFLOW_OCCURRENCES
    )
    assert "benchmarks/longmemeval_v2_composition_validation.json" in workflow
    assert "benchmarks/longmemeval_v2_composition_validation_v2.json" in workflow
    assert 'moon run "$VALIDATION_TASK" -- "${args[@]}"' in workflow
    assert "bench-longmemeval-v2-validation-slice-v2" in workflow
    assert 'args+=(--max-context-total-chars "$MAX_CONTEXT_TOTAL_CHARS")' in workflow
    assert 'if [[ "$OFFICIAL_VALIDATION_SLICE" == composition-v2* ]]; then' in workflow
    assert 'args+=(--questions "$DATA_ROOT/questions.jsonl")' in workflow
    assert 'args+=(--question-ids "$official_question_ids")' in workflow
    assert 'rm -rf "$domain_output"' in workflow
    assert '--output-dir "$domain_output"' in workflow
    assert "--save-memory" in workflow
    assert '--checkpoint-dir "$domain_output/ingest_checkpoint"' in workflow
    assert 'rm -rf "$domain_output/ingest_checkpoint"' in workflow
    assert workflow.index('rm -rf "$domain_output"') < workflow.index(
        "moon run bench-longmemeval-v2-official-full"
    )
    assert workflow.index('rm -rf "$domain_output/ingest_checkpoint"') > workflow.index(
        "moon run bench-longmemeval-v2-official-full"
    )


def test_longmemeval_v2_workflow_rejects_unsealed_paid_shapes_before_work() -> None:
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "longmemeval-v2.yml"
    ).read_text(encoding="utf-8")

    assert "official-preflight:" in workflow
    assert "needs: official-preflight" in workflow
    assert "Sealed paid arms require the complete Small corpus." in workflow
    assert "Sealed paid arms require both official Small domains." in workflow
    assert "Sealed paid arms forbid official_limit." in workflow
    assert "Sealed paid arms cannot use a validation slice." in workflow
    assert "https://openrouter.ai/api/v1" in workflow


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"OFFICIAL_TIER": "medium"}, "complete Small corpus"),
        ({"OFFICIAL_DOMAIN": "web"}, "both official Small domains"),
        ({"OFFICIAL_LIMIT": "1"}, "forbid official_limit"),
        ({"OFFICIAL_VALIDATION_SLICE": "composition-v1"}, "validation slice"),
    ],
)
def test_paid_preflight_exits_before_invalid_dispatch(
    override: dict[str, str],
    message: str,
) -> None:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "longmemeval-v2.yml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    script = workflow["jobs"]["official-preflight"]["steps"][0]["run"]
    env = {
        **os.environ,
        "OFFICIAL_DOMAIN": "both",
        "OFFICIAL_LIMIT": "",
        "OFFICIAL_TIER": "small",
        "OFFICIAL_VALIDATION_SLICE": "none",
        **override,
    }

    result = subprocess.run(  # noqa: S603
        ["/bin/bash", "-c", script],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )

    assert result.returncode == 1
    assert message in result.stdout


def test_longmemeval_v2_workflow_seals_paid_arm_manifest() -> None:
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "longmemeval-v2.yml"
    ).read_text(encoding="utf-8")

    assert "environment: longmemeval-paid" in workflow
    assert "official_arm_manifest_json:" in workflow
    assert "official_experiment_id:" not in workflow
    assert "ARM_MANIFEST_JSON: ${{ inputs.official_arm_manifest_json || '' }}" in workflow
    assert "keys | sort) == ([" in workflow
    assert '"experiment_id"' in workflow
    assert '"experiment_phase"' in workflow
    assert '"pass_id"' in workflow
    assert '"pass_seed"' in workflow
    assert '"arm_role"' in workflow
    assert '"substrate"' in workflow
    assert '"preregistration_sha256"' in workflow
    assert '"max_spend_usd"' in workflow
    assert '"max_context_total_chars"' in workflow
    assert '"operational_note_distillation_profile"' in workflow
    assert "official_arm_manifest_json does not match the sealed schema" in workflow
    assert '--experiment-id "$EXPERIMENT_ID"' in workflow
    assert '--experiment-phase "$EXPERIMENT_PHASE"' in workflow
    assert '--pass-id "$PASS_ID"' in workflow
    assert '--pass-seed "$PASS_SEED"' in workflow
    assert '--shuffle-questions-seed "$PASS_SEED"' in workflow
    assert '--max-spend-usd "$MAX_SPEND_USD"' in workflow


def test_longmemeval_v2_workflow_packages_receipts_and_diagnostics() -> None:
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "longmemeval-v2.yml"
    workflow = workflow_path.read_text(encoding="utf-8")
    official_job = yaml.safe_load(workflow)["jobs"]["official-full"]
    official_env = official_job["env"]
    telemetry_script = next(
        step["run"]
        for step in official_job["steps"]
        if step.get("name") == "Start SurrealDB runtime telemetry"
    )
    runtime_gate_script = next(
        step["run"]
        for step in official_job["steps"]
        if step.get("name") == "Gate SurrealDB runtime integrity"
    )

    assert "build_submission_step_1_single_operating_point.py" in workflow
    assert "build_submission_step_2_build_package.py" in workflow
    assert "--receipt-only" in workflow
    assert "--profile longmemeval-v2" in workflow
    assert "moon run longmemeval-v2-artifact-bridge" in workflow
    assert "arm \\" in workflow
    assert '"$OUTPUT_ROOT/longmemeval_v2_${OFFICIAL_TIER}_receipt.json"' in workflow
    assert '--output "$OUTPUT_ROOT/arm_run.json"' in workflow
    assert ".moon/cache/evals/longmemeval-v2-official/arm_run.json" in workflow
    assert "Upload service diagnostics" in workflow
    assert "SIBYL_SURREAL_PATH: rocksdb:///data/sibyl-longmemeval-v2.db" in workflow
    assert official_env["SURREAL_ROCKSDB_BLOCK_CACHE_SIZE"] == "4294967296"
    assert 'actual_cache_size="$(' in telemetry_script
    assert '[[ "$actual_cache_size" != "$expected_cache_size" ]]' in telemetry_script
    assert "Could not parse configuration value" in telemetry_script
    assert "Block cache size: $expected_cache_size" in telemetry_script
    assert "logs are unavailable for configuration validation" in telemetry_script
    assert '[[ ! -s "$telemetry/container.id" ]]' in runtime_gate_script
    assert "sibyld.log" in workflow
    assert "sibyl-worker.log" in workflow
    assert "Capture service diagnostics" in workflow
    assert "surrealdb-inspect.json" in workflow
    assert "surrealdb.log" in workflow
    assert "runner-memory.txt" in workflow
    assert workflow.count('"tools/dev/surreal-runtime-monitor.sh"') == EXPECTED_WORKFLOW_OCCURRENCES
    assert "Start SurrealDB runtime telemetry" in workflow
    assert "Gate SurrealDB runtime integrity" in workflow
    assert "${{ runner.temp }}/surrealdb-runtime" in workflow
    assert "monitor-unexpected-exit" in workflow
    assert "monitor-force-killed" in workflow
    assert "docker-events-orphaned" in workflow
    assert "for _ in {1..70}" in workflow
    assert workflow.index("Start SurrealDB runtime telemetry") < workflow.index(
        "Run official LongMemEval-V2 domain"
    )
    assert workflow.index("Capture service diagnostics") < workflow.index(
        "Gate SurrealDB runtime integrity"
    )
    assert workflow.index("Gate SurrealDB runtime integrity") < workflow.index(
        "Upload service diagnostics"
    )


def test_longmemeval_v2_workflow_forwards_frozen_operating_point() -> None:
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "longmemeval-v2.yml"
    ).read_text(encoding="utf-8")

    assert "official_max_context_chars_per_item:" in workflow
    assert "official_typed_stream_retrieval:" in workflow
    assert "official_typed_stream_limit:" in workflow
    assert "official_note_distillation:" in workflow
    assert "official_note_distillation_model:" in workflow
    assert "official_api_retry_attempts:" in workflow
    assert "official_prompt_build_max_workers:" in workflow
    assert '--max-context-chars-per-item "$MAX_CONTEXT_CHARS_PER_ITEM"' in workflow
    assert '--max-context-total-chars "$MAX_CONTEXT_TOTAL_CHARS"' in workflow
    assert '--operational-note-dedupe-mode "$NOTE_DEDUPE_MODE"' in workflow
    assert '--operational-note-lane-mode "$NOTE_LANE_MODE"' in workflow
    assert '--operational-note-distillation-profile "$NOTE_DISTILLATION_PROFILE"' in workflow
    assert '--typed-stream-limit "$TYPED_STREAM_LIMIT"' in workflow
    assert '--note-distillation-model "$NOTE_DISTILLATION_MODEL"' in workflow
    assert '--api-retry-attempts "$API_RETRY_ATTEMPTS"' in workflow
    assert '--prompt-build-max-workers "$PROMPT_BUILD_MAX_WORKERS"' in workflow
    assert "args+=(--typed-stream-retrieval)" in workflow
    assert "args+=(--note-distillation)" in workflow
    assert "args+=(--render-char-total-treatment)" in workflow
    assert "args+=(--render-group-lanes)" in workflow
    assert "args+=(--render-action-spines)" in workflow


def test_longmemeval_v2_workflow_defaults_to_shared_relevance() -> None:
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "longmemeval-v2.yml"
    ).read_text(encoding="utf-8")

    assert "default: shared_relevance" in workflow
    assert "${{ inputs.official_evidence_composition_mode || 'shared_relevance' }}" in workflow


def test_longmemeval_v2_workflow_labels_accurate_replay_as_developmental() -> None:
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "longmemeval-v2.yml"
    ).read_text(encoding="utf-8")

    assert "composition-v2-developmental-accurate" in workflow
    assert (
        "Developmental replay requires accurate retrieval with three planned queries." in workflow
    )
    assert "Existing frozen validation slices require retrieval_mode=fast." in workflow
    assert "--developmental-replay" in workflow


def test_longmemeval_v2_full_moon_task_installs_official_harness_deps() -> None:
    moon = (Path(__file__).parents[2] / "moon.yml").read_text(encoding="utf-8")
    task = moon.split("bench-longmemeval-v2-official-full:", 1)[1].split(
        "bench-live-smoke:",
        1,
    )[0]

    assert "--with openai-agents" in task
    assert "--with torchvision" in task


def test_longmemeval_v2_release_runbook_names_both_embedding_processes() -> None:
    runbook = (Path(__file__).parents[2] / "docs" / "testing" / "longmemeval-v2.md").read_text(
        encoding="utf-8"
    )

    normalized = " ".join(runbook.split())
    assert "moon run api:serve-local-embeddings" in runbook
    assert "moon run api:worker-local-embeddings" in runbook
    assert "Do not substitute the generic API or worker task" in normalized
    assert "up -d surrealdb-eval redis" in normalized
    assert "SIBYL_REDIS_PORT=6393" in runbook


def test_local_embedding_tasks_share_the_release_runtime() -> None:
    moon = (Path(__file__).parents[2] / "apps" / "api" / "moon.yml").read_text(encoding="utf-8")
    serve = moon.split("  serve-local-embeddings:", 1)[1].split("  # Worker", 1)[0]
    worker = moon.split("  worker-local-embeddings:", 1)[1].split("  # Dependencies", 1)[0]

    for task in (serve, worker):
        assert "SIBYL_COORDINATION_BACKEND: redis" in task
        assert "SIBYL_SURREAL_URL: ws://127.0.0.1:8018/rpc" in task
        assert "SIBYL_SURREAL_USERNAME: root" in task
        assert "SIBYL_SURREAL_PASSWORD: root" in task
        assert "SIBYL_REDIS_HOST: 127.0.0.1" in task
        assert 'SIBYL_REDIS_PORT: "6393"' in task


def test_release_compose_service_pins_the_bounded_surreal_runtime() -> None:
    compose = (Path(__file__).parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    eval_service = compose.split("  surrealdb-eval:", 1)[1].split("  redis:", 1)[0]

    assert "surrealdb/surrealdb:v3.2.3" in eval_service
    assert '["start", "--log", "info", "rocksdb:///data/sibyl.db"]' in eval_service
    assert "SURREAL_BIND: 0.0.0.0:8000" in eval_service
    assert 'SURREAL_ROCKSDB_BLOCK_CACHE_SIZE: "8589934592"' in eval_service
    assert 'SURREAL_ROCKSDB_WRITE_BUFFER_SIZE: "134217728"' in eval_service
    assert 'SURREAL_ROCKSDB_MAX_WRITE_BUFFER_NUMBER: "4"' in eval_service
    assert '"127.0.0.1:8018:8000"' in eval_service
    assert "${SIBYL_RELEASE_ROOT:-./.moon/cache/surreal-eval}/surreal" in eval_service


def test_default_compose_service_forwards_an_explicit_rocksdb_cache_size() -> None:
    compose = yaml.safe_load(
        (Path(__file__).parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    )
    environment = compose["services"]["surrealdb"]["environment"]

    assert environment == ["SURREAL_ROCKSDB_BLOCK_CACHE_SIZE"]


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
