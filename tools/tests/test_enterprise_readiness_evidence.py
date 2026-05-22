from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from shutil import which
from typing import Any, cast

import pytest
from tools.tests.conftest import REPO_ROOT
from tools.trust import enterprise_readiness_evidence as evidence


def _write_artifact(evidence_dir: Path, key: str, content: str = "receipt") -> dict[str, str]:
    path = Path(key) / "receipt.md"
    full_path = evidence_dir / path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content, encoding="utf-8")
    return {
        "path": path.as_posix(),
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
    }


def _valid_manifest(evidence_dir: Path) -> dict[str, Any]:
    return {
        "schema_version": evidence.SCHEMA_VERSION,
        "items": {
            requirement.key: {
                "gate": requirement.gate,
                "status": "PASS",
                "description": requirement.description,
                "artifacts": [_write_artifact(evidence_dir, requirement.key)],
            }
            for requirement in evidence.REQUIRED_EVIDENCE
        },
    }


def _write_manifest(evidence_dir: Path, payload: dict[str, Any]) -> Path:
    manifest_path = evidence_dir / "enterprise-readiness-evidence.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def _root_task(task_id: str) -> dict[str, Any]:
    moon = which("moon")
    assert moon is not None

    result = subprocess.run(  # noqa: S603
        [moon, "query", "tasks", "--project", "root", "--id", task_id],
        cwd=REPO_ROOT,
        env={**os.environ, "MOON_COLOR": "false"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = cast(dict[str, Any], json.loads(result.stdout))
    return cast(dict[str, Any], payload["tasks"]["root"][task_id])


def test_required_evidence_covers_external_acceptance_gates() -> None:
    keys = {requirement.key for requirement in evidence.REQUIRED_EVIDENCE}

    assert keys == {
        "entra_happy_path",
        "entra_missing_role_denial",
        "mcp_cursor_auth",
        "mcp_claude_code_auth",
        "mcp_claude_desktop_auth",
        "kubernetes_restore_drill",
        "restore_recall_sample",
        "rendered_helm_manifests",
        "idp_role_claim_evidence",
        "audit_export_sample",
        "image_sbom_receipt",
        "cosign_signature_receipt",
        "package_lock_diff",
    }


def test_template_contains_every_required_item(tmp_path: Path) -> None:
    manifest_path = evidence.write_template(tmp_path)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == evidence.SCHEMA_VERSION
    assert set(payload["items"]) == {item.key for item in evidence.REQUIRED_EVIDENCE}
    assert all(item["status"] == "TODO" for item in payload["items"].values())
    for requirement in evidence.REQUIRED_EVIDENCE:
        receipt = tmp_path / requirement.key / "receipt.md"
        assert receipt.is_file()
        assert f"# {requirement.key}" in receipt.read_text(encoding="utf-8")
        assert requirement.description in receipt.read_text(encoding="utf-8")


def test_template_refuses_to_overwrite_existing_manifest(tmp_path: Path) -> None:
    evidence.write_template(tmp_path)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.write_template(tmp_path)

    assert "pass --force-template to overwrite" in str(exc_info.value)


def test_template_preserves_receipts_without_force(tmp_path: Path) -> None:
    manifest_path = evidence.write_template(tmp_path)
    manifest_path.unlink()
    receipt = tmp_path / "entra_happy_path" / "receipt.md"
    receipt.write_text("real receipt", encoding="utf-8")

    evidence.write_template(tmp_path)

    assert receipt.read_text(encoding="utf-8") == "real receipt"


def test_template_force_regenerates_receipts(tmp_path: Path) -> None:
    evidence.write_template(tmp_path)
    receipt = tmp_path / "entra_happy_path" / "receipt.md"
    receipt.write_text("stale", encoding="utf-8")

    evidence.write_template(tmp_path, force=True)

    assert "Required proof" in receipt.read_text(encoding="utf-8")


def test_sync_manifest_hashes_updates_artifacts(tmp_path: Path) -> None:
    manifest_path = evidence.write_template(tmp_path)
    receipt = tmp_path / "entra_happy_path" / "receipt.md"
    receipt.write_text("real receipt", encoding="utf-8")

    sync_receipt = evidence.sync_manifest_hashes(manifest_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert sync_receipt["status"] == "SYNCED"
    assert (
        payload["items"]["entra_happy_path"]["artifacts"][0]["sha256"]
        == hashlib.sha256(b"real receipt").hexdigest()
    )


def test_sync_manifest_hashes_rejects_missing_artifact(tmp_path: Path) -> None:
    manifest_path = evidence.write_template(tmp_path)
    (tmp_path / "restore_recall_sample" / "receipt.md").unlink()

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.sync_manifest_hashes(manifest_path)

    assert "restore_recall_sample artifact not found" in str(exc_info.value)


def test_inspect_manifest_reports_template_incomplete(tmp_path: Path) -> None:
    manifest_path = evidence.write_template(tmp_path)

    report = evidence.inspect_manifest(manifest_path)

    assert report["status"] == "INCOMPLETE"
    assert report["summary"] == {"PASS": 0, "INCOMPLETE": 13}
    assert report["items"][0]["issues"] == [
        "status is 'TODO', not PASS",
        "artifact hash mismatch: entra_happy_path/receipt.md",
    ]


def test_inspect_manifest_reports_multiple_issues(tmp_path: Path) -> None:
    payload = _valid_manifest(tmp_path)
    payload["items"].pop("mcp_cursor_auth")
    payload["items"]["audit_export_sample"]["artifacts"][0]["sha256"] = "bad"
    (tmp_path / "restore_recall_sample" / "receipt.md").unlink()
    manifest_path = _write_manifest(tmp_path, payload)

    report = evidence.inspect_manifest(manifest_path)
    issues_by_key = {
        item["key"]: item["issues"]
        for item in cast(list[dict[str, Any]], report["items"])
        if item["issues"]
    }

    assert report["status"] == "INCOMPLETE"
    assert issues_by_key["mcp_cursor_auth"] == ["missing evidence item"]
    assert issues_by_key["audit_export_sample"] == [
        "artifact hash mismatch: audit_export_sample/receipt.md"
    ]
    assert issues_by_key["restore_recall_sample"] == [
        "artifact not found: restore_recall_sample/receipt.md"
    ]


def test_validate_manifest_passes_with_hashes(tmp_path: Path) -> None:
    payload = _valid_manifest(tmp_path)
    manifest_path = _write_manifest(tmp_path, payload)

    receipt = evidence.validate_manifest(manifest_path)

    assert receipt["status"] == "PASS"
    assert len(receipt["items"]) == len(evidence.REQUIRED_EVIDENCE)
    assert receipt["items"][0]["artifacts"][0]["bytes"] > 0


def test_run_gate_writes_failure_receipt_for_missing_item(tmp_path: Path) -> None:
    payload = _valid_manifest(tmp_path)
    payload["items"].pop("mcp_cursor_auth")
    manifest_path = _write_manifest(tmp_path, payload)
    receipt_path = tmp_path / "receipt.json"

    exit_code = evidence.run_gate(manifest_path=manifest_path, receipt_path=receipt_path)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert receipt["status"] == "FAIL"
    assert receipt["error"] == "missing evidence item: mcp_cursor_auth"


def test_validate_manifest_rejects_non_pass_status(tmp_path: Path) -> None:
    payload = _valid_manifest(tmp_path)
    payload["items"]["entra_happy_path"]["status"] = "TODO"
    manifest_path = _write_manifest(tmp_path, payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.validate_manifest(manifest_path)

    assert str(exc_info.value) == "entra_happy_path status must be PASS, got 'TODO'"


def test_validate_manifest_rejects_hash_mismatch(tmp_path: Path) -> None:
    payload = _valid_manifest(tmp_path)
    payload["items"]["audit_export_sample"]["artifacts"][0]["sha256"] = "bad"
    manifest_path = _write_manifest(tmp_path, payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.validate_manifest(manifest_path)

    assert "audit_export_sample artifact hash mismatch" in str(exc_info.value)


def test_validate_manifest_rejects_path_escape(tmp_path: Path) -> None:
    payload = _valid_manifest(tmp_path)
    payload["items"]["package_lock_diff"]["artifacts"][0] = {
        "path": "../lock.diff",
        "sha256": "unused",
    }
    manifest_path = _write_manifest(tmp_path, payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.validate_manifest(manifest_path)

    assert str(exc_info.value) == "artifact path must stay inside evidence dir: ../lock.diff"


def test_root_moon_tasks_expose_enterprise_evidence_gate() -> None:
    gate = _root_task("enterprise-readiness-evidence")
    assert gate["command"] == "uv"
    assert gate["args"] == ["run", "python", "-m", "tools.trust.enterprise_readiness_evidence"]

    test_task = _root_task("enterprise-readiness-evidence-test")
    assert test_task["command"] == "uv"
    assert test_task["args"] == [
        "run",
        "pytest",
        "tools/tests/test_enterprise_readiness_evidence.py",
        "-v",
    ]


def test_main_refuses_template_overwrite(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert evidence.main(["--init-template", str(tmp_path)]) == 0

    exit_code = evidence.main(["--init-template", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "pass --force-template to overwrite" in captured.out


def test_main_sync_hashes_updates_manifest(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    manifest_path = evidence.write_template(tmp_path)

    exit_code = evidence.main(["--manifest", str(manifest_path), "--sync-hashes"])
    captured = capsys.readouterr()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert "synced artifact hashes: 13" in captured.out
    assert payload["items"]["entra_happy_path"]["artifacts"][0]["sha256"] != (
        "<fill-after-capture>"
    )


def test_main_status_reports_incomplete_manifest(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    manifest_path = evidence.write_template(tmp_path)

    exit_code = evidence.main(["--manifest", str(manifest_path), "--status"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Enterprise readiness evidence: INCOMPLETE" in captured.out
    assert "summary: 0 PASS, 13 INCOMPLETE" in captured.out


def test_main_status_reports_valid_manifest(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    manifest_path = _write_manifest(tmp_path, _valid_manifest(tmp_path))

    exit_code = evidence.main(["--manifest", str(manifest_path), "--status"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Enterprise readiness evidence: PASS" in captured.out
    assert "summary: 13 PASS, 0 INCOMPLETE" in captured.out


def test_capture_package_lock_diff_updates_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_git_output(args: list[str]) -> str:
        if args[0] == "diff":
            return """
diff --git a/apps/api/pyproject.toml b/apps/api/pyproject.toml
+    "argon2-cffi>=25.1.0",
+    "authlib>=1.7.2,<1.8",
+    "pyjwt[crypto]>=2.13.0,<3",
diff --git a/uv.lock b/uv.lock
+name = "argon2-cffi"
+name = "authlib"
+name = "pyjwt"
"""
        if args == ["rev-parse", "base"]:
            return "base-sha"
        if args == ["rev-parse", "HEAD"]:
            return "head-sha"
        raise AssertionError(args)

    monkeypatch.setattr(evidence, "_git_output", fake_git_output)

    receipt = evidence.capture_package_lock_diff(tmp_path, base_ref="base")
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = payload["items"]["package_lock_diff"]

    assert item["status"] == "PASS"
    assert item["artifacts"][0]["path"] == "package_lock_diff/receipt.md"
    assert item["artifacts"][1]["path"] == "package_lock_diff/package-lock.diff"
    assert (tmp_path / "package_lock_diff" / "package-lock.diff").is_file()
    assert "Base sha: base-sha" in (tmp_path / "package_lock_diff" / "receipt.md").read_text(
        encoding="utf-8"
    )


def test_capture_package_lock_diff_rejects_missing_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        evidence,
        "_git_output",
        lambda args: '+    "authlib>=1.7.2,<1.8"\n',
    )

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_package_lock_diff(tmp_path, base_ref="base")

    assert "pyjwt, argon2-cffi" in str(exc_info.value)


def test_capture_rendered_helm_manifests_updates_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_helm_output(args: list[str]) -> str:
        if args[2] == "charts/sibyl":
            return """
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
---
apiVersion: networking.k8s.io/v1
kind: Ingress
---
apiVersion: v1
kind: Namespace
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
---
apiVersion: batch/v1
kind: Job
"""
        if args[2] == "charts/surrealdb":
            return """
apiVersion: batch/v1
kind: CronJob
metadata:
  labels:
    app.kubernetes.io/component: restore-drill
---
apiVersion: batch/v1
kind: Job
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
"""
        raise AssertionError(args)

    monkeypatch.setattr(evidence, "_helm_output", fake_helm_output)

    receipt = evidence.capture_rendered_helm_manifests(tmp_path)
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = payload["items"]["rendered_helm_manifests"]

    assert item["status"] == "PASS"
    assert item["artifacts"][0]["path"] == "rendered_helm_manifests/receipt.md"
    assert item["artifacts"][1]["path"] == "rendered_helm_manifests/sibyl-enterprise.yaml"
    assert item["artifacts"][2]["path"] == "rendered_helm_manifests/surrealdb-enterprise.yaml"
    assert (tmp_path / "rendered_helm_manifests" / "sibyl-enterprise.yaml").is_file()
    assert (tmp_path / "rendered_helm_manifests" / "surrealdb-enterprise.yaml").is_file()


def test_capture_rendered_helm_manifests_rejects_missing_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_helm_output(args: list[str]) -> str:
        if args[2] == "charts/sibyl":
            return "kind: HTTPRoute\n"
        if args[2] == "charts/surrealdb":
            return "kind: CronJob\n"
        raise AssertionError(args)

    monkeypatch.setattr(evidence, "_helm_output", fake_helm_output)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_rendered_helm_manifests(tmp_path)

    assert "Sibyl enterprise chart rendered manifest missing required snippets" in str(
        exc_info.value
    )
