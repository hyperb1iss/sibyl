"""Exact saved-memory validation for LongMemEval-V2 release stages."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from benchmarks.longmemeval_v2_memory.render_bundle import build_action_spine
from benchmarks.longmemeval_v2_memory.sibyl_memory import build_entity_payloads_for_trajectory
from benchmarks.longmemeval_v2_release_inputs import (
    DOMAINS,
    StagePlanError,
    bind_artifact,
    load_json,
    require_exact_keys,
    require_string,
)
from tools.bench import longmemeval_v2_rig as rig

MEMORY_ARTIFACT_NAMES = (
    "memory_config.json",
    "chunk_catalog.jsonl.gz",
    "memory_manifest.json",
    "action_spines.jsonl.gz",
    "distillation_receipts.jsonl.gz",
)
MEMORY_MANIFEST_SCHEMA_VERSION = "sibyl-longmemeval-v2-memory-state-v1"
BASELINE_CONTENT_MAX_CHARS = 18_000
MEMORY_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "api_url",
        "longmemeval_v2_domain",
        "project_id",
        "run_id",
        "chunking_mode",
        "content_max_chars",
        "inserted_trajectories",
        "created_entities",
        "ingest_api_runtime",
        "ingest_embedding_usage",
        "completed_trajectory_ids",
        "operational_trajectory_ids",
        "pending_embedding_job_ids",
        "pending_projection_job_ids",
        "pending_note_distillation_job_ids",
        "ingest_note_distillation_usage",
        "ingest_note_distillation_receipt_count",
        "ingest_note_distillation_receipt_set_sha256",
        "ingest_finalized",
        "memory_config_sha256",
        "chunk_catalog_sha256",
        "action_spine_count",
        "action_spines_sha256",
        "distillation_receipt_count",
        "distillation_receipts_sha256",
    }
)
SAVED_MEMORY_SECRET_KEYS = frozenset(
    {"api_token", "api_credentials_path", "refresh_token", "email", "password"}
)


def _canonical_root(path: Path, *, name: str) -> Path:
    expanded = path.expanduser()
    resolved = expanded.resolve()
    if not expanded.is_absolute() or expanded != resolved or not resolved.is_dir():
        raise StagePlanError(f"{name} must be one canonical non-symlinked directory")
    return resolved


def _contained_artifact(root: Path, relative: str, *, name: str) -> dict[str, Any]:
    candidate = root / relative
    resolved = candidate.resolve()
    if candidate != resolved or not resolved.is_relative_to(root):
        raise StagePlanError(f"{name} escapes its canonical root through a symlink")
    return bind_artifact(resolved, name=name)


def _gzip_jsonl(path: Path, *, name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise StagePlanError(f"{name}:{line_number} is not an object")
                rows.append(row)
    except (OSError, EOFError, json.JSONDecodeError) as exc:
        raise StagePlanError(f"{name} is not valid gzip JSONL") from exc
    return rows


def _required_trajectory_ids(dataset: dict[str, Any], *, domain: str) -> list[str]:
    question_ids: list[str] = []
    questions_path = Path(dataset["artifacts"]["questions"]["path"])
    with questions_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("domain") == domain:
                question_ids.append(str(row["id"]))
    haystack = load_json(Path(dataset["artifacts"]["small_haystack"]["path"]))
    required: set[str] = set()
    for question_id in question_ids:
        trajectory_ids = haystack.get(question_id)
        if not isinstance(trajectory_ids, list) or any(
            not isinstance(trajectory_id, str) or not trajectory_id
            for trajectory_id in trajectory_ids
        ):
            raise StagePlanError(f"dataset haystack is invalid for {domain}.{question_id}")
        required.update(trajectory_ids)
    if not required:
        raise StagePlanError(f"dataset has no required trajectories for {domain}")
    return sorted(required)


def _trajectory_rows(
    dataset: dict[str, Any],
    *,
    expected_ids: list[str],
) -> dict[str, dict[str, Any]]:
    expected = set(expected_ids)
    selected: dict[str, dict[str, Any]] = {}
    path = Path(dataset["artifacts"]["trajectories"]["path"])
    try:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise StagePlanError(f"trajectories.jsonl:{line_number} is not an object")
                trajectory_id = row.get("id")
                if trajectory_id not in expected:
                    continue
                if trajectory_id in selected:
                    raise StagePlanError(f"duplicate required trajectory {trajectory_id}")
                selected[str(trajectory_id)] = row
    except (OSError, json.JSONDecodeError) as exc:
        raise StagePlanError("dataset trajectories.jsonl is unreadable") from exc
    if set(selected) != expected:
        raise StagePlanError("dataset omits required saved-memory trajectories")
    return selected


def _expected_catalog_and_spines(
    trajectories: dict[str, dict[str, Any]],
    *,
    params: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    catalog: list[dict[str, Any]] = []
    spines: list[dict[str, Any]] = []
    for trajectory_id in sorted(trajectories):
        trajectory = trajectories[trajectory_id]
        payloads = build_entity_payloads_for_trajectory(
            trajectory,
            project_id=str(params["project_id"]),
            run_id=str(params["run_id"]),
            content_max_chars=int(params["content_max_chars"]),
            chunking_mode=str(params["chunking_mode"]),
            include_screenshot_refs=bool(params.get("include_screenshot_refs", False)),
        )
        for payload in payloads:
            metadata = dict(payload["metadata"])
            chunk_index = int(metadata["longmemeval_v2_chunk_index"])
            catalog.append(
                {
                    "id": f"catalog:{trajectory_id}:{chunk_index}",
                    "type": payload["entity_type"],
                    "name": payload["name"],
                    "content": payload["content"],
                    "score": 0.0,
                    "result_origin": "graph",
                    "metadata": metadata,
                }
            )
        spine = build_action_spine(trajectory)
        if spine is not None:
            spines.append(dict(spine))
    return catalog, spines


def _require_memory_config(
    raw: dict[str, Any],
    *,
    name: str,
    domain: str,
    source: dict[str, str],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    require_exact_keys(raw, frozenset({"memory_type", "memory_params"}), name=name)
    params = raw.get("memory_params")
    if raw.get("memory_type") != "sibyl_live_api" or not isinstance(params, dict):
        raise StagePlanError(f"{name} is not a Sibyl live API memory")
    if SAVED_MEMORY_SECRET_KEYS & set(params):
        raise StagePlanError(f"{name} contains secret-bearing fields")
    expected_provenance = {
        "sibyl_commit": source["sha"],
        "git_dirty": False,
        "git_status": "clean",
    }
    if params.get("runner_provenance") != expected_provenance:
        raise StagePlanError(f"{name} differs from the sealed Sibyl source")
    project_id = require_string(params.get("project_id"), name=f"{name}.project_id")
    run_id = require_string(params.get("run_id"), name=f"{name}.run_id")
    api_url = require_string(params.get("api_url"), name=f"{name}.api_url")
    parsed_api_url = urlsplit(api_url)
    if (
        api_url != runtime["api_url"]
        or parsed_api_url.username is not None
        or parsed_api_url.password is not None
        or parsed_api_url.query
        or parsed_api_url.fragment
    ):
        raise StagePlanError(f"{name} API URL differs from the sealed local runtime")
    if params.get("longmemeval_v2_domain") != domain:
        raise StagePlanError(f"{name} domain identity differs from {domain}")
    if not run_id.endswith(f"-{domain}"):
        raise StagePlanError(f"{name} run ID is not bound to {domain}")
    if (
        params.get("chunking_mode") != "state"
        or params.get("content_max_chars") != BASELINE_CONTENT_MAX_CHARS
        or params.get("include_screenshot_refs", False) is not False
    ):
        raise StagePlanError(f"{name} replay configuration differs from the release baseline")
    if (
        params.get("operational_note_distillation_profile", "baseline") != "baseline"
        or params.get("render_group_lanes", False) is not False
        or params.get("render_action_spines", False) is not False
    ):
        raise StagePlanError(f"{name} is not a baseline memory artifact")
    return {**params, "project_id": project_id, "run_id": run_id}


def _require_manifest(
    raw: dict[str, Any],
    *,
    name: str,
    params: dict[str, Any],
    artifacts: dict[str, dict[str, Any]],
    completed: list[str],
    source: dict[str, str],
    domain: str,
    catalog_count: int,
    spine_count: int,
) -> None:
    require_exact_keys(raw, MEMORY_MANIFEST_KEYS, name=name)
    if (
        raw.get("schema_version") != MEMORY_MANIFEST_SCHEMA_VERSION
        or raw.get("ingest_finalized") is not True
        or raw.get("completed_trajectory_ids") != completed
        or raw.get("operational_trajectory_ids") != completed
        or raw.get("inserted_trajectories") != len(completed)
        or raw.get("created_entities") != catalog_count
        or any(
            raw.get(key) != []
            for key in (
                "pending_embedding_job_ids",
                "pending_projection_job_ids",
                "pending_note_distillation_job_ids",
            )
        )
    ):
        raise StagePlanError(f"{name} is incomplete or has wrong lineage")
    if (
        raw.get("memory_config_sha256") != artifacts["memory_config.json"]["sha256"]
        or raw.get("chunk_catalog_sha256") != artifacts["chunk_catalog.jsonl.gz"]["sha256"]
        or raw.get("action_spines_sha256") != artifacts["action_spines.jsonl.gz"]["sha256"]
        or raw.get("distillation_receipts_sha256")
        != artifacts["distillation_receipts.jsonl.gz"]["sha256"]
    ):
        raise StagePlanError(f"{name} artifact hashes differ")
    if any(
        raw.get(key) != params.get(key)
        for key in (
            "api_url",
            "longmemeval_v2_domain",
            "project_id",
            "run_id",
            "chunking_mode",
            "content_max_chars",
        )
    ):
        raise StagePlanError(f"{name} and memory config identities differ")
    if raw.get("longmemeval_v2_domain") != domain:
        raise StagePlanError(f"{name} domain identity differs from {domain}")
    if (
        raw.get("action_spine_count") != spine_count
        or raw.get("distillation_receipt_count") != 0
        or raw.get("ingest_note_distillation_receipt_count") != 0
        or raw.get("ingest_note_distillation_receipt_set_sha256") != rig.canonical_sha256({})
        or raw.get("ingest_note_distillation_usage") != {}
    ):
        raise StagePlanError(f"{name} baseline sidecar receipt is invalid")
    runtime = raw.get("ingest_api_runtime")
    identity = runtime.get("runtime") if isinstance(runtime, dict) else None
    if (
        not isinstance(runtime, dict)
        or runtime.get("status") != "healthy"
        or not isinstance(identity, dict)
        or identity.get("commit") != source["sha"]
        or identity.get("git_dirty") is not False
        or identity.get("git_status") != "clean"
    ):
        raise StagePlanError(f"{name} runtime differs from the sealed Sibyl source")


def _validate_saved_memory(
    path: Path,
    *,
    name: str,
    domain: str,
    expected_trajectory_ids: list[str],
    dataset: dict[str, Any],
    source: dict[str, str],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    artifacts = {
        filename: _contained_artifact(path, filename, name=f"{name}.{filename}")
        for filename in MEMORY_ARTIFACT_NAMES
    }
    config = load_json(Path(artifacts["memory_config.json"]["path"]))
    params = _require_memory_config(
        config,
        name=f"{name} memory config",
        domain=domain,
        source=source,
        runtime=runtime,
    )
    trajectories = _trajectory_rows(dataset, expected_ids=expected_trajectory_ids)
    expected_catalog, expected_spines = _expected_catalog_and_spines(
        trajectories,
        params=params,
    )
    catalog = _gzip_jsonl(
        Path(artifacts["chunk_catalog.jsonl.gz"]["path"]),
        name=f"{name} chunk catalog",
    )
    spines = _gzip_jsonl(
        Path(artifacts["action_spines.jsonl.gz"]["path"]),
        name=f"{name} action spines",
    )
    distillation = _gzip_jsonl(
        Path(artifacts["distillation_receipts.jsonl.gz"]["path"]),
        name=f"{name} distillation receipts",
    )
    if catalog != expected_catalog:
        raise StagePlanError(f"{name} chunk catalog differs from deterministic source chunks")
    if spines != expected_spines:
        raise StagePlanError(f"{name} action spines differ from deterministic trajectories")
    if distillation:
        raise StagePlanError(f"{name} baseline distillation receipts must be empty")
    manifest = load_json(Path(artifacts["memory_manifest.json"]["path"]))
    _require_manifest(
        manifest,
        name=f"{name} memory manifest",
        params=params,
        artifacts=artifacts,
        completed=expected_trajectory_ids,
        source=source,
        domain=domain,
        catalog_count=len(catalog),
        spine_count=len(spines),
    )
    return {
        "domain": domain,
        "path": str(path),
        "project_id": params["project_id"],
        "run_id": params["run_id"],
        "artifacts": artifacts,
        "manifest": manifest,
    }


def _memory_root(
    raw: object,
    *,
    name: str,
    dataset: dict[str, Any],
    source: dict[str, str],
    runtime: dict[str, Any],
) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) != set(DOMAINS):
        raise StagePlanError(f"{name} memory root must cover both domains")
    paths = {
        domain: _canonical_root(
            Path(require_string(raw.get(domain), name=f"{name}.{domain}")),
            name=f"{name}.{domain} memory root",
        )
        for domain in DOMAINS
    }
    if paths["web"] == paths["enterprise"]:
        raise StagePlanError(f"{name} memory domains must use distinct canonical roots")
    domains = {
        domain: _validate_saved_memory(
            path,
            name=f"{name}.{domain}",
            domain=domain,
            expected_trajectory_ids=_required_trajectory_ids(dataset, domain=domain),
            dataset=dataset,
            source=source,
            runtime=runtime,
        )
        for domain, path in paths.items()
    }
    if len({item["project_id"] for item in domains.values()}) != len(DOMAINS):
        raise StagePlanError(f"{name} memory domains must use distinct project IDs")
    if len({item["run_id"] for item in domains.values()}) != len(DOMAINS):
        raise StagePlanError(f"{name} memory domains must use distinct run IDs")
    return domains


def build_memory_bindings(
    spec: dict[str, Any],
    *,
    dataset: dict[str, Any],
    source: dict[str, str],
) -> dict[str, Any]:
    roots = spec["memory_roots"]
    if spec["stage"] == "render" and spec["mode"] == "not_applicable":
        if roots["baseline"] is not None or roots["render"] is not None:
            raise StagePlanError("not-applicable render stage cannot bind saved memory")
        return {"baseline": None, "render": None}
    baseline = _memory_root(
        roots["baseline"],
        name="baseline",
        dataset=dataset,
        source=source,
        runtime=spec["runtime"],
    )
    render = _memory_root(
        roots["render"],
        name="render",
        dataset=dataset,
        source=source,
        runtime=spec["runtime"],
    )
    if spec["stage"] == "aa" and spec["mode"] == "initial":
        if baseline is not None or render is not None:
            raise StagePlanError("initial A/A must create baseline memory inside the stage")
    elif baseline is None:
        raise StagePlanError("stage requires an externally completed baseline memory")
    if spec["stage"] == "render" and render is not None:
        raise StagePlanError("render stage must create fresh treatment memory")
    if spec["stage"] != "render" and render is not None:
        raise StagePlanError("non-render stage cannot bind treatment memory")
    return {"baseline": baseline, "render": render}


def require_memory_bindings(
    raw: object,
    *,
    spec: dict[str, Any],
    dataset: dict[str, Any],
    source: dict[str, str],
) -> None:
    if raw != build_memory_bindings(spec, dataset=dataset, source=source):
        raise StagePlanError("memory bindings changed after stage planning")
