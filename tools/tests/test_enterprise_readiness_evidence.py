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


def _test_access_token() -> str:
    return "-".join(("secret", "token"))


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


def _cyclonedx_sbom(image: str) -> str:
    return json.dumps(
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "version": 1,
            "components": [
                {
                    "type": "application",
                    "name": f"sibyl-{image}",
                    "version": "1.2.3",
                }
            ],
        }
    )


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
        assert (
            payload["items"][requirement.key]["artifacts"][0]["sha256"]
            == hashlib.sha256(receipt.read_bytes()).hexdigest()
        )
    source_templates = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "source").glob("*.json")
    }
    assert set(source_templates) == {
        "entra-smoke.json",
        "idp-role-claim-config.json",
        "mcp-client-smoke.json",
        "restore-drill.json",
    }
    assert source_templates["entra-smoke.json"]["status"] == "TODO"
    assert source_templates["mcp-client-smoke.json"]["clients"]["cursor"]["status"] == "TODO"
    assert source_templates["restore-drill.json"]["recall_sample"]["result_count"] == 0
    assert source_templates["idp-role-claim-config.json"]["appRoles"][0]["isEnabled"] is False


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
    source_template = tmp_path / "source" / "entra-smoke.json"
    source_template.write_text('{"real": true}\n', encoding="utf-8")

    manifest_path = evidence.write_template(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert receipt.read_text(encoding="utf-8") == "real receipt"
    assert source_template.read_text(encoding="utf-8") == '{"real": true}\n'
    assert (
        payload["items"]["entra_happy_path"]["artifacts"][0]["sha256"]
        == hashlib.sha256(b"real receipt").hexdigest()
    )


def test_template_force_regenerates_receipts(tmp_path: Path) -> None:
    evidence.write_template(tmp_path)
    receipt = tmp_path / "entra_happy_path" / "receipt.md"
    receipt.write_text("stale", encoding="utf-8")
    source_template = tmp_path / "source" / "entra-smoke.json"
    source_template.write_text('{"stale": true}\n', encoding="utf-8")

    evidence.write_template(tmp_path, force=True)

    assert "Required proof" in receipt.read_text(encoding="utf-8")
    assert json.loads(source_template.read_text(encoding="utf-8"))["provider"] == "entra"


def test_write_source_templates_backfills_existing_bundle(tmp_path: Path) -> None:
    manifest_path = evidence.write_template(tmp_path)
    source_dir = tmp_path / "source"
    for source_template in source_dir.glob("*.json"):
        source_template.unlink()

    receipt = evidence.write_source_templates(tmp_path)

    assert manifest_path.is_file()
    assert receipt["status"] == "PASS"
    assert (source_dir / "entra-smoke.json").is_file()
    assert (source_dir / "mcp-client-smoke.json").is_file()


def test_main_writes_source_templates(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    exit_code = evidence.main(["--evidence-dir", str(tmp_path), "--write-source-templates"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "wrote source evidence templates" in captured.out
    assert (tmp_path / "source" / "restore-drill.json").is_file()


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
    assert report["items"][0]["issues"] == ["status is 'TODO', not PASS"]


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


def _package_lock_manifest(
    evidence_dir: Path,
    *,
    receipt_text: str,
    diff_text: str,
) -> Path:
    payload = _valid_manifest(evidence_dir)
    package_dir = evidence_dir / "package_lock_diff"
    package_dir.mkdir(parents=True, exist_ok=True)
    receipt_path = package_dir / "receipt.md"
    diff_path = package_dir / "package-lock.diff"
    receipt_path.write_text(receipt_text, encoding="utf-8")
    diff_path.write_text(diff_text, encoding="utf-8")

    payload["items"]["package_lock_diff"] = {
        "gate": "security-review-packet",
        "status": "PASS",
        "description": "Package lock diff for Authlib, PyJWT, and argon2-cffi is captured.",
        "artifacts": [
            {
                "path": "package_lock_diff/receipt.md",
                "sha256": hashlib.sha256(receipt_text.encode()).hexdigest(),
            },
            {
                "path": "package_lock_diff/package-lock.diff",
                "sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
            },
        ],
    }
    return _write_manifest(evidence_dir, payload)


def _rendered_helm_manifest(
    evidence_dir: Path,
    *,
    sibyl_text: str,
    surrealdb_text: str,
) -> Path:
    payload = _valid_manifest(evidence_dir)
    artifact_dir = evidence_dir / "rendered_helm_manifests"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    receipt_text = "# rendered_helm_manifests\n"
    artifacts = {
        "receipt.md": receipt_text,
        "sibyl-enterprise.yaml": sibyl_text,
        "surrealdb-enterprise.yaml": surrealdb_text,
    }

    for filename, content in artifacts.items():
        artifact_dir.joinpath(filename).write_text(content, encoding="utf-8")

    payload["items"]["rendered_helm_manifests"] = {
        "gate": "security-review-packet",
        "status": "PASS",
        "description": "Rendered enterprise Helm manifests for Sibyl and SurrealDB are captured.",
        "artifacts": [
            {
                "path": f"rendered_helm_manifests/{filename}",
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
            for filename, content in artifacts.items()
        ],
    }
    return _write_manifest(evidence_dir, payload)


def test_inspect_manifest_reports_stale_package_lock_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    diff_text = "diff text\n"
    manifest_path = _package_lock_manifest(
        tmp_path,
        receipt_text=(
            "# package_lock_diff\n"
            "- Base ref: base\n"
            "- Base sha: base-sha\n"
            "- Head ref: HEAD\n"
            "- Head sha: stale-sha\n"
        ),
        diff_text=diff_text,
    )

    def fake_git_output(args: list[str]) -> str:
        if args == ["rev-parse", "HEAD"]:
            return "current-sha"
        if args[0] == "diff":
            return diff_text
        raise AssertionError(args)

    monkeypatch.setattr(evidence, "_git_output", fake_git_output)

    report = evidence.inspect_manifest(manifest_path)
    package_item = next(
        item
        for item in cast(list[dict[str, Any]], report["items"])
        if item["key"] == "package_lock_diff"
    )

    assert package_item["status"] == "INCOMPLETE"
    assert package_item["issues"] == [
        "head sha is stale: HEAD resolves to current-sha, receipt has stale-sha"
    ]


def test_validate_manifest_rejects_stale_package_lock_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = _package_lock_manifest(
        tmp_path,
        receipt_text=(
            "# package_lock_diff\n"
            "- Base ref: base\n"
            "- Base sha: base-sha\n"
            "- Head ref: HEAD\n"
            "- Head sha: current-sha\n"
        ),
        diff_text="stale diff\n",
    )

    def fake_git_output(args: list[str]) -> str:
        if args == ["rev-parse", "HEAD"]:
            return "current-sha"
        if args[0] == "diff":
            return "fresh diff"
        raise AssertionError(args)

    monkeypatch.setattr(evidence, "_git_output", fake_git_output)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.validate_manifest(manifest_path)

    assert "package_lock_diff package lock diff artifact is stale for base..HEAD" in str(
        exc_info.value
    )


def test_inspect_manifest_reports_stale_rendered_helm_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = _rendered_helm_manifest(
        tmp_path,
        sibyl_text="stale sibyl\n",
        surrealdb_text="fresh db\n",
    )

    def fake_helm_output(args: list[str]) -> str:
        if args[2] == "charts/sibyl":
            return "fresh sibyl"
        if args[2] == "charts/surrealdb":
            return "fresh db"
        raise AssertionError(args)

    monkeypatch.setattr(evidence, "_helm_output", fake_helm_output)

    report = evidence.inspect_manifest(manifest_path)
    helm_item = next(
        item
        for item in cast(list[dict[str, Any]], report["items"])
        if item["key"] == "rendered_helm_manifests"
    )

    assert helm_item["status"] == "INCOMPLETE"
    assert helm_item["issues"] == [
        "rendered Helm artifact is stale: rendered_helm_manifests/sibyl-enterprise.yaml"
    ]


def test_validate_manifest_rejects_stale_rendered_helm_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    manifest_path = _rendered_helm_manifest(
        tmp_path,
        sibyl_text="stale sibyl\n",
        surrealdb_text="fresh db\n",
    )

    def fake_helm_output(args: list[str]) -> str:
        if args[2] == "charts/sibyl":
            return "fresh sibyl"
        if args[2] == "charts/surrealdb":
            return "fresh db"
        raise AssertionError(args)

    monkeypatch.setattr(evidence, "_helm_output", fake_helm_output)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.validate_manifest(manifest_path)

    assert (
        "rendered_helm_manifests rendered Helm artifact is stale: "
        "rendered_helm_manifests/sibyl-enterprise.yaml"
    ) in str(exc_info.value)


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
    receipt = tmp_path / "entra_happy_path" / "receipt.md"
    receipt.write_text("real receipt", encoding="utf-8")

    exit_code = evidence.main(["--manifest", str(manifest_path), "--sync-hashes"])
    captured = capsys.readouterr()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert "synced artifact hashes: 13" in captured.out
    assert (
        payload["items"]["entra_happy_path"]["artifacts"][0]["sha256"]
        == hashlib.sha256(b"real receipt").hexdigest()
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


def test_main_serializes_manifest_access(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest_path = evidence.write_template(tmp_path)
    flock_calls: list[int] = []

    def fake_flock(_fd: int, operation: int) -> None:
        flock_calls.append(operation)

    monkeypatch.setattr(evidence.fcntl, "flock", fake_flock)

    exit_code = evidence.main(["--manifest", str(manifest_path), "--status"])
    capsys.readouterr()

    assert exit_code == 1
    assert flock_calls == [evidence.fcntl.LOCK_EX, evidence.fcntl.LOCK_UN]
    assert (tmp_path / evidence.LOCK_FILENAME).is_file()


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
data:
  SIBYL_LOCAL_AUTH_ENABLED: "false"
  SIBYL_PUBLIC_SIGNUPS_ENABLED: "false"
"""
        if args[2] == "charts/surrealdb":
            return """
apiVersion: batch/v1
kind: CronJob
metadata:
  labels:
    app.kubernetes.io/component: restore-drill
spec:
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - env:
                - name: SIBYL_RESTORE_RECEIPT_PATH
                  value: /tmp/restore-drill-receipt.json
            - args:
                - |
                  echo "SIBYL_RESTORE_RECEIPT_JSON_BEGIN"
                  echo "SIBYL_RESTORE_RECEIPT_JSON_END"
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


def test_capture_github_release_evidence_updates_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_gh_json_output(args: list[str]) -> dict[str, Any]:
        if args[0:2] == ["run", "view"]:
            return {
                "conclusion": "success",
                "createdAt": "2026-05-22T12:00:00Z",
                "databaseId": 12345,
                "headBranch": "main",
                "headSha": "abc123",
                "name": "Publish",
                "url": "https://github.example/run/12345",
                "workflowName": "Publish",
                "jobs": [
                    {
                        "conclusion": "success",
                        "databaseId": 101,
                        "name": "◆ Docker: Security api",
                    },
                    {
                        "conclusion": "success",
                        "databaseId": 102,
                        "name": "◆ Docker: Security web",
                    },
                    {
                        "conclusion": "success",
                        "databaseId": 201,
                        "name": "◆ Docker: Sign api",
                    },
                    {
                        "conclusion": "success",
                        "databaseId": 202,
                        "name": "◆ Docker: Sign web",
                    },
                ],
            }
        if args[0] == "api":
            return {
                "artifacts": [
                    {
                        "expired": False,
                        "name": "sibyl-api-1.2.3-sbom",
                        "size_in_bytes": 123,
                    },
                    {
                        "expired": False,
                        "name": "sibyl-web-1.2.3-sbom",
                        "size_in_bytes": 456,
                    },
                ]
            }
        raise AssertionError(args)

    def fake_gh_text_output(args: list[str]) -> str:
        if args[0:2] == ["run", "view"]:
            return "cosign signing log"
        raise AssertionError(args)

    def fake_download_github_artifact(
        *,
        repo: str,
        run_id: str,
        artifact_name: str,
        destination: Path,
    ) -> None:
        assert repo == "hyperb1iss/sibyl"
        assert run_id == "12345"
        image = artifact_name.split("-")[1]
        destination.joinpath(f"sibyl-{image}-1.2.3.cdx.json").write_text(
            _cyclonedx_sbom(image),
            encoding="utf-8",
        )

    monkeypatch.setattr(evidence, "_gh_json_output", fake_gh_json_output)
    monkeypatch.setattr(evidence, "_gh_text_output", fake_gh_text_output)
    monkeypatch.setattr(evidence, "_download_github_artifact", fake_download_github_artifact)

    receipt = evidence.capture_github_release_evidence(tmp_path, run_id="12345")
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    sbom_item = payload["items"]["image_sbom_receipt"]
    sign_item = payload["items"]["cosign_signature_receipt"]

    assert sbom_item["status"] == "PASS"
    assert sign_item["status"] == "PASS"
    assert sbom_item["artifacts"][0]["path"] == "image_sbom_receipt/receipt.md"
    assert sign_item["artifacts"][0]["path"] == "cosign_signature_receipt/receipt.md"
    assert (tmp_path / "image_sbom_receipt" / "sibyl-api-1.2.3.cdx.json").is_file()
    assert (tmp_path / "image_sbom_receipt" / "sibyl-web-1.2.3.cdx.json").is_file()
    assert (tmp_path / "cosign_signature_receipt" / "sign-api.log").is_file()
    assert (tmp_path / "cosign_signature_receipt" / "sign-web.log").is_file()


def test_capture_github_release_evidence_rejects_invalid_sbom(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_gh_json_output(args: list[str]) -> dict[str, Any]:
        if args[0:2] == ["run", "view"]:
            return {
                "conclusion": "success",
                "createdAt": "2026-05-22T12:00:00Z",
                "databaseId": 12345,
                "headBranch": "main",
                "headSha": "abc123",
                "name": "Publish",
                "url": "https://github.example/run/12345",
                "workflowName": "Publish",
                "jobs": [
                    {
                        "conclusion": "success",
                        "databaseId": 101,
                        "name": "◆ Docker: Security api",
                    },
                    {
                        "conclusion": "success",
                        "databaseId": 102,
                        "name": "◆ Docker: Security web",
                    },
                    {
                        "conclusion": "success",
                        "databaseId": 201,
                        "name": "◆ Docker: Sign api",
                    },
                    {
                        "conclusion": "success",
                        "databaseId": 202,
                        "name": "◆ Docker: Sign web",
                    },
                ],
            }
        if args[0] == "api":
            return {
                "artifacts": [
                    {
                        "expired": False,
                        "name": "sibyl-api-1.2.3-sbom",
                        "size_in_bytes": 123,
                    },
                    {
                        "expired": False,
                        "name": "sibyl-web-1.2.3-sbom",
                        "size_in_bytes": 456,
                    },
                ]
            }
        raise AssertionError(args)

    def fake_download_github_artifact(
        *,
        repo: str,
        run_id: str,
        artifact_name: str,
        destination: Path,
    ) -> None:
        image = artifact_name.split("-")[1]
        destination.joinpath(f"sibyl-{image}-1.2.3.cdx.json").write_text(
            "{}",
            encoding="utf-8",
        )

    monkeypatch.setattr(evidence, "_gh_json_output", fake_gh_json_output)
    monkeypatch.setattr(evidence, "_download_github_artifact", fake_download_github_artifact)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_github_release_evidence(tmp_path, run_id="12345")

    assert "SBOM artifact for api must be CycloneDX JSON" in str(exc_info.value)


def test_capture_github_release_evidence_rejects_missing_sign_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_gh_json_output(args: list[str]) -> dict[str, Any]:
        if args[0:2] == ["run", "view"]:
            return {
                "conclusion": "success",
                "name": "Publish",
                "workflowName": "Publish",
                "jobs": [
                    {
                        "conclusion": "success",
                        "databaseId": 101,
                        "name": "◆ Docker: Security api",
                    },
                    {
                        "conclusion": "success",
                        "databaseId": 102,
                        "name": "◆ Docker: Security web",
                    },
                    {
                        "conclusion": "success",
                        "databaseId": 201,
                        "name": "◆ Docker: Sign api",
                    },
                ],
            }
        raise AssertionError(args)

    monkeypatch.setattr(evidence, "_gh_json_output", fake_gh_json_output)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_github_release_evidence(tmp_path, run_id="12345")

    assert "GitHub run is missing required job: ◆ Docker: Sign web" in str(exc_info.value)


def test_preflight_github_release_evidence_reports_all_missing_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_gh_json_output(args: list[str]) -> dict[str, Any]:
        if args[0:2] == ["run", "view"]:
            return {
                "conclusion": "success",
                "createdAt": "2026-05-22T12:00:00Z",
                "databaseId": 12345,
                "headBranch": "main",
                "headSha": "abc123",
                "name": "Publish",
                "url": "https://github.example/run/12345",
                "workflowName": "Publish",
                "jobs": [
                    {
                        "conclusion": "success",
                        "databaseId": 101,
                        "name": "◆ Docker: Security api",
                    },
                    {
                        "conclusion": "failure",
                        "databaseId": 201,
                        "name": "◆ Docker: Sign api",
                    },
                ],
            }
        if args[0] == "api":
            return {
                "artifacts": [
                    {
                        "expired": False,
                        "name": "sibyl-api-1.2.3-sbom",
                        "size_in_bytes": 123,
                    }
                ]
            }
        raise AssertionError(args)

    monkeypatch.setattr(evidence, "_gh_json_output", fake_gh_json_output)

    report = evidence.preflight_github_release_evidence(run_id="12345")

    assert report["status"] == "FAIL"
    assert report["issues"] == [
        "GitHub job ◆ Docker: Sign api must have conclusion success, got 'failure'",
        "GitHub run is missing required job: ◆ Docker: Security web",
        "GitHub run is missing required job: ◆ Docker: Sign web",
        "GitHub run is missing required SBOM artifact for web",
    ]


def test_latest_successful_github_publish_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_gh_json_list_output(args: list[str]) -> list[dict[str, Any]]:
        assert args == [
            "run",
            "list",
            "--repo",
            "hyperb1iss/sibyl",
            "--workflow",
            "publish.yml",
            "--status",
            "success",
            "--limit",
            "1",
            "--json",
            "conclusion,createdAt,databaseId,displayTitle,event,headSha,status,url,workflowName",
        ]
        return [{"databaseId": 12345}]

    monkeypatch.setattr(evidence, "_gh_json_list_output", fake_gh_json_list_output)

    assert evidence.latest_successful_github_publish_run_id() == "12345"


def test_latest_successful_github_publish_run_id_rejects_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(evidence, "_gh_json_list_output", lambda args: [])

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.latest_successful_github_publish_run_id()

    assert "no successful GitHub publish run found" in str(exc_info.value)


def test_main_preflights_github_release_evidence(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_preflight_github_release_evidence(*, run_id: str, repo: str) -> dict[str, Any]:
        assert run_id == "12345"
        assert repo == "hyperb1iss/sibyl"
        return {
            "schema_version": evidence.SCHEMA_VERSION,
            "status": "FAIL",
            "repo": repo,
            "run_id": run_id,
            "run": {"url": "https://github.example/run/12345"},
            "issues": ["GitHub run is missing required job: ◆ Docker: Sign web"],
        }

    monkeypatch.setattr(
        evidence,
        "preflight_github_release_evidence",
        fake_preflight_github_release_evidence,
    )

    exit_code = evidence.main(["--preflight-github-release-evidence", "12345"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "GitHub release evidence preflight: FAIL" in captured.out
    assert "GitHub run is missing required job: ◆ Docker: Sign web" in captured.out


def test_main_preflights_latest_github_release_evidence(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_latest_successful_github_publish_run_id(*, repo: str, workflow: str) -> str:
        assert repo == "hyperb1iss/sibyl"
        assert workflow == "publish.yml"
        return "12345"

    def fake_preflight_github_release_evidence(*, run_id: str, repo: str) -> dict[str, Any]:
        assert run_id == "12345"
        return {
            "schema_version": evidence.SCHEMA_VERSION,
            "status": "FAIL",
            "repo": repo,
            "run_id": run_id,
            "run": {"url": "https://github.example/run/12345"},
            "issues": ["GitHub run is missing required job: ◆ Docker: Sign web"],
        }

    monkeypatch.setattr(
        evidence,
        "latest_successful_github_publish_run_id",
        fake_latest_successful_github_publish_run_id,
    )
    monkeypatch.setattr(
        evidence,
        "preflight_github_release_evidence",
        fake_preflight_github_release_evidence,
    )

    exit_code = evidence.main(["--preflight-latest-github-release-evidence"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "latest successful publish run: hyperb1iss/sibyl#12345" in captured.out
    assert "GitHub release evidence preflight: FAIL" in captured.out


def test_main_captures_latest_github_release_evidence(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_latest_successful_github_publish_run_id(*, repo: str, workflow: str) -> str:
        assert repo == "hyperb1iss/sibyl"
        assert workflow == "publish.yml"
        return "12345"

    def fake_capture_github_release_evidence(
        evidence_dir: Path,
        *,
        run_id: str,
        repo: str,
    ) -> dict[str, Any]:
        assert evidence_dir == tmp_path
        assert run_id == "12345"
        assert repo == "hyperb1iss/sibyl"
        return {"manifest": str(tmp_path / "enterprise-readiness-evidence.json")}

    monkeypatch.setattr(
        evidence,
        "latest_successful_github_publish_run_id",
        fake_latest_successful_github_publish_run_id,
    )
    monkeypatch.setattr(
        evidence,
        "capture_github_release_evidence",
        fake_capture_github_release_evidence,
    )

    exit_code = evidence.main(
        ["--evidence-dir", str(tmp_path), "--capture-latest-github-release-evidence"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "latest successful publish run: hyperb1iss/sibyl#12345" in captured.out
    assert "captured GitHub release SBOM and Cosign evidence" in captured.out


def test_preflight_current_environment_reports_section_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        evidence,
        "_preflight_source_templates",
        lambda evidence_dir: {
            "status": "PASS",
            "templates": [],
            "issues": [],
        },
    )
    monkeypatch.setattr(
        evidence,
        "_preflight_desktop_mcp_clients",
        lambda: {
            "status": "FAIL",
            "clients": [{"key": "cursor", "status": "FAIL"}],
            "issues": ["missing desktop MCP clients: cursor"],
        },
    )
    monkeypatch.setattr(
        evidence,
        "_preflight_local_kubernetes",
        lambda: {
            "status": "PASS",
            "contexts": [{"context": "orbstack", "status": "PASS"}],
            "issues": [],
        },
    )
    monkeypatch.setattr(
        evidence,
        "_preflight_latest_release_evidence",
        lambda *, repo, workflow: {
            "status": "PASS",
            "repo": repo,
            "workflow": workflow,
            "run_id": "12345",
            "run": {},
            "issues": [],
        },
    )

    report = evidence.preflight_current_environment(tmp_path)

    assert report["status"] == "FAIL"
    assert report["checks"]["desktop_mcp_clients"]["status"] == "FAIL"
    assert report["checks"]["release_evidence"]["run_id"] == "12345"


def test_preflight_current_environment_uses_kubernetes_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_preflight_local_kubernetes(*, contexts: tuple[str, ...]) -> dict[str, Any]:
        assert contexts == ("kind-sibyl-enterprise",)
        return {
            "status": "PASS",
            "contexts": [{"context": "kind-sibyl-enterprise", "status": "PASS"}],
            "issues": [],
        }

    monkeypatch.setattr(
        evidence,
        "_preflight_source_templates",
        lambda evidence_dir: {
            "status": "PASS",
            "templates": [],
            "issues": [],
        },
    )
    monkeypatch.setattr(
        evidence,
        "_preflight_desktop_mcp_clients",
        lambda: {
            "status": "PASS",
            "clients": [],
            "issues": [],
        },
    )
    monkeypatch.setattr(evidence, "_preflight_local_kubernetes", fake_preflight_local_kubernetes)
    monkeypatch.setattr(
        evidence,
        "_preflight_latest_release_evidence",
        lambda *, repo, workflow: {
            "status": "PASS",
            "repo": repo,
            "workflow": workflow,
            "run_id": "12345",
            "run": {},
            "issues": [],
        },
    )

    report = evidence.preflight_current_environment(
        tmp_path,
        kubernetes_context="kind-sibyl-enterprise",
    )

    assert report["status"] == "PASS"


def test_preflight_source_templates_reports_missing(tmp_path: Path) -> None:
    report = evidence._preflight_source_templates(tmp_path)

    assert report["status"] == "FAIL"
    assert "missing source templates" in report["issues"][0]


def test_preflight_source_templates_passes_after_template_init(tmp_path: Path) -> None:
    evidence.write_template(tmp_path)

    report = evidence._preflight_source_templates(tmp_path)

    assert report["status"] == "PASS"
    assert report["issues"] == []


def test_main_preflights_current_environment(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_preflight_current_environment(
        evidence_dir: Path,
        *,
        repo: str,
        workflow: str,
        kubernetes_context: str,
    ) -> dict[str, Any]:
        assert evidence_dir == tmp_path
        assert repo == "hyperb1iss/sibyl"
        assert workflow == "publish.yml"
        assert kubernetes_context == "kind-sibyl-enterprise"
        return {
            "schema_version": evidence.SCHEMA_VERSION,
            "status": "FAIL",
            "evidence_dir": str(evidence_dir),
            "checks": {
                "source_templates": {
                    "status": "PASS",
                    "templates": [{"name": "entra-smoke.json", "status": "PASS"}],
                    "issues": [],
                },
                "desktop_mcp_clients": {
                    "status": "FAIL",
                    "clients": [{"key": "cursor", "status": "FAIL"}],
                    "issues": ["missing desktop MCP clients: cursor"],
                },
                "local_kubernetes": {
                    "status": "PASS",
                    "contexts": [{"context": "orbstack", "status": "PASS"}],
                    "issues": [],
                },
                "release_evidence": {
                    "status": "FAIL",
                    "repo": repo,
                    "run_id": "12345",
                    "run": {},
                    "issues": ["GitHub run is missing required job"],
                },
            },
        }

    monkeypatch.setattr(
        evidence,
        "preflight_current_environment",
        fake_preflight_current_environment,
    )

    exit_code = evidence.main(
        [
            "--evidence-dir",
            str(tmp_path),
            "--preflight-current-environment",
            "--kubernetes-context",
            "kind-sibyl-enterprise",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Enterprise readiness environment preflight: FAIL" in captured.out
    assert "desktop_mcp_clients: FAIL" in captured.out
    assert "release_evidence: FAIL" in captured.out


def test_capture_audit_export_sample_updates_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_http_get_bytes(url: str, headers: dict[str, str]) -> bytes:
        assert url == "https://sibyl.example.com/api/admin/audit/export?format=json&limit=25"
        assert headers["Authorization"] == "Bearer secret-token"
        return json.dumps(
            {
                "events": [
                    {
                        "action": "auth.login",
                        "created_at": "2026-05-22T12:00:00Z",
                        "id": "audit-1",
                    }
                ],
                "has_more": False,
                "limit": 25,
                "offset": 0,
                "total": 1,
            }
        ).encode()

    monkeypatch.setattr(evidence, "_http_get_bytes", fake_http_get_bytes)
    access_token = _test_access_token()

    receipt = evidence.capture_audit_export_sample(
        tmp_path,
        api_url="https://sibyl.example.com/api",
        access_token=access_token,
        export_format="json",
        limit=25,
    )
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = payload["items"]["audit_export_sample"]

    assert item["status"] == "PASS"
    assert item["artifacts"][0]["path"] == "audit_export_sample/receipt.md"
    assert item["artifacts"][1]["path"] == "audit_export_sample/audit-export.json"
    export = tmp_path / "audit_export_sample" / "audit-export.json"
    receipt_file = tmp_path / "audit_export_sample" / "receipt.md"
    assert "auth.login" in export.read_text(encoding="utf-8")
    assert "secret-token" not in receipt_file.read_text(encoding="utf-8")


def test_capture_audit_export_sample_rejects_empty_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        evidence,
        "_http_get_bytes",
        lambda url, headers: b'{"events": [], "total": 0, "limit": 1000, "offset": 0}',
    )

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_audit_export_sample(
            tmp_path,
            api_url="https://sibyl.example.com",
            access_token=_test_access_token(),
        )

    assert "audit JSON export must include at least one event" in str(exc_info.value)


def test_capture_audit_export_sample_accepts_csv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    csv_body = (
        "created_at,action,user_id,organization_id,resource,ip_address,user_agent,details\n"
        "2026-05-22T12:00:00Z,memory.recall,user,org,project:sibyl,127.0.0.1,curl,{}\n"
    )
    monkeypatch.setattr(
        evidence,
        "_http_get_bytes",
        lambda url, headers: csv_body.encode(),
    )

    receipt = evidence.capture_audit_export_sample(
        tmp_path,
        api_url="https://sibyl.example.com/api/admin/audit/export?resource=project:sibyl",
        access_token=_test_access_token(),
        export_format="csv",
    )
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = payload["items"]["audit_export_sample"]

    assert item["status"] == "PASS"
    assert item["artifacts"][1]["path"] == "audit_export_sample/audit-export.csv"


def _entra_smoke_payload() -> dict[str, Any]:
    return {
        "provider": "entra",
        "status": "PASS",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "runtime": "https://sibyl.example.com",
        "role_claim": "roles",
        "happy_path": {
            "status": "PASS",
            "tid": "11111111-1111-1111-1111-111111111111",
            "oid": "22222222-2222-2222-2222-222222222222",
            "roles": ["Sibyl.Member"],
        },
        "missing_role_denial": {
            "status": "PASS",
            "roles": [],
            "http_status": 403,
            "reason": "missing Sibyl role",
        },
    }


def _write_entra_smoke_receipt(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_capture_entra_smoke_evidence_updates_manifest(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    source = _write_entra_smoke_receipt(
        tmp_path / "entra-smoke.json",
        _entra_smoke_payload(),
    )

    receipt = evidence.capture_entra_smoke_evidence(
        evidence_dir,
        source_receipt=source,
        captured_by="Nova",
    )
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    happy_item = payload["items"]["entra_happy_path"]
    denial_item = payload["items"]["entra_missing_role_denial"]
    happy_receipt = evidence_dir / "entra_happy_path" / "receipt.md"
    denial_receipt = evidence_dir / "entra_missing_role_denial" / "receipt.md"

    assert happy_item["status"] == "PASS"
    assert denial_item["status"] == "PASS"
    assert happy_item["artifacts"][1]["path"] == "entra_oidc_smoke/entra-smoke-receipt.json"
    assert denial_item["artifacts"][1]["path"] == "entra_oidc_smoke/entra-smoke-receipt.json"
    assert "role(s): Sibyl.Member" in happy_receipt.read_text(encoding="utf-8")
    assert "missing Sibyl role" in denial_receipt.read_text(encoding="utf-8")


def test_capture_entra_smoke_evidence_rejects_missing_happy_role(
    tmp_path: Path,
) -> None:
    payload = _entra_smoke_payload()
    payload["happy_path"]["roles"] = ["Other.Role"]
    source = _write_entra_smoke_receipt(tmp_path / "entra-smoke.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_entra_smoke_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "happy_path.roles must include Sibyl.Member or higher" in str(exc_info.value)


def test_capture_entra_smoke_evidence_rejects_missing_provider(
    tmp_path: Path,
) -> None:
    payload = _entra_smoke_payload()
    payload.pop("provider")
    source = _write_entra_smoke_receipt(tmp_path / "entra-smoke.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_entra_smoke_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "Entra smoke provider must be 'entra'" in str(exc_info.value)


def test_capture_entra_smoke_evidence_rejects_mismatched_tenant(
    tmp_path: Path,
) -> None:
    payload = _entra_smoke_payload()
    payload["happy_path"]["tid"] = "33333333-3333-3333-3333-333333333333"
    source = _write_entra_smoke_receipt(tmp_path / "entra-smoke.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_entra_smoke_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "happy_path.tid must match tenant_id" in str(exc_info.value)


def test_capture_entra_smoke_evidence_rejects_undenied_missing_role(
    tmp_path: Path,
) -> None:
    payload = _entra_smoke_payload()
    payload["missing_role_denial"]["http_status"] = 200
    source = _write_entra_smoke_receipt(tmp_path / "entra-smoke.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_entra_smoke_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "missing_role_denial must prove denied=true or HTTP 401/403" in str(exc_info.value)


def test_main_capture_entra_smoke_evidence_requires_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "entra-smoke.txt"
    source.write_text("not JSON", encoding="utf-8")

    exit_code = evidence.main(
        [
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--capture-entra-smoke-evidence",
            str(source),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Entra smoke receipt must be a JSON file" in captured.out


def _mcp_smoke_payload() -> dict[str, Any]:
    return {
        "status": "PASS",
        "runtime": "https://sibyl.example.com/mcp",
        "clients": {
            "cursor": {
                "status": "PASS",
                "client": "Cursor stable",
                "auth_method": "scoped API key",
                "tools_listed": True,
                "tool_call_succeeded": True,
                "result": "Cursor listed Sibyl tools and completed memory.recall.",
            },
            "claude_code": {
                "status": "PASS",
                "client": "Claude Code",
                "auth_method": "scoped API key",
                "tools_listed": True,
                "recall_succeeded": True,
                "result": "Claude Code listed tools and completed recall.",
            },
            "claude_desktop": {
                "status": "PASS",
                "client": "Claude Desktop",
                "auth_method": "scoped API key",
                "tools_listed": True,
                "tool_call_succeeded": True,
                "result": "Claude Desktop listed tools and completed recall.",
            },
        },
    }


def _write_mcp_smoke_receipt(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_capture_mcp_client_smoke_evidence_updates_manifest(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    source = _write_mcp_smoke_receipt(
        tmp_path / "mcp-smoke.json",
        _mcp_smoke_payload(),
    )

    receipt = evidence.capture_mcp_client_smoke_evidence(
        evidence_dir,
        source_receipt=source,
        captured_by="Nova",
    )
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    cursor_item = payload["items"]["mcp_cursor_auth"]
    code_item = payload["items"]["mcp_claude_code_auth"]
    desktop_item = payload["items"]["mcp_claude_desktop_auth"]
    cursor_receipt = evidence_dir / "mcp_cursor_auth" / "receipt.md"

    assert cursor_item["status"] == "PASS"
    assert code_item["status"] == "PASS"
    assert desktop_item["status"] == "PASS"
    assert cursor_item["artifacts"][1]["path"] == ("mcp_client_smoke/mcp-client-smoke-receipt.json")
    assert "Cursor listed Sibyl tools" in cursor_receipt.read_text(encoding="utf-8")


def test_capture_mcp_client_smoke_evidence_accepts_partial_client_receipts(
    tmp_path: Path,
) -> None:
    evidence_dir = tmp_path / "evidence"
    payload = _mcp_smoke_payload()
    payload["clients"].pop("claude_desktop")
    source = _write_mcp_smoke_receipt(tmp_path / "mcp-smoke.json", payload)

    receipt = evidence.capture_mcp_client_smoke_evidence(
        evidence_dir,
        source_receipt=source,
        captured_by="Nova",
    )
    manifest_path = Path(str(receipt["manifest"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["items"]["mcp_cursor_auth"]["status"] == "PASS"
    assert manifest["items"]["mcp_claude_code_auth"]["status"] == "PASS"
    assert manifest["items"]["mcp_claude_desktop_auth"]["status"] == "TODO"
    assert "mcp-client-smoke-receipt.json" not in json.dumps(
        manifest["items"]["mcp_claude_desktop_auth"]
    )


def test_capture_mcp_client_smoke_evidence_rejects_unknown_clients(
    tmp_path: Path,
) -> None:
    payload = _mcp_smoke_payload()
    payload["clients"] = {
        "other": {
            "status": "PASS",
            "auth_method": "scoped API key",
            "tools_listed": True,
            "tool_call_succeeded": True,
        }
    }
    source = _write_mcp_smoke_receipt(tmp_path / "mcp-smoke.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_mcp_client_smoke_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "must include at least one supported client" in str(exc_info.value)


def test_capture_mcp_client_smoke_evidence_rejects_unlisted_tools(
    tmp_path: Path,
) -> None:
    payload = _mcp_smoke_payload()
    payload["clients"]["cursor"]["tools_listed"] = False
    source = _write_mcp_smoke_receipt(tmp_path / "mcp-smoke.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_mcp_client_smoke_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "cursor must prove tools_listed=true" in str(exc_info.value)


def test_capture_mcp_client_smoke_evidence_rejects_missing_tool_call(
    tmp_path: Path,
) -> None:
    payload = _mcp_smoke_payload()
    payload["clients"]["cursor"]["tool_call_succeeded"] = False
    source = _write_mcp_smoke_receipt(tmp_path / "mcp-smoke.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_mcp_client_smoke_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "cursor must prove a tool call or recall succeeded" in str(exc_info.value)


def test_main_capture_mcp_client_smoke_evidence_requires_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "mcp-smoke.txt"
    source.write_text("not JSON", encoding="utf-8")

    exit_code = evidence.main(
        [
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--capture-mcp-client-smoke-evidence",
            str(source),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "MCP client smoke receipt must be a JSON file" in captured.out


def _restore_drill_payload() -> dict[str, Any]:
    return {
        "status": "PASS",
        "runtime": "kind/sibyl-enterprise namespace sibyl-restore-drill",
        "row_counts": {
            "entity": {"expected": 12, "actual": 12},
            "episode": {"expected": 4, "actual": 4},
        },
        "recall_sample": {
            "query": "restore drill fixture memory",
            "result_count": 1,
            "sample": "restore drill fixture memory returned from restored runtime",
        },
    }


def _write_restore_drill_receipt(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_capture_restore_drill_evidence_updates_manifest(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    source = _write_restore_drill_receipt(
        tmp_path / "restore-drill.json",
        _restore_drill_payload(),
    )

    receipt = evidence.capture_restore_drill_evidence(
        evidence_dir,
        source_receipt=source,
        captured_by="Nova",
    )
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    drill_item = payload["items"]["kubernetes_restore_drill"]
    recall_item = payload["items"]["restore_recall_sample"]
    drill_receipt = evidence_dir / "kubernetes_restore_drill" / "receipt.md"
    recall_sample = evidence_dir / "restore_recall_sample" / "restore-recall-sample.json"

    assert drill_item["status"] == "PASS"
    assert recall_item["status"] == "PASS"
    assert drill_item["artifacts"][1]["path"] == (
        "kubernetes_restore_drill/restore-drill-receipt.json"
    )
    assert recall_item["artifacts"][1]["path"] == "restore_recall_sample/restore-recall-sample.json"
    assert "entity: expected 12, actual 12" in drill_receipt.read_text(encoding="utf-8")
    assert json.loads(recall_sample.read_text(encoding="utf-8"))["result_count"] == 1


def test_capture_restore_drill_evidence_from_kubernetes_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = _restore_drill_payload()
    log_text = "\n".join(
        [
            "restore drill receipt: /tmp/restore-drill-receipt.json",
            evidence.RESTORE_RECEIPT_LOG_BEGIN,
            json.dumps(payload),
            evidence.RESTORE_RECEIPT_LOG_END,
        ]
    )

    def fake_kubectl_text_output(args: list[str]) -> str:
        assert args == [
            "logs",
            "job/sibyl-surrealdb-restore-drill-manual",
            "--namespace",
            "sibyl-restore",
            "--container",
            "restore-drill",
        ]
        return log_text

    monkeypatch.setattr(evidence, "_kubectl_text_output", fake_kubectl_text_output)

    receipt = evidence.capture_restore_drill_evidence_from_kubernetes(
        tmp_path / "evidence",
        namespace="sibyl-restore",
        job_name="sibyl-surrealdb-restore-drill-manual",
        captured_by="Nova",
    )

    assert receipt["status"] == "PASS"
    source_receipt = (
        tmp_path / "evidence" / "kubernetes_restore_drill" / "restore-drill-receipt.json"
    )
    assert json.loads(source_receipt.read_text(encoding="utf-8"))["runtime"].startswith("kind/")


def test_capture_restore_drill_evidence_from_kubernetes_uses_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = _restore_drill_payload()
    log_text = "\n".join(
        [
            evidence.RESTORE_RECEIPT_LOG_BEGIN,
            json.dumps(payload),
            evidence.RESTORE_RECEIPT_LOG_END,
        ]
    )

    def fake_kubectl_text_output(args: list[str]) -> str:
        assert args == [
            "--context",
            "kind-sibyl-enterprise",
            "logs",
            "job/restore-job",
            "--namespace",
            "sibyl-restore",
            "--container",
            "restore-drill",
        ]
        return log_text

    monkeypatch.setattr(evidence, "_kubectl_text_output", fake_kubectl_text_output)

    receipt = evidence.capture_restore_drill_evidence_from_kubernetes(
        tmp_path / "evidence",
        namespace="sibyl-restore",
        job_name="restore-job",
        context="kind-sibyl-enterprise",
        captured_by="Nova",
    )

    assert receipt["status"] == "PASS"


def test_capture_restore_drill_evidence_from_kubernetes_requires_log_markers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(evidence, "_kubectl_text_output", lambda args: "restore drill passed")

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_restore_drill_evidence_from_kubernetes(
            tmp_path / "evidence",
            namespace="sibyl-restore",
            job_name="sibyl-surrealdb-restore-drill-manual",
            captured_by="Nova",
        )

    assert "restore drill logs must include" in str(exc_info.value)


def test_capture_restore_drill_evidence_rejects_failed_status(tmp_path: Path) -> None:
    payload = _restore_drill_payload()
    payload["status"] = "FAIL"
    source = _write_restore_drill_receipt(tmp_path / "restore-drill.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_restore_drill_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "restore drill status must be PASS" in str(exc_info.value)


def test_capture_restore_drill_evidence_rejects_mismatched_row_count(
    tmp_path: Path,
) -> None:
    payload = _restore_drill_payload()
    payload["row_counts"]["entity"]["actual"] = 11
    source = _write_restore_drill_receipt(tmp_path / "restore-drill.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_restore_drill_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "row_counts.entity expected 12, got 11" in str(exc_info.value)


def test_capture_restore_drill_evidence_accepts_results_fallback(tmp_path: Path) -> None:
    payload = _restore_drill_payload()
    payload["recall_sample"].pop("result_count")
    payload["recall_sample"]["results"] = [{"name": "restore fixture"}]
    source = _write_restore_drill_receipt(tmp_path / "restore-drill.json", payload)

    receipt = evidence.capture_restore_drill_evidence(
        tmp_path / "evidence",
        source_receipt=source,
        captured_by="Nova",
    )

    recall_item = receipt["items"]["restore_recall_sample"]
    assert recall_item["status"] == "PASS"


def test_capture_restore_drill_evidence_rejects_missing_recall_sample(
    tmp_path: Path,
) -> None:
    payload = _restore_drill_payload()
    payload.pop("recall_sample")
    source = _write_restore_drill_receipt(tmp_path / "restore-drill.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_restore_drill_evidence(
            tmp_path / "evidence",
            source_receipt=source,
            captured_by="Nova",
        )

    assert "restore drill receipt must include recall_sample" in str(exc_info.value)


def test_main_capture_restore_drill_evidence_requires_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "restore-drill.txt"
    source.write_text("not JSON", encoding="utf-8")

    exit_code = evidence.main(
        [
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--capture-restore-drill-evidence",
            str(source),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "restore drill receipt must be a JSON file" in captured.out


def test_main_capture_kubernetes_restore_drill(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fake_capture_restore_drill_evidence_from_kubernetes(
        evidence_dir: Path,
        *,
        namespace: str,
        job_name: str,
        container: str,
        context: str,
        captured_by: str,
    ) -> dict[str, Any]:
        assert evidence_dir == tmp_path / "evidence"
        assert namespace == "sibyl-restore"
        assert job_name == "restore-job"
        assert container == "restore-drill"
        assert context == "kind-sibyl-enterprise"
        assert captured_by == "Nova"
        return {
            "schema_version": evidence.SCHEMA_VERSION,
            "status": "PASS",
            "manifest": str(tmp_path / "evidence" / "enterprise-readiness-evidence.json"),
        }

    monkeypatch.setattr(
        evidence,
        "capture_restore_drill_evidence_from_kubernetes",
        fake_capture_restore_drill_evidence_from_kubernetes,
    )

    exit_code = evidence.main(
        [
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--capture-kubernetes-restore-drill",
            "restore-job",
            "--kubernetes-namespace",
            "sibyl-restore",
            "--kubernetes-context",
            "kind-sibyl-enterprise",
            "--manual-captured-by",
            "Nova",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "captured Kubernetes restore drill and recall evidence from job logs" in captured.out


def _idp_role_claim_payload() -> dict[str, Any]:
    return {
        "provider": "entra",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "appId": "22222222-2222-2222-2222-222222222222",
        "displayName": "Sibyl Dev",
        "appRoles": [
            {
                "allowedMemberTypes": ["User"],
                "displayName": "Sibyl member",
                "id": "33333333-3333-3333-3333-333333333333",
                "isEnabled": True,
                "value": "Sibyl.Member",
            },
            {
                "allowedMemberTypes": ["User"],
                "displayName": "Sibyl admin",
                "id": "44444444-4444-4444-4444-444444444444",
                "isEnabled": True,
                "value": "Sibyl.Admin",
            },
            {
                "allowedMemberTypes": ["User"],
                "displayName": "Sibyl owner",
                "id": "55555555-5555-5555-5555-555555555555",
                "isEnabled": True,
                "value": "Sibyl.Owner",
            },
        ],
    }


def _write_idp_role_claim_config(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_capture_idp_role_claim_evidence_updates_manifest(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    source = _write_idp_role_claim_config(
        tmp_path / "idp-config.json",
        _idp_role_claim_payload(),
    )

    receipt = evidence.capture_idp_role_claim_evidence(
        evidence_dir,
        source_config=source,
        captured_by="Nova",
    )
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = payload["items"]["idp_role_claim_evidence"]
    receipt_file = evidence_dir / "idp_role_claim_evidence" / "receipt.md"
    config_file = evidence_dir / "idp_role_claim_evidence" / "idp-role-claim-config.json"

    assert item["status"] == "PASS"
    assert item["artifacts"][0]["path"] == "idp_role_claim_evidence/receipt.md"
    assert item["artifacts"][1]["path"] == ("idp_role_claim_evidence/idp-role-claim-config.json")
    assert "Sibyl.Member" in receipt_file.read_text(encoding="utf-8")
    assert json.loads(config_file.read_text(encoding="utf-8"))["provider"] == "entra"


def test_capture_idp_role_claim_evidence_rejects_missing_role(tmp_path: Path) -> None:
    payload = _idp_role_claim_payload()
    payload["appRoles"] = [role for role in payload["appRoles"] if role["value"] != "Sibyl.Owner"]
    source = _write_idp_role_claim_config(tmp_path / "idp-config.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_idp_role_claim_evidence(
            tmp_path / "evidence",
            source_config=source,
            captured_by="Nova",
        )

    assert "missing enabled app role: Sibyl.Owner" in str(exc_info.value)


def test_capture_idp_role_claim_evidence_rejects_disabled_role(tmp_path: Path) -> None:
    payload = _idp_role_claim_payload()
    payload["appRoles"][0]["isEnabled"] = False
    source = _write_idp_role_claim_config(tmp_path / "idp-config.json", payload)

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_idp_role_claim_evidence(
            tmp_path / "evidence",
            source_config=source,
            captured_by="Nova",
        )

    assert "Sibyl.Member must be enabled" in str(exc_info.value)


def test_main_capture_idp_role_claim_evidence_requires_json(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "idp-config.txt"
    source.write_text("not JSON", encoding="utf-8")

    exit_code = evidence.main(
        [
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--capture-idp-role-claim-evidence",
            str(source),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "IdP role-claim config must be a JSON file" in captured.out


def test_capture_manual_evidence_updates_manifest(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    source = tmp_path / "role-claim-config.txt"
    source.write_text("Entra App Roles emit a roles claim for Sibyl.Member", encoding="utf-8")

    receipt = evidence.capture_manual_evidence(
        evidence_dir,
        key="idp_role_claim_evidence",
        source_artifacts=[source],
        runtime="Entra admin center",
        flow="Exported app registration role-claim settings.",
        result="The config maps Sibyl.Member to the OIDC roles claim.",
        captured_by="Nova",
        redactions="Tenant display name redacted",
    )
    manifest_path = Path(str(receipt["manifest"]))
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    item = payload["items"]["idp_role_claim_evidence"]
    captured = evidence_dir / "idp_role_claim_evidence" / "role-claim-config.txt"
    receipt_file = evidence_dir / "idp_role_claim_evidence" / "receipt.md"

    assert item["status"] == "PASS"
    assert item["artifacts"][0]["path"] == "idp_role_claim_evidence/receipt.md"
    assert item["artifacts"][1]["path"] == "idp_role_claim_evidence/role-claim-config.txt"
    assert captured.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert "Runtime or environment: Entra admin center" in receipt_file.read_text(encoding="utf-8")
    assert "Tenant display name redacted" in receipt_file.read_text(encoding="utf-8")


def test_capture_manual_evidence_rejects_dedicated_capture_item(tmp_path: Path) -> None:
    source = tmp_path / "audit.json"
    source.write_text("{}", encoding="utf-8")

    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_manual_evidence(
            tmp_path / "evidence",
            key="audit_export_sample",
            source_artifacts=[source],
            runtime="live runtime",
            flow="downloaded audit export",
            result="audit export returned JSON",
            captured_by="Nova",
        )

    assert "audit_export_sample cannot be captured manually" in str(exc_info.value)


def test_capture_manual_evidence_rejects_missing_artifact(tmp_path: Path) -> None:
    with pytest.raises(evidence.EvidenceFailure) as exc_info:
        evidence.capture_manual_evidence(
            tmp_path / "evidence",
            key="idp_role_claim_evidence",
            source_artifacts=[tmp_path / "missing.txt"],
            runtime="Entra admin center",
            flow="exported claim config",
            result="roles claim configured",
            captured_by="Nova",
        )

    assert "manual evidence artifact not found" in str(exc_info.value)


def test_main_capture_manual_evidence_requires_artifact(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    exit_code = evidence.main(
        [
            "--evidence-dir",
            str(tmp_path),
            "--capture-manual-evidence",
            "idp_role_claim_evidence",
            "--manual-runtime",
            "Entra admin center",
            "--manual-flow",
            "exported app role config",
            "--manual-result",
            "roles claim configured",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "manual evidence requires at least one artifact" in captured.out
