#!/usr/bin/env python3
"""Validate the external evidence bundle for enterprise readiness."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import copy2, which
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_DIR = REPO_ROOT / ".moon/cache/enterprise-readiness-evidence"
DEFAULT_MANIFEST = DEFAULT_EVIDENCE_DIR / "enterprise-readiness-evidence.json"
DEFAULT_RECEIPT = DEFAULT_EVIDENCE_DIR / "receipt.json"
SCHEMA_VERSION = "enterprise-readiness-evidence/v1"
PACKAGE_LOCK_DEPENDENCIES = ("authlib", "pyjwt", "argon2-cffi")
PACKAGE_LOCK_PATHS = ("apps/api/pyproject.toml", "uv.lock")
DEFAULT_GITHUB_REPO = "hyperb1iss/sibyl"
GITHUB_RELEASE_IMAGES = ("api", "web")
AUDIT_EXPORT_FORMATS = ("csv", "json")
AUDIT_EXPORT_MAX_LIMIT = 5000
AUDIT_EXPORT_CSV_COLUMNS = (
    "created_at",
    "action",
    "user_id",
    "organization_id",
    "resource",
    "ip_address",
    "user_agent",
    "details",
)
MANUAL_EVIDENCE_KEYS = frozenset(
    {
        "entra_happy_path",
        "entra_missing_role_denial",
        "mcp_cursor_auth",
        "mcp_claude_code_auth",
        "mcp_claude_desktop_auth",
        "kubernetes_restore_drill",
        "restore_recall_sample",
        "idp_role_claim_evidence",
    }
)
SIBYL_HELM_RENDER_ARGS = (
    "template",
    "enterprise",
    "charts/sibyl",
    "--set",
    "ingress.gatewayApi.enabled=true",
    "--set",
    "ingress.gatewayApi.parentRefs[0].name=shared-gateway",
    "--set",
    "ingress.gatewayApi.parentRefs[0].namespace=gateway-system",
    "--set",
    "ingress.classic.enabled=true",
    "--set",
    "networkPolicy.enabled=true",
    "--set",
    "podSecurity.enforceRestricted=true",
    "--set",
    "bootstrap.enabled=true",
    "--set",
    "breakGlass.enabled=true",
    "--set",
    "breakGlass.existingSecret=sibyl-break-glass",
    "--set",
    "breakGlass.allowedIPs[0]=203.0.113.0/24",
    "--set",
    "backend.surreal.existingSecret=sibyl-surrealdb-root",
    "--set",
    "oidc.providers[0].name=entra",
    "--set",
    "oidc.providers[0].issuer=https://login.microsoftonline.com/example/v2.0",
    "--set",
    "oidc.providers[0].client_id=sibyl-client",
    "--set",
    "oidc.providers[0].client_secret_env=SIBYL_OIDC_ENTRA_CLIENT_SECRET",
)
SURREALDB_HELM_RENDER_ARGS = (
    "template",
    "enterprise-db",
    "charts/surrealdb",
    "--set",
    "snapshot.enabled=true",
    "--set",
    "export.enabled=true",
    "--set",
    "restoreDrill.enabled=true",
    "--set",
    "snapshot.persistentVolumeClaimName=surrealdb-data",
    "--set",
    "snapshot.volumeSnapshotClassName=csi-snapshots",
    "--set",
    "export.destination.path=/backups",
    "--set",
    "restoreDrill.source.path=/backups",
)
SIBYL_RENDER_SNIPPETS = (
    "kind: HTTPRoute",
    "kind: Ingress",
    "kind: Namespace",
    "kind: NetworkPolicy",
    "kind: Job",
)
SURREALDB_RENDER_SNIPPETS = (
    "kind: CronJob",
    "kind: Job",
    "kind: Role",
    "kind: RoleBinding",
    "app.kubernetes.io/component: restore-drill",
)
RENDERED_HELM_RENDER_SPECS: tuple[
    tuple[str, str, Sequence[str], Sequence[str]],
    ...,
] = (
    (
        "Sibyl enterprise chart",
        "sibyl-enterprise.yaml",
        SIBYL_HELM_RENDER_ARGS,
        SIBYL_RENDER_SNIPPETS,
    ),
    (
        "SurrealDB enterprise chart",
        "surrealdb-enterprise.yaml",
        SURREALDB_HELM_RENDER_ARGS,
        SURREALDB_RENDER_SNIPPETS,
    ),
)

type JsonObject = dict[str, Any]


@dataclass(frozen=True)
class EvidenceRequirement:
    key: str
    gate: str
    description: str


REQUIRED_EVIDENCE: tuple[EvidenceRequirement, ...] = (
    EvidenceRequirement(
        key="entra_happy_path",
        gate="auth",
        description="Real Entra dev-tenant OIDC login reaches Sibyl with a valid role claim.",
    ),
    EvidenceRequirement(
        key="entra_missing_role_denial",
        gate="auth",
        description="Real Entra dev-tenant OIDC login without a Sibyl role is denied.",
    ),
    EvidenceRequirement(
        key="mcp_cursor_auth",
        gate="mcp",
        description="Cursor authenticates against Sibyl after the OAuth/Authlib changes.",
    ),
    EvidenceRequirement(
        key="mcp_claude_code_auth",
        gate="mcp",
        description="Claude Code authenticates against Sibyl after the OAuth/Authlib changes.",
    ),
    EvidenceRequirement(
        key="mcp_claude_desktop_auth",
        gate="mcp",
        description="Claude Desktop authenticates against Sibyl after the OAuth/Authlib changes.",
    ),
    EvidenceRequirement(
        key="kubernetes_restore_drill",
        gate="data-durability",
        description="A local Kubernetes restore drill imports an export and verifies row counts.",
    ),
    EvidenceRequirement(
        key="restore_recall_sample",
        gate="data-durability",
        description="The restored Kubernetes runtime returns a sampled recall query.",
    ),
    EvidenceRequirement(
        key="rendered_helm_manifests",
        gate="security-review-packet",
        description="Rendered enterprise Helm manifests for Sibyl and SurrealDB are captured.",
    ),
    EvidenceRequirement(
        key="idp_role_claim_evidence",
        gate="security-review-packet",
        description="IdP role-claim screenshot or config export shows the Sibyl role mapping.",
    ),
    EvidenceRequirement(
        key="audit_export_sample",
        gate="security-review-packet",
        description="Admin audit JSON or CSV export sample is captured from the target runtime.",
    ),
    EvidenceRequirement(
        key="image_sbom_receipt",
        gate="security-review-packet",
        description="Release run produced the image SBOM artifact.",
    ),
    EvidenceRequirement(
        key="cosign_signature_receipt",
        gate="security-review-packet",
        description="Release run produced a Cosign signing receipt for published images.",
    ),
    EvidenceRequirement(
        key="package_lock_diff",
        gate="security-review-packet",
        description="Package lock diff for Authlib, PyJWT, and argon2-cffi is captured.",
    ),
)


class EvidenceFailure(RuntimeError):
    pass


def _json_bytes(payload: Mapping[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_artifact_path(evidence_dir: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        msg = "artifact path must be a non-empty string"
        raise EvidenceFailure(msg)

    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        msg = f"artifact path must stay inside evidence dir: {value}"
        raise EvidenceFailure(msg)

    artifact_path = (evidence_dir / path).resolve()
    evidence_root = evidence_dir.resolve()
    if artifact_path != evidence_root and evidence_root not in artifact_path.parents:
        msg = f"artifact path escaped evidence dir: {value}"
        raise EvidenceFailure(msg)
    return artifact_path


def _load_manifest(path: Path) -> JsonObject:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"manifest not found: {path}"
        raise EvidenceFailure(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"manifest is not valid JSON: {exc}"
        raise EvidenceFailure(msg) from exc

    if not isinstance(payload, dict):
        msg = "manifest root must be an object"
        raise EvidenceFailure(msg)
    return payload


def _git_output(args: Sequence[str]) -> str:
    git = which("git")
    if git is None:
        msg = "git executable not found"
        raise EvidenceFailure(msg)
    result = subprocess.run(  # noqa: S603
        [git, *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        msg = f"git {' '.join(args)} failed: {details}"
        raise EvidenceFailure(msg)
    return result.stdout.strip()


def _helm_output(args: Sequence[str]) -> str:
    helm = which("helm")
    if helm is None:
        msg = "helm executable not found"
        raise EvidenceFailure(msg)
    result = subprocess.run(  # noqa: S603
        [helm, *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        msg = f"helm {' '.join(args)} failed: {details}"
        raise EvidenceFailure(msg)
    return result.stdout.strip()


def _gh_text_output(args: Sequence[str]) -> str:
    gh = which("gh")
    if gh is None:
        msg = "gh executable not found"
        raise EvidenceFailure(msg)
    result = subprocess.run(  # noqa: S603
        [gh, *args],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        msg = f"gh {' '.join(args)} failed: {details}"
        raise EvidenceFailure(msg)
    return result.stdout.strip()


def _gh_json_output(args: Sequence[str]) -> JsonObject:
    output = _gh_text_output(args)
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        msg = f"gh {' '.join(args)} did not return JSON: {exc}"
        raise EvidenceFailure(msg) from exc
    if not isinstance(payload, dict):
        msg = f"gh {' '.join(args)} JSON root must be an object"
        raise EvidenceFailure(msg)
    return payload


def _http_get_bytes(url: str, headers: Mapping[str, str]) -> bytes:
    request = Request(url, headers=dict(headers), method="GET")  # noqa: S310
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310
            return response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        suffix = f": {detail}" if detail else ""
        msg = f"audit export request failed with HTTP {exc.code}{suffix}"
        raise EvidenceFailure(msg) from exc
    except URLError as exc:
        msg = f"audit export request failed: {exc.reason}"
        raise EvidenceFailure(msg) from exc


def _require_item(
    *,
    evidence_dir: Path,
    items: Mapping[str, object],
    requirement: EvidenceRequirement,
) -> JsonObject:
    raw_item = items.get(requirement.key)
    if not isinstance(raw_item, dict):
        msg = f"missing evidence item: {requirement.key}"
        raise EvidenceFailure(msg)
    item = cast(Mapping[str, object], raw_item)

    status = item.get("status")
    if status != "PASS":
        msg = f"{requirement.key} status must be PASS, got {status!r}"
        raise EvidenceFailure(msg)

    artifacts = item.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        msg = f"{requirement.key} must list at least one artifact"
        raise EvidenceFailure(msg)

    verified_artifacts: list[JsonObject] = []
    for raw_artifact in artifacts:
        if not isinstance(raw_artifact, dict):
            msg = f"{requirement.key} artifacts must be objects"
            raise EvidenceFailure(msg)
        artifact = cast(Mapping[str, object], raw_artifact)
        artifact_rel_path = artifact.get("path")
        artifact_path = _safe_artifact_path(evidence_dir, artifact_rel_path)
        if not artifact_path.is_file():
            msg = f"{requirement.key} artifact not found: {artifact_rel_path}"
            raise EvidenceFailure(msg)
        if artifact_path.stat().st_size == 0:
            msg = f"{requirement.key} artifact is empty: {artifact_rel_path}"
            raise EvidenceFailure(msg)

        actual_sha = _sha256(artifact_path)
        expected_sha = artifact.get("sha256")
        if expected_sha != actual_sha:
            msg = (
                f"{requirement.key} artifact hash mismatch for {artifact_rel_path}: "
                f"expected {expected_sha!r}, got {actual_sha}"
            )
            raise EvidenceFailure(msg)

        verified_artifacts.append(
            {
                "path": artifact_rel_path,
                "sha256": actual_sha,
                "bytes": artifact_path.stat().st_size,
            }
        )

    freshness_issues = _inspect_generated_artifact_freshness(
        evidence_dir=evidence_dir,
        requirement=requirement,
        item=item,
    )
    if freshness_issues:
        msg = f"{requirement.key} {freshness_issues[0]}"
        raise EvidenceFailure(msg)

    return {
        "key": requirement.key,
        "gate": requirement.gate,
        "description": requirement.description,
        "artifacts": verified_artifacts,
    }


def validate_manifest(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    evidence_dir: Path | None = None,
) -> JsonObject:
    manifest_path = manifest_path.resolve()
    evidence_dir = evidence_dir.resolve() if evidence_dir is not None else manifest_path.parent
    payload = _load_manifest(manifest_path)

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        msg = f"schema_version must be {SCHEMA_VERSION!r}, got {schema_version!r}"
        raise EvidenceFailure(msg)

    items = payload.get("items")
    if not isinstance(items, dict):
        msg = "manifest must include an items object"
        raise EvidenceFailure(msg)

    verified = [
        _require_item(evidence_dir=evidence_dir, items=items, requirement=requirement)
        for requirement in REQUIRED_EVIDENCE
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "validated_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "evidence_dir": str(evidence_dir),
        "items": verified,
    }


def sync_manifest_hashes(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    evidence_dir: Path | None = None,
) -> JsonObject:
    manifest_path = manifest_path.resolve()
    evidence_dir = evidence_dir.resolve() if evidence_dir is not None else manifest_path.parent
    payload = _load_manifest(manifest_path)

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        msg = f"schema_version must be {SCHEMA_VERSION!r}, got {schema_version!r}"
        raise EvidenceFailure(msg)

    items = payload.get("items")
    if not isinstance(items, dict):
        msg = "manifest must include an items object"
        raise EvidenceFailure(msg)

    synced_items: list[JsonObject] = []
    for requirement in REQUIRED_EVIDENCE:
        raw_item = items.get(requirement.key)
        if not isinstance(raw_item, dict):
            msg = f"missing evidence item: {requirement.key}"
            raise EvidenceFailure(msg)
        item = cast(JsonObject, raw_item)

        artifacts = item.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            msg = f"{requirement.key} must list at least one artifact"
            raise EvidenceFailure(msg)

        synced_artifacts: list[JsonObject] = []
        for raw_artifact in artifacts:
            if not isinstance(raw_artifact, dict):
                msg = f"{requirement.key} artifacts must be objects"
                raise EvidenceFailure(msg)
            artifact = cast(JsonObject, raw_artifact)
            artifact_rel_path = artifact.get("path")
            artifact_path = _safe_artifact_path(evidence_dir, artifact_rel_path)
            if not artifact_path.is_file():
                msg = f"{requirement.key} artifact not found: {artifact_rel_path}"
                raise EvidenceFailure(msg)
            if artifact_path.stat().st_size == 0:
                msg = f"{requirement.key} artifact is empty: {artifact_rel_path}"
                raise EvidenceFailure(msg)

            actual_sha = _sha256(artifact_path)
            artifact["sha256"] = actual_sha
            synced_artifacts.append(
                {
                    "path": artifact_rel_path,
                    "sha256": actual_sha,
                    "bytes": artifact_path.stat().st_size,
                }
            )

        synced_items.append(
            {
                "key": requirement.key,
                "gate": requirement.gate,
                "artifacts": synced_artifacts,
            }
        )

    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "SYNCED",
        "synced_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "evidence_dir": str(evidence_dir),
        "items": synced_items,
    }


def inspect_manifest(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    evidence_dir: Path | None = None,
) -> JsonObject:
    manifest_path = manifest_path.resolve()
    evidence_dir = evidence_dir.resolve() if evidence_dir is not None else manifest_path.parent
    payload = _load_manifest(manifest_path)

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        msg = f"schema_version must be {SCHEMA_VERSION!r}, got {schema_version!r}"
        raise EvidenceFailure(msg)

    items = payload.get("items")
    if not isinstance(items, dict):
        msg = "manifest must include an items object"
        raise EvidenceFailure(msg)

    item_reports: list[JsonObject] = []
    summary = {"PASS": 0, "INCOMPLETE": 0}
    for requirement in REQUIRED_EVIDENCE:
        raw_item = items.get(requirement.key)
        issues: list[str] = []
        artifacts_report: list[JsonObject] = []
        status: object = None

        if not isinstance(raw_item, dict):
            issues.append("missing evidence item")
        else:
            item = cast(Mapping[str, object], raw_item)
            status = item.get("status")
            if status != "PASS":
                issues.append(f"status is {status!r}, not PASS")

            artifacts = item.get("artifacts")
            if not isinstance(artifacts, list) or not artifacts:
                issues.append("must list at least one artifact")
            else:
                for raw_artifact in artifacts:
                    artifact_report = _inspect_artifact(
                        evidence_dir=evidence_dir,
                        raw_artifact=raw_artifact,
                    )
                    artifacts_report.append(artifact_report)
                    issues.extend(cast(list[str], artifact_report["issues"]))
            issues.extend(
                _inspect_generated_artifact_freshness(
                    evidence_dir=evidence_dir,
                    requirement=requirement,
                    item=item,
                )
            )

        item_status = "PASS" if status == "PASS" and not issues else "INCOMPLETE"
        summary[item_status] += 1
        item_reports.append(
            {
                "key": requirement.key,
                "gate": requirement.gate,
                "status": item_status,
                "manifest_status": status,
                "issues": issues,
                "artifacts": artifacts_report,
            }
        )

    overall_status = "PASS" if summary["INCOMPLETE"] == 0 else "INCOMPLETE"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": overall_status,
        "inspected_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "evidence_dir": str(evidence_dir),
        "summary": summary,
        "items": item_reports,
    }


def _inspect_artifact(*, evidence_dir: Path, raw_artifact: object) -> JsonObject:
    if not isinstance(raw_artifact, dict):
        return {
            "path": None,
            "issues": ["artifact entry must be an object"],
        }

    artifact = cast(Mapping[str, object], raw_artifact)
    artifact_rel_path = artifact.get("path")
    issues: list[str] = []
    report: JsonObject = {
        "path": artifact_rel_path,
        "expected_sha256": artifact.get("sha256"),
        "issues": issues,
    }

    try:
        artifact_path = _safe_artifact_path(evidence_dir, artifact_rel_path)
    except EvidenceFailure as exc:
        issues.append(str(exc))
        return report

    if not artifact_path.is_file():
        issues.append(f"artifact not found: {artifact_rel_path}")
        return report

    size = artifact_path.stat().st_size
    report["bytes"] = size
    if size == 0:
        issues.append(f"artifact is empty: {artifact_rel_path}")
        return report

    actual_sha = _sha256(artifact_path)
    report["actual_sha256"] = actual_sha
    if artifact.get("sha256") != actual_sha:
        issues.append(f"artifact hash mismatch: {artifact_rel_path}")

    return report


def _inspect_generated_artifact_freshness(
    *,
    evidence_dir: Path,
    requirement: EvidenceRequirement,
    item: Mapping[str, object],
) -> list[str]:
    if requirement.key == "package_lock_diff":
        return _inspect_package_lock_diff_freshness(evidence_dir=evidence_dir, item=item)
    if requirement.key == "rendered_helm_manifests":
        return _inspect_rendered_helm_freshness(evidence_dir=evidence_dir, item=item)
    return []


def _inspect_package_lock_diff_freshness(
    *,
    evidence_dir: Path,
    item: Mapping[str, object],
) -> list[str]:
    receipt_path, diff_path = _package_lock_artifact_paths(evidence_dir=evidence_dir, item=item)
    if receipt_path is None or not receipt_path.is_file():
        return []

    receipt_text = receipt_path.read_text(encoding="utf-8")
    base_ref = _receipt_field(receipt_text, "Base ref")
    head_ref = _receipt_field(receipt_text, "Head ref")
    head_sha = _receipt_field(receipt_text, "Head sha")
    if not base_ref or not head_ref or not head_sha:
        return []

    return [
        *_package_lock_head_issues(head_ref=head_ref, head_sha=head_sha),
        *_package_lock_diff_issues(base_ref=base_ref, head_ref=head_ref, diff_path=diff_path),
    ]


def _package_lock_artifact_paths(
    *,
    evidence_dir: Path,
    item: Mapping[str, object],
) -> tuple[Path | None, Path | None]:
    artifacts = item.get("artifacts")
    if not isinstance(artifacts, list):
        return None, None

    receipt_path: Path | None = None
    diff_path: Path | None = None
    for raw_artifact in artifacts:
        if not isinstance(raw_artifact, dict):
            continue
        artifact = cast(Mapping[str, object], raw_artifact)
        artifact_path_value = artifact.get("path")
        if artifact_path_value == "package_lock_diff/receipt.md":
            receipt_path = _safe_artifact_path(evidence_dir, artifact_path_value)
        elif artifact_path_value == "package_lock_diff/package-lock.diff":
            diff_path = _safe_artifact_path(evidence_dir, artifact_path_value)
    return receipt_path, diff_path


def _package_lock_head_issues(*, head_ref: str, head_sha: str) -> list[str]:
    try:
        current_head_sha = _git_output(["rev-parse", head_ref])
    except EvidenceFailure as exc:
        return [f"head ref cannot be resolved: {exc}"]

    if current_head_sha != head_sha:
        return [
            f"head sha is stale: {head_ref} resolves to {current_head_sha}, receipt has {head_sha}"
        ]
    return []


def _package_lock_diff_issues(
    *,
    base_ref: str,
    head_ref: str,
    diff_path: Path | None,
) -> list[str]:
    if diff_path is None or not diff_path.is_file():
        return []

    diff_args = [
        "diff",
        "--no-ext-diff",
        "--unified=80",
        f"{base_ref}..{head_ref}",
        "--",
        *PACKAGE_LOCK_PATHS,
    ]
    try:
        expected_diff = _git_output(diff_args).rstrip() + "\n"
    except EvidenceFailure as exc:
        return [f"package lock diff cannot be regenerated: {exc}"]

    actual_diff = diff_path.read_text(encoding="utf-8")
    if actual_diff != expected_diff:
        return [f"package lock diff artifact is stale for {base_ref}..{head_ref}"]
    return []


def _receipt_field(receipt_text: str, label: str) -> str | None:
    match = re.search(rf"^- {re.escape(label)}: (.+)$", receipt_text, re.MULTILINE)
    return match.group(1).strip() if match else None


def _inspect_rendered_helm_freshness(
    *,
    evidence_dir: Path,
    item: Mapping[str, object],
) -> list[str]:
    artifact_paths = _rendered_helm_artifact_paths(evidence_dir=evidence_dir, item=item)
    if not artifact_paths:
        return []

    try:
        expected_manifests = _rendered_helm_expected_manifests()
    except EvidenceFailure as exc:
        return [f"rendered Helm artifact cannot be regenerated: {exc}"]

    issues: list[str] = []
    for _, filename, _, _ in RENDERED_HELM_RENDER_SPECS:
        artifact_path = artifact_paths.get(filename)
        if artifact_path is None or not artifact_path.is_file():
            continue
        actual_manifest = artifact_path.read_text(encoding="utf-8")
        if actual_manifest != expected_manifests[filename]:
            issues.append(f"rendered Helm artifact is stale: rendered_helm_manifests/{filename}")
    return issues


def _rendered_helm_artifact_paths(
    *,
    evidence_dir: Path,
    item: Mapping[str, object],
) -> dict[str, Path]:
    artifacts = item.get("artifacts")
    if not isinstance(artifacts, list):
        return {}

    canonical_paths = {
        f"rendered_helm_manifests/{filename}": filename
        for _, filename, _, _ in RENDERED_HELM_RENDER_SPECS
    }
    artifact_paths: dict[str, Path] = {}
    for raw_artifact in artifacts:
        if not isinstance(raw_artifact, dict):
            continue
        artifact = cast(Mapping[str, object], raw_artifact)
        artifact_path_value = artifact.get("path")
        if not isinstance(artifact_path_value, str):
            continue
        filename = canonical_paths.get(artifact_path_value)
        if filename is not None:
            artifact_paths[filename] = _safe_artifact_path(evidence_dir, artifact_path_value)
    return artifact_paths


def _rendered_helm_expected_manifests() -> dict[str, str]:
    return {
        filename: _helm_output(render_args).rstrip() + "\n"
        for _, filename, render_args, _ in RENDERED_HELM_RENDER_SPECS
    }


def capture_package_lock_diff(
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    *,
    base_ref: str,
    head_ref: str = "HEAD",
) -> JsonObject:
    if not base_ref.strip():
        msg = "base ref is required to capture package lock diff"
        raise EvidenceFailure(msg)

    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / DEFAULT_MANIFEST.name
    if not manifest_path.exists():
        write_template(evidence_dir)
    payload = _load_manifest(manifest_path)

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        msg = f"schema_version must be {SCHEMA_VERSION!r}, got {schema_version!r}"
        raise EvidenceFailure(msg)

    items = payload.get("items")
    if not isinstance(items, dict):
        msg = "manifest must include an items object"
        raise EvidenceFailure(msg)

    diff_args = [
        "diff",
        "--no-ext-diff",
        "--unified=80",
        f"{base_ref}..{head_ref}",
        "--",
        *PACKAGE_LOCK_PATHS,
    ]
    diff_text = _git_output(diff_args)
    missing = [
        dependency
        for dependency in PACKAGE_LOCK_DEPENDENCIES
        if dependency not in diff_text.lower()
    ]
    if missing:
        msg = f"package lock diff does not mention required dependencies: {', '.join(missing)}"
        raise EvidenceFailure(msg)

    artifact_dir = evidence_dir / "package_lock_diff"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    diff_path = artifact_dir / "package-lock.diff"
    receipt_path = artifact_dir / "receipt.md"
    diff_path.write_text(diff_text.rstrip() + "\n", encoding="utf-8")

    base_sha = _git_output(["rev-parse", base_ref])
    head_sha = _git_output(["rev-parse", head_ref])
    receipt_path.write_text(
        _package_lock_receipt(
            base_ref=base_ref,
            base_sha=base_sha,
            head_ref=head_ref,
            head_sha=head_sha,
            diff_args=diff_args,
        ),
        encoding="utf-8",
    )

    items = cast(dict[str, object], items)
    raw_item = items.get("package_lock_diff")
    item = cast(JsonObject, raw_item.copy()) if isinstance(raw_item, dict) else {}
    update_payload: JsonObject = {
        "gate": "security-review-packet",
        "status": "PASS",
        "description": "Package lock diff for Authlib, PyJWT, and argon2-cffi is captured.",
        "artifacts": [
            _artifact_entry(evidence_dir, receipt_path),
            _artifact_entry(evidence_dir, diff_path),
        ],
        "notes": f"Captured from {base_ref}..{head_ref}.",
    }
    item.update(update_payload)
    items["package_lock_diff"] = item
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "captured_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "evidence_dir": str(evidence_dir.resolve()),
        "item": item,
    }


def capture_rendered_helm_manifests(
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
) -> JsonObject:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / DEFAULT_MANIFEST.name
    if not manifest_path.exists():
        write_template(evidence_dir)
    payload = _load_manifest(manifest_path)

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        msg = f"schema_version must be {SCHEMA_VERSION!r}, got {schema_version!r}"
        raise EvidenceFailure(msg)

    items = payload.get("items")
    if not isinstance(items, dict):
        msg = "manifest must include an items object"
        raise EvidenceFailure(msg)

    artifact_dir = evidence_dir / "rendered_helm_manifests"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    rendered_artifacts: list[tuple[str, Path, Sequence[str]]] = []
    for label, filename, render_args, required_snippets in RENDERED_HELM_RENDER_SPECS:
        rendered = _helm_output(render_args)
        missing = [snippet for snippet in required_snippets if snippet not in rendered]
        if missing:
            msg = f"{label} rendered manifest missing required snippets: {', '.join(missing)}"
            raise EvidenceFailure(msg)

        artifact_path = artifact_dir / filename
        artifact_path.write_text(rendered.rstrip() + "\n", encoding="utf-8")
        rendered_artifacts.append((label, artifact_path, render_args))

    receipt_path = artifact_dir / "receipt.md"
    receipt_path.write_text(
        _rendered_helm_receipt(rendered_artifacts),
        encoding="utf-8",
    )

    items = cast(dict[str, object], items)
    raw_item = items.get("rendered_helm_manifests")
    item = cast(JsonObject, raw_item.copy()) if isinstance(raw_item, dict) else {}
    update_payload: JsonObject = {
        "gate": "security-review-packet",
        "status": "PASS",
        "description": "Rendered enterprise Helm manifests for Sibyl and SurrealDB are captured.",
        "artifacts": [
            _artifact_entry(evidence_dir, receipt_path),
            *[
                _artifact_entry(evidence_dir, artifact_path)
                for _, artifact_path, _ in rendered_artifacts
            ],
        ],
        "notes": "Captured from local Helm chart renders.",
    }
    item.update(update_payload)
    items["rendered_helm_manifests"] = item
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "captured_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "evidence_dir": str(evidence_dir.resolve()),
        "item": item,
    }


def capture_github_release_evidence(
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    *,
    run_id: str,
    repo: str = DEFAULT_GITHUB_REPO,
) -> JsonObject:
    if not run_id.strip():
        msg = "GitHub run id is required to capture release evidence"
        raise EvidenceFailure(msg)

    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / DEFAULT_MANIFEST.name
    if not manifest_path.exists():
        write_template(evidence_dir)
    payload = _load_manifest(manifest_path)
    items = _manifest_items(payload)

    run = _gh_json_output(
        [
            "run",
            "view",
            run_id,
            "--repo",
            repo,
            "--json",
            "conclusion,createdAt,databaseId,headBranch,headSha,jobs,name,url,workflowName",
        ]
    )
    _require_publish_run(run, run_id=run_id)

    jobs = _run_jobs(run)
    security_jobs = {
        image: _require_successful_job(jobs, f"◆ Docker: Security {image}")
        for image in GITHUB_RELEASE_IMAGES
    }
    sign_jobs = {
        image: _require_successful_job(jobs, f"◆ Docker: Sign {image}")
        for image in GITHUB_RELEASE_IMAGES
    }

    artifacts_payload = _gh_json_output(
        ["api", f"repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"]
    )
    artifacts = _run_artifacts(artifacts_payload)
    sbom_artifacts = {
        image: _require_sbom_artifact(artifacts, image) for image in GITHUB_RELEASE_IMAGES
    }

    sbom_dir = evidence_dir / "image_sbom_receipt"
    sign_dir = evidence_dir / "cosign_signature_receipt"
    sbom_dir.mkdir(parents=True, exist_ok=True)
    sign_dir.mkdir(parents=True, exist_ok=True)

    downloaded_sboms: set[Path] = set()
    for image, artifact in sbom_artifacts.items():
        artifact_name = _artifact_name(artifact)
        _download_github_artifact(
            repo=repo,
            run_id=run_id,
            artifact_name=artifact_name,
            destination=sbom_dir,
        )
        image_files = sorted(
            file for file in _files_under(sbom_dir) if f"sibyl-{image}-" in file.name
        )
        if not image_files:
            msg = f"SBOM artifact download produced no files for {image}: {artifact_name}"
            raise EvidenceFailure(msg)
        downloaded_sboms.update(image_files)

    sign_logs: list[Path] = []
    for image, job in sign_jobs.items():
        job_id = _job_database_id(job)
        log_text = _gh_text_output(
            ["run", "view", run_id, "--repo", repo, "--job", job_id, "--log"]
        )
        if not log_text:
            msg = f"Cosign sign job log is empty for {image}: {job_id}"
            raise EvidenceFailure(msg)
        log_path = sign_dir / f"sign-{image}.log"
        log_path.write_text(log_text.rstrip() + "\n", encoding="utf-8")
        sign_logs.append(log_path)

    sbom_receipt_path = sbom_dir / "receipt.md"
    sign_receipt_path = sign_dir / "receipt.md"
    sbom_receipt_path.write_text(
        _github_sbom_receipt(
            repo=repo,
            run=run,
            security_jobs=security_jobs,
            sbom_artifacts=sbom_artifacts,
            downloaded_sboms=sorted(downloaded_sboms),
        ),
        encoding="utf-8",
    )
    sign_receipt_path.write_text(
        _github_cosign_receipt(
            repo=repo,
            run=run,
            sign_jobs=sign_jobs,
            sign_logs=sign_logs,
        ),
        encoding="utf-8",
    )

    _update_manifest_item(
        items=items,
        key="image_sbom_receipt",
        gate="security-review-packet",
        description="Release run produced the image SBOM artifact.",
        artifacts=[
            _artifact_entry(evidence_dir, sbom_receipt_path),
            *[_artifact_entry(evidence_dir, path) for path in sorted(downloaded_sboms)],
        ],
        notes=f"Captured from GitHub Actions run {run_id}.",
    )
    _update_manifest_item(
        items=items,
        key="cosign_signature_receipt",
        gate="security-review-packet",
        description="Release run produced a Cosign signing receipt for published images.",
        artifacts=[
            _artifact_entry(evidence_dir, sign_receipt_path),
            *[_artifact_entry(evidence_dir, path) for path in sign_logs],
        ],
        notes=f"Captured from GitHub Actions run {run_id}.",
    )
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "captured_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "evidence_dir": str(evidence_dir.resolve()),
        "items": {
            "image_sbom_receipt": items["image_sbom_receipt"],
            "cosign_signature_receipt": items["cosign_signature_receipt"],
        },
    }


def capture_audit_export_sample(
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    *,
    api_url: str,
    access_token: str,
    export_format: str = "json",
    limit: int = 1000,
) -> JsonObject:
    if export_format not in AUDIT_EXPORT_FORMATS:
        msg = f"audit export format must be one of {', '.join(AUDIT_EXPORT_FORMATS)}"
        raise EvidenceFailure(msg)
    if not access_token.strip():
        msg = "audit export access token is required"
        raise EvidenceFailure(msg)
    if limit < 1 or limit > AUDIT_EXPORT_MAX_LIMIT:
        msg = f"audit export limit must be between 1 and {AUDIT_EXPORT_MAX_LIMIT}"
        raise EvidenceFailure(msg)

    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / DEFAULT_MANIFEST.name
    if not manifest_path.exists():
        write_template(evidence_dir)
    payload = _load_manifest(manifest_path)
    items = _manifest_items(payload)

    endpoint_url = _audit_export_url(
        api_url,
        export_format=export_format,
        limit=limit,
    )
    body = _http_get_bytes(
        endpoint_url,
        {
            "Accept": "application/json" if export_format == "json" else "text/csv",
            "Authorization": f"Bearer {access_token}",
        },
    )
    event_count = _validate_audit_export(body, export_format=export_format)

    artifact_dir = evidence_dir / "audit_export_sample"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    export_path = artifact_dir / f"audit-export.{export_format}"
    receipt_path = artifact_dir / "receipt.md"
    export_path.write_bytes(body.rstrip() + b"\n")
    receipt_path.write_text(
        _audit_export_receipt(
            endpoint_url=endpoint_url,
            export_format=export_format,
            event_count=event_count,
            limit=limit,
        ),
        encoding="utf-8",
    )

    _update_manifest_item(
        items=items,
        key="audit_export_sample",
        gate="security-review-packet",
        description="Admin audit JSON or CSV export sample is captured from the target runtime.",
        artifacts=[
            _artifact_entry(evidence_dir, receipt_path),
            _artifact_entry(evidence_dir, export_path),
        ],
        notes="Captured from a live admin audit export endpoint.",
    )
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "captured_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "evidence_dir": str(evidence_dir.resolve()),
        "item": items["audit_export_sample"],
    }


def capture_manual_evidence(
    evidence_dir: Path = DEFAULT_EVIDENCE_DIR,
    *,
    key: str,
    source_artifacts: Sequence[Path],
    runtime: str,
    flow: str,
    result: str,
    captured_by: str,
    redactions: str = "none",
) -> JsonObject:
    requirement = _requirement_by_key(key)
    if requirement.key not in MANUAL_EVIDENCE_KEYS:
        msg = f"{requirement.key} cannot be captured manually; use its dedicated capture command"
        raise EvidenceFailure(msg)
    if not source_artifacts:
        msg = "manual evidence requires at least one artifact"
        raise EvidenceFailure(msg)

    runtime = _require_manual_field("runtime", runtime)
    flow = _require_manual_field("flow", flow)
    result = _require_manual_field("result", result)
    captured_by = _require_manual_field("captured by", captured_by)
    redactions = redactions.strip() or "none"

    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / DEFAULT_MANIFEST.name
    if not manifest_path.exists():
        write_template(evidence_dir)
    payload = _load_manifest(manifest_path)
    items = _manifest_items(payload)

    artifact_dir = evidence_dir / requirement.key
    artifact_dir.mkdir(parents=True, exist_ok=True)
    copied_artifacts = [
        _copy_manual_artifact(source_path, artifact_dir=artifact_dir)
        for source_path in source_artifacts
    ]
    receipt_path = artifact_dir / "receipt.md"
    receipt_path.write_text(
        _manual_evidence_receipt(
            requirement=requirement,
            runtime=runtime,
            flow=flow,
            result=result,
            captured_by=captured_by,
            redactions=redactions,
            artifact_paths=copied_artifacts,
        ),
        encoding="utf-8",
    )

    _update_manifest_item(
        items=items,
        key=requirement.key,
        gate=requirement.gate,
        description=requirement.description,
        artifacts=[
            _artifact_entry(evidence_dir, receipt_path),
            *[_artifact_entry(evidence_dir, path) for path in copied_artifacts],
        ],
        notes="Captured from external/manual enterprise validation evidence.",
    )
    manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PASS",
        "captured_at": datetime.now(UTC).isoformat(),
        "manifest": str(manifest_path),
        "evidence_dir": str(evidence_dir.resolve()),
        "item": items[requirement.key],
    }


def _audit_export_url(api_url: str, *, export_format: str, limit: int) -> str:
    raw_url = api_url.strip()
    if not raw_url:
        msg = "audit export API URL is required"
        raise EvidenceFailure(msg)

    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        msg = f"audit export API URL must be absolute HTTP(S): {api_url}"
        raise EvidenceFailure(msg)

    path = parsed.path.rstrip("/")
    if path.endswith("/api/admin/audit/export"):
        endpoint_path = path
    elif path.endswith("/api"):
        endpoint_path = f"{path}/admin/audit/export"
    elif path:
        endpoint_path = f"{path}/api/admin/audit/export"
    else:
        endpoint_path = "/api/admin/audit/export"

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in {"format", "limit"}
    ]
    query_pairs.extend([("format", export_format), ("limit", str(limit))])
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            endpoint_path,
            urlencode(query_pairs),
            "",
        )
    )


def _validate_audit_export(body: bytes, *, export_format: str) -> int:
    if not body.strip():
        msg = "audit export sample is empty"
        raise EvidenceFailure(msg)
    if export_format == "json":
        return _validate_audit_json_export(body)
    return _validate_audit_csv_export(body)


def _validate_audit_json_export(body: bytes) -> int:
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"audit JSON export is not valid JSON: {exc}"
        raise EvidenceFailure(msg) from exc
    if not isinstance(payload, dict):
        msg = "audit JSON export root must be an object"
        raise EvidenceFailure(msg)

    events = payload.get("events")
    total = payload.get("total")
    if not isinstance(events, list):
        msg = "audit JSON export must include an events list"
        raise EvidenceFailure(msg)
    if not isinstance(total, int):
        msg = "audit JSON export must include an integer total"
        raise EvidenceFailure(msg)
    if not events:
        msg = "audit JSON export must include at least one event"
        raise EvidenceFailure(msg)
    first_event = events[0]
    if not isinstance(first_event, dict) or not first_event.get("action"):
        msg = "audit JSON export events must include an action"
        raise EvidenceFailure(msg)
    return len(events)


def _validate_audit_csv_export(body: bytes) -> int:
    text = body.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing_columns = [column for column in AUDIT_EXPORT_CSV_COLUMNS if column not in fieldnames]
    if missing_columns:
        msg = f"audit CSV export is missing required columns: {', '.join(missing_columns)}"
        raise EvidenceFailure(msg)

    rows = list(reader)
    if not rows:
        msg = "audit CSV export must include at least one event"
        raise EvidenceFailure(msg)
    if not rows[0].get("action"):
        msg = "audit CSV export events must include an action"
        raise EvidenceFailure(msg)
    return len(rows)


def _manifest_items(payload: JsonObject) -> dict[str, object]:
    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        msg = f"schema_version must be {SCHEMA_VERSION!r}, got {schema_version!r}"
        raise EvidenceFailure(msg)

    items = payload.get("items")
    if not isinstance(items, dict):
        msg = "manifest must include an items object"
        raise EvidenceFailure(msg)
    return cast(dict[str, object], items)


def _require_publish_run(run: Mapping[str, object], *, run_id: str) -> None:
    if run.get("conclusion") != "success":
        msg = f"GitHub run {run_id} must have conclusion success, got {run.get('conclusion')!r}"
        raise EvidenceFailure(msg)
    workflow_name = run.get("workflowName")
    run_name = run.get("name")
    if workflow_name != "Publish" and run_name != "Publish":
        msg = (
            f"GitHub run {run_id} must be the Publish workflow, "
            f"got workflowName={workflow_name!r}, name={run_name!r}"
        )
        raise EvidenceFailure(msg)


def _run_jobs(run: Mapping[str, object]) -> list[Mapping[str, object]]:
    jobs = run.get("jobs")
    if not isinstance(jobs, list):
        msg = "GitHub run payload must include a jobs list"
        raise EvidenceFailure(msg)
    return [cast(Mapping[str, object], job) for job in jobs if isinstance(job, dict)]


def _require_successful_job(
    jobs: Sequence[Mapping[str, object]],
    name: str,
) -> Mapping[str, object]:
    matches = [job for job in jobs if job.get("name") == name]
    if not matches:
        msg = f"GitHub run is missing required job: {name}"
        raise EvidenceFailure(msg)
    job = matches[0]
    if job.get("conclusion") != "success":
        msg = f"GitHub job {name} must have conclusion success, got {job.get('conclusion')!r}"
        raise EvidenceFailure(msg)
    return job


def _run_artifacts(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        msg = "GitHub artifacts payload must include an artifacts list"
        raise EvidenceFailure(msg)
    return [
        cast(Mapping[str, object], artifact) for artifact in artifacts if isinstance(artifact, dict)
    ]


def _require_sbom_artifact(
    artifacts: Sequence[Mapping[str, object]],
    image: str,
) -> Mapping[str, object]:
    prefix = f"sibyl-{image}-"
    matches = [
        artifact
        for artifact in artifacts
        if isinstance(artifact.get("name"), str)
        and cast(str, artifact["name"]).startswith(prefix)
        and cast(str, artifact["name"]).endswith("-sbom")
    ]
    if not matches:
        msg = f"GitHub run is missing required SBOM artifact for {image}"
        raise EvidenceFailure(msg)
    if len(matches) > 1:
        names = ", ".join(_artifact_name(match) for match in matches)
        msg = f"GitHub run has ambiguous SBOM artifacts for {image}: {names}"
        raise EvidenceFailure(msg)

    artifact = matches[0]
    if artifact.get("expired") is True:
        msg = f"GitHub SBOM artifact is expired for {image}: {_artifact_name(artifact)}"
        raise EvidenceFailure(msg)
    size = artifact.get("size_in_bytes")
    if not isinstance(size, int) or size <= 0:
        msg = f"GitHub SBOM artifact is empty for {image}: {_artifact_name(artifact)}"
        raise EvidenceFailure(msg)
    return artifact


def _artifact_name(artifact: Mapping[str, object]) -> str:
    name = artifact.get("name")
    if not isinstance(name, str) or not name:
        msg = "GitHub artifact is missing a name"
        raise EvidenceFailure(msg)
    return name


def _job_database_id(job: Mapping[str, object]) -> str:
    database_id = job.get("databaseId")
    if not isinstance(database_id, int):
        msg = f"GitHub job {job.get('name')!r} is missing databaseId"
        raise EvidenceFailure(msg)
    return str(database_id)


def _download_github_artifact(
    *,
    repo: str,
    run_id: str,
    artifact_name: str,
    destination: Path,
) -> None:
    _gh_text_output(
        [
            "run",
            "download",
            run_id,
            "--repo",
            repo,
            "--dir",
            str(destination),
            "--name",
            artifact_name,
        ]
    )


def _files_under(path: Path) -> set[Path]:
    return {file for file in path.rglob("*") if file.is_file()}


def _update_manifest_item(
    *,
    items: dict[str, object],
    key: str,
    gate: str,
    description: str,
    artifacts: list[JsonObject],
    notes: str,
) -> None:
    raw_item = items.get(key)
    item = cast(JsonObject, raw_item.copy()) if isinstance(raw_item, dict) else {}
    item.update(
        {
            "gate": gate,
            "status": "PASS",
            "description": description,
            "artifacts": artifacts,
            "notes": notes,
        }
    )
    items[key] = item


def _requirement_by_key(key: str) -> EvidenceRequirement:
    for requirement in REQUIRED_EVIDENCE:
        if requirement.key == key:
            return requirement
    msg = f"unknown evidence item: {key}"
    raise EvidenceFailure(msg)


def _require_manual_field(label: str, value: str) -> str:
    stripped = value.strip()
    if stripped:
        return stripped
    msg = f"manual evidence {label} is required"
    raise EvidenceFailure(msg)


def _copy_manual_artifact(source_path: Path, *, artifact_dir: Path) -> Path:
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file():
        msg = f"manual evidence artifact not found: {source_path}"
        raise EvidenceFailure(msg)
    if source_path.stat().st_size == 0:
        msg = f"manual evidence artifact is empty: {source_path}"
        raise EvidenceFailure(msg)
    if source_path.name == "receipt.md":
        msg = "manual evidence artifact must not be named receipt.md"
        raise EvidenceFailure(msg)

    target_path = artifact_dir / source_path.name
    if source_path != target_path.resolve():
        copy2(source_path, target_path)
    return target_path


def _artifact_entry(evidence_dir: Path, artifact_path: Path) -> JsonObject:
    relative_path = artifact_path.relative_to(evidence_dir).as_posix()
    return {
        "path": relative_path,
        "sha256": _sha256(artifact_path),
    }


def _package_lock_receipt(
    *,
    base_ref: str,
    base_sha: str,
    head_ref: str,
    head_sha: str,
    diff_args: Sequence[str],
) -> str:
    command = "git " + " ".join(diff_args)
    dependencies = ", ".join(PACKAGE_LOCK_DEPENDENCIES)
    paths = ", ".join(PACKAGE_LOCK_PATHS)
    return f"""# package_lock_diff

- Gate: security-review-packet
- Status: PASS
- Required proof: Package lock diff for Authlib, PyJWT, and argon2-cffi is captured.
- Captured at: {datetime.now(UTC).isoformat()}
- Captured by: enterprise_readiness_evidence.py
- Runtime or environment: local git checkout
- Base ref: {base_ref}
- Base sha: {base_sha}
- Head ref: {head_ref}
- Head sha: {head_sha}
- Dependency names: {dependencies}
- Diff paths: {paths}
- Command: `{command}`
- Observed result: package-lock diff artifact captured and dependency names verified.
- Redactions: none
- Related artifact paths:
  - package_lock_diff/package-lock.diff
"""


def _rendered_helm_receipt(
    rendered_artifacts: Sequence[tuple[str, Path, Sequence[str]]],
) -> str:
    lines = [
        "# rendered_helm_manifests",
        "",
        "- Gate: security-review-packet",
        "- Status: PASS",
        "- Required proof: Rendered enterprise Helm manifests for Sibyl and SurrealDB are captured.",
        f"- Captured at: {datetime.now(UTC).isoformat()}",
        "- Captured by: enterprise_readiness_evidence.py",
        "- Runtime or environment: local Helm chart render",
        "- Commands or manual flow:",
    ]
    for label, _, render_args in rendered_artifacts:
        lines.append(f"  - {label}: `helm {' '.join(render_args)}`")
    lines.extend(
        [
            "- Observed result: rendered manifests captured and required enterprise resources verified.",
            "- Redactions: none",
            "- Related artifact paths:",
        ]
    )
    for _, artifact_path, _ in rendered_artifacts:
        lines.append(f"  - rendered_helm_manifests/{artifact_path.name}")
    return "\n".join(lines) + "\n"


def _github_sbom_receipt(
    *,
    repo: str,
    run: Mapping[str, object],
    security_jobs: Mapping[str, Mapping[str, object]],
    sbom_artifacts: Mapping[str, Mapping[str, object]],
    downloaded_sboms: Sequence[Path],
) -> str:
    lines = [
        "# image_sbom_receipt",
        "",
        "- Gate: security-review-packet",
        "- Status: PASS",
        "- Required proof: Release run produced the image SBOM artifact.",
        f"- Captured at: {datetime.now(UTC).isoformat()}",
        "- Captured by: enterprise_readiness_evidence.py",
        "- Runtime or environment: GitHub Actions publish workflow",
        f"- Repository: {repo}",
        f"- Run ID: {run.get('databaseId')}",
        f"- Run URL: {run.get('url')}",
        f"- Head branch: {run.get('headBranch')}",
        f"- Head sha: {run.get('headSha')}",
        f"- Created at: {run.get('createdAt')}",
        "- Observed result: SBOM artifacts downloaded after successful Docker security jobs.",
        "- Security jobs:",
    ]
    for image, job in security_jobs.items():
        lines.append(f"  - {image}: {job.get('name')} ({job.get('conclusion')})")
    lines.append("- SBOM artifacts:")
    for image, artifact in sbom_artifacts.items():
        lines.append(
            f"  - {image}: {_artifact_name(artifact)} ({artifact.get('size_in_bytes')} bytes)"
        )
    lines.extend(["- Redactions: none", "- Related artifact paths:"])
    for path in downloaded_sboms:
        lines.append(f"  - image_sbom_receipt/{path.name}")
    return "\n".join(lines) + "\n"


def _github_cosign_receipt(
    *,
    repo: str,
    run: Mapping[str, object],
    sign_jobs: Mapping[str, Mapping[str, object]],
    sign_logs: Sequence[Path],
) -> str:
    lines = [
        "# cosign_signature_receipt",
        "",
        "- Gate: security-review-packet",
        "- Status: PASS",
        "- Required proof: Release run produced a Cosign signing receipt for published images.",
        f"- Captured at: {datetime.now(UTC).isoformat()}",
        "- Captured by: enterprise_readiness_evidence.py",
        "- Runtime or environment: GitHub Actions publish workflow",
        f"- Repository: {repo}",
        f"- Run ID: {run.get('databaseId')}",
        f"- Run URL: {run.get('url')}",
        f"- Head branch: {run.get('headBranch')}",
        f"- Head sha: {run.get('headSha')}",
        f"- Created at: {run.get('createdAt')}",
        "- Observed result: Cosign signing jobs completed and logs were captured.",
        "- Sign jobs:",
    ]
    for image, job in sign_jobs.items():
        lines.append(f"  - {image}: {job.get('name')} ({job.get('conclusion')})")
    lines.extend(["- Redactions: GitHub log masking applies", "- Related artifact paths:"])
    for path in sign_logs:
        lines.append(f"  - cosign_signature_receipt/{path.name}")
    return "\n".join(lines) + "\n"


def _audit_export_receipt(
    *,
    endpoint_url: str,
    export_format: str,
    event_count: int,
    limit: int,
) -> str:
    return f"""# audit_export_sample

- Gate: security-review-packet
- Status: PASS
- Required proof: Admin audit JSON or CSV export sample is captured from the target runtime.
- Captured at: {datetime.now(UTC).isoformat()}
- Captured by: enterprise_readiness_evidence.py
- Runtime or environment: live Sibyl admin audit endpoint
- Export URL: {endpoint_url}
- Export format: {export_format}
- Export limit: {limit}
- Observed result: audit export sample captured with {event_count} event(s).
- Redactions: bearer token omitted
- Related artifact paths:
  - audit_export_sample/audit-export.{export_format}
"""


def _manual_evidence_receipt(
    *,
    requirement: EvidenceRequirement,
    runtime: str,
    flow: str,
    result: str,
    captured_by: str,
    redactions: str,
    artifact_paths: Sequence[Path],
) -> str:
    lines = [
        f"# {requirement.key}",
        "",
        f"- Gate: {requirement.gate}",
        "- Status: PASS",
        f"- Required proof: {requirement.description}",
        f"- Captured at: {datetime.now(UTC).isoformat()}",
        f"- Captured by: {captured_by}",
        f"- Runtime or environment: {runtime}",
        f"- Commands or manual flow: {flow}",
        f"- Observed result: {result}",
        f"- Redactions: {redactions}",
        "- Related artifact paths:",
    ]
    for path in artifact_paths:
        lines.append(f"  - {requirement.key}/{path.name}")
    return "\n".join(lines) + "\n"


def build_template_payload(evidence_dir: Path | None = None) -> JsonObject:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "items": {
            requirement.key: {
                "gate": requirement.gate,
                "status": "TODO",
                "description": requirement.description,
                "artifacts": [_template_artifact_entry(evidence_dir, requirement)],
                "notes": "",
            }
            for requirement in REQUIRED_EVIDENCE
        },
    }


def _template_artifact_entry(
    evidence_dir: Path | None,
    requirement: EvidenceRequirement,
) -> JsonObject:
    relative_path = f"{requirement.key}/receipt.md"
    artifact: JsonObject = {
        "path": relative_path,
        "sha256": "<fill-after-capture>",
    }
    if evidence_dir is not None:
        receipt_path = evidence_dir / relative_path
        if receipt_path.is_file() and receipt_path.stat().st_size > 0:
            artifact["sha256"] = _sha256(receipt_path)
    return artifact


def _receipt_template(requirement: EvidenceRequirement) -> str:
    return f"""# {requirement.key}

- Gate: {requirement.gate}
- Status: TODO
- Required proof: {requirement.description}
- Captured at:
- Captured by:
- Runtime or environment:
- Commands or manual flow:
- Observed result:
- Redactions:
- Related artifact paths:

Replace this stub with the real receipt before marking the manifest item PASS.
After manual edits, update manifest artifact hashes:

```bash
moon run enterprise-readiness-evidence -- --sync-hashes
```
"""


def write_template(evidence_dir: Path, *, force: bool = False) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / DEFAULT_MANIFEST.name
    if manifest_path.exists() and not force:
        msg = f"manifest already exists: {manifest_path}; pass --force-template to overwrite"
        raise EvidenceFailure(msg)

    for requirement in REQUIRED_EVIDENCE:
        receipt_path = evidence_dir / requirement.key / "receipt.md"
        if receipt_path.exists() and not force:
            continue
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(_receipt_template(requirement), encoding="utf-8")

    manifest_path.write_text(
        json.dumps(build_template_payload(evidence_dir), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    return manifest_path


def run_gate(
    *,
    manifest_path: Path = DEFAULT_MANIFEST,
    evidence_dir: Path | None = None,
    receipt_path: Path = DEFAULT_RECEIPT,
) -> int:
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        receipt = validate_manifest(manifest_path, evidence_dir=evidence_dir)
    except EvidenceFailure as exc:
        receipt = {
            "schema_version": SCHEMA_VERSION,
            "status": "FAIL",
            "validated_at": datetime.now(UTC).isoformat(),
            "manifest": str(manifest_path),
            "error": str(exc),
        }
        receipt_path.write_bytes(_json_bytes(receipt))
        sys.stdout.write("Enterprise readiness evidence: FAIL\n")
        sys.stdout.write(f"{exc}\n")
        return 1

    receipt_path.write_bytes(_json_bytes(receipt))
    sys.stdout.write("Enterprise readiness evidence: PASS\n")
    sys.stdout.write(f"verified items: {len(receipt['items'])}\n")
    sys.stdout.write(f"receipt: {receipt_path}\n")
    return 0


def _list_requirements() -> None:
    for requirement in REQUIRED_EVIDENCE:
        sys.stdout.write(f"{requirement.key} [{requirement.gate}]\n")
        sys.stdout.write(f"  {requirement.description}\n")


def _handle_init_template(path: Path, *, force: bool) -> int:
    try:
        manifest_path = write_template(path, force=force)
    except EvidenceFailure as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    sys.stdout.write(f"wrote template: {manifest_path}\n")
    return 0


def _handle_sync_hashes(manifest_path: Path, evidence_dir: Path | None) -> int:
    try:
        receipt = sync_manifest_hashes(
            manifest_path,
            evidence_dir=evidence_dir,
        )
    except EvidenceFailure as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    artifact_count = sum(len(item["artifacts"]) for item in receipt["items"])
    sys.stdout.write(f"synced artifact hashes: {artifact_count}\n")
    sys.stdout.write(f"manifest: {receipt['manifest']}\n")
    return 0


def _handle_status(manifest_path: Path, evidence_dir: Path | None) -> int:
    try:
        report = inspect_manifest(manifest_path, evidence_dir=evidence_dir)
    except EvidenceFailure as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    _print_status_report(report)
    return 0 if report["status"] == "PASS" else 1


def _handle_package_lock_capture(
    evidence_dir: Path | None,
    *,
    base_ref: str,
    head_ref: str,
) -> int:
    try:
        receipt = capture_package_lock_diff(
            DEFAULT_EVIDENCE_DIR if evidence_dir is None else evidence_dir,
            base_ref=base_ref,
            head_ref=head_ref,
        )
    except EvidenceFailure as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    sys.stdout.write("captured package lock diff evidence\n")
    sys.stdout.write(f"manifest: {receipt['manifest']}\n")
    return 0


def _handle_rendered_helm_capture(evidence_dir: Path | None) -> int:
    try:
        receipt = capture_rendered_helm_manifests(
            DEFAULT_EVIDENCE_DIR if evidence_dir is None else evidence_dir,
        )
    except EvidenceFailure as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    sys.stdout.write("captured rendered Helm manifest evidence\n")
    sys.stdout.write(f"manifest: {receipt['manifest']}\n")
    return 0


def _handle_github_release_capture(
    evidence_dir: Path | None,
    *,
    run_id: str,
    repo: str,
) -> int:
    try:
        receipt = capture_github_release_evidence(
            DEFAULT_EVIDENCE_DIR if evidence_dir is None else evidence_dir,
            run_id=run_id,
            repo=repo,
        )
    except EvidenceFailure as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    sys.stdout.write("captured GitHub release SBOM and Cosign evidence\n")
    sys.stdout.write(f"manifest: {receipt['manifest']}\n")
    return 0


def _handle_audit_export_capture(
    evidence_dir: Path | None,
    *,
    api_url: str,
    token_env: str,
    export_format: str,
    limit: int,
) -> int:
    try:
        receipt = capture_audit_export_sample(
            DEFAULT_EVIDENCE_DIR if evidence_dir is None else evidence_dir,
            api_url=api_url,
            access_token=os.environ.get(token_env, ""),
            export_format=export_format,
            limit=limit,
        )
    except EvidenceFailure as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    sys.stdout.write("captured audit export sample evidence\n")
    sys.stdout.write(f"manifest: {receipt['manifest']}\n")
    return 0


def _handle_manual_evidence_capture(
    evidence_dir: Path | None,
    *,
    key: str,
    source_artifacts: Sequence[Path],
    runtime: str,
    flow: str,
    result: str,
    captured_by: str,
    redactions: str,
) -> int:
    try:
        receipt = capture_manual_evidence(
            DEFAULT_EVIDENCE_DIR if evidence_dir is None else evidence_dir,
            key=key,
            source_artifacts=source_artifacts,
            runtime=runtime,
            flow=flow,
            result=result,
            captured_by=captured_by,
            redactions=redactions,
        )
    except EvidenceFailure as exc:
        sys.stdout.write(f"{exc}\n")
        return 1
    sys.stdout.write(f"captured manual evidence for {key}\n")
    sys.stdout.write(f"manifest: {receipt['manifest']}\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--init-template", type=Path)
    parser.add_argument("--force-template", action="store_true")
    parser.add_argument("--sync-hashes", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--capture-package-lock-diff")
    parser.add_argument("--capture-rendered-helm-manifests", action="store_true")
    parser.add_argument("--capture-github-release-evidence")
    parser.add_argument("--github-repo", default=DEFAULT_GITHUB_REPO)
    parser.add_argument("--capture-audit-export-sample")
    parser.add_argument("--audit-export-format", choices=AUDIT_EXPORT_FORMATS, default="json")
    parser.add_argument("--audit-export-limit", type=int, default=1000)
    parser.add_argument("--audit-export-token-env", default="SIBYL_ACCESS_TOKEN")
    parser.add_argument("--capture-manual-evidence")
    parser.add_argument("--manual-artifact", type=Path, action="append", default=[])
    parser.add_argument("--manual-runtime", default="")
    parser.add_argument("--manual-flow", default="")
    parser.add_argument("--manual-result", default="")
    parser.add_argument("--manual-captured-by", default=os.environ.get("USER", "operator"))
    parser.add_argument("--manual-redactions", default="none")
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        _list_requirements()
        exit_code = 0
    elif args.init_template is not None:
        exit_code = _handle_init_template(args.init_template, force=args.force_template)
    elif args.sync_hashes:
        exit_code = _handle_sync_hashes(args.manifest, args.evidence_dir)
    elif args.status:
        exit_code = _handle_status(args.manifest, args.evidence_dir)
    elif args.capture_package_lock_diff is not None:
        exit_code = _handle_package_lock_capture(
            args.evidence_dir,
            base_ref=args.capture_package_lock_diff,
            head_ref=args.head_ref,
        )
    elif args.capture_rendered_helm_manifests:
        exit_code = _handle_rendered_helm_capture(args.evidence_dir)
    elif args.capture_github_release_evidence is not None:
        exit_code = _handle_github_release_capture(
            args.evidence_dir,
            run_id=args.capture_github_release_evidence,
            repo=args.github_repo,
        )
    elif args.capture_audit_export_sample is not None:
        exit_code = _handle_audit_export_capture(
            args.evidence_dir,
            api_url=args.capture_audit_export_sample,
            token_env=args.audit_export_token_env,
            export_format=args.audit_export_format,
            limit=args.audit_export_limit,
        )
    elif args.capture_manual_evidence is not None:
        exit_code = _handle_manual_evidence_capture(
            args.evidence_dir,
            key=args.capture_manual_evidence,
            source_artifacts=args.manual_artifact,
            runtime=args.manual_runtime,
            flow=args.manual_flow,
            result=args.manual_result,
            captured_by=args.manual_captured_by,
            redactions=args.manual_redactions,
        )
    else:
        exit_code = run_gate(
            manifest_path=args.manifest,
            evidence_dir=args.evidence_dir,
            receipt_path=args.receipt,
        )
    return exit_code


def _print_status_report(report: JsonObject) -> None:
    summary = cast(Mapping[str, int], report["summary"])
    sys.stdout.write(f"Enterprise readiness evidence: {report['status']}\n")
    sys.stdout.write(f"summary: {summary['PASS']} PASS, {summary['INCOMPLETE']} INCOMPLETE\n")
    for item in cast(list[JsonObject], report["items"]):
        sys.stdout.write(f"{item['key']}: {item['status']}\n")
        for issue in cast(list[str], item["issues"]):
            sys.stdout.write(f"  - {issue}\n")


if __name__ == "__main__":
    raise SystemExit(main())
