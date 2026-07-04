"""Shared helpers for live dogfood receipt normalization."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from shutil import which
from typing import Any

DebugQueryRunner = Callable[[str], list[dict[str, Any]]]

REQUIRED_V11_SOURCE_COMMITS: tuple[str, ...] = (
    "36094084",  # W6A usage-event schema/service foundation
    "e59e9be1",  # W6B exposure stamping on read surfaces
    "b9e3ade8",  # W6C citation surfaces and usage-loop fixture gate
    "6bf8881f",  # W6D usage-ordered consolidation input
    "4bf80afd",  # W7A usage-aware temporal ranking and forgetting gate
    "2095b616",  # W7C exposure-vs-citation survival semantics
)

DOGFOOD_DEPLOYMENT_BUDGETS: dict[str, float] = {
    "deployed_version_match": 1.0,
    "image_digest_match": 1.0,
    "required_source_commit_coverage": 1.0,
}

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_dogfood_evidence(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        msg = "dogfood evidence must be a JSON object"
        raise TypeError(msg)
    return payload


def load_deployment_evidence(path: Path) -> dict[str, Any]:
    payload = load_dogfood_evidence(path)
    deployment = payload.get("deployment", payload)
    if not isinstance(deployment, dict):
        msg = "deployment evidence must be a JSON object"
        raise TypeError(msg)
    return dict(deployment)


def run_sibyl_debug_query(query: str) -> list[dict[str, Any]]:
    sibyl = which("sibyl")
    if sibyl is None:
        msg = "Required executable not found on PATH: sibyl"
        raise RuntimeError(msg)

    completed = subprocess.run(  # noqa: S603
        (sibyl, "debug", "query", query, "--json"),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        msg = f"sibyl debug query failed with exit {completed.returncode}: {detail}"
        raise RuntimeError(msg)

    payload = json.loads(completed.stdout)
    if not isinstance(payload, dict):
        msg = "sibyl debug query JSON output must be an object"
        raise TypeError(msg)
    error = payload.get("error")
    if error:
        msg = f"sibyl debug query failed: {error}"
        raise RuntimeError(msg)
    rows = payload.get("rows")
    if not isinstance(rows, list):
        msg = "sibyl debug query JSON output missing rows list"
        raise TypeError(msg)
    return [dict(row) for row in rows if isinstance(row, dict)]


def build_deployment_metrics(evidence: dict[str, Any]) -> dict[str, float]:
    deployment = _mapping(evidence.get("deployment"))
    actual_version = _string_value(deployment.get("version") or deployment.get("actual_version"))
    expected_version = _string_value(
        deployment.get("expected_version") or deployment.get("target_version")
    )
    actual_digests = _string_map(
        deployment.get("image_digests") or deployment.get("actual_image_digests")
    )
    expected_digests = _string_map(
        deployment.get("expected_image_digests") or deployment.get("image_digest_pins")
    )
    required_commits = _string_list(deployment.get("required_source_commits")) or list(
        REQUIRED_V11_SOURCE_COMMITS
    )
    source_commits = _string_list(deployment.get("source_commits"))
    source_revision = _string_value(deployment.get("source_revision"))
    if source_revision:
        source_commits.append(source_revision)

    return {
        "deployed_version_match": _truth_metric(
            bool(actual_version)
            and actual_version == expected_version
            and actual_version.startswith("1.1.")
        ),
        "image_digest_match": _coverage(
            expected_digests.keys(),
            (
                key
                for key, expected_digest in expected_digests.items()
                if actual_digests.get(key) == expected_digest
                and _SHA256_RE.match(expected_digest) is not None
            ),
        ),
        "required_source_commit_coverage": _coverage(
            required_commits,
            (
                required
                for required in required_commits
                if any(_commit_matches(required, actual) for actual in source_commits)
            ),
        ),
    }


def validate_metric_budgets(
    metrics: dict[str, Any],
    budgets: dict[str, float],
    *,
    lower_is_better: set[str] | frozenset[str],
) -> list[str]:
    failures: list[str] = []
    for metric, budget in budgets.items():
        value = metrics.get(metric)
        if not isinstance(value, int | float) or isinstance(value, bool):
            failures.append(f"metric {metric!r} must be numeric")
            continue
        if metric in lower_is_better:
            if float(value) > float(budget):
                failures.append(f"metric {metric!r} exceeds budget {budget:g}: {value}")
        elif float(value) < float(budget):
            failures.append(f"metric {metric!r} below budget {budget:g}: {value}")
    return failures


def validate_required_checks(
    receipt: dict[str, Any],
    *,
    required_surfaces: tuple[str, ...],
) -> list[str]:
    checks = receipt.get("checks")
    if not isinstance(checks, list) or not checks:
        return ["dogfood receipt checks must be a non-empty list"]

    failures: list[str] = []
    covered_surfaces: set[str] = set()
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            failures.append(f"dogfood receipt checks[{index}] must be an object")
            continue
        if check.get("status") != "PASS":
            failures.append(f"dogfood receipt checks[{index}] did not pass")
        surfaces = check.get("surfaces")
        if not isinstance(surfaces, list):
            failures.append(f"dogfood receipt checks[{index}].surfaces must be a list")
            continue
        for surface_index, surface in enumerate(surfaces):
            if not isinstance(surface, str) or not surface.strip():
                failures.append(
                    "dogfood receipt "
                    f"checks[{index}].surfaces[{surface_index}] must be a non-empty string"
                )
                continue
            covered_surfaces.add(surface.strip())

    for surface in required_surfaces:
        if surface not in covered_surfaces:
            failures.append(f"dogfood receipt missing required surface {surface!r}")
    return failures


def evidence_checks(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    checks = evidence.get("checks")
    if not isinstance(checks, list):
        return []
    return [dict(check) for check in checks if isinstance(check, dict)]


def list_of_mappings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def string_value(value: Any) -> str:
    return _string_value(value)


def truth_metric(value: bool) -> float:
    return _truth_metric(value)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _string_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key).strip(): str(item).strip()
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _string_value(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _truth_metric(value: bool) -> float:
    return 1.0 if value else 0.0


def _coverage(required: Any, matched: Any) -> float:
    required_items = tuple(str(item).strip() for item in required if str(item).strip())
    if not required_items:
        return 0.0
    matched_items = {str(item).strip() for item in matched if str(item).strip()}
    return len(matched_items) / len(required_items)


def _commit_matches(required: str, actual: str) -> bool:
    required_commit = required.strip().lower()
    actual_commit = actual.strip().lower()
    return bool(required_commit) and actual_commit.startswith(required_commit)
