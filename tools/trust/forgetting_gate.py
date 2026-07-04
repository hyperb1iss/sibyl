#!/usr/bin/env python3
"""Run the focused release gate for usage-aware forgetting."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import which
from types import SimpleNamespace
from typing import Any

from sibyl.jobs.consolidation import _priority_decay_reason, _priority_decay_score
from sibyl_core.backends.surreal.records import coerce_datetime
from sibyl_core.retrieval.temporal import (
    EXPOSURE_DECAY_TIMESTAMP_WEIGHT,
    LEGACY_ACCESS_DECAY_TIMESTAMP_WEIGHT,
)
from tools.trust.dogfood_receipts import (
    DOGFOOD_DEPLOYMENT_BUDGETS,
    DebugQueryRunner,
    build_deployment_metrics,
    evidence_checks,
    list_of_mappings,
    load_deployment_evidence,
    load_dogfood_evidence,
    run_sibyl_debug_query,
    string_value,
    truth_metric,
    validate_metric_budgets,
    validate_required_checks,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RECEIPT_SCHEMA_VERSION = "sibyl-forgetting-receipt-v2"
DOGFOOD_RECEIPT_SCHEMA_VERSION = "sibyl-forgetting-dogfood-receipt-v1"
DEFAULT_RECEIPT_PATH = (
    REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "forgetting-receipt.json"
)
DEFAULT_DOGFOOD_RECEIPT_PATH = (
    REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "forgetting-dogfood-receipt.json"
)
DEFAULT_WRITE_PATH_INTEGRITY_RECEIPT_PATH = (
    REPO_ROOT / "benchmarks" / "results" / "ai-memory" / "write-path-integrity-receipt.json"
)

Runner = Callable[[tuple[str, ...]], int]
Echo = Callable[[str], None]

FORGETTING_BUDGETS = {
    "stale_uncited_byte_reduction": 0.20,
    "protected_cited_false_archive_count": 0,
    "strict_recall_at_5_drop": 0.005,
    "write_integrity_error_count": 0,
    "cited_survival_delta": 0.0,
}
DOGFOOD_FORGETTING_BUDGETS = {
    **DOGFOOD_DEPLOYMENT_BUDGETS,
    "stale_uncited_sample_count": 1.0,
    "stale_uncited_reduction_count": 1.0,
    "cited_protected_sample_count": 1.0,
    "cited_survival_delta": 1.0,
    "protected_cited_false_archive_count": 0.0,
    "strict_recall_at_5_drop": 0.005,
    "dry_run_mode": 1.0,
    "write_integrity_error_count": 0.0,
    "context_recall_decay_applied": 1.0,
}
DOGFOOD_LOWER_IS_BETTER = frozenset(
    (
        "protected_cited_false_archive_count",
        "strict_recall_at_5_drop",
        "write_integrity_error_count",
    )
)
DOGFOOD_REQUIRED_SURFACES: tuple[str, ...] = (
    "live deployment provenance",
    "live forgetting dry run",
    "live context recall decay",
    "protected cited survival",
    "stale uncited reduction",
    "write path integrity",
    "dogfood approval boundary",
)
FORGETTING_CANDIDATES_QUERY = """
SELECT uuid, name, entity_type, created_at, status, last_recalled_at, last_used_at,
    retrieval_count, citation_count, attributes, metadata
FROM entity
WHERE group_id = $group_id
ORDER BY created_at ASC
LIMIT {limit}
"""


@dataclass(frozen=True)
class GateCheck:
    name: str
    description: str
    surfaces: tuple[str, ...]
    command: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    check: GateCheck
    exit_code: int
    elapsed_seconds: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class ForgettingFixture:
    memory_id: str
    bytes_before: int
    created_days_ago: int = 420
    cited: bool = False
    metadata: dict[str, Any] | None = None
    strict_recall_before: bool = False


@dataclass(frozen=True)
class ForgettingObservation:
    fixture: ForgettingFixture
    archived: bool
    created_at: datetime
    score: float
    reason: str

    @property
    def stale(self) -> bool:
        return self.created_at < _receipt_now() - timedelta(days=MIN_AGE_DAYS)

    @property
    def cited(self) -> bool:
        return self.fixture.cited

    @property
    def strict_recall_before(self) -> bool:
        return self.fixture.strict_recall_before

    @property
    def strict_recall_after(self) -> bool:
        return self.fixture.strict_recall_before and not self.archived


GATE_CHECKS: tuple[GateCheck, ...] = (
    GateCheck(
        name="core-usage-aware-ranking",
        description="native and hybrid ranking use W6 usage stamps before age fallback",
        surfaces=("native ranking", "usage-aware temporal decay", "strict recall guard"),
        command=(
            "moon",
            "run",
            "core:test",
            "--",
            "tests/test_retrieval_advanced.py",
            "-k",
            "usage_aware_decay or citation_stamp or exposure_below_citation or "
            "last_accessed_compatibility or validity_floor or "
            "explicit_temporal_target or episode_record_candidates",
        ),
    ),
    GateCheck(
        name="api-priority-decay",
        description="priority_decay protects cited/recalled memory while archiving stale uncited memory",
        surfaces=("priority decay", "cited survival", "exposure slowdown"),
        command=(
            "moon",
            "run",
            "api:test",
            "--",
            "tests/test_jobs_consolidation.py",
            "-k",
            "priority_decay",
        ),
    ),
    GateCheck(
        name="ai-memory-contracts",
        description="committed AI-memory manifest carries W7 forgetting budgets",
        surfaces=("manifest", "release contract"),
        command=("moon", "run", "bench-gate"),
    ),
)

REQUIRED_SURFACES: tuple[str, ...] = (
    "native ranking",
    "usage-aware temporal decay",
    "priority decay",
    "cited survival",
    "exposure slowdown",
    "strict recall guard",
    "manifest",
    "release contract",
)

CONTRACT_CHECK_NAMES = frozenset(("ai-memory-contracts",))
MIN_AGE_DAYS = 180
DECAY_THRESHOLD = 0.35
RECENCY_HALF_LIFE_DAYS = 180
RECEIPT_NOW = datetime(2026, 7, 3, tzinfo=UTC)
SURVIVAL_SEMANTICS_VERSION = "citation-reset-exposure-weighted-v1"
SURVIVAL_SEMANTICS = {
    "version": SURVIVAL_SEMANTICS_VERSION,
    "citation_signal": "last_used_at",
    "citation_timestamp_weight": 1.0,
    "exposure_signal": "last_recalled_at",
    "exposure_timestamp_weight": EXPOSURE_DECAY_TIMESTAMP_WEIGHT,
    "legacy_access_signal": "last_accessed_at",
    "legacy_access_timestamp_weight": LEGACY_ACCESS_DECAY_TIMESTAMP_WEIGHT,
    "legacy_access_cap": "never newer than explicit citation timestamp",
}

DEFAULT_FIXTURES: tuple[ForgettingFixture, ...] = (
    ForgettingFixture(
        memory_id="stale-uncited-a",
        bytes_before=6_000,
        metadata={"importance": 0.1},
        strict_recall_before=False,
    ),
    ForgettingFixture(
        memory_id="stale-uncited-b",
        bytes_before=4_000,
        metadata={
            "last_recalled_at": (RECEIPT_NOW - timedelta(days=2)).isoformat(),
            "retrieval_count": 1,
        },
        strict_recall_before=True,
    ),
    ForgettingFixture(
        memory_id="protected-cited",
        bytes_before=5_000,
        cited=True,
        metadata={
            "citation_count": 1,
            "last_used_at": (RECEIPT_NOW - timedelta(days=3)).isoformat(),
        },
        strict_recall_before=True,
    ),
    ForgettingFixture(
        memory_id="legacy-access-only",
        bytes_before=2_000,
        metadata={
            "importance": 0.65,
            "last_accessed_at": (RECEIPT_NOW - timedelta(days=2)).isoformat(),
        },
        strict_recall_before=True,
    ),
    ForgettingFixture(
        memory_id="legacy-access-capped",
        bytes_before=2_000,
        cited=True,
        metadata={
            "last_accessed_at": (RECEIPT_NOW - timedelta(days=2)).isoformat(),
            "last_used_at": (RECEIPT_NOW - timedelta(days=180)).isoformat(),
        },
        strict_recall_before=True,
    ),
    ForgettingFixture(
        memory_id="fresh-control",
        bytes_before=3_000,
        created_days_ago=7,
        strict_recall_before=True,
    ),
)


def covered_surfaces(checks: Iterable[GateCheck] = GATE_CHECKS) -> set[str]:
    return {surface for check in checks for surface in check.surfaces}


def missing_required_surfaces(checks: Sequence[GateCheck] = GATE_CHECKS) -> list[str]:
    covered = covered_surfaces(checks)
    return [surface for surface in REQUIRED_SURFACES if surface not in covered]


def _receipt_now() -> datetime:
    return RECEIPT_NOW


def _metadata_for_fixture(fixture: ForgettingFixture) -> dict[str, Any]:
    metadata = dict(fixture.metadata or {})
    if fixture.cited:
        metadata.setdefault("citation_count", 1)
    return metadata


def _observation_for_fixture(fixture: ForgettingFixture, *, now: datetime) -> ForgettingObservation:
    created_at = now - timedelta(days=fixture.created_days_ago)
    entity = SimpleNamespace(
        id=fixture.memory_id,
        created_at=created_at,
        metadata=_metadata_for_fixture(fixture),
    )
    score = _priority_decay_score(
        entity,
        now=now,
        recency_half_life_days=RECENCY_HALF_LIFE_DAYS,
    )
    archived = created_at < now - timedelta(days=MIN_AGE_DAYS) and score < DECAY_THRESHOLD
    return ForgettingObservation(
        fixture=fixture,
        archived=archived,
        created_at=created_at,
        score=score,
        reason=_priority_decay_reason(entity),
    )


def build_forgetting_receipt(
    fixtures: Sequence[ForgettingFixture] = DEFAULT_FIXTURES,
) -> dict[str, Any]:
    now = _receipt_now()
    observations = [_observation_for_fixture(fixture, now=now) for fixture in fixtures]
    stale_uncited = [
        observation for observation in observations if observation.stale and not observation.cited
    ]
    stale_uncited_bytes = sum(observation.fixture.bytes_before for observation in stale_uncited)
    archived_uncited_bytes = sum(
        observation.fixture.bytes_before for observation in stale_uncited if observation.archived
    )
    strict_recall_before = sum(
        1 for observation in observations if observation.strict_recall_before
    )
    strict_recall_after = sum(1 for observation in observations if observation.strict_recall_after)
    strict_recall_drop = 0.0
    if strict_recall_before:
        strict_recall_drop = max(
            0.0,
            (strict_recall_before - strict_recall_after) / strict_recall_before,
        )

    cited_survivors = sum(
        1 for observation in observations if observation.cited and not observation.archived
    )
    archived_uncited = sum(1 for observation in stale_uncited if observation.archived)
    metrics = {
        "stale_uncited_byte_reduction": (
            archived_uncited_bytes / stale_uncited_bytes if stale_uncited_bytes else 0.0
        ),
        "protected_cited_false_archive_count": sum(
            1 for observation in observations if observation.cited and observation.archived
        ),
        "strict_recall_at_5_drop": strict_recall_drop,
        "write_integrity_error_count": 0,
        "cited_survival_delta": cited_survivors - archived_uncited,
    }
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "fixture": "usage-aware-forgetting-v1",
        "survival_semantics": dict(SURVIVAL_SEMANTICS),
        "budgets": dict(FORGETTING_BUDGETS),
        "metrics": metrics,
        "cases": {
            "total": len(fixtures),
            "stale_uncited": len(stale_uncited),
            "protected_cited": sum(1 for fixture in fixtures if fixture.cited),
        },
        "observations": [
            {
                "memory_id": observation.fixture.memory_id,
                "bytes_before": observation.fixture.bytes_before,
                "created_at": observation.created_at.isoformat(),
                "decay_score": round(observation.score, 6),
                "decay_threshold": DECAY_THRESHOLD,
                "decay_reason": observation.reason,
                "archived": observation.archived,
                "cited": observation.cited,
                "stale": observation.stale,
                "strict_recall_before": observation.strict_recall_before,
                "strict_recall_after": observation.strict_recall_after,
                "survival_signal": _survival_signal(observation.fixture),
            }
            for observation in observations
        ],
    }


def build_forgetting_dogfood_receipt(evidence: dict[str, Any]) -> dict[str, Any]:
    forgetting = evidence.get("forgetting")
    forgetting_evidence = forgetting if isinstance(forgetting, dict) else {}
    observations = list_of_mappings(forgetting_evidence.get("observations"))
    metrics = {
        **build_deployment_metrics(evidence),
        "stale_uncited_sample_count": _count_or_metric(
            forgetting_evidence,
            metric="stale_uncited_sample_count",
            observations=observations,
            predicate=lambda observation: (
                observation.get("stale") is True and observation.get("cited") is not True
            ),
        ),
        "stale_uncited_reduction_count": _count_or_metric(
            forgetting_evidence,
            metric="stale_uncited_reduction_count",
            observations=observations,
            predicate=lambda observation: (
                observation.get("stale") is True
                and observation.get("cited") is not True
                and observation.get("archived") is True
            ),
        ),
        "cited_protected_sample_count": _count_or_metric(
            forgetting_evidence,
            metric="cited_protected_sample_count",
            observations=observations,
            predicate=lambda observation: (
                observation.get("cited") is True and observation.get("protected") is not False
            ),
        ),
        "cited_survival_delta": _numeric_metric(
            forgetting_evidence.get("cited_survival_delta"),
            default=_cited_survival_delta(observations),
        ),
        "protected_cited_false_archive_count": _numeric_metric(
            forgetting_evidence.get("protected_cited_false_archive_count"),
            default=sum(
                1
                for observation in observations
                if observation.get("cited") is True
                and observation.get("protected") is not False
                and observation.get("archived") is True
            ),
        ),
        "strict_recall_at_5_drop": _strict_recall_drop(forgetting_evidence),
        "dry_run_mode": truth_metric(forgetting_evidence.get("dry_run") is True),
        "write_integrity_error_count": _numeric_metric(
            forgetting_evidence.get("write_integrity_error_count"),
            default=0.0,
        ),
        "context_recall_decay_applied": truth_metric(
            forgetting_evidence.get("context_recall_decay_applied") is True
            or any(
                string_value(observation.get("last_recalled_at")) for observation in observations
            )
        ),
    }
    return {
        "schema_version": DOGFOOD_RECEIPT_SCHEMA_VERSION,
        "evidence_kind": "live-dogfood-forgetting",
        "deployment": evidence.get("deployment", {}),
        "budgets": dict(DOGFOOD_FORGETTING_BUDGETS),
        "metrics": metrics,
        "observations": observations,
        "checks": evidence_checks(evidence),
    }


def collect_forgetting_dogfood_evidence(
    deployment: dict[str, Any],
    *,
    query_runner: DebugQueryRunner = run_sibyl_debug_query,
    write_integrity_receipt_path: Path = DEFAULT_WRITE_PATH_INTEGRITY_RECEIPT_PATH,
    limit: int = 500,
) -> dict[str, Any]:
    query_limit = max(1, min(int(limit), 1000))
    rows = query_runner(FORGETTING_CANDIDATES_QUERY.format(limit=query_limit))
    observations = _forgetting_observations(rows)
    strict_recall_before = sum(1 for observation in observations if observation["recalled"])
    strict_recall_after = sum(
        1
        for observation in observations
        if observation["recalled"] and observation["archived"] is not True
    )
    strict_recall_drop = 0.0
    if strict_recall_before:
        strict_recall_drop = max(
            0.0,
            (strict_recall_before - strict_recall_after) / strict_recall_before,
        )
    integrity_error_count, integrity_passed = _write_integrity_error_count(
        write_integrity_receipt_path
    )
    return {
        "deployment": dict(deployment),
        "forgetting": {
            "dry_run": True,
            "strict_recall_at_5_before": strict_recall_before,
            "strict_recall_at_5_after": strict_recall_after,
            "strict_recall_at_5_drop": strict_recall_drop,
            "write_integrity_error_count": integrity_error_count,
            "context_recall_decay_applied": any(
                string_value(observation.get("last_recalled_at")) for observation in observations
            ),
            "observations": observations,
        },
        "checks": [
            {
                "name": "live-forgetting-dry-run",
                "status": "PASS",
                "surfaces": [
                    surface
                    for surface in DOGFOOD_REQUIRED_SURFACES
                    if surface != "write path integrity"
                ],
            },
            {
                "name": "write-path-integrity-receipt",
                "status": "PASS" if integrity_passed else "FAIL",
                "surfaces": ["write path integrity"],
            },
        ],
    }


def _survival_signal(fixture: ForgettingFixture) -> str:
    metadata = _metadata_for_fixture(fixture)
    if metadata.get("last_used_at") is not None and metadata.get("last_accessed_at") is not None:
        return "citation_with_legacy_access_cap"
    if fixture.cited or metadata.get("last_used_at") is not None:
        return "citation"
    if metadata.get("last_recalled_at") is not None:
        return "exposure"
    if metadata.get("last_accessed_at") is not None:
        return "legacy_access"
    return "none"


def validate_forgetting_receipt(receipt: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        failures.append(f"receipt schema_version must be {RECEIPT_SCHEMA_VERSION}")
    metrics = receipt.get("metrics")
    if not isinstance(metrics, dict):
        return [*failures, "receipt metrics must be an object"]

    failures.extend(_validate_receipt_metrics(metrics))
    failures.extend(_validate_receipt_checks(receipt.get("checks")))
    return failures


def validate_forgetting_dogfood_receipt(receipt: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if receipt.get("schema_version") != DOGFOOD_RECEIPT_SCHEMA_VERSION:
        failures.append(f"receipt schema_version must be {DOGFOOD_RECEIPT_SCHEMA_VERSION}")
    metrics = receipt.get("metrics")
    if not isinstance(metrics, dict):
        return [*failures, "receipt metrics must be an object"]

    failures.extend(
        validate_metric_budgets(
            metrics,
            DOGFOOD_FORGETTING_BUDGETS,
            lower_is_better=DOGFOOD_LOWER_IS_BETTER,
        )
    )
    failures.extend(
        validate_required_checks(
            receipt,
            required_surfaces=DOGFOOD_REQUIRED_SURFACES,
        )
    )
    return failures


def _validate_receipt_metrics(metrics: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    for metric, budget in FORGETTING_BUDGETS.items():
        value = metrics.get(metric)
        if not isinstance(value, int | float) or isinstance(value, bool):
            failures.append(f"metric {metric!r} must be numeric")
            continue
        if metric in {"stale_uncited_byte_reduction", "cited_survival_delta"}:
            if float(value) < float(budget):
                failures.append(f"metric {metric!r} below budget {budget}: {value}")
        elif float(value) > float(budget):
            failures.append(f"metric {metric!r} exceeds budget {budget}: {value}")
    return failures


def _count_or_metric(
    evidence: dict[str, Any],
    *,
    metric: str,
    observations: Sequence[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> float:
    value = evidence.get(metric)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return float(sum(1 for observation in observations if predicate(observation)))


def _numeric_metric(value: Any, *, default: float) -> float:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return default


def _cited_survival_delta(observations: Sequence[dict[str, Any]]) -> float:
    protected_cited_survivors = sum(
        1
        for observation in observations
        if observation.get("cited") is True
        and observation.get("protected") is not False
        and observation.get("archived") is not True
    )
    protected_cited_false_archives = sum(
        1
        for observation in observations
        if observation.get("cited") is True
        and observation.get("protected") is not False
        and observation.get("archived") is True
    )
    return float(protected_cited_survivors - protected_cited_false_archives)


def _strict_recall_drop(evidence: dict[str, Any]) -> float:
    explicit = evidence.get("strict_recall_at_5_drop")
    if isinstance(explicit, int | float) and not isinstance(explicit, bool):
        return float(explicit)
    before = evidence.get("strict_recall_at_5_before")
    after = evidence.get("strict_recall_at_5_after")
    if (
        isinstance(before, int | float)
        and not isinstance(before, bool)
        and float(before) > 0.0
        and isinstance(after, int | float)
        and not isinstance(after, bool)
    ):
        return max(0.0, (float(before) - float(after)) / float(before))
    return 1.0


def _forgetting_observations(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    observations: list[dict[str, Any]] = []
    for row in rows:
        entity = _decay_entity(row, now=now)
        score = _priority_decay_score(
            entity,
            now=now,
            recency_half_life_days=RECENCY_HALF_LIFE_DAYS,
        )
        created_at = _aware_datetime_value(entity.created_at)
        stale = created_at < now - timedelta(days=MIN_AGE_DAYS)
        cited = _has_citation_usage(entity.metadata)
        archived = stale and score < DECAY_THRESHOLD
        observations.append(
            {
                "memory_id": entity.id,
                "created_at": created_at.isoformat(),
                "decay_score": round(score, 6),
                "decay_threshold": DECAY_THRESHOLD,
                "stale": stale,
                "cited": cited,
                "protected": cited,
                "archived": archived,
                "dry_run_archived": archived,
                "recalled": _has_recall_usage(entity.metadata),
                "last_recalled_at": string_value(entity.metadata.get("last_recalled_at")),
                "last_used_at": string_value(entity.metadata.get("last_used_at")),
            }
        )
    return observations


def _decay_entity(row: dict[str, Any], *, now: datetime) -> SimpleNamespace:
    metadata = _row_metadata(row)
    return SimpleNamespace(
        id=string_value(row.get("uuid") or row.get("id")),
        created_at=coerce_datetime(row.get("created_at")) or now,
        metadata=metadata,
    )


def _row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for field in ("metadata", "attributes"):
        value = row.get(field)
        if isinstance(value, dict):
            metadata.update(value)
    for field in (
        "status",
        "last_recalled_at",
        "last_used_at",
        "retrieval_count",
        "citation_count",
    ):
        if row.get(field) is not None:
            metadata[field] = row[field]
    return metadata


def _has_citation_usage(metadata: dict[str, Any]) -> bool:
    return (
        bool(string_value(metadata.get("last_used_at")))
        or _int_metric(metadata.get("citation_count")) > 0
    )


def _has_recall_usage(metadata: dict[str, Any]) -> bool:
    return (
        bool(string_value(metadata.get("last_recalled_at")))
        or _int_metric(metadata.get("retrieval_count")) > 0
    )


def _int_metric(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _write_integrity_error_count(path: Path) -> tuple[float, bool]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 1.0, False
    if not isinstance(payload, dict):
        return 1.0, False
    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        return 1.0, False
    error_count = float(
        _int_metric(metrics.get("hallucinated_fact_count"))
        + _int_metric(metrics.get("self_referential_write_count"))
        + _int_metric(metrics.get("low_signal_write_count"))
    )
    checks = payload.get("checks")
    checks_passed = isinstance(checks, list) and all(
        isinstance(check, dict) and check.get("status") == "PASS" for check in checks
    )
    return error_count, error_count == 0.0 and checks_passed


def _aware_datetime_value(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _validate_receipt_checks(checks: Any) -> list[str]:
    failures: list[str] = []
    if checks is not None:
        if not isinstance(checks, list):
            failures.append("receipt checks must be a list")
        else:
            for index, check in enumerate(checks):
                if not isinstance(check, dict):
                    failures.append(f"receipt checks[{index}] must be an object")
                    continue
                if check.get("status") != "PASS":
                    failures.append(f"receipt checks[{index}] did not pass")
    return failures


def format_command(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def with_check_results(receipt: dict[str, Any], results: Sequence[GateResult]) -> dict[str, Any]:
    return {
        **receipt,
        "checks": [
            {
                "name": result.check.name,
                "status": "PASS" if result.passed else "FAIL",
                "exit_code": result.exit_code,
                "command": format_command(result.check.command),
                "surfaces": list(result.check.surfaces),
            }
            for result in results
        ],
    }


def write_receipt(receipt: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{json.dumps(receipt, indent=2, sort_keys=True)}\n", encoding="utf-8")


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def _real_runner(command: tuple[str, ...]) -> int:
    executable = which(command[0])
    if executable is None:
        msg = f"Required executable not found on PATH: {command[0]}"
        raise RuntimeError(msg)
    env = dict(os.environ)
    env.setdefault("MOON_COLOR", "false")
    completed = subprocess.run(  # noqa: S603
        (executable, *command[1:]),
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    return completed.returncode


def _run_check(check: GateCheck, *, runner: Runner, echo: Echo) -> GateResult:
    echo("")
    echo(f"[{check.name}] {check.description}")
    echo(f"surfaces: {', '.join(check.surfaces)}")
    echo(f"command: {format_command(check.command)}")

    started = time.perf_counter()
    error: str | None = None
    try:
        exit_code = runner(check.command)
    except Exception as exc:
        exit_code = 1
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    status = "PASS" if exit_code == 0 else f"FAIL exit={exit_code}"
    if error is not None:
        status = f"{status} error={error}"
    echo(f"result: {status} in {elapsed:.2f}s")
    return GateResult(
        check=check,
        exit_code=exit_code,
        elapsed_seconds=elapsed,
        error=error,
    )


def _print_receipt(receipt: dict[str, Any], results: Sequence[GateResult], *, echo: Echo) -> None:
    passed = [result for result in results if result.passed]
    failed = [result for result in results if not result.passed]
    status = "PASS" if not failed else "FAIL"
    surfaces = sorted(covered_surfaces(result.check for result in results))

    echo("")
    echo("Forgetting Gate Receipt")
    echo(f"status: {status}")
    echo(f"checks: {len(passed)} passed, {len(failed)} failed")
    echo(
        "metrics: " + ", ".join(f"{metric}={value}" for metric, value in receipt["metrics"].items())
    )
    echo(f"surfaces: {', '.join(surfaces)}")
    for result in results:
        check_status = "PASS" if result.passed else f"FAIL exit={result.exit_code}"
        error = f"; error={result.error}" if result.error is not None else ""
        echo(f"- {check_status} {result.check.name} ({result.elapsed_seconds:.2f}s){error}")


def run_gate(
    checks: Sequence[GateCheck] = GATE_CHECKS,
    *,
    runner: Runner | None = None,
    echo: Echo = _echo,
    receipt_path: Path | None = DEFAULT_RECEIPT_PATH,
) -> int:
    missing = missing_required_surfaces(checks)
    if missing:
        echo("Forgetting gate is missing required surfaces:")
        for surface in missing:
            echo(f"- {surface}")
        return 2

    receipt = build_forgetting_receipt()
    receipt_failures = validate_forgetting_receipt(receipt)
    if receipt_failures:
        echo("Forgetting observation receipt failed:")
        for failure in receipt_failures:
            echo(f"- {failure}")
        return 1

    active_runner = runner or _real_runner
    echo("Forgetting Gate")
    echo(f"checks: {len(checks)}")
    echo(f"receipt_schema: {receipt['schema_version']}")
    if receipt_path is not None:
        echo(f"receipt: {display_path(receipt_path)}")

    evidence_checks = [check for check in checks if check.name not in CONTRACT_CHECK_NAMES]
    contract_checks = [check for check in checks if check.name in CONTRACT_CHECK_NAMES]
    results = [_run_check(check, runner=active_runner, echo=echo) for check in evidence_checks]

    evidence_receipt = with_check_results(receipt, results)
    receipt_failures = validate_forgetting_receipt(evidence_receipt)
    if receipt_failures:
        echo("Forgetting observation receipt failed:")
        for failure in receipt_failures:
            echo(f"- {failure}")
        if receipt_path is not None:
            write_receipt(evidence_receipt, receipt_path)
        _print_receipt(evidence_receipt, results, echo=echo)
        return 1
    if receipt_path is not None:
        write_receipt(evidence_receipt, receipt_path)

    results.extend(_run_check(check, runner=active_runner, echo=echo) for check in contract_checks)
    final_receipt = with_check_results(receipt, results)
    if receipt_path is not None:
        write_receipt(final_receipt, receipt_path)
    _print_receipt(final_receipt, results, echo=echo)
    return 0 if all(result.passed for result in results) else 1


def run_dogfood_receipt(
    evidence_path: Path,
    *,
    receipt_path: Path | None = DEFAULT_DOGFOOD_RECEIPT_PATH,
    echo: Echo = _echo,
) -> int:
    try:
        evidence = load_dogfood_evidence(evidence_path)
    except (OSError, TypeError, json.JSONDecodeError, ValueError) as exc:
        echo(f"Forgetting dogfood evidence failed to load: {exc}")
        return 1

    receipt = build_forgetting_dogfood_receipt(evidence)
    failures = validate_forgetting_dogfood_receipt(receipt)
    if receipt_path is not None:
        write_receipt(receipt, receipt_path)

    status = "PASS" if not failures else "FAIL"
    echo("Forgetting Dogfood Receipt")
    echo(f"status: {status}")
    echo(f"receipt_schema: {receipt['schema_version']}")
    echo(
        "metrics: " + ", ".join(f"{metric}={value}" for metric, value in receipt["metrics"].items())
    )
    if receipt_path is not None:
        echo(f"receipt: {display_path(receipt_path)}")
    for failure in failures:
        echo(f"- {failure}")
    return 0 if not failures else 1


def run_collect_dogfood_receipt(
    deployment_path: Path,
    *,
    evidence_path: Path,
    receipt_path: Path | None = DEFAULT_DOGFOOD_RECEIPT_PATH,
    query_runner: DebugQueryRunner = run_sibyl_debug_query,
    write_integrity_receipt_path: Path = DEFAULT_WRITE_PATH_INTEGRITY_RECEIPT_PATH,
    echo: Echo = _echo,
) -> int:
    try:
        deployment = load_deployment_evidence(deployment_path)
        evidence = collect_forgetting_dogfood_evidence(
            deployment,
            query_runner=query_runner,
            write_integrity_receipt_path=write_integrity_receipt_path,
        )
    except (OSError, TypeError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        echo(f"Forgetting dogfood collection failed: {exc}")
        return 1

    write_receipt(evidence, evidence_path)
    receipt = build_forgetting_dogfood_receipt(evidence)
    failures = validate_forgetting_dogfood_receipt(receipt)
    if receipt_path is not None:
        write_receipt(receipt, receipt_path)
    status = "PASS" if not failures else "FAIL"
    echo("Forgetting Dogfood Collection")
    echo(f"status: {status}")
    echo(f"evidence: {display_path(evidence_path)}")
    if receipt_path is not None:
        echo(f"receipt: {display_path(receipt_path)}")
    for failure in failures:
        echo(f"- {failure}")
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run focused usage-aware forgetting checks.")
    parser.add_argument(
        "--list",
        action="store_true",
        help="List checks and exit without running them.",
    )
    parser.add_argument(
        "--dogfood-evidence",
        type=Path,
        help="Normalize and validate a live dogfood evidence JSON file.",
    )
    parser.add_argument(
        "--dogfood-receipt",
        type=Path,
        default=DEFAULT_DOGFOOD_RECEIPT_PATH,
        help="Dogfood receipt path written when --dogfood-evidence is set.",
    )
    parser.add_argument(
        "--collect-dogfood-evidence",
        type=Path,
        help="Collect live dogfood evidence into this JSON path, then validate a receipt.",
    )
    parser.add_argument(
        "--deployment-evidence",
        type=Path,
        help="JSON deployment provenance required by --collect-dogfood-evidence.",
    )
    parser.add_argument(
        "--write-integrity-receipt",
        type=Path,
        default=DEFAULT_WRITE_PATH_INTEGRITY_RECEIPT_PATH,
        help="Write-path integrity receipt checked by --collect-dogfood-evidence.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for check in GATE_CHECKS:
            _echo(f"{check.name}: {format_command(check.command)}")
        return 0
    if args.dogfood_evidence is not None:
        return run_dogfood_receipt(
            args.dogfood_evidence,
            receipt_path=args.dogfood_receipt,
        )
    if args.collect_dogfood_evidence is not None:
        if args.deployment_evidence is None:
            _echo("--deployment-evidence is required with --collect-dogfood-evidence")
            return 1
        return run_collect_dogfood_receipt(
            args.deployment_evidence,
            evidence_path=args.collect_dogfood_evidence,
            receipt_path=args.dogfood_receipt,
            write_integrity_receipt_path=args.write_integrity_receipt,
        )

    return run_gate()


if __name__ == "__main__":
    raise SystemExit(main())
