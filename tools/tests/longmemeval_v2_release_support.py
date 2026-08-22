from __future__ import annotations

import gzip
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_contract as contract
from benchmarks import longmemeval_v2_release_inputs as inputs
from benchmarks import longmemeval_v2_release_memory as memory

MATCHED_PASS_INDEX = 4


def runtime_spec() -> dict[str, Any]:
    return {
        "api_url": "http://127.0.0.1:3334/api",
        "allow_localhost": True,
        "reader_base_url": "https://openrouter.ai/api/v1",
        "reader_model": "qwen/qwen3.5-9b",
        "reader_api_key_env": "OPENROUTER_API_KEY",
        "reader_max_concurrent_requests": 16,
        "reader_retry_attempts": 4,
        "evaluator_model": "gpt-5.2",
        "evaluator_api_key_env": "OPENAI_API_KEY",
        "evidence_composition_mode": "shared_relevance",
        "retrieval_max_planned_queries": 3,
        "max_context_chars_per_item": 18_000,
        "typed_stream_limit": 8,
        "note_distillation_model": "gpt-5.4-nano",
        "api_retry_attempts": 3,
        "prompt_build_max_workers": 1,
    }


def manifest(
    *,
    phase: str,
    pass_id: str,
    seed: int,
    role: str = "machine",
    preregistration: str = "",
) -> dict[str, Any]:
    treatment = role == "render_treatment"
    naive = role == "naive"
    retrieval_mode = "naive" if naive else "fast"
    dedupe_mode = "source_kind" if treatment else "source"
    lane_mode = "additive" if treatment else "reserved"
    distillation_profile = "render_v1" if treatment else "baseline"
    total_chars = 72_000 if treatment else 60_000
    return {
        "experiment_id": "sibyl-v1.3-release",
        "experiment_phase": phase,
        "pass_id": pass_id,
        "pass_seed": seed,
        "arm_role": role,
        "substrate": "naive" if naive else "machine",
        "preregistration_sha256": preregistration,
        "max_spend_usd": 3.6 if treatment else 3.0,
        "retrieval_mode": retrieval_mode,
        "max_context_total_chars": total_chars,
        "operational_note_dedupe_mode": dedupe_mode,
        "operational_note_lane_mode": lane_mode,
        "operational_note_distillation_profile": distillation_profile,
        "render_group_lanes": treatment,
        "render_action_spines": treatment,
        "configuration": {
            "retrieval_mode": retrieval_mode,
            "max_context_chars_per_item": 18_000,
            "operational_note_dedupe_mode": dedupe_mode,
            "operational_note_lane_mode": lane_mode,
            "operational_note_distillation_profile": distillation_profile,
            "render_group_lanes": treatment,
            "render_action_spines": treatment,
        },
        "geometry": {
            "max_context_items": 8,
            "max_context_chars_per_item": 18_000,
            "max_context_total_chars": total_chars,
        },
    }


def aa_spec() -> dict[str, Any]:
    passes: list[dict[str, Any]] = []
    for index, seed in enumerate((1301, 1302, 1303), start=1):
        pass_id = f"aa-{index}"
        passes.append(
            {
                "kind": "paired",
                "pass_id": pass_id,
                "seed": seed,
                "arms": [
                    {
                        "arm_id": f"{pass_id}-left",
                        "memory_source": "build_baseline" if index == 1 else "baseline",
                        "manifest": manifest(phase="aa", pass_id=pass_id, seed=seed),
                    },
                    {
                        "arm_id": f"{pass_id}-right",
                        "memory_source": "baseline",
                        "manifest": manifest(phase="aa", pass_id=pass_id, seed=seed),
                    },
                ],
            }
        )
    return {
        "schema_version": contract.STAGE_SPEC_SCHEMA_VERSION,
        "experiment_id": "sibyl-v1.3-release",
        "stage": "aa",
        "mode": "initial",
        "runtime": runtime_spec(),
        "memory_roots": {"baseline": None, "render": None},
        "upstream": {
            "aa_authorization": None,
            "preregistration_authorization": None,
        },
        "passes": passes,
    }


def arm_contract(arm: dict[str, Any]) -> dict[str, Any]:
    arm_manifest = arm["manifest"]
    return {
        "substrate": arm_manifest["substrate"],
        "configuration": arm_manifest["configuration"],
        "geometry": arm_manifest["geometry"],
    }


def aa_extension_spec() -> dict[str, Any]:
    spec = aa_spec()
    spec["mode"] = "extension"
    spec["passes"] = []
    for index, seed in enumerate((1304, 1305), start=4):
        pass_id = f"aa-{index}"
        spec["passes"].append(
            {
                "kind": "paired",
                "pass_id": pass_id,
                "seed": seed,
                "arms": [
                    {
                        "arm_id": f"{pass_id}-left",
                        "memory_source": "baseline",
                        "manifest": manifest(phase="aa", pass_id=pass_id, seed=seed),
                    },
                    {
                        "arm_id": f"{pass_id}-right",
                        "memory_source": "baseline",
                        "manifest": manifest(phase="aa", pass_id=pass_id, seed=seed),
                    },
                ],
            }
        )
    return spec


def anchor_spec() -> dict[str, Any]:
    anchor_id = "anchor-1"
    seed = 1401
    spec = aa_spec()
    spec["stage"] = "anchor"
    spec["mode"] = "standard"
    spec["passes"] = [
        {
            "kind": "anchor",
            "pass_id": anchor_id,
            "seed": seed,
            "arms": [
                {
                    "arm_id": "anchor-machine",
                    "memory_source": "baseline",
                    "manifest": manifest(
                        phase="anchor",
                        pass_id=anchor_id,
                        seed=seed,
                    ),
                }
            ],
        }
    ]
    return spec


def race_spec() -> tuple[dict[str, Any], dict[str, Any]]:
    preregistration = "b" * 64
    passes: list[dict[str, Any]] = []
    for index, seed in enumerate((1501, 1502, 1503, 1504), start=1):
        pass_id = f"race-{index}"
        passes.append(
            {
                "kind": "matched" if index == MATCHED_PASS_INDEX else "paired",
                "pass_id": pass_id,
                "seed": seed,
                "arms": [
                    {
                        "arm_id": f"{pass_id}-machine",
                        "memory_source": "baseline",
                        "manifest": manifest(
                            phase="race",
                            pass_id=pass_id,
                            seed=seed,
                            preregistration=preregistration,
                        ),
                    },
                    {
                        "arm_id": f"{pass_id}-naive",
                        "memory_source": "baseline",
                        "manifest": manifest(
                            phase="race",
                            pass_id=pass_id,
                            seed=seed,
                            role="naive",
                            preregistration=preregistration,
                        ),
                    },
                ],
            }
        )
    machine, naive = passes[0]["arms"]
    contracts = {
        "machine_configuration": deepcopy(machine["manifest"]["configuration"]),
        "naive_configuration": deepcopy(naive["manifest"]["configuration"]),
        "shipping_geometry": {
            "machine": deepcopy(machine["manifest"]["geometry"]),
            "naive": deepcopy(naive["manifest"]["geometry"]),
        },
        "matched_geometry": deepcopy(passes[-1]["arms"][0]["manifest"]["geometry"]),
    }
    spec = aa_spec()
    spec.update({"stage": "race", "mode": "standard", "passes": passes})
    return spec, contracts


def render_spec() -> tuple[dict[str, Any], dict[str, Any]]:
    preregistration = "c" * 64
    passes: list[dict[str, Any]] = []
    for index, seed in enumerate((1601, 1602, 1603), start=1):
        pass_id = f"render-{index}"
        passes.append(
            {
                "kind": "paired",
                "pass_id": pass_id,
                "seed": seed,
                "arms": [
                    {
                        "arm_id": f"{pass_id}-control",
                        "memory_source": "baseline",
                        "manifest": manifest(
                            phase="render",
                            pass_id=pass_id,
                            seed=seed,
                            role="render_control",
                            preregistration=preregistration,
                        ),
                    },
                    {
                        "arm_id": f"{pass_id}-treatment",
                        "memory_source": "build_render" if index == 1 else "render",
                        "manifest": manifest(
                            phase="render",
                            pass_id=pass_id,
                            seed=seed,
                            role="render_treatment",
                            preregistration=preregistration,
                        ),
                    },
                ],
            }
        )
    control, treatment = passes[0]["arms"]
    contracts = {
        "control_configuration": deepcopy(control["manifest"]["configuration"]),
        "treatment_configuration": deepcopy(treatment["manifest"]["configuration"]),
        "control_geometry": deepcopy(control["manifest"]["geometry"]),
        "treatment_geometry": deepcopy(treatment["manifest"]["geometry"]),
    }
    spec = aa_spec()
    spec.update({"stage": "render", "mode": "standard", "passes": passes})
    return spec, contracts


def trajectory(domain: str) -> dict[str, Any]:
    trajectory_id = f"{domain}-t1"
    return {
        "id": trajectory_id,
        "domain": domain,
        "environment": "browsergym",
        "goal": f"Finish the {domain} task",
        "outcome": "success",
        "start_url": "https://example.test/start",
        "states": [
            {
                "state_index": 0,
                "step": 0,
                "url": "https://example.test/start",
                "action": "click('submit')",
                "thought": "Submit the completed form",
                "accessibility_tree": "[submit] button 'Submit'",
                "screenshot": None,
            }
        ],
    }


def write_dataset(root: Path) -> None:
    (root / "haystacks").mkdir(parents=True)
    rows = [
        {
            "id": "web-1",
            "domain": "web",
            "environment": "browsergym",
            "question_type": "single-session-user",
            "question": "What happened in the web task?",
            "answer": "success",
            "eval_function": "exact_match",
        },
        {
            "id": "enterprise-1",
            "domain": "enterprise",
            "environment": "browsergym",
            "question_type": "single-session-user",
            "question": "What happened in the enterprise task?",
            "answer": "success",
            "eval_function": "exact_match",
        },
    ]
    (root / "questions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    (root / "trajectories.jsonl").write_text(
        "".join(json.dumps(trajectory(domain)) + "\n" for domain in inputs.DOMAINS),
        encoding="utf-8",
    )
    (root / "haystacks" / "lme_v2_small.json").write_text(
        json.dumps({"web-1": ["web-t1"], "enterprise-1": ["enterprise-t1"]}),
        encoding="utf-8",
    )


def _write_gzip_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def write_saved_memory(
    root: Path,
    *,
    domain: str,
    source_sha: str,
    project_id: str | None = None,
    run_id: str | None = None,
    api_url: str = "http://127.0.0.1:3334/api",
    include_screenshot_refs: bool = False,
) -> None:
    root.mkdir(parents=True)
    trajectory_id = f"{domain}-t1"
    project_id = project_id or f"project-{domain}"
    run_id = run_id or f"memory-{domain}"
    config = {
        "memory_type": "sibyl_live_api",
        "memory_params": {
            "api_url": api_url,
            "longmemeval_v2_domain": domain,
            "project_id": project_id,
            "run_id": run_id,
            "chunking_mode": "state",
            "content_max_chars": 18_000,
            "include_screenshot_refs": include_screenshot_refs,
            "runner_provenance": {
                "sibyl_commit": source_sha,
                "git_dirty": False,
                "git_status": "clean",
            },
        },
    }
    (root / "memory_config.json").write_text(
        json.dumps(config, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    catalog, spines = memory._expected_catalog_and_spines(
        {trajectory_id: trajectory(domain)},
        params=config["memory_params"],
    )
    _write_gzip_jsonl(root / "chunk_catalog.jsonl.gz", catalog)
    _write_gzip_jsonl(root / "action_spines.jsonl.gz", spines)
    _write_gzip_jsonl(root / "distillation_receipts.jsonl.gz", [])
    manifest_payload = {
        "schema_version": memory.MEMORY_MANIFEST_SCHEMA_VERSION,
        "api_url": api_url,
        "longmemeval_v2_domain": domain,
        "project_id": project_id,
        "run_id": run_id,
        "chunking_mode": "state",
        "content_max_chars": 18_000,
        "inserted_trajectories": 1,
        "created_entities": len(catalog),
        "ingest_api_runtime": {
            "status": "healthy",
            "version": "1.3.0",
            "runtime": {
                "commit": source_sha,
                "git_dirty": False,
                "git_status": "clean",
            },
        },
        "ingest_embedding_usage": {},
        "completed_trajectory_ids": [trajectory_id],
        "operational_trajectory_ids": [trajectory_id],
        "pending_embedding_job_ids": [],
        "pending_projection_job_ids": [],
        "pending_note_distillation_job_ids": [],
        "ingest_note_distillation_usage": {},
        "ingest_note_distillation_receipt_count": 0,
        "ingest_note_distillation_receipt_set_sha256": inputs.rig.canonical_sha256({}),
        "ingest_finalized": True,
        "memory_config_sha256": inputs.sha256_file(root / "memory_config.json"),
        "chunk_catalog_sha256": inputs.sha256_file(root / "chunk_catalog.jsonl.gz"),
        "action_spine_count": len(spines),
        "action_spines_sha256": inputs.sha256_file(root / "action_spines.jsonl.gz"),
        "distillation_receipt_count": 0,
        "distillation_receipts_sha256": inputs.sha256_file(root / "distillation_receipts.jsonl.gz"),
    }
    (root / "memory_manifest.json").write_text(
        json.dumps(manifest_payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
