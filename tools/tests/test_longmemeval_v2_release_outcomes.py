from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_outcomes as outcomes
from benchmarks.longmemeval_v2_release_handoff import ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import StagePlanError


def _run(arm_id: str, pass_id: str) -> dict[str, Any]:
    return {
        "arm_id": arm_id,
        "pass_id": pass_id,
        "seed": 1301,
        "execution": {"run_id": arm_id},
        "manifest": {
            "experiment_id": "sibyl-v1.3-release",
            "experiment_phase": "aa",
        },
    }


def _executed(
    stage: str,
    pass_arms: list[list[str]],
    *,
    mode: str = "initial",
) -> ExecutedStage:
    runs = tuple(
        _run(arm_id, f"pass-{index}")
        for index, arm_ids in enumerate(pass_arms)
        for arm_id in arm_ids
    )
    return ExecutedStage(
        plan={
            "spec": {
                "stage": stage,
                "mode": mode,
                "passes": [
                    {
                        "pass_id": f"pass-{index}",
                        "arms": [{"arm_id": arm_id} for arm_id in arm_ids],
                    }
                    for index, arm_ids in enumerate(pass_arms)
                ],
            },
            "upstream_bindings": {
                "aa_authorization": None,
                "preregistration_authorization": None,
            },
        },
        runs=runs,
        domains=(),
        status_receipt={"status": "EXECUTED"},
        control_artifacts=(),
    )


def _arms(executed: ExecutedStage) -> tuple[outcomes.OfficialArm, ...]:
    return tuple(
        outcomes.OfficialArm(
            arm_id=str(run["arm_id"]),
            authority={},
            authority_artifact={},
            object_artifact={},
            arm_run={"arm_id": run["arm_id"], "pass_id": run["pass_id"]},
        )
        for run in executed.runs
    )


def _pair(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    return {
        "pass_id": left["pass_id"],
        "left": left["arm_id"],
        "right": right["arm_id"],
    }


def test_aa_initial_and_extension_use_exact_three_plus_two_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _executed("aa", [["a", "b"], ["c", "d"], ["e", "f"]])
    monkeypatch.setattr(outcomes.bridge, "build_paired_pass", _pair)
    monkeypatch.setattr(
        outcomes.rig,
        "build_aa_receipt",
        lambda passes: {"status": "PASS", "pass_ids": [item["pass_id"] for item in passes]},
    )

    built = outcomes.build_stage_outcome(
        initial,
        packages_root=Path("/unused"),
        official_arms=_arms(initial),
    )
    assert built.receipt["pass_ids"] == ["pass-0", "pass-1", "pass-2"]

    extension = _executed("aa", [["g", "h"], ["i", "j"]], mode="extension")
    prior = [
        {
            "pass_id": f"prior-{index}",
            "paired_pass_artifact": {"pass_id": f"prior-{index}"},
        }
        for index in range(3)
    ]
    monkeypatch.setattr(outcomes, "_aa_authorization", lambda _executed: {"passes": prior})
    monkeypatch.setattr(
        outcomes,
        "_bound_json",
        lambda binding, **_kwargs: {"pass_id": binding["pass_id"]},
    )
    extended = outcomes.build_stage_outcome(
        extension,
        packages_root=Path("/unused"),
        official_arms=_arms(extension),
    )
    assert extended.receipt["pass_ids"] == [
        "prior-0",
        "prior-1",
        "prior-2",
        "pass-0",
        "pass-1",
    ]


def test_race_packages_three_decision_passes_and_one_matched_sanity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed = _executed(
        "race",
        [["a", "b"], ["c", "d"], ["e", "f"], ["g", "h"]],
    )
    monkeypatch.setattr(outcomes.bridge, "build_paired_pass", _pair)
    monkeypatch.setattr(outcomes, "require_bound_preregistration", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        outcomes.rig,
        "build_race_receipt",
        lambda _prereg, passes, matched: {
            "status": "PASS",
            "decision": [item["pass_id"] for item in passes],
            "matched": matched["pass_id"],
        },
    )

    built = outcomes.build_stage_outcome(
        executed,
        packages_root=Path("/unused"),
        official_arms=_arms(executed),
    )
    assert built.receipt == {
        "status": "PASS",
        "decision": ["pass-0", "pass-1", "pass-2"],
        "matched": "pass-3",
    }
    assert [item["pass_id"] for item in built.paired_passes] == [
        "pass-0",
        "pass-1",
        "pass-2",
        "pass-3",
    ]


def test_race_rejects_missing_matched_sanity_before_rig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed = _executed("race", [["a", "b"], ["c", "d"], ["e", "f"]])
    monkeypatch.setattr(outcomes.bridge, "build_paired_pass", _pair)
    monkeypatch.setattr(outcomes, "require_bound_preregistration", lambda *_args, **_kwargs: {})

    with pytest.raises(StagePlanError, match="three decision passes and one sanity"):
        outcomes.build_stage_outcome(
            executed,
            packages_root=Path("/unused"),
            official_arms=_arms(executed),
        )


def test_anchor_uses_one_arm_and_bound_aa_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    executed = _executed("anchor", [["anchor"]])
    authority = {"status": "PASS", "aa_receipt_sha256": "sha256:aa"}
    monkeypatch.setattr(outcomes, "require_bound_aa_receipt", lambda _executed: authority)
    monkeypatch.setattr(
        outcomes.rig,
        "build_anchor_receipt",
        lambda arm, *, aa_receipt: {
            "status": "PASS",
            "arm": arm["arm_id"],
            "authority": aa_receipt,
        },
    )

    built = outcomes.build_stage_outcome(
        executed,
        packages_root=Path("/unused"),
        official_arms=_arms(executed),
    )
    assert built.receipt["arm"] == "anchor"
    assert built.receipt["authority"] == authority


def test_render_zero_run_not_applicable_reaches_canonical_rig(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed = _executed("render", [])
    monkeypatch.setattr(
        outcomes,
        "require_bound_preregistration",
        lambda *_args, **_kwargs: {"policy": "naive"},
    )
    monkeypatch.setattr(
        outcomes.rig,
        "build_render_receipt",
        lambda prereg, passes: {
            "status": "NOT_APPLICABLE",
            "preregistration": prereg,
            "pass_count": len(passes),
        },
    )

    built = outcomes.build_stage_outcome(
        executed,
        packages_root=Path("/unused"),
        official_arms=(),
    )
    assert built.receipt["status"] == "NOT_APPLICABLE"
    assert built.receipt["pass_count"] == 0


def test_official_arm_claim_requires_exact_executed_arm_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed = _executed("anchor", [["anchor"]])
    monkeypatch.setattr(
        outcomes,
        "require_official_arm",
        lambda *_args, **_kwargs: pytest.fail("arm validation must not start"),
    )

    with pytest.raises(StagePlanError, match="claim set"):
        outcomes.require_official_arms(
            executed,
            packages_root=Path("/unused"),
            expected={},
        )


def test_claimed_official_arm_uses_canonical_immutable_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed = _executed("anchor", [["anchor"]])
    packages_root = tmp_path / "official"
    authority_path = packages_root / "arms" / "anchor" / "authority.json"
    authority_path.parent.mkdir(parents=True)
    object_path = tmp_path / "object.tar.gz"
    object_path.write_bytes(b"object")
    object_binding = outcomes.bind_artifact(
        object_path,
        name="official arm package object",
    )
    authority = {
        "arm_run": {"path": "arm_run.json"},
        "package_object": object_binding,
    }
    authority_path.write_text(json.dumps(authority), encoding="utf-8")
    expected = {
        "publication": authority,
        "authority_artifact": outcomes.bind_artifact(
            authority_path,
            name="official arm authority",
        ),
        "object_artifact": object_binding,
    }
    called: list[dict[str, Any]] = []
    retained = False

    @contextmanager
    def canonical(*_args: Any, **kwargs: Any) -> Any:
        nonlocal retained
        called.append(kwargs)
        retained = True
        try:
            yield authority
            called.append({"final_revalidation": True})
        finally:
            retained = False

    monkeypatch.setattr(
        outcomes.publication,
        "open_claimed_official_arm_package",
        canonical,
    )

    def read_retained_object(*_args: Any) -> bytes:
        assert retained
        return b"object"

    monkeypatch.setattr(outcomes, "_read_frozen_object", read_retained_object)
    arm_run = {
        "experiment_id": "sibyl-v1.3-release",
        "experiment_phase": "aa",
        "pass_id": "pass-0",
        "seed": 1301,
        "execution": {"run_id": "anchor"},
        "stack": {},
    }
    content = json.dumps(arm_run).encode()
    monkeypatch.setattr(
        outcomes.package_archive,
        "require_package_object",
        lambda _content: ({"arm_run.json": content}, {}),
    )
    monkeypatch.setattr(
        outcomes.package_archive,
        "member_binding",
        lambda _name, _content: {"path": "arm_run.json"},
    )
    authority["arm_run"] = {"path": "arm_run.json"}
    monkeypatch.setattr(outcomes.rig, "validate_stack", lambda raw: raw)
    monkeypatch.setattr(outcomes.rig, "stack_fingerprint", lambda _raw: "stack")
    monkeypatch.setattr(outcomes.rig, "validate_arm", lambda raw, **_kwargs: raw)
    executed.plan["stack_identity"] = {}

    result = outcomes.require_official_arm(
        executed,
        executed.runs[0],
        packages_root=packages_root,
        expected=expected,
        packaging_status={"status": "PACKAGING"},
    )

    assert result.arm_id == "anchor"
    assert called == [
        {
            "arm_id": "anchor",
            "packages_root": packages_root,
            "expected": authority,
            "packaging_status": {"status": "PACKAGING"},
        },
        {"final_revalidation": True},
    ]


def test_preregistration_issuance_rejects_score_bearing_template() -> None:
    with pytest.raises(StagePlanError, match="score-bearing"):
        outcomes.issue_preregistration(
            {"accuracy": 0.75},
            kind="race",
            aa_receipt={},
        )
