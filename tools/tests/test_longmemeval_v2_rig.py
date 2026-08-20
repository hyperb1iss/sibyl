from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from benchmarks.longmemeval_v2_official_source import OFFICIAL_HARNESS_COMMIT
from tools.bench import longmemeval_v2_rig as rig

QUESTION_COUNT_PER_DOMAIN = 10


def _stack() -> dict[str, Any]:
    return {
        "sibyl_commit": "a" * 40,
        "sibyl_git_status": "clean",
        "official_source": {
            "commit": OFFICIAL_HARNESS_COMMIT,
            "expected_commit": OFFICIAL_HARNESS_COMMIT,
            "pin_matches": True,
            "git_status": "clean",
            "harness_exists": True,
        },
        "dataset_sha256_by_domain": {
            "web": "sha256:web",
            "enterprise": "sha256:enterprise",
        },
        "reader": {"provider": "local", "model": "reader"},
        "judge": {"provider": "openai", "model": "judge"},
    }


def _arm(
    name: str,
    *,
    mode: str,
    accuracy: float,
    latency: float = 2.0,
    tokens: int = 100,
    exposure: float = 0.5,
    geometry: dict[str, int] | None = None,
) -> dict[str, Any]:
    stack = _stack()
    digest = rig.stack_fingerprint(stack)
    correct_per_domain = round(accuracy * QUESTION_COUNT_PER_DOMAIN)
    exposed_per_domain = round(exposure * QUESTION_COUNT_PER_DOMAIN)
    rows = []
    for domain in sorted(rig.DOMAINS):
        for index in range(QUESTION_COUNT_PER_DOMAIN):
            activity = {
                "activity_events": 1,
                "typed_evidence_applicable": mode != "naive",
                "hybrid_vector_attempts": 1 if mode != "naive" else 0,
                "naive_vector_attempts": 1 if mode == "naive" else 0,
            }
            rows.append(
                {
                    "status": "valid",
                    "domain": domain,
                    "question_id": f"{domain}-{index}",
                    "score_bool": index < correct_per_domain,
                    "latency_seconds": latency,
                    "reader_tokens": tokens,
                    "evidence_exposed": index < exposed_per_domain,
                    "context_status": "complete",
                    "stack_fingerprint": digest,
                    "provider_usage": {
                        "complete": True,
                        "requests": 1,
                        "total_tokens": tokens,
                    },
                    "activity": activity,
                }
            )
    return {
        "name": name,
        "configuration": {"retrieval_mode": mode},
        "geometry": geometry or {"max_context_items": 8, "max_context_total_chars": 60_000},
        "rows": rows,
        "official_lafs": 0.1,
    }


def _paired_pass(
    pass_id: str,
    *,
    left: dict[str, Any],
    right: dict[str, Any],
    preregistration_sha256: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": rig.RUN_PAIR_SCHEMA_VERSION,
        "pass_id": pass_id,
        "seed": f"seed-{pass_id}",
        "stack": _stack(),
        "arms": {"left": left, "right": right},
    }
    if preregistration_sha256 is not None:
        payload["preregistration_sha256"] = preregistration_sha256
    return payload


def test_aa_receipt_sets_three_point_noise_floor() -> None:
    passes = [
        _paired_pass(
            str(index),
            left=_arm("machine", mode="fast", accuracy=0.5),
            right=_arm("machine", mode="fast", accuracy=0.5),
        )
        for index in range(3)
    ]

    receipt = rig.build_aa_receipt(passes)

    assert receipt["status"] == "PASS"
    assert receipt["noise_floor_pp"] == rig.INITIAL_NOISE_FLOOR_PP
    assert receipt["paid_benchmark_allowed"] is True
    assert receipt["score_claim_allowed"] is False


def test_aa_receipt_blocks_when_five_pass_span_keeps_expanding() -> None:
    right_accuracies = [0.6, 0.6, 0.6, 0.7, 0.8]
    passes = [
        _paired_pass(
            str(index),
            left=_arm("machine", mode="fast", accuracy=0.5),
            right=_arm("machine", mode="fast", accuracy=accuracy),
        )
        for index, accuracy in enumerate(right_accuracies)
    ]

    receipt = rig.build_aa_receipt(passes)

    assert receipt["status"] == "RIG_BLOCKED"
    assert receipt["paid_benchmark_allowed"] is False
    assert receipt["noise_floor_pp"] == pytest.approx(30.0)


def test_aa_receipt_rejects_configuration_and_geometry_drift_between_passes() -> None:
    passes = [
        _paired_pass(
            str(index),
            left=_arm(
                "machine",
                mode="accurate" if index == 1 else "fast",
                accuracy=0.5,
                geometry={
                    "max_context_items": 8 + index,
                    "max_context_total_chars": 60_000 + 10_000 * index,
                },
            ),
            right=_arm(
                "machine",
                mode="accurate" if index == 1 else "fast",
                accuracy=0.5,
                geometry={
                    "max_context_items": 8 + index,
                    "max_context_total_chars": 60_000 + 10_000 * index,
                },
            ),
        )
        for index in range(3)
    ]

    with pytest.raises(rig.RigInputError, match="changed between passes"):
        rig.build_aa_receipt(passes)


def test_failed_rows_and_inactive_arms_fail_closed() -> None:
    failed = _paired_pass(
        "failed",
        left=_arm("machine", mode="fast", accuracy=0.5),
        right=_arm("machine", mode="fast", accuracy=0.5),
    )
    failed["arms"]["left"]["rows"][0]["status"] = "failed"
    with pytest.raises(rig.RigInputError, match="failed or incomplete"):
        rig.validate_pass(failed)

    inactive = _paired_pass(
        "inactive",
        left=_arm("machine", mode="fast", accuracy=0.5),
        right=_arm("machine", mode="fast", accuracy=0.5),
    )
    inactive["arms"]["left"]["rows"][0]["activity"]["hybrid_vector_attempts"] = 0
    with pytest.raises(rig.RigInputError, match="hybrid_vector_attempts"):
        rig.validate_pass(inactive)


def _aa_receipt() -> dict[str, Any]:
    return rig.build_aa_receipt(
        [
            _paired_pass(
                str(index),
                left=_arm("machine", mode="fast", accuracy=0.5),
                right=_arm("machine", mode="fast", accuracy=0.5),
            )
            for index in range(3)
        ]
    )


def _race_preregistration_input() -> dict[str, Any]:
    return {
        "created_at": "2026-08-20T00:00:00Z",
        "stack": _stack(),
        "seeds": ["seed-0", "seed-1", "seed-2"],
        "aa_receipt": _aa_receipt(),
        "machine_configuration": {"retrieval_mode": "fast"},
        "naive_configuration": {"retrieval_mode": "naive"},
        "shipping_geometry": {
            "machine": {"max_context_items": 8, "max_context_total_chars": 60_000},
            "naive": {"max_context_items": 8, "max_context_total_chars": 60_000},
        },
        "matched_geometry": {
            "max_context_items": 8,
            "max_context_total_chars": 50_000,
        },
        "decision_rule": rig.RACE_DECISION_RULE,
    }


def _race_preregistration() -> dict[str, Any]:
    return rig.freeze_preregistration(_race_preregistration_input(), kind="race")


def test_anchor_derives_noise_floor_and_contract_from_aa_receipt() -> None:
    aa_receipt = _aa_receipt()
    anchor_pass = _paired_pass(
        "anchor",
        left=_arm("machine", mode="fast", accuracy=0.6),
        right=_arm("machine", mode="fast", accuracy=0.6),
    )

    receipt = rig.build_anchor_receipt(
        anchor_pass,
        arm_side="left",
        aa_receipt=aa_receipt,
    )

    assert receipt["status"] == "PASS"
    assert receipt["aa_receipt_sha256"] == aa_receipt["aa_receipt_sha256"]
    assert receipt["metrics"]["noise_floor_pp"] == aa_receipt["noise_floor_pp"]


def test_anchor_rejects_tampered_or_drifted_aa_lineage() -> None:
    aa_receipt = _aa_receipt()
    tampered = deepcopy(aa_receipt)
    tampered["noise_floor_pp"] = 9.0
    anchor_pass = _paired_pass(
        "anchor",
        left=_arm("machine", mode="fast", accuracy=0.6),
        right=_arm("machine", mode="fast", accuracy=0.6),
    )

    with pytest.raises(rig.RigInputError, match="digest does not bind"):
        rig.build_anchor_receipt(anchor_pass, arm_side="left", aa_receipt=tampered)

    drifted_pass = _paired_pass(
        "anchor-drifted",
        left=_arm("machine", mode="accurate", accuracy=0.6),
        right=_arm("machine", mode="accurate", accuracy=0.6),
    )
    with pytest.raises(rig.RigInputError, match="differs from A/A"):
        rig.build_anchor_receipt(drifted_pass, arm_side="left", aa_receipt=aa_receipt)


def test_preregistration_requires_untampered_aa_lineage() -> None:
    missing = _race_preregistration_input()
    missing.pop("aa_receipt")
    with pytest.raises(rig.RigInputError, match="A/A receipt is missing"):
        rig.freeze_preregistration(missing, kind="race")

    tampered = _race_preregistration_input()
    tampered["aa_receipt"]["noise_floor_pp"] = 9.0
    with pytest.raises(rig.RigInputError, match="digest does not bind"):
        rig.freeze_preregistration(tampered, kind="race")

    manual_threshold = _race_preregistration_input()
    manual_threshold["noise_floor_pp"] = 9.0
    with pytest.raises(rig.RigInputError, match="derived from the bound receipt"):
        rig.freeze_preregistration(manual_threshold, kind="race")


def test_race_is_preregistered_and_keeps_deletion_out_of_v13() -> None:
    preregistration = _race_preregistration()
    digest = preregistration["preregistration_sha256"]
    passes = [
        _paired_pass(
            str(index),
            left=_arm("machine", mode="fast", accuracy=0.8, latency=2.0),
            right=_arm("naive", mode="naive", accuracy=0.6, latency=1.0),
            preregistration_sha256=digest,
        )
        for index in range(3)
    ]
    matched_geometry = preregistration["matched_geometry"]
    matched = _paired_pass(
        "matched",
        left=_arm(
            "machine",
            mode="fast",
            accuracy=0.8,
            geometry=matched_geometry,
        ),
        right=_arm(
            "naive",
            mode="naive",
            accuracy=0.6,
            geometry=matched_geometry,
        ),
        preregistration_sha256=digest,
    )

    receipt = rig.build_race_receipt(preregistration, passes, matched)

    assert receipt["decision"] == "RETAIN_MACHINE_ON_ACCURACY"
    assert receipt["machine_deleted"] is False
    assert receipt["naive_default"] is False


def _render_preregistration() -> dict[str, Any]:
    return rig.freeze_preregistration(
        {
            "created_at": "2026-08-20T00:00:00Z",
            "stack": _stack(),
            "seeds": ["seed-0", "seed-1", "seed-2"],
            "aa_receipt": _aa_receipt(),
            "control_configuration": {"retrieval_mode": "fast", "render": "control"},
            "treatment_configuration": {
                "retrieval_mode": "fast",
                "render": "bounded_bundle",
            },
            "geometry": {"max_context_items": 8, "max_context_total_chars": 60_000},
            "included_levers": ["plain_english_lanes", "action_spine"],
            "replay_survivors": {"plain_english_lanes": True, "action_spine": True},
            "decision_rule": rig.RENDER_DECISION_RULE,
        },
        kind="render",
    )


def test_render_bundle_ships_only_when_every_gate_passes() -> None:
    preregistration = _render_preregistration()
    digest = preregistration["preregistration_sha256"]
    passes = []
    for index in range(3):
        treatment = _arm(
            "render_treatment",
            mode="fast",
            accuracy=0.7,
            latency=2.0,
            tokens=120,
            exposure=0.6,
        )
        treatment["configuration"] = preregistration["treatment_configuration"]
        treatment["lever_activity"] = {"plain_english_lanes": 4, "action_spine": 2}
        control = _arm(
            "render_control",
            mode="fast",
            accuracy=0.5,
            latency=1.0,
            tokens=100,
            exposure=0.5,
        )
        control["configuration"] = preregistration["control_configuration"]
        passes.append(
            _paired_pass(
                str(index),
                left=control,
                right=treatment,
                preregistration_sha256=digest,
            )
        )

    receipt = rig.build_render_receipt(preregistration, passes)

    assert receipt["decision"] == "SHIP_RENDER_BUNDLE"
    assert set(receipt["gates"].values()) == {True}


def test_preregistration_rejects_score_bearing_input() -> None:
    raw = _race_preregistration_input()
    raw["accuracy"] = 0.9

    with pytest.raises(rig.RigInputError, match="score-bearing"):
        rig.freeze_preregistration(raw, kind="race")


def test_cli_writes_failure_receipt_and_returns_nonzero(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"schema_version": "wrong"}), encoding="utf-8")
    output = tmp_path / "receipt.json"

    exit_code = rig.main(
        [
            "anchor",
            "--pass",
            str(invalid),
            "--arm-side",
            "left",
            "--aa-receipt",
            str(invalid),
            "--output",
            str(output),
        ]
    )

    assert exit_code != 0
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["status"] == "FAIL"
    assert receipt["score_claim_allowed"] is False
    assert receipt["paid_benchmark_allowed"] is False
