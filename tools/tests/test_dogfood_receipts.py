from __future__ import annotations

import json
from pathlib import Path

from tools.trust import dogfood_receipts

API_DIGEST = f"sha256:{'a' * 64}"
WEB_DIGEST = f"sha256:{'b' * 64}"
SOURCE_REVISION = "f" * 40
REQUIRED_COMMITS = [
    "36094084",
    "e59e9be1",
    "b9e3ade8",
    "6bf8881f",
    "4bf80afd",
    "2095b616",
    "dcb8d340",
    "98d9043c",
    "f74f23f4",
]


def _image_receipt() -> dict[str, object]:
    deployment = {
        "version": "1.1.0-rc.1",
        "expected_version": "1.1.0-rc.1",
        "source_revision": SOURCE_REVISION,
        "source_commits": [SOURCE_REVISION, *REQUIRED_COMMITS],
        "required_source_commits": REQUIRED_COMMITS,
        "image_digests": {"api": API_DIGEST, "web": WEB_DIGEST},
        "expected_image_digests": {"api": API_DIGEST, "web": WEB_DIGEST},
    }
    return {
        "schema_version": "sibyl-dogfood-deployment-image-receipt-v1",
        "deployment": deployment,
        **deployment,
    }


def _health() -> dict[str, object]:
    return {"status": "healthy", "version": "1.1.0-rc.1"}


def _container_inspects(*, revision: str = SOURCE_REVISION) -> list[dict[str, object]]:
    return [
        {
            "Name": "/sibyl-backend",
            "RepoDigests": [f"ghcr.io/hyperb1iss/sibyl-api@{API_DIGEST}"],
            "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
        },
        {
            "Name": "/sibyl-frontend",
            "RepoDigests": [f"ghcr.io/hyperb1iss/sibyl-web@{WEB_DIGEST}"],
            "Config": {"Labels": {"org.opencontainers.image.revision": revision}},
        },
    ]


def test_build_live_deployment_evidence_satisfies_deployment_metrics() -> None:
    evidence = dogfood_receipts.build_live_deployment_evidence(
        _image_receipt(),
        _health(),
        _container_inspects(),
    )

    assert evidence["checks"][0]["status"] == "PASS"
    assert dogfood_receipts.build_deployment_metrics(evidence) == {
        "deployed_version_match": 1.0,
        "image_digest_match": 1.0,
        "required_source_commit_coverage": 1.0,
    }


def test_build_live_deployment_evidence_rejects_mismatched_runtime_revision() -> None:
    evidence = dogfood_receipts.build_live_deployment_evidence(
        _image_receipt(),
        _health(),
        _container_inspects(revision="0" * 40),
    )

    assert evidence["checks"][0]["status"] == "FAIL"
    metrics = dogfood_receipts.build_deployment_metrics(evidence)
    assert metrics["deployed_version_match"] == 1.0
    assert metrics["image_digest_match"] == 1.0
    assert metrics["required_source_commit_coverage"] == 0.0


def test_collect_deployment_cli_writes_valid_evidence(tmp_path: Path) -> None:
    image_receipt = tmp_path / "image-receipt.json"
    health = tmp_path / "health.json"
    inspect = tmp_path / "inspect.json"
    output = tmp_path / "deployment-evidence.json"
    image_receipt.write_text(json.dumps(_image_receipt()), encoding="utf-8")
    health.write_text(json.dumps(_health()), encoding="utf-8")
    inspect.write_text(json.dumps(_container_inspects()), encoding="utf-8")

    exit_code = dogfood_receipts.main(
        [
            "collect-deployment",
            "--image-receipt",
            str(image_receipt),
            "--health-json",
            str(health),
            "--docker-inspect-json",
            str(inspect),
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["schema_version"] == "sibyl-live-deployment-evidence-v1"
    assert dogfood_receipts.build_deployment_metrics(evidence) == {
        "deployed_version_match": 1.0,
        "image_digest_match": 1.0,
        "required_source_commit_coverage": 1.0,
    }
