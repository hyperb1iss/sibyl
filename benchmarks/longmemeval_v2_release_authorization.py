"""Score-blind authority artifacts for LongMemEval-V2 release stages."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    load_json,
    require_artifact,
    require_exact_keys,
    require_nonnegative_int,
    require_string,
)
from tools.bench import longmemeval_v2_rig as rig

AA_AUTHORIZATION_SCHEMA_VERSION = "sibyl-longmemeval-v2-aa-authorization-v2"
PREREGISTRATION_AUTHORIZATION_SCHEMA_VERSION = (
    "sibyl-longmemeval-v2-preregistration-authorization-v2"
)
AA_AUTHORIZATION_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "source_receipt",
        "status",
        "aa_receipt_sha256",
        "stack",
        "arm_contract",
        "passes",
        "authorization_sha256",
    }
)
PREREGISTRATION_AUTHORIZATION_KEYS = frozenset(
    {
        "schema_version",
        "kind",
        "source_preregistration",
        "preregistration_sha256",
        "stack",
        "seeds",
        "aa_receipt_sha256",
        "aa_passes",
        "contracts",
        "gate",
        "policy",
        "authorization_sha256",
    }
)
PASS_AUTHORIZATION_KEYS = frozenset({"pass_id", "seed", "paired_pass_sha256"})
BOUND_PASS_AUTHORIZATION_KEYS = PASS_AUTHORIZATION_KEYS | {"paired_pass_artifact"}
GATE_AUTHORIZATION_KEYS = frozenset({"kind", "source_receipt", "receipt_sha256"})
RENDER_POLICY_KEYS = frozenset(
    {
        "selected_render_substrate",
        "render_applicable",
        "included_levers",
        "replay_survivors",
    }
)
SCORE_BEARING_KEY_FRAGMENTS = (
    "score",
    "accuracy",
    "latency",
    "reader_token",
    "noise_floor",
    "observed_span",
    "first_three_span",
    "stabilized",
)


def reject_score_bearing_keys(raw: object, *, name: str) -> None:
    """Reject score or metric fields anywhere in a planning-side object."""
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            normalized = str(key).lower()
            if any(fragment in normalized for fragment in SCORE_BEARING_KEY_FRAGMENTS):
                raise StagePlanError(f"{name} contains score-bearing field {key!r}")
            reject_score_bearing_keys(value, name=name)
    elif isinstance(raw, list | tuple):
        for value in raw:
            reject_score_bearing_keys(value, name=name)


def _pass_projection(raw: object, *, name: str, bound: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError(f"{name} is missing")
    expected_keys = BOUND_PASS_AUTHORIZATION_KEYS if bound else PASS_AUTHORIZATION_KEYS
    require_exact_keys(raw, expected_keys, name=name)
    pass_id = require_string(raw.get("pass_id"), name=f"{name}.pass_id")
    seed = require_nonnegative_int(raw.get("seed"), name=f"{name}.seed")
    digest = rig._sha256_digest(
        raw.get("paired_pass_sha256"),
        name=f"{name}.paired_pass_sha256",
    )
    result: dict[str, Any] = {
        "pass_id": pass_id,
        "seed": seed,
        "paired_pass_sha256": digest,
    }
    if bound:
        result["paired_pass_artifact"] = require_artifact(
            raw.get("paired_pass_artifact"),
            name=f"{name}.paired_pass_artifact",
        )
    return result


def _passes(
    raw: object,
    *,
    name: str,
    counts: set[int],
    bound: bool = False,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) not in counts:
        raise StagePlanError(f"{name} has an invalid pass count")
    passes = [
        _pass_projection(item, name=f"{name}[{index}]", bound=bound)
        for index, item in enumerate(raw)
    ]
    if len({item["pass_id"] for item in passes}) != len(passes) or len(
        {item["seed"] for item in passes}
    ) != len(passes):
        raise StagePlanError(f"{name} pass identities are not unique")
    return passes


def _authorization_digest(raw: dict[str, Any], *, field: str, name: str) -> None:
    unsigned = {key: value for key, value in raw.items() if key != field}
    if raw.get(field) != rig.canonical_sha256(unsigned):
        raise StagePlanError(f"{name} digest does not bind its projection")


def _require_arm_contract(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError("A/A authorization arm contract is missing")
    require_exact_keys(
        raw,
        frozenset({"substrate", "configuration", "geometry"}),
        name="A/A authorization arm contract",
    )
    if raw.get("substrate") != "machine" or not isinstance(raw.get("configuration"), dict):
        raise StagePlanError("A/A authorization arm contract is invalid")
    geometry = rig._validate_geometry(raw.get("geometry"), name="A/A authorization geometry")
    return {**raw, "geometry": geometry}


def require_aa_authorization(raw: object) -> dict[str, Any]:
    """Validate one scoreless A/A authority projection without reading its receipt."""
    reject_score_bearing_keys(raw, name="A/A authorization")
    if not isinstance(raw, dict):
        raise StagePlanError("A/A authorization is missing")
    require_exact_keys(raw, AA_AUTHORIZATION_KEYS, name="A/A authorization")
    if raw.get("schema_version") != AA_AUTHORIZATION_SCHEMA_VERSION or raw.get("kind") != "aa":
        raise StagePlanError("A/A authorization schema or kind is invalid")
    if raw.get("status") not in {"PASS", "NEEDS_TWO_MORE", "RIG_BLOCKED"}:
        raise StagePlanError("A/A authorization status is invalid")
    source = require_artifact(raw.get("source_receipt"), name="A/A source receipt")
    receipt_digest = rig._sha256_digest(
        raw.get("aa_receipt_sha256"),
        name="A/A authorization receipt digest",
    )
    stack = rig.validate_stack(raw.get("stack"))
    contract = _require_arm_contract(raw.get("arm_contract"))
    passes = _passes(
        raw.get("passes"),
        name="A/A authorization passes",
        counts={rig.INITIAL_AA_PASS_COUNT, rig.EXTENDED_AA_PASS_COUNT},
        bound=True,
    )
    _authorization_digest(
        raw,
        field="authorization_sha256",
        name="A/A authorization",
    )
    return {
        **raw,
        "source_receipt": source,
        "aa_receipt_sha256": receipt_digest,
        "stack": stack,
        "arm_contract": contract,
        "passes": passes,
    }


def contract_keys(kind: str) -> tuple[str, ...]:
    if kind == "race":
        return (
            "machine_configuration",
            "naive_configuration",
            "shipping_geometry",
            "matched_geometry",
        )
    if kind == "render":
        return (
            "control_configuration",
            "treatment_configuration",
            "control_geometry",
            "treatment_geometry",
        )
    raise StagePlanError("preregistration authorization kind must be race or render")


def _require_gate(raw: object, *, kind: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError("preregistration gate receipt is missing")
    require_exact_keys(raw, GATE_AUTHORIZATION_KEYS, name="preregistration gate")
    expected_kind = "anchor" if kind == "race" else "race"
    if raw.get("kind") != expected_kind:
        raise StagePlanError(f"{kind} preregistration has the wrong gate kind")
    source = require_artifact(raw.get("source_receipt"), name=f"{expected_kind} gate receipt")
    digest = rig._sha256_digest(
        raw.get("receipt_sha256"),
        name=f"{expected_kind} gate receipt digest",
    )
    return {"kind": expected_kind, "source_receipt": source, "receipt_sha256": digest}


def _require_policy(raw: object, *, kind: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError("preregistration policy is missing")
    if kind == "race":
        require_exact_keys(raw, frozenset(), name="race preregistration policy")
        return {}
    require_exact_keys(raw, RENDER_POLICY_KEYS, name="render preregistration policy")
    selected = raw.get("selected_render_substrate")
    if selected not in {"machine", "naive"}:
        raise StagePlanError("render preregistration selected substrate is invalid")
    applicable = raw.get("render_applicable")
    if not isinstance(applicable, bool) or applicable is not (selected == "machine"):
        raise StagePlanError("render preregistration applicability is inconsistent")
    levers = raw.get("included_levers")
    if (
        not isinstance(levers, list)
        or not levers
        or any(not isinstance(item, str) or not item for item in levers)
        or len(set(levers)) != len(levers)
    ):
        raise StagePlanError("render preregistration included levers are invalid")
    survivors = raw.get("replay_survivors")
    if (
        not isinstance(survivors, dict)
        or set(survivors) != set(levers)
        or any(survivors.get(item) is not True for item in levers)
    ):
        raise StagePlanError("render preregistration replay survivors are invalid")
    return {
        "selected_render_substrate": selected,
        "render_applicable": applicable,
        "included_levers": list(levers),
        "replay_survivors": dict(survivors),
    }


def require_preregistration_authorization(raw: object, *, kind: str) -> dict[str, Any]:
    """Validate one scoreless preregistration authority projection."""
    reject_score_bearing_keys(raw, name=f"{kind} preregistration authorization")
    if not isinstance(raw, dict):
        raise StagePlanError("preregistration authorization is missing")
    require_exact_keys(
        raw,
        PREREGISTRATION_AUTHORIZATION_KEYS,
        name="preregistration authorization",
    )
    if (
        raw.get("schema_version") != PREREGISTRATION_AUTHORIZATION_SCHEMA_VERSION
        or raw.get("kind") != kind
    ):
        raise StagePlanError("preregistration authorization schema or kind is invalid")
    source = require_artifact(
        raw.get("source_preregistration"),
        name="source preregistration",
    )
    preregistration_digest = rig._sha256_digest(
        raw.get("preregistration_sha256"),
        name="preregistration authorization digest",
    )
    aa_digest = rig._sha256_digest(
        raw.get("aa_receipt_sha256"),
        name="preregistration A/A receipt digest",
    )
    stack = rig.validate_stack(raw.get("stack"))
    seeds_raw = raw.get("seeds")
    if not isinstance(seeds_raw, list) or len(seeds_raw) != rig.PAIRED_PASS_COUNT:
        raise StagePlanError("preregistration authorization seeds are invalid")
    seeds = [require_nonnegative_int(seed, name="preregistration seed") for seed in seeds_raw]
    if len(set(seeds)) != len(seeds):
        raise StagePlanError("preregistration authorization seeds are not unique")
    aa_passes = _passes(
        raw.get("aa_passes"),
        name="preregistration A/A passes",
        counts={rig.INITIAL_AA_PASS_COUNT, rig.EXTENDED_AA_PASS_COUNT},
    )
    contracts = raw.get("contracts")
    expected_contract_keys = frozenset(contract_keys(kind))
    if not isinstance(contracts, dict):
        raise StagePlanError("preregistration authorization contracts are missing")
    require_exact_keys(
        contracts,
        expected_contract_keys,
        name="preregistration authorization contracts",
    )
    if any(not isinstance(contracts[key], dict) for key in expected_contract_keys):
        raise StagePlanError("preregistration authorization contract is invalid")
    gate = _require_gate(raw.get("gate"), kind=kind)
    policy = _require_policy(raw.get("policy"), kind=kind)
    _authorization_digest(
        raw,
        field="authorization_sha256",
        name="preregistration authorization",
    )
    return {
        **raw,
        "source_preregistration": source,
        "preregistration_sha256": preregistration_digest,
        "aa_receipt_sha256": aa_digest,
        "stack": stack,
        "seeds": seeds,
        "aa_passes": aa_passes,
        "contracts": dict(contracts),
        "gate": gate,
        "policy": policy,
    }


def _planned_arm_contract(arm: dict[str, Any]) -> dict[str, Any]:
    manifest = arm["manifest"]
    return {
        "substrate": manifest["substrate"],
        "configuration": manifest["configuration"],
        "geometry": manifest["geometry"],
    }


def _require_aa_arm_contract(spec: dict[str, Any], authorization: dict[str, Any]) -> None:
    expected = authorization["arm_contract"]
    if any(
        _planned_arm_contract(arm) != expected for item in spec["passes"] for arm in item["arms"]
    ):
        raise StagePlanError("planned arm contract differs from its A/A authorization")


def _require_preregistered_arm_contracts(
    spec: dict[str, Any],
    authorization: dict[str, Any],
) -> None:
    contracts = authorization["contracts"]
    for item in spec["passes"]:
        left, right = item["arms"]
        if spec["stage"] == "race":
            matched = item["kind"] == "matched"
            geometry = contracts["matched_geometry"] if matched else None
            expected = (
                {
                    "substrate": "machine",
                    "configuration": contracts["machine_configuration"],
                    "geometry": geometry or contracts["shipping_geometry"]["machine"],
                },
                {
                    "substrate": "naive",
                    "configuration": contracts["naive_configuration"],
                    "geometry": geometry or contracts["shipping_geometry"]["naive"],
                },
            )
        else:
            expected = (
                {
                    "substrate": "machine",
                    "configuration": contracts["control_configuration"],
                    "geometry": contracts["control_geometry"],
                },
                {
                    "substrate": "machine",
                    "configuration": contracts["treatment_configuration"],
                    "geometry": contracts["treatment_geometry"],
                },
            )
        if (_planned_arm_contract(left), _planned_arm_contract(right)) != expected:
            raise StagePlanError(f"planned {spec['stage']} arm contract differs from authorization")


def _require_paid_stage_authorization(
    spec: dict[str, Any],
    authorization: dict[str, Any],
) -> None:
    if spec["stage"] == "render" and (
        (spec["mode"] == "standard") is not authorization["policy"]["render_applicable"]
    ):
        raise StagePlanError("render stage applicability differs from its authorization")
    _require_preregistered_arm_contracts(spec, authorization)


def build_upstream_bindings(
    spec: dict[str, Any],
    *,
    expected_stack: dict[str, Any],
) -> dict[str, Any]:
    """Load only scoreless authority projections and bind their exact stack."""
    upstream = spec["upstream"]
    aa_path = upstream["aa_authorization"]
    prereg_path = upstream["preregistration_authorization"]
    aa = (
        require_aa_authorization(
            load_json(Path(require_string(aa_path, name="upstream.aa_authorization")))
        )
        if aa_path is not None
        else None
    )
    preregistration = (
        require_preregistration_authorization(
            load_json(
                Path(
                    require_string(
                        prereg_path,
                        name="upstream.preregistration_authorization",
                    )
                )
            ),
            kind=spec["stage"],
        )
        if prereg_path is not None
        else None
    )
    for name, authorization in (
        ("A/A", aa),
        ("preregistration", preregistration),
    ):
        if authorization is not None and authorization["stack"] != expected_stack:
            raise StagePlanError(f"{name} authorization stack differs from the sealed stage")

    stage = spec["stage"]
    mode = spec["mode"]
    if stage == "aa" and mode == "initial":
        if aa is not None or preregistration is not None:
            raise StagePlanError("initial A/A cannot bind upstream authority")
    elif stage == "aa":
        if (
            aa is None
            or aa["status"] != "NEEDS_TWO_MORE"
            or len(aa["passes"]) != rig.INITIAL_AA_PASS_COUNT
            or preregistration is not None
        ):
            raise StagePlanError("A/A extension requires the initial NEEDS_TWO_MORE authority")
        prior_ids = {item["pass_id"] for item in aa["passes"]}
        prior_seeds = {item["seed"] for item in aa["passes"]}
        if any(
            item["pass_id"] in prior_ids or item["seed"] in prior_seeds for item in spec["passes"]
        ):
            raise StagePlanError("A/A extension must use two fresh passes")
        _require_aa_arm_contract(spec, aa)
    elif stage == "anchor":
        if aa is None or aa["status"] != "PASS" or preregistration is not None:
            raise StagePlanError("anchor requires only a passing A/A authorization")
        if spec["passes"][0]["seed"] in {item["seed"] for item in aa["passes"]}:
            raise StagePlanError("anchor must use a fresh post-A/A seed")
        _require_aa_arm_contract(spec, aa)
    else:
        if aa is not None or preregistration is None:
            raise StagePlanError(f"{stage} requires only its preregistration authorization")
        _require_paid_stage_authorization(spec, preregistration)
    return {
        "aa_authorization": aa,
        "preregistration_authorization": preregistration,
    }


def require_upstream_bindings(
    raw: object,
    *,
    spec: dict[str, Any],
    expected_stack: dict[str, Any],
) -> None:
    if raw != build_upstream_bindings(spec, expected_stack=expected_stack):
        raise StagePlanError("upstream authorizations changed after stage planning")
