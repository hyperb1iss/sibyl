"""Build and verify sealed local LongMemEval-V2 release stage plans."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from benchmarks.local_execution_identity import require_local_checkout
from benchmarks.longmemeval_v2_official_source import (
    OFFICIAL_HARNESS_COMMIT,
    require_pinned_source,
)
from benchmarks.longmemeval_v2_release_authorization import (
    build_upstream_bindings,
    reject_score_bearing_keys,
    require_upstream_bindings,
)
from benchmarks.longmemeval_v2_release_contract import (
    MAX_WORKERS_CAP,
    STAGE_PLAN_SCHEMA_VERSION,
    require_stage_spec,
)
from benchmarks.longmemeval_v2_release_inputs import (
    DOMAINS,
    StagePlanError,
    bind_artifact,
    build_expected_stack,
    dataset_record,
    load_json,
    require_artifact,
    require_dataset,
    require_exact_keys,
    require_source_identity,
    require_string,
)
from benchmarks.longmemeval_v2_release_memory import (
    build_memory_bindings,
    require_memory_bindings,
)
from tools.bench import longmemeval_v2_rig as rig

ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_COMMAND_PREFIX = (
    "moon",
    "run",
    "root:bench-longmemeval-v2-official-full",
    "--",
)
PLAN_KEYS = frozenset(
    {
        "schema_version",
        "created_at",
        "spec",
        "spec_artifact",
        "source_identity",
        "sibyl_provenance",
        "official_source",
        "dataset",
        "stack_identity",
        "memory_bindings",
        "upstream_bindings",
        "output_root",
        "max_workers_cap",
        "runs",
        "waves",
        "stage_output",
        "stage_plan_sha256",
    }
)
RUN_KEYS = frozenset(
    {
        "arm_id",
        "pass_id",
        "pass_kind",
        "pass_index",
        "seed",
        "memory_source",
        "manifest",
        "execution",
        "domains",
        "spend_reservation",
    }
)
DOMAIN_RUN_KEYS = frozenset(
    {
        "domain",
        "planning_output_dir",
        "output_dir",
        "planning_memory_dir",
        "execution_memory_dir",
        "plan_command",
        "run_command",
    }
)


def _require_checkout() -> dict[str, Any]:
    try:
        return require_local_checkout(ROOT)
    except ValueError as exc:
        raise StagePlanError(str(exc)) from exc


def _execution_for_arm(source: dict[str, str], *, run_id: str) -> dict[str, Any]:
    execution = {
        "schema_version": rig.EXECUTION_IDENTITY_SCHEMA_VERSION,
        "kind": "local",
        **source,
        "run_id": run_id,
        "run_attempt": 1,
    }
    return rig.validate_execution_identity(execution, name="local stage arm execution")


def _base_command(
    *,
    runtime: dict[str, Any],
    manifest: dict[str, Any],
    source: dict[str, str],
    execution: dict[str, Any],
    official_repo: Path,
    data_root: Path,
    domain: str,
    output_dir: Path,
) -> list[str]:
    return [
        *OFFICIAL_COMMAND_PREFIX,
        "--official-repo",
        str(official_repo),
        "--data-root",
        str(data_root),
        "--domain",
        domain,
        "--tier",
        "small",
        "--output-dir",
        str(output_dir),
        "--api-url",
        runtime["api_url"],
        "--reader-base-url",
        runtime["reader_base_url"],
        "--reader-model",
        runtime["reader_model"],
        "--reader-api-key-env",
        runtime["reader_api_key_env"],
        "--reader-max-concurrent-requests",
        str(runtime["reader_max_concurrent_requests"]),
        "--reader-retry-attempts",
        str(runtime["reader_retry_attempts"]),
        "--evaluator-model",
        runtime["evaluator_model"],
        "--evaluator-api-key-env",
        runtime["evaluator_api_key_env"],
        "--evidence-composition-mode",
        runtime["evidence_composition_mode"],
        "--retrieval-max-planned-queries",
        str(runtime["retrieval_max_planned_queries"]),
        "--max-context-chars-per-item",
        str(runtime["max_context_chars_per_item"]),
        "--typed-stream-limit",
        str(runtime["typed_stream_limit"]),
        "--note-distillation-model",
        runtime["note_distillation_model"],
        "--api-retry-attempts",
        str(runtime["api_retry_attempts"]),
        "--prompt-build-max-workers",
        str(runtime["prompt_build_max_workers"]),
        "--experiment-id",
        manifest["experiment_id"],
        "--experiment-phase",
        manifest["experiment_phase"],
        "--pass-id",
        manifest["pass_id"],
        "--pass-seed",
        str(manifest["pass_seed"]),
        "--arm-role",
        manifest["arm_role"],
        "--substrate",
        manifest["substrate"],
        "--max-spend-usd",
        str(manifest["max_spend_usd"]),
        "--shuffle-questions-seed",
        str(manifest["pass_seed"]),
        "--retrieval-mode",
        manifest["retrieval_mode"],
        "--max-context-total-chars",
        str(manifest["max_context_total_chars"]),
        "--operational-note-dedupe-mode",
        manifest["operational_note_dedupe_mode"],
        "--operational-note-lane-mode",
        manifest["operational_note_lane_mode"],
        "--operational-note-distillation-profile",
        manifest["operational_note_distillation_profile"],
        "--execution-kind",
        "local",
        "--local-repository",
        source["repository"],
        "--local-ref",
        source["ref"],
        "--local-sha",
        source["sha"],
        "--local-run-id",
        execution["run_id"],
        "--local-run-attempt",
        "1",
        "--run-id",
        f"lme-v2-{execution['run_id']}-{domain}",
    ]


def _command(
    *,
    runtime: dict[str, Any],
    manifest: dict[str, Any],
    source: dict[str, str],
    execution: dict[str, Any],
    official_repo: Path,
    data_root: Path,
    domain: str,
    output_dir: Path,
    memory_dir: Path | None,
    build_memory: bool,
    future_memory: bool,
    plan_only: bool,
) -> list[str]:
    command = _base_command(
        runtime=runtime,
        manifest=manifest,
        source=source,
        execution=execution,
        official_repo=official_repo,
        data_root=data_root,
        domain=domain,
        output_dir=output_dir,
    )
    if runtime["allow_localhost"]:
        command.append("--allow-localhost")
    if manifest["preregistration_sha256"]:
        command.extend(["--preregistration-sha256", manifest["preregistration_sha256"]])
    if manifest["arm_role"] == "render_treatment":
        command.extend(
            [
                "--typed-stream-retrieval",
                "--note-distillation",
                "--render-char-total-treatment",
                "--render-group-lanes",
                "--render-action-spines",
            ]
        )
    if build_memory:
        command.extend(["--save-memory", "--checkpoint-dir", str(output_dir / "checkpoint")])
    elif memory_dir is not None:
        memory_flag = "--checkpoint-dir" if plan_only and future_memory else "--load-memory-dir"
        command.extend([memory_flag, str(memory_dir)])
    else:
        raise StagePlanError("non-builder arm has no memory source")
    if plan_only:
        command.append("--plan-only")
    return command


def _canonical_uuid(factory: Callable[[], Any]) -> str:
    raw = str(factory())
    try:
        canonical = str(UUID(raw))
    except (AttributeError, TypeError, ValueError) as exc:
        raise StagePlanError("execution UUID factory returned an invalid UUID") from exc
    if canonical != raw:
        raise StagePlanError("execution UUID factory returned a non-canonical UUID")
    return canonical


def _memory_path(
    *,
    memory_source: str,
    domain: str,
    bindings: dict[str, Any],
    builders: dict[str, Path],
) -> tuple[Path, bool]:
    external = bindings[memory_source]
    if external is not None:
        return Path(external[domain]["path"]), False
    if memory_source not in builders:
        raise StagePlanError(f"memory source {memory_source!r} has no earlier builder")
    return builders[memory_source] / domain / "checkpoint", True


def _domain_run(
    *,
    spec: dict[str, Any],
    arm: dict[str, Any],
    source: dict[str, str],
    execution: dict[str, Any],
    official_repo: Path,
    data_root: Path,
    output_root: Path,
    domain: str,
    memory_bindings: dict[str, Any],
    builders: dict[str, Path],
) -> dict[str, Any]:
    arm_id = arm["arm_id"]
    memory_source = arm["memory_source"]
    build_memory = memory_source.startswith("build_")
    memory_name = memory_source.removeprefix("build_")
    planning_output = output_root / "planning" / arm_id / domain
    execution_output = output_root / "runs" / arm_id / domain
    planning_memory = None
    execution_memory = None
    future_memory = False
    if not build_memory:
        saved_memory, future_memory = _memory_path(
            memory_source=memory_name,
            domain=domain,
            bindings=memory_bindings,
            builders=builders,
        )
        planning_memory = saved_memory
        execution_memory = saved_memory
    common = {
        "runtime": spec["runtime"],
        "manifest": arm["manifest"],
        "source": source,
        "execution": execution,
        "official_repo": official_repo,
        "data_root": data_root,
        "domain": domain,
        "build_memory": build_memory,
    }
    return {
        "domain": domain,
        "planning_output_dir": str(planning_output),
        "output_dir": str(execution_output),
        "planning_memory_dir": (str(planning_memory) if planning_memory is not None else None),
        "execution_memory_dir": (str(execution_memory) if execution_memory is not None else None),
        "plan_command": _command(
            **common,
            output_dir=planning_output,
            memory_dir=planning_memory,
            future_memory=future_memory,
            plan_only=True,
        ),
        "run_command": _command(
            **common,
            output_dir=execution_output,
            memory_dir=execution_memory,
            future_memory=False,
            plan_only=False,
        ),
    }


def _pass_waves(pass_spec: dict[str, Any]) -> list[list[str]]:
    builders = [
        arm["arm_id"] for arm in pass_spec["arms"] if arm["memory_source"].startswith("build_")
    ]
    others = [arm["arm_id"] for arm in pass_spec["arms"] if arm["arm_id"] not in builders]
    return [wave for wave in (builders, others) if wave]


def _expand_runs(
    *,
    spec: dict[str, Any],
    source: dict[str, str],
    official_repo: Path,
    data_root: Path,
    output_root: Path,
    memory_bindings: dict[str, Any],
    uuid_factory: Callable[[], Any],
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    runs: list[dict[str, Any]] = []
    waves: list[list[str]] = []
    builders: dict[str, Path] = {}
    for pass_index, pass_spec in enumerate(spec["passes"]):
        waves.extend(_pass_waves(pass_spec))
        for arm in pass_spec["arms"]:
            memory_source = arm["memory_source"]
            if memory_source.startswith("build_"):
                builders[memory_source.removeprefix("build_")] = (
                    output_root / "runs" / arm["arm_id"]
                )
            execution = _execution_for_arm(source, run_id=_canonical_uuid(uuid_factory))
            domains = {
                domain: _domain_run(
                    spec=spec,
                    arm=arm,
                    source=source,
                    execution=execution,
                    official_repo=official_repo,
                    data_root=data_root,
                    output_root=output_root,
                    domain=domain,
                    memory_bindings=memory_bindings,
                    builders=builders,
                )
                for domain in DOMAINS
            }
            cap = float(arm["manifest"]["max_spend_usd"])
            runs.append(
                {
                    "arm_id": arm["arm_id"],
                    "pass_id": pass_spec["pass_id"],
                    "pass_kind": pass_spec["kind"],
                    "pass_index": pass_index,
                    "seed": pass_spec["seed"],
                    "memory_source": memory_source,
                    "manifest": dict(arm["manifest"]),
                    "execution": execution,
                    "domains": domains,
                    "spend_reservation": {
                        "currency": "USD",
                        "max_spend_usd_per_domain": cap,
                        "max_spend_usd_total": len(DOMAINS) * cap,
                        "enforcement": ("official plan-only reservation before provider calls"),
                    },
                }
            )
    return runs, waves


def build_stage_plan(
    *,
    spec: dict[str, Any],
    spec_path: Path,
    official_repo: Path,
    data_root: Path,
    output_root: Path,
    uuid_factory: Callable[[], Any] = uuid4,
) -> dict[str, Any]:
    """Build an immutable, plan-only-first declaration for exactly one stage."""
    require_stage_spec(spec)
    spec_path = spec_path.expanduser().resolve()
    if load_json(spec_path) != spec:
        raise StagePlanError("stage spec path differs from the loaded spec")
    output_root = output_root.expanduser().resolve()
    if output_root.exists():
        raise StagePlanError("stage output root already exists; choose a fresh output root")
    checkout = _require_checkout()
    source = require_source_identity(checkout["source_identity"])
    provenance = checkout["provenance"]
    official_repo = official_repo.expanduser().resolve()
    official_source = require_pinned_source(official_repo)
    if official_source["commit"] != OFFICIAL_HARNESS_COMMIT:
        raise StagePlanError("official source differs from the reviewed pin")
    dataset = dataset_record(data_root)
    memory_bindings = build_memory_bindings(spec, dataset=dataset, source=source)
    stack_identity = build_expected_stack(
        source=source,
        official_source=official_source,
        dataset=dataset,
        runtime=spec["runtime"],
    )
    upstream_bindings = build_upstream_bindings(spec, expected_stack=stack_identity)
    runs, waves = _expand_runs(
        spec=spec,
        source=source,
        official_repo=official_repo,
        data_root=Path(dataset["root"]),
        output_root=output_root,
        memory_bindings=memory_bindings,
        uuid_factory=uuid_factory,
    )
    payload = {
        "schema_version": STAGE_PLAN_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "spec": spec,
        "spec_artifact": bind_artifact(spec_path, name="stage spec"),
        "source_identity": source,
        "sibyl_provenance": provenance,
        "official_source": official_source,
        "dataset": dataset,
        "stack_identity": stack_identity,
        "memory_bindings": memory_bindings,
        "upstream_bindings": upstream_bindings,
        "output_root": str(output_root),
        "max_workers_cap": MAX_WORKERS_CAP,
        "runs": runs,
        "waves": waves,
        "stage_output": str(output_root / "stage_receipt.json"),
    }
    payload["stage_plan_sha256"] = rig.canonical_sha256(payload)
    require_stage_plan(payload, check_checkout=False)
    return payload


def _require_spec_artifact(raw: object, *, spec: dict[str, Any]) -> None:
    artifact = require_artifact(raw, name="stage spec")
    if load_json(Path(artifact["path"])) != spec:
        raise StagePlanError("stage spec artifact content differs from the plan")


def _require_official_source(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError("official source binding is missing")
    path = Path(require_string(raw.get("path"), name="official source path"))
    current = require_pinned_source(path)
    if current != raw or current["commit"] != OFFICIAL_HARNESS_COMMIT:
        raise StagePlanError("official source changed after stage planning")
    return dict(raw)


def _require_created_at(raw: object) -> None:
    value = require_string(raw, name="created_at")
    try:
        created_at = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StagePlanError("stage plan created_at is not ISO 8601") from exc
    if created_at.tzinfo is None or created_at.utcoffset() != UTC.utcoffset(None):
        raise StagePlanError("stage plan created_at must be UTC")
    if created_at.isoformat() != value:
        raise StagePlanError("stage plan created_at is not canonical")


def _execution_ids(raw: object, *, source: dict[str, str], expected_count: int) -> Iterator[str]:
    if not isinstance(raw, list) or len(raw) != expected_count:
        raise StagePlanError("stage run count differs from its spec")
    run_ids: list[str] = []
    for index, run in enumerate(raw):
        if not isinstance(run, dict):
            raise StagePlanError(f"runs[{index}] is not an object")
        require_exact_keys(run, RUN_KEYS, name=f"runs[{index}]")
        execution = rig.validate_execution_identity(run.get("execution"), name="arm execution")
        if execution["kind"] != "local":
            raise StagePlanError("release plan contains a non-local execution")
        if {key: execution[key] for key in source} != source:
            raise StagePlanError("arm execution differs from the sealed source")
        if execution["run_id"] in run_ids:
            raise StagePlanError("arm executions must use distinct local UUIDs")
        run_ids.append(execution["run_id"])
    return iter(run_ids)


def _uuid_factory(run_ids: Iterator[str]) -> Callable[[], str]:
    def next_uuid() -> str:
        try:
            return next(run_ids)
        except StopIteration as exc:
            raise StagePlanError("stage run IDs ended before expansion") from exc

    return next_uuid


def _require_expansion(
    raw: dict[str, Any],
    *,
    spec: dict[str, Any],
    source: dict[str, str],
    official_source: dict[str, Any],
    dataset: dict[str, Any],
    memory_bindings: dict[str, Any],
    output_root: Path,
) -> list[dict[str, Any]]:
    arm_count = sum(len(item["arms"]) for item in spec["passes"])
    run_ids = _execution_ids(raw.get("runs"), source=source, expected_count=arm_count)
    expected_runs, expected_waves = _expand_runs(
        spec=spec,
        source=source,
        official_repo=Path(official_source["path"]),
        data_root=Path(dataset["root"]),
        output_root=output_root,
        memory_bindings=memory_bindings,
        uuid_factory=_uuid_factory(run_ids),
    )
    if raw["runs"] != expected_runs:
        raise StagePlanError("stage run expansion, commands, paths, or costs changed")
    if raw.get("waves") != expected_waves:
        raise StagePlanError("stage waves changed their declared dependencies")
    return expected_runs


def _require_stage_plan(
    raw: object,
    *,
    check_checkout: bool,
    claimed_root: bool,
) -> list[dict[str, Any]]:
    reject_score_bearing_keys(raw, name="stage plan")
    if not isinstance(raw, dict):
        raise StagePlanError("stage plan must be a JSON object")
    require_exact_keys(raw, PLAN_KEYS, name="stage plan")
    if raw.get("schema_version") != STAGE_PLAN_SCHEMA_VERSION:
        raise StagePlanError("stage plan schema is invalid")
    _require_created_at(raw.get("created_at"))
    unsigned = {key: value for key, value in raw.items() if key != "stage_plan_sha256"}
    if raw.get("stage_plan_sha256") != rig.canonical_sha256(unsigned):
        raise StagePlanError("stage plan digest does not bind its content")
    spec = require_stage_spec(raw.get("spec"))
    _require_spec_artifact(raw.get("spec_artifact"), spec=spec)
    source = require_source_identity(raw.get("source_identity"))
    expected_provenance = {
        "sibyl_commit": source["sha"],
        "git_dirty": False,
        "git_status": "clean",
    }
    if check_checkout:
        checkout = _require_checkout()
        if checkout != {
            "source_identity": source,
            "provenance": expected_provenance,
        }:
            raise StagePlanError("current checkout differs from the sealed source identity")
    if raw.get("sibyl_provenance") != expected_provenance:
        raise StagePlanError("stage plan does not bind clean Sibyl provenance")
    official_source = _require_official_source(raw.get("official_source"))
    dataset = require_dataset(raw.get("dataset"))
    expected_stack = build_expected_stack(
        source=source,
        official_source=official_source,
        dataset=dataset,
        runtime=spec["runtime"],
    )
    if raw.get("stack_identity") != expected_stack:
        raise StagePlanError("stage stack differs from its sealed source, data, or models")
    require_memory_bindings(
        raw.get("memory_bindings"),
        spec=spec,
        dataset=dataset,
        source=source,
    )
    memory_bindings = build_memory_bindings(spec, dataset=dataset, source=source)
    require_upstream_bindings(
        raw.get("upstream_bindings"),
        spec=spec,
        expected_stack=expected_stack,
    )
    output_value = require_string(raw.get("output_root"), name="output_root")
    output_root = Path(output_value).resolve()
    if output_value != str(output_root):
        raise StagePlanError("stage output root is not canonical")
    if claimed_root and (not output_root.is_dir() or output_root.is_symlink()):
        raise StagePlanError("claimed stage output root is missing or non-canonical")
    if not claimed_root and output_root.exists():
        raise StagePlanError("stage output root is no longer fresh")
    if raw.get("max_workers_cap") != MAX_WORKERS_CAP:
        raise StagePlanError("stage plan changed the fixed worker cap")
    if raw.get("stage_output") != str(output_root / "stage_receipt.json"):
        raise StagePlanError("stage output escaped its output root")
    return _require_expansion(
        raw,
        spec=spec,
        source=source,
        official_source=official_source,
        dataset=dataset,
        memory_bindings=memory_bindings,
        output_root=output_root,
    )


def require_stage_plan(raw: object, *, check_checkout: bool = True) -> list[dict[str, Any]]:
    """Reconstruct and validate every sealed input before claiming its fresh root."""

    return _require_stage_plan(raw, check_checkout=check_checkout, claimed_root=False)


def write_stage_plan(path: Path, payload: dict[str, Any]) -> None:
    """Write a validated stage plan without creating its execution root."""
    require_stage_plan(payload)
    target = path.expanduser().resolve()
    output_root = Path(payload["output_root"])
    if target == output_root or output_root in target.parents:
        raise StagePlanError("stage plan must remain outside its fresh output root")
    if target.exists():
        raise StagePlanError("stage plan path already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
