#!/usr/bin/env python3
"""Validate the external evidence bundle for enterprise readiness."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from shutil import which
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_DIR = REPO_ROOT / ".moon/cache/enterprise-readiness-evidence"
DEFAULT_MANIFEST = DEFAULT_EVIDENCE_DIR / "enterprise-readiness-evidence.json"
DEFAULT_RECEIPT = DEFAULT_EVIDENCE_DIR / "receipt.json"
SCHEMA_VERSION = "enterprise-readiness-evidence/v1"
PACKAGE_LOCK_DEPENDENCIES = ("authlib", "pyjwt", "argon2-cffi")
PACKAGE_LOCK_PATHS = ("apps/api/pyproject.toml", "uv.lock")
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

    render_specs = (
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
    artifact_dir = evidence_dir / "rendered_helm_manifests"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    rendered_artifacts: list[tuple[str, Path, Sequence[str]]] = []
    for label, filename, render_args, required_snippets in render_specs:
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


def build_template_payload() -> JsonObject:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "items": {
            requirement.key: {
                "gate": requirement.gate,
                "status": "TODO",
                "description": requirement.description,
                "artifacts": [
                    {
                        "path": f"{requirement.key}/receipt.md",
                        "sha256": "<fill-after-capture>",
                    }
                ],
                "notes": "",
            }
            for requirement in REQUIRED_EVIDENCE
        },
    }


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
After capture, update the manifest sha256 for this file:

```bash
shasum -a 256 {requirement.key}/receipt.md
```
"""


def write_template(evidence_dir: Path, *, force: bool = False) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / DEFAULT_MANIFEST.name
    if manifest_path.exists() and not force:
        msg = f"manifest already exists: {manifest_path}; pass --force-template to overwrite"
        raise EvidenceFailure(msg)

    manifest_path.write_text(
        json.dumps(build_template_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    for requirement in REQUIRED_EVIDENCE:
        receipt_path = evidence_dir / requirement.key / "receipt.md"
        if receipt_path.exists() and not force:
            continue
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(_receipt_template(requirement), encoding="utf-8")

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
