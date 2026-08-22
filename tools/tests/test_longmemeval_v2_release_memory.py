from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_contract as contract
from benchmarks import longmemeval_v2_release_inputs as inputs
from benchmarks import longmemeval_v2_release_memory as memory
from tools.tests.longmemeval_v2_release_support import (
    anchor_spec,
    write_dataset,
    write_saved_memory,
)


@pytest.fixture
def sealed_memory_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    data_root = tmp_path / "data"
    write_dataset(data_root)
    monkeypatch.setattr(
        inputs,
        "OFFICIAL_DATASET_SHA256",
        {
            name: inputs.sha256_file(data_root / relative)
            for name, relative in inputs.DATASET_ARTIFACT_NAMES.items()
        },
    )
    monkeypatch.setattr(
        inputs.rig,
        "OFFICIAL_SMALL_QUESTION_COUNTS",
        {"web": 1, "enterprise": 1},
    )
    monkeypatch.setattr(
        inputs.rig,
        "OFFICIAL_SMALL_QUESTION_IDS_SHA256",
        {
            "web": inputs.rig.canonical_sha256(["web-1"]),
            "enterprise": inputs.rig.canonical_sha256(["enterprise-1"]),
        },
    )
    return {
        "data_root": data_root,
        "output_root": tmp_path / "output",
        "source": {
            "repository": "hyperb1iss/sibyl",
            "ref": "refs/heads/main",
            "sha": "a" * 40,
        },
    }


def _baseline_spec(
    *,
    web_root: Path,
    enterprise_root: Path,
) -> dict[str, Any]:
    spec = anchor_spec()
    spec["memory_roots"]["baseline"] = {
        "web": str(web_root),
        "enterprise": str(enterprise_root),
    }
    return spec


def test_external_memory_rejects_swapped_domain_stamp(
    sealed_memory_inputs: dict[str, Any],
) -> None:
    source = sealed_memory_inputs["source"]
    roots = {
        domain: sealed_memory_inputs["output_root"].parent / f"swapped-domain-{domain}"
        for domain in inputs.DOMAINS
    }
    for domain, root in roots.items():
        write_saved_memory(root, domain=domain, source_sha=source["sha"])
        other_domain = "enterprise" if domain == "web" else "web"
        config_path = root / "memory_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["memory_params"]["longmemeval_v2_domain"] = other_domain
        config_path.write_text(json.dumps(config) + "\n", encoding="utf-8")
        manifest_path = root / "memory_manifest.json"
        saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        saved_manifest["longmemeval_v2_domain"] = other_domain
        saved_manifest["memory_config_sha256"] = inputs.sha256_file(config_path)
        manifest_path.write_text(json.dumps(saved_manifest) + "\n", encoding="utf-8")
    spec = _baseline_spec(
        web_root=roots["web"],
        enterprise_root=roots["enterprise"],
    )

    with pytest.raises(contract.StagePlanError, match="domain identity"):
        memory.build_memory_bindings(
            spec,
            dataset=inputs.dataset_record(sealed_memory_inputs["data_root"]),
            source=source,
        )


@pytest.mark.parametrize(
    ("memory_options", "message"),
    [
        (
            {"api_url": "https://user:SECRET@evil.example/api?token=SECRET"},
            "API URL differs",
        ),
        ({"include_screenshot_refs": True}, "replay configuration differs"),
    ],
)
def test_external_memory_rejects_unsealed_endpoint_and_replay_flags(
    sealed_memory_inputs: dict[str, Any],
    memory_options: dict[str, Any],
    message: str,
) -> None:
    source = sealed_memory_inputs["source"]
    option_name = next(iter(memory_options))
    web_root = sealed_memory_inputs["output_root"].parent / f"unsealed-{option_name}-web"
    enterprise_root = (
        sealed_memory_inputs["output_root"].parent / f"unsealed-{option_name}-enterprise"
    )
    write_saved_memory(
        web_root,
        domain="web",
        source_sha=source["sha"],
        **memory_options,
    )
    write_saved_memory(
        enterprise_root,
        domain="enterprise",
        source_sha=source["sha"],
        **memory_options,
    )
    spec = _baseline_spec(
        web_root=web_root,
        enterprise_root=enterprise_root,
    )

    with pytest.raises(contract.StagePlanError, match=message) as exc_info:
        memory.build_memory_bindings(
            spec,
            dataset=inputs.dataset_record(sealed_memory_inputs["data_root"]),
            source=source,
        )
    assert "SECRET" not in str(exc_info.value)
