from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_ci as release_ci
from benchmarks import longmemeval_v2_release_contract as contract
from benchmarks.longmemeval_v2_release_inputs import StagePlanError
from tools.bench import longmemeval_v2_rig as rig
from tools.tests.test_longmemeval_v2_rig import (
    QUESTION_COUNT_PER_DOMAIN,
    _arm,
    _paired_pass,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DISPATCHING_JOB_COUNT = 2
CONTROLLER_ACCEPTANCE_GATE_COUNT = 2


@pytest.fixture(autouse=True)
def _use_synthetic_official_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    question_ids = {
        domain: [f"{domain}-{index}" for index in range(QUESTION_COUNT_PER_DOMAIN)]
        for domain in rig.DOMAINS
    }
    monkeypatch.setattr(
        rig,
        "OFFICIAL_SMALL_QUESTION_COUNTS",
        {domain: len(ids) for domain, ids in question_ids.items()},
    )
    monkeypatch.setattr(
        rig,
        "OFFICIAL_SMALL_QUESTION_IDS_SHA256",
        {domain: rig.canonical_sha256(sorted(ids)) for domain, ids in question_ids.items()},
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_map(passes: list[dict[str, Any]]) -> dict[str, Any]:
    runs = {
        f"{paired['pass_id']}-{side}": paired["arms"][side]["execution"]["run_id"]
        for paired in passes
        for side in ("left", "right")
    }
    payload = {
        "schema_version": release_ci.RUN_MAP_SCHEMA_VERSION,
        "experiment_id": "experiment-v1.3",
        "orchestration_id": "release-aa-test",
        "source": {
            "repository": "hyperb1iss/sibyl",
            "ref": "refs/heads/main",
            "sha": "a" * 40,
        },
        "builder_run_id": runs[release_ci.BUILDER_ARM_ID],
        "runs": runs,
    }
    payload["run_map_sha256"] = rig.canonical_sha256(payload)
    return payload


def _paired_passes() -> list[dict[str, Any]]:
    return [
        _paired_pass(
            pass_id,
            seed=seed,
            left=_arm("machine", mode="fast", accuracy=0.7),
            right=_arm("machine", mode="fast", accuracy=0.7),
        )
        for pass_id, seed in release_ci.PASS_SEEDS.items()
    ]


def _bundle(tmp_path: Path) -> Path:
    passes = _paired_passes()
    artifacts = tmp_path / "artifacts"
    for paired in passes:
        for side in ("left", "right"):
            arm_id = f"{paired['pass_id']}-{side}"
            _write_json(artifacts / arm_id / "arm_run.json", paired["arms"][side])
    run_map_path = tmp_path / "run-map.json"
    _write_json(run_map_path, _run_map(passes))
    bundle = tmp_path / "bundle"
    release_ci.aggregate_aa_bundle(
        artifacts_root=artifacts,
        run_map_path=run_map_path,
        output_root=bundle,
    )
    return bundle


def test_dispatch_plan_matches_the_fixed_release_contract() -> None:
    payload = release_ci.build_dispatch_plan(
        experiment_id="sibyl-v1.3-ci-aa",
        orchestration_id="release-aa-123",
        source={
            "repository": "hyperb1iss/sibyl",
            "ref": "refs/heads/main",
            "sha": "a" * 40,
        },
    )

    assert [arm["arm_id"] for arm in payload["arms"]] == list(release_ci.ARM_IDS)
    assert payload["arms"][0]["memory_mode"] == "save"
    assert all(arm["memory_mode"] == "load" for arm in payload["arms"][1:])
    assert [arm["seed"] for arm in payload["arms"]] == [1301, 1301, 1302, 1302, 1303, 1303]
    for arm in payload["arms"]:
        manifest = json.loads(arm["official_arm_manifest_json"])
        assert manifest["max_spend_usd"] == contract.RELEASE_ROLE_CAPS_USD["machine"]
        assert "configuration" not in manifest
        assert "geometry" not in manifest


def test_run_map_rejects_reused_workflow_execution() -> None:
    passes = _paired_passes()
    run_map = _run_map(passes)
    run_map["runs"]["aa-1-right"] = run_map["runs"]["aa-1-left"]
    unsigned = {key: value for key, value in run_map.items() if key != "run_map_sha256"}
    run_map["run_map_sha256"] = rig.canonical_sha256(unsigned)

    with pytest.raises(StagePlanError, match="reused a workflow run ID"):
        release_ci.require_run_map(run_map)


def test_run_map_binds_the_exact_dispatch_plan() -> None:
    dispatch = release_ci.build_dispatch_plan(
        experiment_id="experiment-v1.3",
        orchestration_id="release-aa-test",
        source={
            "repository": "hyperb1iss/sibyl",
            "ref": "refs/heads/main",
            "sha": "a" * 40,
        },
    )
    runs = {arm_id: str(index) for index, arm_id in enumerate(release_ci.ARM_IDS, start=1)}

    run_map = release_ci.build_run_map(dispatch_plan=dispatch, runs=runs)

    assert run_map["builder_run_id"] == "1"
    assert run_map["runs"] == runs


def test_aggregate_and_import_publish_portable_authority(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    output = tmp_path / "aa-authorization.json"

    authority = release_ci.import_aa_bundle(bundle_root=bundle, output=output)

    assert output.is_file()
    assert authority["status"] == "PASS"
    assert Path(authority["source_receipt"]["path"]) == bundle / "aa_receipt.json"
    assert [Path(item["paired_pass_artifact"]["path"]).name for item in authority["passes"]] == [
        "aa-1.json",
        "aa-2.json",
        "aa-3.json",
    ]


def test_import_rejects_tampered_download(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    with (bundle / "passes" / "aa-2.json").open("a", encoding="utf-8") as handle:
        handle.write("\n")

    with pytest.raises(StagePlanError, match="bytes differ"):
        release_ci.import_aa_bundle(
            bundle_root=bundle,
            output=tmp_path / "aa-authorization.json",
        )


def test_import_rejects_self_consistent_run_map_splice(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    run_map_path = bundle / "run_map.json"
    run_map = json.loads(run_map_path.read_text(encoding="utf-8"))
    run_map["source"]["sha"] = "b" * 40
    unsigned_run_map = {key: value for key, value in run_map.items() if key != "run_map_sha256"}
    run_map["run_map_sha256"] = rig.canonical_sha256(unsigned_run_map)
    _write_json(run_map_path, run_map)

    manifest_path = bundle / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["run_map"]["sha256"] = release_ci.sha256_file(run_map_path)
    manifest["run_map"]["size_bytes"] = run_map_path.stat().st_size
    unsigned_manifest = {
        key: value for key, value in manifest.items() if key != "bundle_manifest_sha256"
    }
    manifest["bundle_manifest_sha256"] = rig.canonical_sha256(unsigned_manifest)
    _write_json(manifest_path, manifest)

    with pytest.raises(StagePlanError, match="manifest differs from its run map"):
        release_ci.import_aa_bundle(
            bundle_root=bundle,
            output=tmp_path / "aa-authorization.json",
        )


def test_aggregate_rejects_arm_run_id_outside_the_run_map(tmp_path: Path) -> None:
    passes = _paired_passes()
    artifacts = tmp_path / "artifacts"
    for paired in passes:
        for side in ("left", "right"):
            arm_id = f"{paired['pass_id']}-{side}"
            arm = paired["arms"][side]
            if arm_id == "aa-2-right":
                arm["execution"]["run_id"] = "999999"
            _write_json(artifacts / arm_id / "arm_run.json", arm)
    run_map_path = tmp_path / "run-map.json"
    _write_json(run_map_path, _run_map(_paired_passes()))

    with pytest.raises(StagePlanError, match="execution differs from its run map"):
        release_ci.aggregate_aa_bundle(
            artifacts_root=artifacts,
            run_map_path=run_map_path,
            output_root=tmp_path / "bundle",
        )


def test_import_rejects_bundle_path_traversal(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    manifest_path = bundle / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["passes"][0]["path"] = "../aa-1.json"
    unsigned = {key: value for key, value in manifest.items() if key != "bundle_manifest_sha256"}
    manifest["bundle_manifest_sha256"] = rig.canonical_sha256(unsigned)
    _write_json(manifest_path, manifest)

    with pytest.raises(StagePlanError, match="escapes the CI bundle"):
        release_ci.import_aa_bundle(
            bundle_root=bundle,
            output=tmp_path / "aa-authorization.json",
        )


def test_release_workflows_preserve_distributed_execution_contract() -> None:
    child = (REPO_ROOT / ".github/workflows/longmemeval-v2.yml").read_text(encoding="utf-8")
    controller = (REPO_ROOT / ".github/workflows/longmemeval-v2-release-aa.yml").read_text(
        encoding="utf-8"
    )

    for fragment in (
        "official_ci_context_json:",
        '"memory_source_run_id"',
        "needs: [official-preflight, official-memory-source]",
        "environment: longmemeval-paid",
        "SIBYL_GRAPH_EMBEDDING_PROVIDER: local",
        "SIBYL_COORDINATION_BACKEND: redis",
        "sentence-transformers/all-MiniLM-L6-v2",
        "--save-memory --checkpoint-dir",
        "--load-memory-dir",
        "database_snapshot_manifest.json",
        "longmemeval-v2-frozen-memory-",
        "Sibyl services did not stop before the database snapshot",
        "github-token: ${{ github.token }}",
        "${{ github.run_id }}",
    ):
        assert fragment in child
    for fragment in (
        "actions: read",
        "actions: write",
        'workflows: ["LongMemEval V2"]',
        "identify-workflow-run:",
        "needs: identify-workflow-run",
        "needs.identify-workflow-run.outputs.orchestration_id",
        'orchestration_id="aa-${GITHUB_RUN_ID}"',
        "needs.identify-workflow-run.outputs.arm_id == 'aa-1-left'",
        "needs.identify-workflow-run.outputs.arm_id == 'aa-1-right'",
        "needs.identify-workflow-run.outputs.arm_id == 'aa-3-right'",
        "dispatch_arm aa-1-left save",
        "Dispatch five frozen baseline consumers",
        'memory_mode: "load"',
        "Reusing child workflow",
        'official_ci_context_json="$ci_context"',
        'official_dataset_revision="$OFFICIAL_DATASET_REVISION"',
        "official_reader_model=qwen/qwen3.5-9b",
        "official_evaluator_model=gpt-5.2",
        "run-map",
        "aggregate",
        "longmemeval-v2-release-aa-run-map-",
        'dispatcher_run_id="$(sort -n <<< "$dispatcher_runs" | tail -1)"',
        'echo "ready=false" >> "$GITHUB_OUTPUT"',
        "Recover sealed controller state",
        "Upload authoritative A/A bundle",
    ):
        assert fragment in controller
    assert controller.count("actions: write") == DISPATCHING_JOB_COUNT
    assert "github.event.workflow_run.head_sha }}\n  cancel-in-progress" not in controller
    assert controller.count("needs: identify-workflow-run") == 2
    assert (
        controller.count('(.conclusion | IN("success", "failure"))')
        == CONTROLLER_ACCEPTANCE_GATE_COUNT
    )
    assert "gh run watch" not in child
    assert controller.index("Upload sealed controller plan") < controller.index(
        "Dispatch the frozen baseline builder"
    )
    assert "Waiting for ${pending} of 6 paid arms." not in controller
