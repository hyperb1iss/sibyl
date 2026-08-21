"""Durable claimed-root state for local LongMemEval-V2 release execution."""

from __future__ import annotations

import fcntl
import json
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_plan as release_plan
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    bind_artifact,
    load_json,
    require_artifact,
    require_exact_keys,
)
from tools.bench import longmemeval_v2_rig as rig

STAGE_CLAIM_SCHEMA_VERSION = "sibyl-longmemeval-v2-release-stage-claim-v1"
RUNNER_STATUS_SCHEMA_VERSION = "sibyl-longmemeval-v2-release-runner-status-v3"
STAGE_CLAIM_KEYS = frozenset(
    {
        "schema_version",
        "stage_plan_sha256",
        "source_identity",
        "output_root",
        "claimed_at",
        "claim_sha256",
    }
)
STATUS_KEYS = frozenset(
    {
        "schema_version",
        "stage_plan_sha256",
        "status",
        "max_workers",
        "completed_domains",
        "resumed_domains",
        "failures",
        "actual_cost_usd",
        "package_claim",
        "executed_status_artifact",
        "stage_receipt",
        "updated_at",
        "status_sha256",
    }
)
CLAIMED_ROOT_NAMES = frozenset(
    {
        "runner_claim.json",
        "runner.lock",
        "runner_status.json",
        "planning",
        "runs",
        "logs",
        "exits",
    }
)
RESUMABLE_STATUS = frozenset({"CLAIMED", "PREFLIGHT_COMPLETE", "RUNNING", "EXECUTED"})
PACKAGE_RESUMABLE_STATUS = frozenset({"EXECUTED", "PACKAGING"})
RUNNER_STATUS = RESUMABLE_STATUS | {"PACKAGING", "PACKAGED", "FAIL"}
_PACKAGE_PENDING_PATTERN = re.compile(
    r"^packages\.pending\.[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SECRET_ENV_NAMES = frozenset(
    {
        "SIBYL_API_TOKEN",
        "LME_SIBYL_API_TOKEN",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    }
)
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_CREDENTIALS", "_EMAIL", "_FILE", "_PASSWORD", "_TOKEN")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:api[-_ ]?key|authorization|credential(?:s|_path)?|password|token)"
    r"\s*[=:]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+")
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_MIN_SECRET_LENGTH = 4

_PLANNING_FILES = frozenset({"longmemeval_v2_official_plan.json"})
_DOMAIN_FILES = frozenset(
    {
        "aggregated_metrics.json",
        "longmemeval_v2_official_plan.json",
        "longmemeval_v2_official_receipt.json",
        "per_question.jsonl",
        "rig_rows.jsonl",
        "run_args.json",
    }
)
_RUNTIME_FILES = frozenset({"haystack.json", "memory_config.json", "questions.json"})
_PROVIDER_FILES = frozenset({"judge.jsonl", "reader.jsonl"})
_CHECKPOINT_FILES = frozenset(
    {
        "action_spines.jsonl.gz",
        "checkpoint_action_spines.jsonl",
        "checkpoint_catalog.jsonl",
        "checkpoint_distillation_receipts.jsonl",
        "checkpoint_manifest.json",
        "chunk_catalog.jsonl.gz",
        "distillation_receipts.jsonl.gz",
        "memory_config.json",
        "memory_manifest.json",
    }
)
_LOG_EVENT_KEYS = {
    "start": frozenset({"event", "recorded_at", "command_sha256"}),
    "output": frozenset({"event", "text"}),
    "exit": frozenset({"event", "recorded_at", "returncode"}),
}


def now() -> str:
    return datetime.now(UTC).isoformat()


def sealed(payload: dict[str, Any], digest_key: str) -> dict[str, Any]:
    result = dict(payload)
    result[digest_key] = rig.canonical_sha256(result)
    return result


def append_log(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def secret_values(plan: dict[str, Any]) -> tuple[str, ...]:
    runtime = plan["spec"]["runtime"]
    names = set(_SECRET_ENV_NAMES) | {
        str(runtime["reader_api_key_env"]),
        str(runtime["evaluator_api_key_env"]),
    }
    names.update(name for name in os.environ if name.upper().endswith(_SECRET_ENV_SUFFIXES))
    return tuple(
        value for name in names if len(value := os.environ.get(name, "")) >= _MIN_SECRET_LENGTH
    )


def _redact_sensitive_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    parsed = urlsplit(raw)
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return "<redacted-url>"
    return raw


def redact(value: object, *, secrets: tuple[str, ...]) -> str:
    text = str(value)
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    text = _URL.sub(_redact_sensitive_url, text)
    text = _BEARER.sub(r"\1<redacted>", text)
    return _SECRET_ASSIGNMENT.sub(r"\1<redacted>", text)


def _require_log_event(
    row: dict[str, Any],
    *,
    event: str,
    active: bool,
    command_sha256: str,
    secrets: tuple[str, ...],
    expected_returncode: int,
) -> bool:
    if event == "start":
        if active or row.get("command_sha256") != command_sha256:
            raise StagePlanError("runner command log start is invalid")
        _require_timestamp(row.get("recorded_at"), name="runner log start timestamp")
        return True
    if event == "output":
        text = row.get("text")
        if not active or not isinstance(text, str) or redact(text, secrets=secrets) != text:
            raise StagePlanError("runner command log output is invalid or unredacted")
        return True
    returncode = row.get("returncode")
    if not active or isinstance(returncode, bool) or returncode != expected_returncode:
        raise StagePlanError("runner command log exit is invalid")
    _require_timestamp(row.get("recorded_at"), name="runner log exit timestamp")
    return False


def require_command_log(
    path: Path,
    *,
    command: list[str],
    secrets: tuple[str, ...],
    expected_returncode: int,
    expected_invocations: int | None = None,
) -> dict[str, Any]:
    """Validate an append-only redacted command log and bind its exact bytes."""

    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise StagePlanError("runner command log is not one canonical file")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StagePlanError("runner command log is not valid JSONL") from exc
    validate_command_log_text(
        raw_text,
        command=command,
        secrets=secrets,
        expected_returncode=expected_returncode,
        expected_invocations=expected_invocations,
    )
    return bind_artifact(path, name="runner command log")


def validate_command_log_text(
    raw_text: str,
    *,
    command: list[str],
    secrets: tuple[str, ...],
    expected_returncode: int,
    expected_invocations: int | None = None,
) -> None:
    """Validate exact JSONL bytes read through a separately owned file handle."""

    try:
        rows = [json.loads(line) for line in raw_text.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise StagePlanError("runner command log is not valid JSONL") from exc
    if any(secret in raw_text for secret in secrets):
        raise StagePlanError("runner command log contains an unredacted secret")
    active = False
    invocations = 0
    exits = 0
    command_sha256 = rig.canonical_sha256(command)
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("event") not in _LOG_EVENT_KEYS:
            raise StagePlanError("runner command log contains an unknown event")
        event = str(row["event"])
        require_exact_keys(row, _LOG_EVENT_KEYS[event], name=f"runner log row {index}")
        active = _require_log_event(
            row,
            event=event,
            active=active,
            command_sha256=command_sha256,
            secrets=secrets,
            expected_returncode=expected_returncode,
        )
        if event == "start":
            invocations += 1
        elif event == "exit":
            exits += 1
    if active or not invocations or exits != invocations:
        raise StagePlanError("runner command log has an incomplete invocation")
    if expected_invocations is not None and invocations != expected_invocations:
        raise StagePlanError("runner command log invocation count is invalid")


def _require_timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise StagePlanError(f"{name} is not a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StagePlanError(f"{name} is not a canonical UTC timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != UTC.utcoffset(None)
        or parsed.isoformat() != value
    ):
        raise StagePlanError(f"{name} is not a canonical UTC timestamp")
    return value


def claim_stage(plan: dict[str, Any], *, max_workers: int) -> None:
    release_plan.require_stage_plan(plan)
    output_root = Path(plan["output_root"])
    output_root.parent.mkdir(parents=True, exist_ok=True)
    output_root.mkdir()
    claim = sealed(
        {
            "schema_version": STAGE_CLAIM_SCHEMA_VERSION,
            "stage_plan_sha256": plan["stage_plan_sha256"],
            "source_identity": plan["source_identity"],
            "output_root": plan["output_root"],
            "claimed_at": now(),
        },
        "claim_sha256",
    )
    release_io.write_json_atomic(output_root / "runner_claim.json", claim)
    write_status(plan, status="CLAIMED", max_workers=max_workers)


def require_claimed_stage_plan(
    raw: object,
    *,
    check_checkout: bool = True,
) -> list[dict[str, Any]]:
    runs = release_plan._require_stage_plan(  # pyright: ignore[reportPrivateUsage]
        raw,
        check_checkout=check_checkout,
        claimed_root=True,
    )
    assert isinstance(raw, dict)
    require_stage_claim(raw)
    require_claimed_output_tree(raw)
    return runs


def require_stage_claim(plan: dict[str, Any]) -> None:
    """Validate the immutable claimed-stage identity."""

    output_root = Path(str(plan["output_root"]))
    claim = load_json(output_root / "runner_claim.json")
    require_exact_keys(claim, STAGE_CLAIM_KEYS, name="stage root claim")
    _require_timestamp(claim.get("claimed_at"), name="stage root claim timestamp")
    unsigned = {key: value for key, value in claim.items() if key != "claim_sha256"}
    if claim.get("claim_sha256") != rig.canonical_sha256(unsigned):
        raise StagePlanError("stage root claim digest does not bind its content")
    if unsigned != {
        "schema_version": STAGE_CLAIM_SCHEMA_VERSION,
        "stage_plan_sha256": plan["stage_plan_sha256"],
        "source_identity": plan["source_identity"],
        "output_root": plan["output_root"],
        "claimed_at": claim["claimed_at"],
    }:
        raise StagePlanError("stage root claim differs from the sealed plan")


def stage_control_snapshot(plan: dict[str, Any]) -> tuple[bytes, bytes]:
    require_stage_claim(plan)
    require_status(plan)
    output_root = Path(plan["output_root"])
    return (
        (output_root / "runner_claim.json").read_bytes(),
        (output_root / "runner_status.json").read_bytes(),
    )


def require_stage_control(plan: dict[str, Any], snapshot: tuple[bytes, bytes]) -> None:
    """Require runner-owned control files to retain their exact pre-wave bytes."""

    if stage_control_snapshot(plan) != snapshot:
        raise StagePlanError("paid wave changed runner-owned claim or status")


def require_claimed_output_tree(plan: dict[str, Any]) -> None:
    """Validate the exact claimed-root inventory without checkout side effects."""

    output_root = Path(plan["output_root"])
    status = read_status_receipt(plan)
    package_entries = {
        path.name
        for path in output_root.iterdir()
        if path.name in {"packages", "stage_receipt.json"}
        or _PACKAGE_PENDING_PATTERN.fullmatch(path.name)
    }
    if package_entries and status["status"] not in {"PACKAGING", "PACKAGED", "FAIL"}:
        raise StagePlanError("claimed stage output root contains unknown entries from packaging")
    pending = {name for name in package_entries if _PACKAGE_PENDING_PATTERN.fullmatch(name)}
    if len(pending) > 1 or (pending and "packages" in package_entries):
        raise StagePlanError("claimed stage package publication state is ambiguous")
    if status["status"] == "PACKAGED" and package_entries != {
        "packages",
        "stage_receipt.json",
    }:
        raise StagePlanError("packaged stage output inventory is incomplete")
    allowed = CLAIMED_ROOT_NAMES | package_entries
    unknown = sorted(path.name for path in output_root.iterdir() if path.name not in allowed)
    if unknown:
        raise StagePlanError(f"claimed stage output root contains unknown entries: {unknown}")
    for path in output_root.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(output_root.resolve()):
            raise StagePlanError("claimed stage output root contains an unsafe path")
    _require_declared_tree(plan, output_root=output_root)


def _require_subset(parent: Path, expected: set[str], *, name: str) -> None:
    if not parent.exists():
        return
    if not parent.is_dir():
        raise StagePlanError(f"claimed stage {name} is not a directory")
    unknown = sorted(path.name for path in parent.iterdir() if path.name not in expected)
    if unknown:
        raise StagePlanError(f"claimed stage {name} contains unknown entries: {unknown}")


def _require_domain_tree(parent: Path, *, planning: bool, build_memory: bool) -> None:
    files = _PLANNING_FILES if planning else _DOMAIN_FILES
    directories = {"provider_usage", "runtime_inputs"}
    if build_memory and not planning:
        directories.add("checkpoint")
    _require_subset(parent, set(files) | directories, name="domain output")
    _require_subset(parent / "runtime_inputs", set(_RUNTIME_FILES), name="runtime inputs")
    _require_subset(
        parent / "provider_usage",
        set() if planning else set(_PROVIDER_FILES),
        name="provider usage",
    )
    if build_memory and not planning:
        _require_subset(parent / "checkpoint", set(_CHECKPOINT_FILES), name="memory checkpoint")


def require_domain_output_tree(
    run: dict[str, Any],
    domain: str,
    *,
    planning: bool = False,
) -> None:
    """Reject foreign or unsafe descendants at the evidence boundary."""

    key = "planning_output_dir" if planning else "output_dir"
    output_dir = Path(run["domains"][domain][key])
    _require_domain_tree(
        output_dir,
        planning=planning,
        build_memory=str(run["memory_source"]).startswith("build_"),
    )
    for path in output_dir.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(output_dir.resolve()):
            raise StagePlanError("claimed stage domain output contains an unsafe path")


def _require_declared_tree(plan: dict[str, Any], *, output_root: Path) -> None:
    runs = plan["runs"]
    arm_ids = {str(run["arm_id"]) for run in runs}
    _require_subset(output_root / "planning", arm_ids, name="planning arms")
    _require_subset(output_root / "runs", arm_ids, name="run arms")
    _require_subset(output_root / "exits", arm_ids, name="exit arms")
    runs_by_id = {str(run["arm_id"]): run for run in runs}
    for group in ("planning", "runs"):
        for arm_id in arm_ids:
            _require_subset(output_root / group / arm_id, {"web", "enterprise"}, name=group)
            for domain in ("web", "enterprise"):
                _require_domain_tree(
                    output_root / group / arm_id / domain,
                    planning=group == "planning",
                    build_memory=str(runs_by_id[arm_id]["memory_source"]).startswith("build_"),
                )
    for arm_id in arm_ids:
        _require_subset(
            output_root / "exits" / arm_id,
            {"web.json", "enterprise.json"},
            name="domain exits",
        )
    _require_subset(output_root / "logs", {"planning", "runs"}, name="log phases")
    for phase in ("planning", "runs"):
        _require_subset(output_root / "logs" / phase, arm_ids, name=f"{phase} log arms")
        for arm_id in arm_ids:
            _require_subset(
                output_root / "logs" / phase / arm_id,
                {"web.jsonl", "enterprise.jsonl"},
                name=f"{phase} domain logs",
            )


@contextmanager
def stage_lock(output_root: Path) -> Iterator[None]:
    lock_path = output_root / "runner.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StagePlanError("another release runner owns this stage root") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _status_payload(
    plan: dict[str, Any],
    *,
    status: str,
    max_workers: int,
    completed: list[str] | None = None,
    resumed: list[str] | None = None,
    failures: list[dict[str, Any]] | None = None,
    cost: float = 0.0,
    package_claim: dict[str, Any] | None = None,
    executed_status_artifact: dict[str, Any] | None = None,
    stage_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return sealed(
        {
            "schema_version": RUNNER_STATUS_SCHEMA_VERSION,
            "stage_plan_sha256": plan["stage_plan_sha256"],
            "status": status,
            "max_workers": max_workers,
            "completed_domains": sorted(completed or []),
            "resumed_domains": sorted(resumed or []),
            "failures": failures or [],
            "actual_cost_usd": cost,
            "package_claim": package_claim,
            "executed_status_artifact": executed_status_artifact,
            "stage_receipt": stage_receipt,
            "updated_at": now(),
        },
        "status_sha256",
    )


def write_status(plan: dict[str, Any], **values: Any) -> dict[str, Any]:
    payload = _status_payload(plan, **values)
    release_io.write_json_atomic(Path(plan["output_root"]) / "runner_status.json", payload)
    return payload


def validate_status_receipt(plan: dict[str, Any], raw: object) -> dict[str, Any]:
    """Validate one runner status object without assuming its live path."""

    if not isinstance(raw, dict):
        raise StagePlanError("release runner status is missing")
    require_exact_keys(raw, STATUS_KEYS, name="release runner status")
    unsigned = {key: value for key, value in raw.items() if key != "status_sha256"}
    if raw.get("status_sha256") != rig.canonical_sha256(unsigned):
        raise StagePlanError("release runner status digest is invalid")
    if raw.get("stage_plan_sha256") != plan["stage_plan_sha256"]:
        raise StagePlanError("release runner status belongs to another stage plan")
    if raw.get("status") not in RUNNER_STATUS:
        raise StagePlanError("release runner status is unknown")
    claim = raw.get("package_claim")
    if raw["status"] in {"PACKAGING", "PACKAGED"}:
        if not isinstance(claim, dict):
            raise StagePlanError("package lifecycle status is missing its claim")
    elif claim is not None:
        raise StagePlanError("execution status contains a premature package claim")
    executed_status = raw.get("executed_status_artifact")
    if raw["status"] in {"PACKAGING", "PACKAGED"}:
        if (
            not isinstance(executed_status, dict)
            or set(executed_status) != {"path", "sha256", "size_bytes"}
            or not isinstance(executed_status.get("path"), str)
            or not isinstance(executed_status.get("sha256"), str)
            or not isinstance(executed_status.get("size_bytes"), int)
            or executed_status["size_bytes"] <= 0
        ):
            raise StagePlanError("package lifecycle status is missing its EXECUTED binding")
    elif executed_status is not None:
        raise StagePlanError("execution status contains a premature EXECUTED binding")
    receipt = raw.get("stage_receipt")
    if raw["status"] == "PACKAGED":
        require_artifact(receipt, name="packaged stage receipt")
    elif receipt is not None:
        raise StagePlanError("release runner status binds a premature stage receipt")
    return raw


def read_status_receipt(plan: dict[str, Any]) -> dict[str, Any]:
    """Read any authentic terminal or resumable runner status receipt."""

    return validate_status_receipt(
        plan,
        load_json(Path(plan["output_root"]) / "runner_status.json"),
    )


def require_status(plan: dict[str, Any]) -> dict[str, Any]:
    raw = read_status_receipt(plan)
    if raw.get("status") not in RESUMABLE_STATUS:
        raise StagePlanError("failed or unknown release runner state requires a fresh output root")
    return raw
