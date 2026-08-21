from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from benchmarks.longmemeval_v2_official_source import (
    OFFICIAL_HARNESS_COMMIT,
    OFFICIAL_HARNESS_DIFF_URL,
    OFFICIAL_HARNESS_PATH,
    OFFICIAL_HARNESS_PREVIOUS_COMMIT,
    OFFICIAL_REPO_URL,
)
from tools.bench import longmemeval_v2_rig as rig

QUESTION_COUNT_PER_DOMAIN = 10
PINNED_OFFICIAL_SMALL_QUESTION_COUNTS = dict(rig.OFFICIAL_SMALL_QUESTION_COUNTS)
PINNED_OFFICIAL_SMALL_QUESTION_IDS_SHA256 = dict(rig.OFFICIAL_SMALL_QUESTION_IDS_SHA256)


@pytest.fixture(autouse=True)
def _use_synthetic_official_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    question_ids = {
        domain: [f"{domain}-{index}" for index in range(QUESTION_COUNT_PER_DOMAIN)]
        for domain in rig.DOMAINS
    }
    monkeypatch.setattr(
        rig,
        "OFFICIAL_SMALL_QUESTION_COUNTS",
        {domain: len(ids) for domain, ids in question_ids.items()},
    )
    monkeypatch.setattr(
        rig,
        "OFFICIAL_SMALL_QUESTION_IDS_SHA256",
        {domain: rig.canonical_sha256(sorted(ids)) for domain, ids in question_ids.items()},
    )


def _geometry(*, items: int = 8, total_chars: int = 60_000) -> dict[str, int]:
    return {
        "max_context_items": items,
        "max_context_chars_per_item": 12_000,
        "max_context_total_chars": total_chars,
    }


def _stack() -> dict[str, Any]:
    return {
        "sibyl_commit": "a" * 40,
        "sibyl_git_status": "clean",
        "official_source": {
            "url": OFFICIAL_REPO_URL,
            "path": "/official/longmemeval-v2",
            "commit": OFFICIAL_HARNESS_COMMIT,
            "expected_commit": OFFICIAL_HARNESS_COMMIT,
            "pin_matches": True,
            "git_status": "clean",
            "harness_path": OFFICIAL_HARNESS_PATH,
            "harness_exists": True,
            "previous_reviewed_commit": OFFICIAL_HARNESS_PREVIOUS_COMMIT,
            "reviewed_diff_url": OFFICIAL_HARNESS_DIFF_URL,
        },
        "dataset_sha256_by_domain": {
            "web": f"sha256:{'1' * 64}",
            "enterprise": f"sha256:{'2' * 64}",
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
                "activity_events": 2,
                "retrieval_mode": mode,
                "mode": mode,
                "context_pack_requests": 1,
                "typed_evidence_applicable": mode != "naive",
                "hybrid_vector_attempts": 1 if mode != "naive" else 0,
                "hybrid_vector_successes": 1 if mode != "naive" else 0,
                "naive_vector_attempts": 1 if mode == "naive" else 0,
                "naive_vector_successes": 1 if mode == "naive" else 0,
                "planner_query_count": 0,
                "typed_search_statuses": ["complete"] if mode != "naive" else [],
                "lever_activity": {},
            }
            rows.append(
                {
                    "status": "valid",
                    "domain": domain,
                    "question_id": f"{domain}-{index}",
                    "score_bool": index < correct_per_domain,
                    "latency_seconds": latency,
                    "reader_tokens": tokens,
                    "evidence_exposure_eligible": True,
                    "evidence_exposed": index < exposed_per_domain,
                    "context_status": "complete",
                    "stack_fingerprint": digest,
                    "activity": activity,
                }
            )
    return {
        "name": name,
        "substrate": "naive" if mode == "naive" else "machine",
        "configuration": {"retrieval_mode": mode},
        "geometry": geometry or _geometry(),
        "rows": rows,
        "lever_activity": {},
    }


def _seed_for(pass_id: str) -> int:
    return int(rig.canonical_sha256(pass_id)[7:15], 16)


def _seal_arm(
    arm: dict[str, Any],
    *,
    pass_id: str,
    seed: int,
    preregistration_sha256: str,
    experiment_phase: str = "aa",
) -> dict[str, Any]:
    payload = {
        "schema_version": rig.ARM_RUN_SCHEMA_VERSION,
        "experiment_id": "experiment-v1.3",
        "experiment_phase": experiment_phase,
        "pass_id": pass_id,
        "seed": seed,
        "name": arm["name"],
        "substrate": arm["substrate"],
        "preregistration_sha256": preregistration_sha256,
        "workflow": {
            "repository": "hyperb1iss/sibyl",
            "workflow_ref": "hyperb1iss/sibyl/.github/workflows/longmemeval-v2.yml@refs/heads/main",
            "workflow_sha": "a" * 40,
            "run_id": f"run-{pass_id}-{arm['name']}",
            "run_attempt": 1,
        },
        "stack": _stack(),
        "configuration": arm["configuration"],
        "geometry": arm["geometry"],
        "rows": arm["rows"],
        "provider_usage": {
            "complete": True,
            "requests": len(arm["rows"]),
            "total_tokens": sum(row["reader_tokens"] for row in arm["rows"]),
            "actual_cost_usd": 1.0,
            "max_spend_usd_total": 2.0,
            "run_ids_by_domain": {
                "web": f"usage-web-{pass_id}-{arm['name']}",
                "enterprise": f"usage-enterprise-{pass_id}-{arm['name']}",
            },
        },
        "lever_activity": arm["lever_activity"],
        "source_artifacts": {
            "web": {"per_question": f"sha256:{'3' * 64}"},
            "enterprise": {"per_question": f"sha256:{'4' * 64}"},
        },
        "official_question_count_by_domain": dict(rig.OFFICIAL_SMALL_QUESTION_COUNTS),
        "official_question_ids_sha256_by_domain": dict(rig.OFFICIAL_SMALL_QUESTION_IDS_SHA256),
        "question_order_sha256": rig.canonical_sha256(
            [[row["domain"], row["question_id"]] for row in arm["rows"]]
        ),
    }
    payload["arm_run_sha256"] = rig.canonical_sha256(payload)
    return payload


def _paired_pass(
    pass_id: str,
    *,
    left: dict[str, Any],
    right: dict[str, Any],
    seed: int | None = None,
    preregistration_sha256: str | None = None,
    experiment_phase: str = "aa",
) -> dict[str, Any]:
    resolved_seed = seed if seed is not None else _seed_for(pass_id)
    resolved_preregistration = (
        preregistration_sha256
        if preregistration_sha256 is not None
        else ""
        if experiment_phase == "aa"
        else f"sha256:{'5' * 64}"
    )
    sealed_left = _seal_arm(
        left,
        pass_id=pass_id,
        seed=resolved_seed,
        preregistration_sha256=resolved_preregistration,
        experiment_phase=experiment_phase,
    )
    sealed_right = _seal_arm(
        right,
        pass_id=pass_id,
        seed=resolved_seed,
        preregistration_sha256=resolved_preregistration,
        experiment_phase=experiment_phase,
    )
    sealed_left["workflow"]["run_id"] = f"run-{pass_id}-left"
    sealed_right["workflow"]["run_id"] = f"run-{pass_id}-right"
    _reseal_arm(sealed_left)
    _reseal_arm(sealed_right)
    payload = {
        "schema_version": rig.RUN_PAIR_SCHEMA_VERSION,
        "experiment_id": "experiment-v1.3",
        "experiment_phase": experiment_phase,
        "pass_id": pass_id,
        "seed": resolved_seed,
        "stack": _stack(),
        "preregistration_sha256": resolved_preregistration,
        "arms": {"left": sealed_left, "right": sealed_right},
    }
    payload["paired_pass_sha256"] = rig.canonical_sha256(payload)
    return payload


def _reseal_arm(arm: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in arm.items() if key != "arm_run_sha256"}
    arm["arm_run_sha256"] = rig.canonical_sha256(unsigned)


def _reseal_pass(paired_pass: dict[str, Any]) -> None:
    unsigned = {key: value for key, value in paired_pass.items() if key != "paired_pass_sha256"}
    paired_pass["paired_pass_sha256"] = rig.canonical_sha256(unsigned)


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


def test_rig_pins_the_full_official_small_corpus() -> None:
    assert PINNED_OFFICIAL_SMALL_QUESTION_COUNTS == {
        "enterprise": 211,
        "web": 240,
    }
    assert PINNED_OFFICIAL_SMALL_QUESTION_IDS_SHA256 == {
        "enterprise": "sha256:984368308cc83c63401bf5e3d53d33a635b2768d434215a52cc9d5effee66c19",
        "web": "sha256:bb4183ef7f554ef278b158b910c6e8c1de6d14572dae8d29789dded57a143eeb",
    }


def test_aa_machine_contract_is_independent_of_display_role() -> None:
    passes = [
        _paired_pass(
            str(index),
            left=_arm("display_control", mode="fast", accuracy=0.5),
            right=_arm("display_treatment", mode="fast", accuracy=0.5),
        )
        for index in range(3)
    ]

    receipt = rig.build_aa_receipt(passes)

    assert receipt["status"] == "PASS"
    assert receipt["arm_contract"] == {
        "substrate": "machine",
        "configuration": {"retrieval_mode": "fast"},
        "geometry": _geometry(),
    }


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


def test_aa_receipt_requires_two_more_when_initial_span_exceeds_target() -> None:
    passes = [
        _paired_pass(
            str(index),
            left=_arm("machine", mode="fast", accuracy=0.5),
            right=_arm("machine", mode="fast", accuracy=0.6),
        )
        for index in range(3)
    ]

    receipt = rig.build_aa_receipt(passes)

    assert receipt["status"] == "NEEDS_TWO_MORE"
    assert receipt["paid_benchmark_allowed"] is False


def test_aa_receipt_accepts_stable_five_pass_span() -> None:
    passes = [
        _paired_pass(
            str(index),
            left=_arm("machine", mode="fast", accuracy=0.5),
            right=_arm("machine", mode="fast", accuracy=accuracy),
        )
        for index, accuracy in enumerate([0.6, 0.5, 0.5, 0.5, 0.5])
    ]

    receipt = rig.build_aa_receipt(passes)

    assert receipt["status"] == "PASS"
    assert receipt["pass_count"] == rig.EXTENDED_AA_PASS_COUNT
    assert receipt["noise_floor_pp"] == pytest.approx(10.0)


def test_aa_receipt_rejects_configuration_and_geometry_drift_between_passes() -> None:
    passes = [
        _paired_pass(
            str(index),
            left=_arm(
                "machine",
                mode="accurate" if index == 1 else "fast",
                accuracy=0.5,
                geometry=_geometry(
                    items=8 + index,
                    total_chars=60_000 + 10_000 * index,
                ),
            ),
            right=_arm(
                "machine",
                mode="accurate" if index == 1 else "fast",
                accuracy=0.5,
                geometry=_geometry(
                    items=8 + index,
                    total_chars=60_000 + 10_000 * index,
                ),
            ),
        )
        for index in range(3)
    ]

    with pytest.raises(rig.RigInputError, match="changed between passes"):
        rig.build_aa_receipt(passes)


def test_aa_command_rejects_non_aa_phase() -> None:
    passes = [
        _paired_pass(
            f"wrong-aa-{index}",
            left=_arm("left", mode="fast", accuracy=0.5),
            right=_arm("right", mode="fast", accuracy=0.5),
            experiment_phase="race",
        )
        for index in range(rig.INITIAL_AA_PASS_COUNT)
    ]

    with pytest.raises(rig.RigInputError, match="A/A requires aa-phase"):
        rig.build_aa_receipt(passes)


def test_failed_rows_and_inactive_arms_fail_closed() -> None:
    failed_left = _arm("machine", mode="fast", accuracy=0.5)
    failed_left["rows"][0]["status"] = "failed"
    failed = _paired_pass(
        "failed",
        left=failed_left,
        right=_arm("machine", mode="fast", accuracy=0.5),
    )
    with pytest.raises(rig.RigInputError, match="failed or incomplete"):
        rig.validate_pass(failed)

    inactive_left = _arm("machine", mode="fast", accuracy=0.5)
    inactive_left["rows"][0]["activity"]["hybrid_vector_attempts"] = 0
    inactive = _paired_pass(
        "inactive",
        left=inactive_left,
        right=_arm("machine", mode="fast", accuracy=0.5),
    )
    with pytest.raises(rig.RigInputError, match="hybrid_vector_attempts"):
        rig.validate_pass(inactive)


def test_impossible_activity_counters_fail_closed() -> None:
    excess_success = _arm("machine", mode="fast", accuracy=0.5)
    excess_success["rows"][0]["activity"]["hybrid_vector_successes"] = 2
    success_pass = _paired_pass(
        "excess-success",
        left=excess_success,
        right=_arm("machine", mode="fast", accuracy=0.5),
    )
    with pytest.raises(rig.RigInputError, match="successes exceeds"):
        rig.validate_pass(success_pass)

    false_total = _arm("machine", mode="fast", accuracy=0.5)
    false_total["rows"][0]["activity"]["activity_events"] = 999
    total_pass = _paired_pass(
        "false-total",
        left=false_total,
        right=_arm("machine", mode="fast", accuracy=0.5),
    )
    with pytest.raises(rig.RigInputError, match="does not match its explicit counters"):
        rig.validate_pass(total_pass)


def test_evidence_exposure_uses_only_eligible_rows() -> None:
    arm = _arm("machine", mode="fast", accuracy=0.5)
    for row in arm["rows"]:
        row["evidence_exposure_eligible"] = row["question_id"].endswith("-0")
        row["evidence_exposed"] = True if row["evidence_exposure_eligible"] else None
    sealed = _seal_arm(
        arm,
        pass_id="eligible",  # noqa: S106
        seed=90,
        preregistration_sha256="",
    )
    validated = rig.validate_arm(
        sealed,
        stack_digest=rig.stack_fingerprint(_stack()),
        side="eligible",
    )

    summary = rig.arm_summary(validated)

    assert summary["evidence_exposure"] == 1.0
    assert summary["evidence_exposure_eligible_count"] == len(rig.DOMAINS)
    assert summary["evidence_exposure_eligible_count_by_domain"] == {
        "enterprise": 1,
        "web": 1,
    }


def test_arm_requires_exposure_eligible_rows_in_each_domain() -> None:
    arm = _arm("machine", mode="fast", accuracy=0.5)
    for row in arm["rows"]:
        if row["domain"] == "web":
            row["evidence_exposure_eligible"] = False
            row["evidence_exposed"] = None
    sealed = _seal_arm(
        arm,
        pass_id="ineligible",  # noqa: S106
        seed=91,
        preregistration_sha256="",
    )

    with pytest.raises(rig.RigInputError, match="no evidence-exposure-eligible web"):
        rig.validate_arm(
            sealed,
            stack_digest=rig.stack_fingerprint(_stack()),
            side="ineligible",
        )


def test_paired_pass_rejects_question_order_and_workflow_drift() -> None:
    paired = _paired_pass(
        "order",
        left=_arm("left", mode="fast", accuracy=0.5),
        right=_arm("right", mode="fast", accuracy=0.5),
    )
    right = paired["arms"]["right"]
    right["rows"][0], right["rows"][1] = right["rows"][1], right["rows"][0]
    right["question_order_sha256"] = rig.canonical_sha256(
        [[row["domain"], row["question_id"]] for row in right["rows"]]
    )
    _reseal_arm(right)
    _reseal_pass(paired)

    with pytest.raises(rig.RigInputError, match="same question order"):
        rig.validate_pass(paired)

    workflow_drift = _paired_pass(
        "workflow",
        left=_arm("left", mode="fast", accuracy=0.5),
        right=_arm("right", mode="fast", accuracy=0.5),
    )
    workflow_drift["arms"]["right"]["workflow"]["workflow_ref"] = "other-workflow"
    _reseal_arm(workflow_drift["arms"]["right"])
    _reseal_pass(workflow_drift)
    with pytest.raises(rig.RigInputError, match="workflows differ for workflow_ref"):
        rig.validate_pass(workflow_drift)


def test_arm_rejects_resealed_official_corpus_truncation() -> None:
    arm = _seal_arm(
        _arm("machine", mode="fast", accuracy=0.5),
        pass_id="truncated",  # noqa: S106
        seed=92,
        preregistration_sha256="",
    )
    arm["rows"] = [
        next(row for row in arm["rows"] if row["domain"] == domain)
        for domain in sorted(rig.DOMAINS)
    ]
    arm["question_order_sha256"] = rig.canonical_sha256(
        [[row["domain"], row["question_id"]] for row in arm["rows"]]
    )
    _reseal_arm(arm)

    with pytest.raises(rig.RigInputError, match="official question count differs"):
        rig.validate_arm(
            arm,
            stack_digest=rig.stack_fingerprint(_stack()),
            side="truncated",
        )


@pytest.mark.parametrize("field", ["domain", "question_id"])
def test_arm_rejects_noncanonical_corpus_identity(field: str) -> None:
    arm = _seal_arm(
        _arm("machine", mode="fast", accuracy=0.5),
        pass_id="noncanonical",  # noqa: S106
        seed=93,
        preregistration_sha256="",
    )
    arm["rows"][0][field] = f" {arm['rows'][0][field]} "
    arm["question_order_sha256"] = rig.canonical_sha256(
        [[row["domain"], row["question_id"]] for row in arm["rows"]]
    )
    _reseal_arm(arm)

    with pytest.raises(rig.RigInputError, match=rf"{field} must use its canonical value"):
        rig.validate_arm(
            arm,
            stack_digest=rig.stack_fingerprint(_stack()),
            side="noncanonical",
        )


def test_arm_provider_usage_fails_closed_on_overspend() -> None:
    paired = _paired_pass(
        "overspend",
        left=_arm("left", mode="fast", accuracy=0.5),
        right=_arm("right", mode="fast", accuracy=0.5),
    )
    paired["arms"]["left"]["provider_usage"]["actual_cost_usd"] = 3.0
    _reseal_arm(paired["arms"]["left"])
    _reseal_pass(paired)

    with pytest.raises(rig.RigInputError, match="exceeds its approved spend"):
        rig.validate_pass(paired)


def _aa_receipt(
    *,
    name: str = "machine",
    configuration: dict[str, Any] | None = None,
    geometry: dict[str, int] | None = None,
    right_accuracies: list[float] | None = None,
) -> dict[str, Any]:
    resolved_configuration = configuration or {"retrieval_mode": "fast"}
    accuracies = right_accuracies or [0.5, 0.5, 0.5]

    def arm(accuracy: float) -> dict[str, Any]:
        result = _arm(
            name,
            mode=str(resolved_configuration["retrieval_mode"]),
            accuracy=accuracy,
            geometry=geometry,
        )
        result["configuration"] = deepcopy(resolved_configuration)
        return result

    return rig.build_aa_receipt(
        [
            _paired_pass(
                str(index),
                left=arm(0.5),
                right=arm(accuracy),
            )
            for index, accuracy in enumerate(accuracies)
        ]
    )


def _race_preregistration_input() -> dict[str, Any]:
    return {
        "created_at": "2026-08-20T00:00:00Z",
        "stack": _stack(),
        "seeds": [100, 101, 102],
        "aa_receipt": _aa_receipt(),
        "machine_configuration": {"retrieval_mode": "fast"},
        "naive_configuration": {"retrieval_mode": "naive"},
        "shipping_geometry": {
            "machine": _geometry(),
            "naive": _geometry(),
        },
        "matched_geometry": _geometry(total_chars=50_000),
        "decision_rule": rig.RACE_DECISION_RULE,
    }


def _race_preregistration() -> dict[str, Any]:
    return rig.freeze_preregistration(_race_preregistration_input(), kind="race")


def test_anchor_derives_noise_floor_and_contract_from_aa_receipt() -> None:
    aa_receipt = _aa_receipt()
    anchor_arm = _seal_arm(
        _arm("machine", mode="fast", accuracy=0.6),
        pass_id="anchor",  # noqa: S106
        seed=200,
        preregistration_sha256="",
        experiment_phase="anchor",
    )

    receipt = rig.build_anchor_receipt(
        anchor_arm,
        aa_receipt=aa_receipt,
    )

    assert receipt["status"] == "PASS"
    assert receipt["aa_receipt_sha256"] == aa_receipt["aa_receipt_sha256"]
    assert receipt["aa_pass_count"] == aa_receipt["pass_count"]
    assert receipt["aa_observed_span_pp"] == aa_receipt["observed_span_pp"]
    assert receipt["metrics"]["noise_floor_pp"] == aa_receipt["noise_floor_pp"]
    assert receipt["anchor_publishable"] is True
    assert receipt["comparative_claim_allowed"] is False
    assert "official_lafs" not in receipt["metrics"]


def test_anchor_rejects_tampered_or_drifted_aa_lineage() -> None:
    aa_receipt = _aa_receipt()
    tampered = deepcopy(aa_receipt)
    tampered["noise_floor_pp"] = 9.0
    anchor_arm = _seal_arm(
        _arm("machine", mode="fast", accuracy=0.6),
        pass_id="anchor",  # noqa: S106
        seed=200,
        preregistration_sha256="",
        experiment_phase="anchor",
    )

    with pytest.raises(rig.RigInputError, match="digest does not bind"):
        rig.build_anchor_receipt(anchor_arm, aa_receipt=tampered)

    drifted_arm = _seal_arm(
        _arm("machine", mode="accurate", accuracy=0.6),
        pass_id="anchor-drifted",  # noqa: S106
        seed=201,
        preregistration_sha256="",
        experiment_phase="anchor",
    )
    with pytest.raises(rig.RigInputError, match="differs from A/A"):
        rig.build_anchor_receipt(drifted_arm, aa_receipt=aa_receipt)

    reused_seed_arm = _seal_arm(
        _arm("machine", mode="fast", accuracy=0.6),
        pass_id="anchor-reused-seed",  # noqa: S106
        seed=int(aa_receipt["passes"][0]["seed"]),
        preregistration_sha256="",
        experiment_phase="anchor",
    )
    with pytest.raises(rig.RigInputError, match="reuses an A/A calibration seed"):
        rig.build_anchor_receipt(
            reused_seed_arm,
            aa_receipt=aa_receipt,
        )


def test_anchor_command_rejects_non_anchor_phase() -> None:
    wrong_phase = _seal_arm(
        _arm("machine", mode="fast", accuracy=0.6),
        pass_id="wrong-anchor",  # noqa: S106
        seed=200,
        preregistration_sha256="",
    )

    with pytest.raises(rig.RigInputError, match="anchor requires an anchor-phase"):
        rig.build_anchor_receipt(wrong_phase, aa_receipt=_aa_receipt())


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


def test_preregistration_binds_aa_to_named_control_and_fresh_seeds() -> None:
    drifted = _race_preregistration_input()
    drifted["machine_configuration"] = {"retrieval_mode": "accurate"}
    with pytest.raises(rig.RigInputError, match="does not match the preregistered race control"):
        rig.freeze_preregistration(drifted, kind="race")

    reused_seed = _race_preregistration_input()
    reused_seed["seeds"][0] = reused_seed["aa_receipt"]["passes"][0]["seed"]
    with pytest.raises(rig.RigInputError, match="must not reuse A/A"):
        rig.freeze_preregistration(reused_seed, kind="race")


def test_preregistration_keeps_three_decision_passes_after_stable_five_pass_aa() -> None:
    raw = _race_preregistration_input()
    raw["aa_receipt"] = _aa_receipt(right_accuracies=[0.6, 0.5, 0.5, 0.5, 0.5])

    preregistration = rig.freeze_preregistration(raw, kind="race")

    assert preregistration["aa_receipt"]["pass_count"] == rig.EXTENDED_AA_PASS_COUNT
    assert len(preregistration["seeds"]) == rig.PAIRED_PASS_COUNT


def test_aa_receipt_rejects_unknown_score_fields_even_with_a_fresh_digest() -> None:
    receipt = _aa_receipt()
    receipt["unexpected_score"] = 0.9
    unsigned = {key: value for key, value in receipt.items() if key != "aa_receipt_sha256"}
    receipt["aa_receipt_sha256"] = rig.canonical_sha256(unsigned)

    with pytest.raises(rig.RigInputError, match=r"unknown=\['unexpected_score'\]"):
        rig.validate_aa_receipt(receipt)


def test_race_is_preregistered_and_keeps_deletion_out_of_v13() -> None:
    preregistration = _race_preregistration()
    digest = preregistration["preregistration_sha256"]
    passes = [
        _paired_pass(
            str(index),
            left=_arm("machine", mode="fast", accuracy=0.8, latency=2.0),
            right=_arm("naive", mode="naive", accuracy=0.6, latency=1.0),
            seed=preregistration["seeds"][index],
            preregistration_sha256=digest,
            experiment_phase="race",
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
        experiment_phase="race",
    )

    receipt = rig.build_race_receipt(preregistration, passes, matched)

    assert receipt["decision"] == "RETAIN_MACHINE_ON_ACCURACY"
    assert receipt["machine_deleted"] is False
    assert receipt["naive_default"] is False
    assert receipt["selected_render_substrate"] == "machine"
    assert rig.validate_race_receipt(receipt) == receipt


def test_race_receipt_rejects_recomputed_lineage_drift() -> None:
    receipt = _race_receipt()
    receipt["matched_character_control"]["seed"] = receipt["passes"][0]["seed"]
    unsigned = {key: value for key, value in receipt.items() if key != "race_receipt_sha256"}
    receipt["race_receipt_sha256"] = rig.canonical_sha256(unsigned)

    with pytest.raises(rig.RigInputError, match="lineage must be fresh"):
        rig.validate_race_receipt(receipt)


def test_race_command_rejects_non_race_phase() -> None:
    preregistration = _race_preregistration()
    digest = preregistration["preregistration_sha256"]
    wrong_phase = [
        _paired_pass(
            f"wrong-race-{index}",
            left=_arm("machine", mode="fast", accuracy=0.8),
            right=_arm("naive", mode="naive", accuracy=0.6),
            seed=preregistration["seeds"][index],
            preregistration_sha256=digest,
            experiment_phase="render",
        )
        for index in range(rig.PAIRED_PASS_COUNT)
    ]

    with pytest.raises(rig.RigInputError, match="race requires race-phase"):
        rig.build_race_receipt(preregistration, wrong_phase, {})


def _race_receipt(
    *,
    machine_accuracy: float = 0.8,
    naive_accuracy: float = 0.6,
) -> dict[str, Any]:
    preregistration = _race_preregistration()
    digest = preregistration["preregistration_sha256"]
    passes = [
        _paired_pass(
            f"race-{index}",
            left=_arm("machine", mode="fast", accuracy=machine_accuracy, latency=2.0),
            right=_arm("naive", mode="naive", accuracy=naive_accuracy, latency=1.0),
            seed=preregistration["seeds"][index],
            preregistration_sha256=digest,
            experiment_phase="race",
        )
        for index in range(3)
    ]
    matched = _paired_pass(
        "race-matched",
        left=_arm(
            "machine",
            mode="fast",
            accuracy=machine_accuracy,
            geometry=preregistration["matched_geometry"],
        ),
        right=_arm(
            "naive",
            mode="naive",
            accuracy=naive_accuracy,
            geometry=preregistration["matched_geometry"],
        ),
        seed=103,
        preregistration_sha256=digest,
        experiment_phase="race",
    )
    return rig.build_race_receipt(preregistration, passes, matched)


def _render_preregistration_input() -> dict[str, Any]:
    control_configuration = {"retrieval_mode": "fast"}
    control_geometry = _geometry()
    return {
        "created_at": "2026-08-20T00:00:00Z",
        "stack": _stack(),
        "seeds": [300, 301, 302],
        "aa_receipt": _aa_receipt(),
        "race_receipt": _race_receipt(),
        "control_configuration": control_configuration,
        "treatment_configuration": {
            "retrieval_mode": "fast",
            "render": "bounded_bundle",
        },
        "control_geometry": control_geometry,
        "treatment_geometry": _geometry(total_chars=400_000),
        "included_levers": ["plain_english_lanes", "action_spine"],
        "replay_survivors": {"plain_english_lanes": True, "action_spine": True},
        "decision_rule": rig.RENDER_DECISION_RULE,
    }


def _render_preregistration() -> dict[str, Any]:
    return rig.freeze_preregistration(_render_preregistration_input(), kind="render")


@pytest.mark.parametrize("drift", ["configuration", "geometry"])
def test_render_preregistration_rejects_aa_control_drift(drift: str) -> None:
    raw = _render_preregistration_input()
    if drift == "configuration":
        raw["control_configuration"] = {"retrieval_mode": "fast", "render": "other"}
    else:
        raw["control_geometry"] = _geometry(items=9)

    with pytest.raises(rig.RigInputError, match="does not match the preregistered render control"):
        rig.freeze_preregistration(raw, kind="render")


def test_render_preregistration_rejects_unapproved_geometry_delta() -> None:
    raw = _render_preregistration_input()
    raw["treatment_geometry"] = _geometry(items=9, total_chars=400_000)

    with pytest.raises(rig.RigInputError, match="may differ only"):
        rig.freeze_preregistration(raw, kind="render")


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
            geometry=preregistration["treatment_geometry"],
        )
        treatment["configuration"] = preregistration["treatment_configuration"]
        treatment["lever_activity"] = {"plain_english_lanes": 4, "action_spine": 2}
        treatment["rows"][0]["activity"]["lever_activity"] = {
            "plain_english_lanes": 4,
            "action_spine": 2,
        }
        treatment["rows"][0]["activity"]["activity_events"] += 6
        control = _arm(
            "render_control",
            mode="fast",
            accuracy=0.5,
            latency=1.0,
            tokens=100,
            exposure=0.5,
            geometry=preregistration["control_geometry"],
        )
        control["configuration"] = preregistration["control_configuration"]
        passes.append(
            _paired_pass(
                str(index),
                left=control,
                right=treatment,
                seed=preregistration["seeds"][index],
                preregistration_sha256=digest,
                experiment_phase="render",
            )
        )

    receipt = rig.build_render_receipt(preregistration, passes)

    assert receipt["decision"] == "SHIP_RENDER_BUNDLE"
    assert set(receipt["gates"].values()) == {True}
    assert receipt["race_receipt_sha256"] == preregistration["race_receipt_sha256"]


def test_render_command_rejects_non_render_phase() -> None:
    preregistration = _render_preregistration()
    wrong_phase = _paired_pass(
        "wrong-render",
        left=_arm("render_control", mode="fast", accuracy=0.5),
        right=_arm("render_treatment", mode="fast", accuracy=0.7),
        seed=preregistration["seeds"][0],
        preregistration_sha256=preregistration["preregistration_sha256"],
        experiment_phase="race",
    )

    with pytest.raises(rig.RigInputError, match="render requires render-phase"):
        rig.build_render_receipt(preregistration, [wrong_phase])


def test_render_is_not_applicable_when_race_selects_naive() -> None:
    raw = _render_preregistration_input()
    raw["race_receipt"] = _race_receipt(machine_accuracy=0.5, naive_accuracy=0.5)
    preregistration = rig.freeze_preregistration(raw, kind="render")

    receipt = rig.build_render_receipt(preregistration, [])

    assert receipt["status"] == "NOT_APPLICABLE"
    assert receipt["decision"] == "NOT_APPLICABLE"
    assert receipt["selected_render_substrate"] == "naive"
    assert receipt["passes"] == []

    with pytest.raises(rig.RigInputError, match="passes are forbidden"):
        rig.build_render_receipt(preregistration, [{}])


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
            "--arm-run",
            str(invalid),
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
