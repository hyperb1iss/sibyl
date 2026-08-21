"""Stage semantics for the sealed LongMemEval-V2 v1.3 experiment."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from benchmarks.longmemeval_v2_release_inputs import (
    MEMORY_ROOT_KEYS,
    UPSTREAM_KEYS,
    StagePlanError,
    load_json,
    require_exact_keys,
    require_nonnegative_int,
    require_positive_int,
    require_positive_number,
    require_string,
)
from tools.bench import longmemeval_v2_rig as rig

STAGE_SPEC_SCHEMA_VERSION = "sibyl-longmemeval-v2-release-stage-spec-v1"
STAGE_PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-release-stage-plan-v1"
DEFAULT_MAX_WORKERS = 4
MAX_WORKERS_CAP = 4
ENV_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
SPEC_KEYS = frozenset(
    {
        "schema_version",
        "experiment_id",
        "stage",
        "mode",
        "runtime",
        "memory_roots",
        "upstream",
        "passes",
    }
)
RUNTIME_KEYS = frozenset(
    {
        "api_url",
        "allow_localhost",
        "reader_base_url",
        "reader_model",
        "reader_api_key_env",
        "reader_max_concurrent_requests",
        "reader_retry_attempts",
        "evaluator_model",
        "evaluator_api_key_env",
        "evidence_composition_mode",
        "retrieval_max_planned_queries",
        "max_context_chars_per_item",
        "typed_stream_limit",
        "note_distillation_model",
        "api_retry_attempts",
        "prompt_build_max_workers",
    }
)
PASS_KEYS = frozenset({"kind", "pass_id", "seed", "arms"})
ARM_KEYS = frozenset({"arm_id", "memory_source", "manifest"})
ARM_MANIFEST_KEYS = frozenset(
    {
        "experiment_id",
        "experiment_phase",
        "pass_id",
        "pass_seed",
        "arm_role",
        "substrate",
        "preregistration_sha256",
        "max_spend_usd",
        "retrieval_mode",
        "max_context_total_chars",
        "operational_note_dedupe_mode",
        "operational_note_lane_mode",
        "operational_note_distillation_profile",
        "render_group_lanes",
        "render_action_spines",
    }
)


def _require_runtime(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError("runtime config is missing")
    require_exact_keys(raw, RUNTIME_KEYS, name="runtime")
    for key in (
        "api_url",
        "reader_base_url",
        "reader_model",
        "evaluator_model",
        "note_distillation_model",
    ):
        require_string(raw.get(key), name=f"runtime.{key}")
    for key in ("reader_api_key_env", "evaluator_api_key_env"):
        value = require_string(raw.get(key), name=f"runtime.{key}")
        if not ENV_NAME_PATTERN.fullmatch(value):
            raise StagePlanError(f"runtime.{key} is not an environment variable name")
    if not isinstance(raw.get("allow_localhost"), bool):
        raise StagePlanError("runtime.allow_localhost must be boolean")
    if raw.get("evidence_composition_mode") not in {
        "shared_relevance",
        "reserved_support",
    }:
        raise StagePlanError("runtime evidence composition mode is invalid")
    integer_keys = (
        "reader_max_concurrent_requests",
        "reader_retry_attempts",
        "retrieval_max_planned_queries",
        "max_context_chars_per_item",
        "typed_stream_limit",
        "api_retry_attempts",
        "prompt_build_max_workers",
    )
    for key in integer_keys:
        require_positive_int(raw.get(key), name=f"runtime.{key}")
    return dict(raw)


def _require_treatment_bundle(raw: dict[str, Any], *, role: str) -> None:
    treatment = role == "render_treatment"
    treatment_bundle = all(
        (
            raw.get("operational_note_dedupe_mode") == "source_kind",
            raw.get("operational_note_lane_mode") == "additive",
            raw.get("operational_note_distillation_profile") == "render_v1",
            raw.get("render_group_lanes") is True,
            raw.get("render_action_spines") is True,
            int(raw["max_context_total_chars"]) > 60_000,
        )
    )
    if treatment is not treatment_bundle:
        raise StagePlanError("render treatment bundle is incomplete or enabled on a control")


def _require_manifest(
    raw: object,
    *,
    experiment_id: str,
    stage: str,
    pass_id: str,
    seed: int,
    preregistration_digest: str,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError("arm manifest is missing")
    require_exact_keys(raw, ARM_MANIFEST_KEYS, name="arm manifest")
    identity = (
        raw.get("experiment_id"),
        raw.get("experiment_phase"),
        raw.get("pass_id"),
        raw.get("pass_seed"),
    )
    if identity != (experiment_id, stage, pass_id, seed):
        raise StagePlanError("arm manifest identity differs from its pass")
    role = raw.get("arm_role")
    substrate = raw.get("substrate")
    retrieval = raw.get("retrieval_mode")
    if role not in {"machine", "naive", "render_control", "render_treatment"}:
        raise StagePlanError("arm manifest role is invalid")
    if substrate not in {"machine", "naive"}:
        raise StagePlanError("arm manifest substrate is invalid")
    if retrieval not in {"fast", "accurate", "naive"}:
        raise StagePlanError("arm manifest retrieval mode is invalid")
    if role == "naive" and (substrate != "naive" or retrieval != "naive"):
        raise StagePlanError("naive arm must use the naive substrate and retrieval")
    if role != "naive" and (substrate != "machine" or retrieval == "naive"):
        raise StagePlanError("machine and render arms must use machine retrieval")
    expected_prereg = preregistration_digest.removeprefix("sha256:")
    if raw.get("preregistration_sha256") != expected_prereg:
        raise StagePlanError("arm manifest preregistration differs from the sealed stage")
    require_positive_number(raw.get("max_spend_usd"), name="arm manifest.max_spend_usd")
    require_positive_int(raw.get("max_context_total_chars"), name="arm context ceiling")
    if raw.get("operational_note_dedupe_mode") not in {"source", "source_kind"}:
        raise StagePlanError("arm note dedupe mode is invalid")
    if raw.get("operational_note_lane_mode") not in {"reserved", "additive"}:
        raise StagePlanError("arm note lane mode is invalid")
    if raw.get("operational_note_distillation_profile") not in {
        "baseline",
        "render_v1",
    }:
        raise StagePlanError("arm note distillation profile is invalid")
    for key in ("render_group_lanes", "render_action_spines"):
        if not isinstance(raw.get(key), bool):
            raise StagePlanError(f"arm manifest.{key} must be boolean")
    _require_treatment_bundle(raw, role=str(role))
    return dict(raw)


def _preregistration_for_spec(spec: dict[str, Any]) -> tuple[dict[str, Any], str]:
    raw_path = spec["upstream"]["preregistration"]
    if raw_path is None:
        return {}, ""
    path = Path(require_string(raw_path, name="upstream.preregistration")).resolve()
    preregistration = rig.validate_preregistration(load_json(path), kind=spec["stage"])
    return preregistration, str(preregistration["preregistration_sha256"])


def _require_fixed_shape(spec: dict[str, Any]) -> None:
    expected = {
        ("aa", "initial"): (["paired"] * 3, 2),
        ("aa", "extension"): (["paired"] * 2, 2),
        ("anchor", "standard"): (["anchor"], 1),
        ("race", "standard"): (["paired"] * 3 + ["matched"], 2),
        ("render", "standard"): (["paired"] * 3, 2),
    }.get((spec["stage"], spec["mode"]))
    if expected is None:
        raise StagePlanError("stage and mode do not name a supported v1.3 stage")
    expected_kinds, arms_per_pass = expected
    if [item["kind"] for item in spec["passes"]] != expected_kinds:
        raise StagePlanError("stage pass kinds differ from the fixed v1.3 schedule")
    if any(len(item["arms"]) != arms_per_pass for item in spec["passes"]):
        raise StagePlanError("stage pass arm count differs from the fixed v1.3 schedule")


def _require_aa_shape(spec: dict[str, Any]) -> None:
    passes = spec["passes"]
    roles = [[arm["manifest"]["arm_role"] for arm in item["arms"]] for item in passes]
    if any(pair != ["machine", "machine"] for pair in roles):
        raise StagePlanError("A/A requires two machine arms per pass")
    if any(item["arms"][0]["manifest"] != item["arms"][1]["manifest"] for item in passes):
        raise StagePlanError("A/A paired arms changed configuration")
    sources = [arm["memory_source"] for item in passes for arm in item["arms"]]
    expected = (
        ["build_baseline", "baseline"] + ["baseline"] * 4
        if spec["mode"] == "initial"
        else ["baseline"] * 4
    )
    if sources != expected:
        raise StagePlanError("A/A memory lineage differs from the fixed baseline reuse")


def _require_anchor_shape(spec: dict[str, Any]) -> None:
    arm = spec["passes"][0]["arms"][0]
    if arm["manifest"]["arm_role"] != "machine":
        raise StagePlanError("anchor must use one machine arm")
    if arm["memory_source"] != "baseline":
        raise StagePlanError("anchor must reuse baseline memory")


def _require_race_shape(spec: dict[str, Any], preregistration: dict[str, Any]) -> None:
    passes = spec["passes"]
    roles = [[arm["manifest"]["arm_role"] for arm in item["arms"]] for item in passes]
    if any(pair != ["machine", "naive"] for pair in roles):
        raise StagePlanError("race passes must pair machine then naive")
    if any(arm["memory_source"] != "baseline" for item in passes for arm in item["arms"]):
        raise StagePlanError("race arms must reuse the baseline memory")
    preregistered = list(preregistration["seeds"])
    if [item["seed"] for item in passes[:3]] != preregistered:
        raise StagePlanError("paired pass seeds differ from preregistration")
    if passes[3]["seed"] in set(preregistered):
        raise StagePlanError("matched race control must use a fresh seed")


def _require_render_shape(spec: dict[str, Any], preregistration: dict[str, Any]) -> None:
    passes = spec["passes"]
    roles = [[arm["manifest"]["arm_role"] for arm in item["arms"]] for item in passes]
    if any(pair != ["render_control", "render_treatment"] for pair in roles):
        raise StagePlanError("render passes must pair control then treatment")
    sources = [arm["memory_source"] for item in passes for arm in item["arms"]]
    expected = [
        "baseline",
        "build_render",
        "baseline",
        "render",
        "baseline",
        "render",
    ]
    if sources != expected:
        raise StagePlanError("render stage changed its separate treatment memory lineage")
    if [item["seed"] for item in passes] != list(preregistration["seeds"]):
        raise StagePlanError("render pass seeds differ from preregistration")


def _require_stage_shape(spec: dict[str, Any], preregistration: dict[str, Any]) -> None:
    _require_fixed_shape(spec)
    validator = {
        "aa": lambda: _require_aa_shape(spec),
        "anchor": lambda: _require_anchor_shape(spec),
        "race": lambda: _require_race_shape(spec, preregistration),
        "render": lambda: _require_render_shape(spec, preregistration),
    }[spec["stage"]]
    validator()


def _require_arm(
    raw: object,
    *,
    spec: dict[str, Any],
    pass_id: str,
    seed: int,
    preregistration_digest: str,
    arm_ids: set[str],
) -> None:
    if not isinstance(raw, dict):
        raise StagePlanError("pass arm is not an object")
    require_exact_keys(raw, ARM_KEYS, name="pass arm")
    arm_id = require_string(raw.get("arm_id"), name="arm_id")
    if arm_id in arm_ids:
        raise StagePlanError("arm IDs must be unique across the stage")
    arm_ids.add(arm_id)
    if raw.get("memory_source") not in {
        "build_baseline",
        "baseline",
        "build_render",
        "render",
    }:
        raise StagePlanError("arm memory source is invalid")
    _require_manifest(
        raw.get("manifest"),
        experiment_id=spec["experiment_id"],
        stage=spec["stage"],
        pass_id=pass_id,
        seed=seed,
        preregistration_digest=preregistration_digest,
    )


def _require_passes(spec: dict[str, Any], *, preregistration_digest: str) -> None:
    pass_ids: set[str] = set()
    seeds: set[int] = set()
    arm_ids: set[str] = set()
    for index, item in enumerate(spec["passes"]):
        if not isinstance(item, dict):
            raise StagePlanError(f"passes[{index}] is not an object")
        require_exact_keys(item, PASS_KEYS, name=f"passes[{index}]")
        if item.get("kind") not in {"paired", "matched", "anchor"}:
            raise StagePlanError(f"passes[{index}].kind is invalid")
        pass_id = require_string(item.get("pass_id"), name="pass_id")
        seed = require_nonnegative_int(item.get("seed"), name="seed")
        if pass_id in pass_ids or seed in seeds:
            raise StagePlanError("stage pass IDs and seeds must be unique")
        pass_ids.add(pass_id)
        seeds.add(seed)
        arms = item.get("arms")
        if not isinstance(arms, list) or not arms:
            raise StagePlanError(f"passes[{index}] has no arms")
        for arm in arms:
            _require_arm(
                arm,
                spec=spec,
                pass_id=pass_id,
                seed=seed,
                preregistration_digest=preregistration_digest,
                arm_ids=arm_ids,
            )


def require_stage_spec(raw: object) -> dict[str, Any]:
    """Validate one separately authorized v1.3 release stage declaration."""
    if not isinstance(raw, dict):
        raise StagePlanError("stage spec must be a JSON object")
    require_exact_keys(raw, SPEC_KEYS, name="stage spec")
    if raw.get("schema_version") != STAGE_SPEC_SCHEMA_VERSION:
        raise StagePlanError("stage spec schema is invalid")
    require_string(raw.get("experiment_id"), name="experiment_id")
    stage = require_string(raw.get("stage"), name="stage")
    if stage not in rig.EXPERIMENT_PHASES:
        raise StagePlanError("stage is invalid")
    require_string(raw.get("mode"), name="mode")
    _require_runtime(raw.get("runtime"))
    memory_roots = raw.get("memory_roots")
    upstream = raw.get("upstream")
    if not isinstance(memory_roots, dict) or not isinstance(upstream, dict):
        raise StagePlanError("memory roots or upstream bindings are missing")
    require_exact_keys(memory_roots, MEMORY_ROOT_KEYS, name="memory roots")
    require_exact_keys(upstream, UPSTREAM_KEYS, name="upstream")
    paired_paths = upstream.get("paired_passes")
    if not isinstance(paired_paths, list) or any(
        not isinstance(path, str) for path in paired_paths
    ):
        raise StagePlanError("upstream.paired_passes must be a path list")
    if not isinstance(raw.get("passes"), list) or not raw["passes"]:
        raise StagePlanError("stage spec has no passes")
    preregistration, digest = _preregistration_for_spec(raw)
    _require_passes(raw, preregistration_digest=digest)
    _require_stage_shape(raw, preregistration)
    return raw
