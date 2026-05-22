#!/usr/bin/env python3
"""Validate the external evidence bundle for enterprise readiness."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_EVIDENCE_DIR = REPO_ROOT / ".moon/cache/enterprise-readiness-evidence"
DEFAULT_MANIFEST = DEFAULT_EVIDENCE_DIR / "enterprise-readiness-evidence.json"
DEFAULT_RECEIPT = DEFAULT_EVIDENCE_DIR / "receipt.json"
SCHEMA_VERSION = "enterprise-readiness-evidence/v1"

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    parser.add_argument("--init-template", type=Path)
    parser.add_argument("--force-template", action="store_true")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        _list_requirements()
        return 0

    if args.init_template is not None:
        try:
            manifest_path = write_template(args.init_template, force=args.force_template)
        except EvidenceFailure as exc:
            sys.stdout.write(f"{exc}\n")
            return 1
        sys.stdout.write(f"wrote template: {manifest_path}\n")
        return 0

    return run_gate(
        manifest_path=args.manifest,
        evidence_dir=args.evidence_dir,
        receipt_path=args.receipt,
    )


if __name__ == "__main__":
    raise SystemExit(main())
