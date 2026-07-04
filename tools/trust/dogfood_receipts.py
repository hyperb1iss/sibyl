"""Shared helpers for live dogfood receipt normalization."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from shutil import which
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

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
DEFAULT_HEALTH_URL = "https://sibyl.hyperbliss.tech/api/health"
DEFAULT_API_CONTAINER = "sibyl-backend"
DEFAULT_WEB_CONTAINER = "sibyl-frontend"


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


def build_live_deployment_evidence(
    image_receipt: dict[str, Any],
    health: dict[str, Any],
    container_inspects: list[dict[str, Any]],
    *,
    api_container: str = DEFAULT_API_CONTAINER,
    web_container: str = DEFAULT_WEB_CONTAINER,
) -> dict[str, Any]:
    expected_deployment = _mapping(image_receipt.get("deployment", image_receipt))
    expected_version = _string_value(
        expected_deployment.get("expected_version")
        or expected_deployment.get("target_version")
        or expected_deployment.get("version")
    )
    expected_source_revision = _string_value(expected_deployment.get("source_revision"))
    required_commits = _string_list(expected_deployment.get("required_source_commits")) or list(
        REQUIRED_V11_SOURCE_COMMITS
    )
    expected_digests = _expected_image_digests(expected_deployment, image_receipt)
    api_inspect = _container_inspect(container_inspects, api_container)
    web_inspect = _container_inspect(container_inspects, web_container)
    api_revision = _container_label(api_inspect, "org.opencontainers.image.revision")
    web_revision = _container_label(web_inspect, "org.opencontainers.image.revision")
    api_digest = _container_digest(api_inspect, "sibyl-api")
    web_digest = _container_digest(web_inspect, "sibyl-web")
    revision_matches = (
        bool(expected_source_revision)
        and api_revision == expected_source_revision
        and web_revision == expected_source_revision
    )
    digest_matches = (
        expected_digests.get("api") == api_digest and expected_digests.get("web") == web_digest
    )
    health_status = _string_value(health.get("status"))
    actual_version = _string_value(health.get("version"))
    source_commits = [expected_source_revision, *required_commits] if revision_matches else []
    deployment = {
        "version": actual_version,
        "expected_version": expected_version,
        "target_version": expected_version,
        "health_status": health_status,
        "source_revision": expected_source_revision if revision_matches else "",
        "source_commits": source_commits,
        "required_source_commits": required_commits,
        "image_digests": {
            "api": api_digest,
            "web": web_digest,
        },
        "expected_image_digests": expected_digests,
        "runtime_labels": {
            "api": {"org.opencontainers.image.revision": api_revision},
            "web": {"org.opencontainers.image.revision": web_revision},
        },
    }
    return {
        "schema_version": "sibyl-live-deployment-evidence-v1",
        "deployment": deployment,
        "checks": [
            {
                "name": "live-deployment-provenance",
                "status": "PASS"
                if (
                    health_status == "healthy"
                    and bool(actual_version)
                    and actual_version == expected_version
                    and digest_matches
                    and revision_matches
                )
                else "FAIL",
                "surfaces": ["live deployment provenance"],
            }
        ],
    }


def fetch_health_json(
    url: str = DEFAULT_HEALTH_URL, *, timeout_seconds: float = 10.0
) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=timeout_seconds) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        msg = f"failed to fetch health JSON from {url}: {exc}"
        raise RuntimeError(msg) from exc
    if not isinstance(payload, dict):
        msg = "health JSON must be an object"
        raise TypeError(msg)
    return dict(payload)


def run_ssh_docker_inspect(
    host: str,
    *,
    api_container: str = DEFAULT_API_CONTAINER,
    web_container: str = DEFAULT_WEB_CONTAINER,
) -> list[dict[str, Any]]:
    ssh = which("ssh")
    if ssh is None:
        msg = "Required executable not found on PATH: ssh"
        raise RuntimeError(msg)
    completed = subprocess.run(  # noqa: S603
        (
            ssh,
            host,
            "sudo",
            "docker",
            "inspect",
            api_container,
            web_container,
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        msg = f"ssh docker inspect failed with exit {completed.returncode}: {detail}"
        raise RuntimeError(msg)
    return _inspect_payload(completed.stdout)


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


def write_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _expected_image_digests(
    expected_deployment: dict[str, Any],
    image_receipt: dict[str, Any],
) -> dict[str, str]:
    expected_digests = _string_map(
        expected_deployment.get("expected_image_digests")
        or expected_deployment.get("image_digest_pins")
        or expected_deployment.get("image_digests")
    )
    if expected_digests:
        return expected_digests
    return {
        key: digest
        for key, digest in {
            "api": _string_value(_mapping(image_receipt.get("api")).get("digest")),
            "web": _string_value(_mapping(image_receipt.get("web")).get("digest")),
        }.items()
        if _SHA256_RE.match(digest) is not None
    }


def _container_inspect(
    rows: list[dict[str, Any]],
    container_name: str,
) -> dict[str, Any]:
    normalized_name = container_name.removeprefix("/")
    for row in rows:
        name = _string_value(row.get("Name")).removeprefix("/")
        if name == normalized_name:
            return row
    msg = f"docker inspect payload missing container {container_name!r}"
    raise ValueError(msg)


def _container_label(row: dict[str, Any], label: str) -> str:
    config = _mapping(row.get("Config"))
    labels = _string_map(config.get("Labels"))
    return labels.get(label, "")


def _container_digest(row: dict[str, Any], image_name: str) -> str:
    repo_digests = _string_list(row.get("RepoDigests"))
    for repo_digest in repo_digests:
        if image_name not in repo_digest or "@" not in repo_digest:
            continue
        digest = repo_digest.rsplit("@", 1)[1]
        if _SHA256_RE.match(digest) is not None:
            return digest
    for repo_digest in repo_digests:
        if "@" not in repo_digest:
            continue
        digest = repo_digest.rsplit("@", 1)[1]
        if _SHA256_RE.match(digest) is not None:
            return digest
    return ""


def _inspect_payload(text: str) -> list[dict[str, Any]]:
    payload = json.loads(text)
    if not isinstance(payload, list):
        msg = "docker inspect JSON must be a list"
        raise TypeError(msg)
    return [dict(row) for row in payload if isinstance(row, dict)]


def _load_inspect_rows(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.docker_inspect_json is not None:
        return _inspect_payload(args.docker_inspect_json.read_text(encoding="utf-8"))
    if args.ssh_host:
        return run_ssh_docker_inspect(
            args.ssh_host,
            api_container=args.api_container,
            web_container=args.web_container,
        )
    msg = "--docker-inspect-json or --ssh-host is required"
    raise ValueError(msg)


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


def _run_collect_deployment(args: argparse.Namespace) -> int:
    try:
        image_receipt = load_dogfood_evidence(args.image_receipt)
        if args.health_json is not None:
            health = load_dogfood_evidence(args.health_json)
        else:
            health = fetch_health_json(args.health_url)
        inspect_rows = _load_inspect_rows(args)
        evidence = build_live_deployment_evidence(
            image_receipt,
            health,
            inspect_rows,
            api_container=args.api_container,
            web_container=args.web_container,
        )
        write_json(evidence, args.output)
    except (OSError, TypeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Deployment evidence collection failed: {exc}")  # noqa: T201
        return 1

    metrics = build_deployment_metrics(evidence)
    failures = validate_metric_budgets(
        metrics,
        DOGFOOD_DEPLOYMENT_BUDGETS,
        lower_is_better=frozenset(),
    )
    failures.extend(
        validate_required_checks(
            evidence,
            required_surfaces=("live deployment provenance",),
        )
    )
    print("Dogfood Deployment Evidence")  # noqa: T201
    print(f"status: {'PASS' if not failures else 'FAIL'}")  # noqa: T201
    print(f"output: {args.output}")  # noqa: T201
    for failure in failures:
        print(f"- {failure}")  # noqa: T201
    return 0 if not failures else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect dogfood deployment evidence.")
    subparsers = parser.add_subparsers(dest="command")
    collect = subparsers.add_parser(
        "collect-deployment",
        help="Collect live deployment evidence from health and docker inspect data.",
    )
    collect.add_argument("--image-receipt", type=Path, required=True)
    collect.add_argument("--output", type=Path, required=True)
    collect.add_argument("--health-url", default=DEFAULT_HEALTH_URL)
    collect.add_argument("--health-json", type=Path)
    collect.add_argument("--docker-inspect-json", type=Path)
    collect.add_argument("--ssh-host")
    collect.add_argument("--api-container", default=DEFAULT_API_CONTAINER)
    collect.add_argument("--web-container", default=DEFAULT_WEB_CONTAINER)
    args = parser.parse_args(argv)

    if args.command == "collect-deployment":
        return _run_collect_deployment(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
