"""Score-aware issuance of LongMemEval-V2 release authority artifacts."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_authorization as authorization
from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    bind_artifact,
    load_json,
)
from tools.bench import longmemeval_v2_rig as rig


def _validated_artifact(
    path: Path,
    *,
    name: str,
    validator: Callable[[object], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = bind_artifact(path, name=name)
    validated = validator(load_json(Path(binding["path"])))
    if bind_artifact(path, name=name) != binding:
        raise StagePlanError(f"{name} changed during validation")
    return binding, validated


def package_aa_authorization(
    receipt_path: Path,
    *,
    paired_pass_paths: list[Path],
) -> dict[str, Any]:
    """Project a fully validated prior-stage A/A receipt into authority."""
    source, receipt = _validated_artifact(
        receipt_path,
        name="A/A source receipt",
        validator=rig.validate_aa_receipt,
    )
    if len(paired_pass_paths) != len(receipt["passes"]):
        raise StagePlanError("A/A paired-pass artifact count differs from its receipt")
    paired_artifacts = [
        _validated_artifact(
            path,
            name="A/A paired-pass artifact",
            validator=rig.validate_pass,
        )
        for path in paired_pass_paths
    ]
    for summary, (_binding, paired_pass) in zip(
        receipt["passes"],
        paired_artifacts,
        strict=True,
    ):
        expected = {
            "pass_id": summary["pass_id"],
            "seed": summary["seed"],
            "paired_pass_sha256": summary["paired_pass_sha256"],
        }
        if any(paired_pass.get(key) != value for key, value in expected.items()):
            raise StagePlanError("A/A paired-pass artifact differs from its receipt lineage")
    payload = {
        "schema_version": authorization.AA_AUTHORIZATION_SCHEMA_VERSION,
        "kind": "aa",
        "source_receipt": source,
        "status": receipt["status"],
        "aa_receipt_sha256": receipt["aa_receipt_sha256"],
        "stack": receipt["stack"],
        "arm_contract": receipt["arm_contract"],
        "passes": [
            {
                "pass_id": item["pass_id"],
                "seed": item["seed"],
                "paired_pass_sha256": item["paired_pass_sha256"],
                "paired_pass_artifact": binding,
            }
            for item, (binding, _paired_pass) in zip(
                receipt["passes"],
                paired_artifacts,
                strict=True,
            )
        ],
    }
    payload["authorization_sha256"] = rig.canonical_sha256(payload)
    return authorization.require_aa_authorization(payload)


def _package_anchor_gate(path: Path, preregistration: dict[str, Any]) -> dict[str, Any]:
    def validate(raw: object) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise StagePlanError("anchor gate receipt is missing")
        rig._require_exact_keys(raw, rig.ANCHOR_RECEIPT_KEYS, name="anchor gate receipt")
        unsigned = {key: value for key, value in raw.items() if key != "anchor_receipt_sha256"}
        if (
            raw.get("schema_version") != rig.ANCHOR_SCHEMA_VERSION
            or raw.get("anchor_receipt_sha256") != rig.canonical_sha256(unsigned)
            or raw.get("status") != "PASS"
            or raw.get("anchor_publishable") is not True
            or raw.get("stack") != preregistration["stack"]
            or raw.get("aa_receipt_sha256") != preregistration["aa_receipt_sha256"]
        ):
            raise StagePlanError("race preregistration anchor gate is invalid")
        return raw

    source, receipt = _validated_artifact(
        path,
        name="anchor gate receipt",
        validator=validate,
    )
    return {
        "kind": "anchor",
        "source_receipt": source,
        "receipt_sha256": receipt["anchor_receipt_sha256"],
    }


def _package_race_gate(path: Path, preregistration: dict[str, Any]) -> dict[str, Any]:
    source, receipt = _validated_artifact(
        path,
        name="race gate receipt",
        validator=rig.validate_race_receipt,
    )
    if (
        receipt["race_receipt_sha256"] != preregistration["race_receipt_sha256"]
        or receipt["stack"] != preregistration["stack"]
    ):
        raise StagePlanError("render preregistration race gate differs from its source")
    return {
        "kind": "race",
        "source_receipt": source,
        "receipt_sha256": receipt["race_receipt_sha256"],
    }


def _package_policy(preregistration: dict[str, Any], *, kind: str) -> dict[str, Any]:
    if kind == "race":
        return {}
    race_receipt = rig.validate_race_receipt(preregistration["race_receipt"])
    selected = race_receipt["selected_render_substrate"]
    return {
        "selected_render_substrate": selected,
        "render_applicable": selected == "machine",
        "included_levers": list(preregistration["included_levers"]),
        "replay_survivors": dict(preregistration["replay_survivors"]),
    }


def package_preregistration_authorization(
    preregistration_path: Path,
    *,
    kind: str,
    gate_receipt_path: Path,
) -> dict[str, Any]:
    """Project a fully validated prior-stage preregistration into authority."""
    source, preregistration = _validated_artifact(
        preregistration_path,
        name="source preregistration",
        validator=lambda raw: rig.validate_preregistration(raw, kind=kind),
    )
    aa_receipt = preregistration["aa_receipt"]
    gate = (
        _package_anchor_gate(gate_receipt_path, preregistration)
        if kind == "race"
        else _package_race_gate(gate_receipt_path, preregistration)
    )
    payload = {
        "schema_version": authorization.PREREGISTRATION_AUTHORIZATION_SCHEMA_VERSION,
        "kind": kind,
        "source_preregistration": source,
        "preregistration_sha256": preregistration["preregistration_sha256"],
        "stack": preregistration["stack"],
        "seeds": list(preregistration["seeds"]),
        "aa_receipt_sha256": preregistration["aa_receipt_sha256"],
        "aa_passes": [
            {
                "pass_id": item["pass_id"],
                "seed": item["seed"],
                "paired_pass_sha256": item["paired_pass_sha256"],
            }
            for item in aa_receipt["passes"]
        ],
        "contracts": {key: preregistration[key] for key in authorization.contract_keys(kind)},
        "gate": gate,
        "policy": _package_policy(preregistration, kind=kind),
    }
    payload["authorization_sha256"] = rig.canonical_sha256(payload)
    return authorization.require_preregistration_authorization(payload, kind=kind)


def write_authorization(
    path: Path,
    payload: dict[str, Any],
    *,
    paid_output_root: Path,
) -> None:
    """Write a validated authority artifact once, outside paid output roots."""
    if payload.get("kind") == "aa":
        authorization.require_aa_authorization(payload)
    else:
        authorization.require_preregistration_authorization(
            payload,
            kind=str(payload.get("kind")),
        )
    target = path.expanduser().resolve()
    canonical_paid_root = paid_output_root.expanduser().resolve()
    if paid_output_root != canonical_paid_root:
        raise StagePlanError("paid output root must be canonical")
    if target == canonical_paid_root or canonical_paid_root in target.parents:
        raise StagePlanError("authorization output must remain outside the paid output root")
    try:
        release_io.write_json_once_atomic(target, payload)
    except FileExistsError as exc:
        raise StagePlanError("authorization output already exists") from exc
