"""Score-aware issuance of LongMemEval-V2 release authority artifacts."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
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


def _public_path(path: Path, *, name: str) -> str:
    canonical = path.expanduser().resolve()
    if path != canonical:
        raise StagePlanError(f"{name} public path must be canonical")
    return str(canonical)


def package_aa_authorization(
    receipt_path: Path,
    *,
    paired_pass_paths: list[Path],
    public_receipt_path: Path | None = None,
    public_paired_pass_paths: list[Path] | None = None,
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
    validated = build_aa_authorization(
        source,
        receipt,
        paired_artifacts=paired_artifacts,
    )
    return rebase_aa_authorization(
        validated,
        public_receipt_path=public_receipt_path,
        public_paired_pass_paths=public_paired_pass_paths,
    )


def build_aa_authorization(
    source: dict[str, Any],
    receipt: dict[str, Any],
    *,
    paired_artifacts: list[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Build A/A authority from already fd-bound physical artifacts."""

    receipt = rig.validate_aa_receipt(receipt)
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


def rebase_aa_authorization(
    validated: dict[str, Any],
    *,
    public_receipt_path: Path | None,
    public_paired_pass_paths: list[Path] | None,
) -> dict[str, Any]:
    """Rebase a physically validated A/A projection to future public paths."""

    if public_receipt_path is None and public_paired_pass_paths is None:
        return validated
    if public_receipt_path is None or public_paired_pass_paths is None:
        raise StagePlanError("A/A public authorization paths are incomplete")
    if len(public_paired_pass_paths) != len(validated["passes"]):
        raise StagePlanError("A/A public paired-pass path count is invalid")
    rebased = deepcopy(validated)
    rebased["source_receipt"]["path"] = _public_path(
        public_receipt_path,
        name="A/A source receipt",
    )
    for item, public_path in zip(
        rebased["passes"],
        public_paired_pass_paths,
        strict=True,
    ):
        item["paired_pass_artifact"]["path"] = _public_path(
            public_path,
            name="A/A paired-pass artifact",
        )
    rebased["authorization_sha256"] = rig.canonical_sha256(
        {key: value for key, value in rebased.items() if key != "authorization_sha256"}
    )
    return rebased


def package_rig_blocked_authorization(
    receipt_path: Path,
    *,
    ledger_path: Path,
    public_receipt_path: Path | None = None,
    public_ledger_path: Path | None = None,
) -> dict[str, Any]:
    """Project a dispatch-exhaustion receipt and the ledger it derives from into authority."""
    source, receipt = _validated_artifact(
        receipt_path,
        name="rig-blocked source receipt",
        validator=rig.validate_rig_blocked_receipt,
    )
    source_ledger, ledger = _validated_artifact(
        ledger_path,
        name="rig-blocked source ledger",
        validator=rig.require_dispatch_ledger,
    )
    validated = build_rig_blocked_authorization(
        source,
        receipt,
        source_ledger=source_ledger,
        ledger=ledger,
    )
    return rebase_rig_blocked_authorization(
        validated,
        public_receipt_path=public_receipt_path,
        public_ledger_path=public_ledger_path,
    )


def build_rig_blocked_authorization(
    source: dict[str, Any],
    receipt: dict[str, Any],
    *,
    source_ledger: dict[str, Any],
    ledger: dict[str, Any],
) -> dict[str, Any]:
    """Build dispatch-exhaustion authority from fd-bound receipt and ledger bytes.

    The receipt is rebuilt from the ledger and must match it field for field
    outside the provenance block, so a receipt cannot cite a ledger it was not
    sealed from.
    """

    receipt = rig.validate_rig_blocked_receipt(receipt)
    if receipt["ledger_provenance"] != rig.LEDGER_PROVENANCE_VERIFIED:
        raise StagePlanError("rig-blocked authorization requires a GitHub-verified ledger")
    try:
        rig.require_receipt_from_ledger(receipt, ledger)
    except rig.RigInputError as exc:
        raise StagePlanError(str(exc)) from exc
    payload = authorization.rig_blocked_projection(source, source_ledger, receipt)
    payload["authorization_sha256"] = rig.canonical_sha256(payload)
    return authorization.require_rig_blocked_authorization(payload)


def rebase_rig_blocked_authorization(
    validated: dict[str, Any],
    *,
    public_receipt_path: Path | None,
    public_ledger_path: Path | None,
) -> dict[str, Any]:
    """Rebase a physically validated rig-blocked projection to public paths."""

    if public_receipt_path is None and public_ledger_path is None:
        return validated
    if public_receipt_path is None or public_ledger_path is None:
        raise StagePlanError("rig-blocked public authorization paths are incomplete")
    rebased = deepcopy(validated)
    rebased["source_receipt"]["path"] = _public_path(
        public_receipt_path,
        name="rig-blocked source receipt",
    )
    rebased["source_ledger"]["path"] = _public_path(
        public_ledger_path,
        name="rig-blocked source ledger",
    )
    rebased["authorization_sha256"] = rig.canonical_sha256(
        {key: value for key, value in rebased.items() if key != "authorization_sha256"}
    )
    return rebased


def _package_anchor_gate(
    path: Path,
    preregistration: dict[str, Any],
) -> dict[str, Any]:
    source, receipt = _validated_artifact(
        path,
        name="anchor gate receipt",
        validator=lambda raw: _validate_anchor_gate(raw, preregistration),
    )
    return _anchor_gate(source, receipt)


def _validate_anchor_gate(raw: object, preregistration: dict[str, Any]) -> dict[str, Any]:
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


def _anchor_gate(source: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "anchor",
        "source_receipt": source,
        "receipt_sha256": receipt["anchor_receipt_sha256"],
    }


def _package_race_gate(
    path: Path,
    preregistration: dict[str, Any],
) -> dict[str, Any]:
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
    return _race_gate(source, receipt)


def _race_gate(source: dict[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
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
    public_preregistration_path: Path | None = None,
    public_gate_receipt_path: Path | None = None,
) -> dict[str, Any]:
    """Project a fully validated prior-stage preregistration into authority."""
    source, preregistration = _validated_artifact(
        preregistration_path,
        name="source preregistration",
        validator=lambda raw: rig.validate_preregistration(raw, kind=kind),
    )
    gate_builder = _package_anchor_gate if kind == "race" else _package_race_gate
    gate = gate_builder(gate_receipt_path, preregistration)
    validated = build_preregistration_authorization(
        source,
        preregistration,
        kind=kind,
        gate=gate,
    )
    return rebase_preregistration_authorization(
        validated,
        kind=kind,
        public_preregistration_path=public_preregistration_path,
        public_gate_receipt_path=public_gate_receipt_path,
    )


def build_preregistration_authorization(
    source: dict[str, Any],
    preregistration: dict[str, Any],
    *,
    kind: str,
    gate: dict[str, Any],
) -> dict[str, Any]:
    """Build preregistration authority from fd-bound physical artifacts."""

    preregistration = rig.validate_preregistration(preregistration, kind=kind)
    aa_receipt = preregistration["aa_receipt"]
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


def build_preregistration_gate(
    source: dict[str, Any],
    receipt: dict[str, Any],
    preregistration: dict[str, Any],
    *,
    kind: str,
) -> dict[str, Any]:
    """Build one gate projection from fd-bound receipt bytes."""

    if kind == "race":
        validated = _validate_anchor_gate(receipt, preregistration)
        return _anchor_gate(source, validated)
    if kind != "render":
        raise StagePlanError("preregistration gate kind is invalid")
    validated = rig.validate_race_receipt(receipt)
    if (
        validated["race_receipt_sha256"] != preregistration["race_receipt_sha256"]
        or validated["stack"] != preregistration["stack"]
    ):
        raise StagePlanError("render preregistration race gate differs from its source")
    return _race_gate(source, validated)


def rebase_preregistration_authorization(
    validated: dict[str, Any],
    *,
    kind: str,
    public_preregistration_path: Path | None,
    public_gate_receipt_path: Path | None,
) -> dict[str, Any]:
    """Rebase a validated preregistration projection to public paths."""

    if public_preregistration_path is None and public_gate_receipt_path is None:
        return validated
    if public_preregistration_path is None or public_gate_receipt_path is None:
        raise StagePlanError("preregistration public authorization paths are incomplete")
    rebased = deepcopy(validated)
    rebased["source_preregistration"]["path"] = _public_path(
        public_preregistration_path,
        name="source preregistration",
    )
    rebased["gate"]["source_receipt"]["path"] = _public_path(
        public_gate_receipt_path,
        name=f"{kind} gate receipt",
    )
    rebased["authorization_sha256"] = rig.canonical_sha256(
        {key: value for key, value in rebased.items() if key != "authorization_sha256"}
    )
    return rebased


def write_authorization(
    path: Path,
    payload: dict[str, Any],
    *,
    paid_output_root: Path,
) -> None:
    """Write a validated authority artifact once, outside paid output roots."""
    if payload.get("kind") == "aa":
        authorization.require_aa_authorization(payload)
    elif payload.get("kind") == "rig_blocked":
        authorization.require_rig_blocked_authorization(payload)
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
