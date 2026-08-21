from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from benchmarks import longmemeval_v2_release_authorization as authorization
from benchmarks import (
    longmemeval_v2_release_authorization_package as authorization_package,
)
from benchmarks import longmemeval_v2_release_contract as contract
from benchmarks import longmemeval_v2_release_inputs as inputs
from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_memory as release_memory
from benchmarks import longmemeval_v2_release_plan as release_plan
from tools.tests.longmemeval_v2_release_support import (
    aa_extension_spec,
    aa_spec,
    anchor_spec,
    arm_contract,
    manifest,
    race_spec,
    render_spec,
)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("reader_base_url", "https://example.test/v1"),
        ("reader_model", "stale/reader"),
        ("reader_api_key_env", "OTHER_READER_KEY"),
        ("reader_max_concurrent_requests", 15),
        ("reader_retry_attempts", 5),
        ("evaluator_model", "gpt-5.1"),
        ("evaluator_api_key_env", "OTHER_EVALUATOR_KEY"),
        ("evidence_composition_mode", "reserved_support"),
        ("retrieval_max_planned_queries", 4),
        ("max_context_chars_per_item", 17_999),
        ("typed_stream_limit", 7),
        ("note_distillation_model", "gpt-5.4-mini"),
        ("api_retry_attempts", 4),
        ("prompt_build_max_workers", 2),
    ],
)
def test_stage_spec_rejects_runtime_pin_drift(key: str, value: object) -> None:
    spec = aa_spec()
    spec["runtime"][key] = value

    with pytest.raises(contract.StagePlanError, match="release pin"):
        contract.require_stage_spec(spec)


@pytest.mark.parametrize(
    ("api_url", "allow_localhost"),
    [
        ("https://api.example.test/api", True),
        ("http://127.0.0.1:3334/api?token=secret", True),
        ("http://127.0.0.1:3334/api", False),
    ],
)
def test_stage_spec_rejects_nonlocal_runtime(
    api_url: str,
    allow_localhost: bool,
) -> None:
    spec = aa_spec()
    spec["runtime"]["api_url"] = api_url
    spec["runtime"]["allow_localhost"] = allow_localhost

    with pytest.raises(contract.StagePlanError, match="local"):
        contract.require_stage_spec(spec)


def test_stage_spec_rejects_role_cap_retrieval_and_geometry_drift() -> None:
    cap_drift = aa_spec()
    cap_drift["passes"][0]["arms"][1]["manifest"]["max_spend_usd"] = 3.5
    with pytest.raises(contract.StagePlanError, match="spend cap"):
        contract.require_stage_spec(cap_drift)

    retrieval_drift = aa_spec()
    for arm in retrieval_drift["passes"][0]["arms"]:
        arm["manifest"]["retrieval_mode"] = "accurate"
        arm["manifest"]["configuration"]["retrieval_mode"] = "accurate"
    with pytest.raises(contract.StagePlanError, match="retrieval mode"):
        contract.require_stage_spec(retrieval_drift)

    geometry_drift = aa_spec()
    for arm in geometry_drift["passes"][0]["arms"]:
        arm["manifest"]["geometry"]["max_context_items"] = 7
    with pytest.raises(contract.StagePlanError, match="geometry"):
        contract.require_stage_spec(geometry_drift)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("experiment_id", "../escape"),
        ("pass_id", "../../escape"),
        ("arm_id", str(Path(Path.cwd().anchor) / "escape")),
    ],
)
def test_stage_spec_rejects_path_unsafe_identifiers(field: str, value: str) -> None:
    spec = aa_spec()
    if field == "experiment_id":
        spec[field] = value
    elif field == "pass_id":
        spec["passes"][0][field] = value
    else:
        spec["passes"][0]["arms"][0][field] = value

    with pytest.raises(contract.StagePlanError, match="path-safe"):
        contract.require_stage_spec(spec)


def test_stage_spec_rejects_cross_pass_configuration_drift() -> None:
    spec = aa_spec()
    for arm in spec["passes"][1]["arms"]:
        arm["manifest"]["configuration"]["unexpected_variant"] = True

    with pytest.raises(contract.StagePlanError, match="changed across passes"):
        contract.require_stage_spec(spec)


def test_race_matched_control_rejects_aa_seed_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _contracts = race_spec()
    matched = spec["passes"][-1]
    matched["seed"] = 1301
    for arm in matched["arms"]:
        arm["manifest"]["pass_seed"] = 1301
    preregistration_path = tmp_path / "race-preregistration.json"
    preregistration_path.write_text("{}\n", encoding="utf-8")
    spec["upstream"]["preregistration_authorization"] = str(preregistration_path)
    preregistration = {
        "seeds": [1501, 1502, 1503],
        "aa_passes": [{"seed": seed} for seed in (1301, 1302, 1303)],
        "preregistration_sha256": f"sha256:{'b' * 64}",
    }
    monkeypatch.setattr(
        contract,
        "require_preregistration_authorization",
        lambda _raw, *, kind: preregistration,
    )

    with pytest.raises(contract.StagePlanError, match="post-A/A seed"):
        contract.require_stage_spec(spec)


def test_stage_spec_rejects_memory_lineage_and_treatment_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = aa_spec()
    spec["passes"][0]["arms"][0]["memory_source"] = "baseline"
    with pytest.raises(contract.StagePlanError, match="memory lineage"):
        contract.require_stage_spec(spec)

    render_identity = "render-1"
    treatment = manifest(
        phase="render",
        pass_id=render_identity,
        seed=1601,
        role="render_treatment",
        preregistration="c" * 64,
    )
    treatment["render_group_lanes"] = False
    treatment["configuration"]["render_group_lanes"] = False
    preregistration_path = tmp_path / "render-preregistration.json"
    preregistration_path.write_text("{}", encoding="utf-8")
    render_stage, _contracts = render_spec()
    render_stage["upstream"]["preregistration_authorization"] = str(preregistration_path)
    render_stage["passes"][0]["arms"][1]["manifest"] = treatment
    preregistration = {
        "seeds": [1601, 1602, 1603],
        "preregistration_sha256": f"sha256:{'c' * 64}",
        "policy": {"render_applicable": True},
    }
    monkeypatch.setattr(
        contract,
        "require_preregistration_authorization",
        lambda _raw, *, kind: preregistration,
    )
    with pytest.raises(contract.StagePlanError, match="treatment bundle"):
        contract.require_stage_spec(render_stage)


def test_aa_authorization_packaging_projects_no_score_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt_path = tmp_path / "aa-receipt.json"
    receipt_path.write_text('{"score_bool":true}\n', encoding="utf-8")
    prior_passes = [
        {
            "pass_id": f"aa-{index}",
            "seed": 1300 + index,
            "paired_pass_sha256": f"sha256:{str(index) * 64}",
        }
        for index in range(1, 4)
    ]
    aa_contract = arm_contract(aa_spec()["passes"][0]["arms"][0])
    validated = {
        "status": "PASS",
        "aa_receipt_sha256": f"sha256:{'a' * 64}",
        "stack": {"sealed": "stack"},
        "arm_contract": aa_contract,
        "passes": [
            {**item, "accuracy_delta_pp": 1.0, "left": {}, "right": {}} for item in prior_passes
        ],
    }
    real_require = authorization.require_aa_authorization
    monkeypatch.setattr(authorization.rig, "validate_aa_receipt", lambda _raw: validated)
    monkeypatch.setattr(authorization.rig, "validate_stack", lambda raw: raw)
    paired_pass_paths = []
    for item in prior_passes:
        path = tmp_path / f"{item['pass_id']}.json"
        path.write_text("{}\n", encoding="utf-8")
        paired_pass_paths.append(path)
    paired_passes = iter(prior_passes)
    monkeypatch.setattr(
        authorization.rig,
        "validate_pass",
        lambda _raw: next(paired_passes),
    )
    monkeypatch.setattr(
        authorization,
        "require_aa_authorization",
        lambda raw: raw,
    )

    projected = authorization_package.package_aa_authorization(
        receipt_path,
        paired_pass_paths=paired_pass_paths,
    )

    assert projected["source_receipt"]["sha256"]
    assert [
        {key: item[key] for key in authorization.PASS_AUTHORIZATION_KEYS}
        for item in projected["passes"]
    ] == prior_passes
    assert all(item["paired_pass_artifact"]["sha256"] for item in projected["passes"])
    with pytest.raises(contract.StagePlanError, match="score-bearing"):
        authorization.reject_score_bearing_keys(validated, name="raw A/A receipt")
    authorization.reject_score_bearing_keys(projected, name="A/A authorization")
    paired_pass_paths[0].write_text("[]\n", encoding="utf-8")
    with pytest.raises(contract.StagePlanError, match="digest changed"):
        real_require(projected)
    paired_pass_paths[0].write_text("{}\n", encoding="utf-8")
    paired_passes = iter(prior_passes)

    def mutate_after_validation(_raw: object) -> dict[str, str | int]:
        paired_pass = next(paired_passes)
        if paired_pass["pass_id"] == prior_passes[0]["pass_id"]:
            paired_pass_paths[0].write_text("[]\n", encoding="utf-8")
        return paired_pass

    monkeypatch.setattr(authorization.rig, "validate_pass", mutate_after_validation)
    with pytest.raises(contract.StagePlanError, match="changed during validation"):
        authorization_package.package_aa_authorization(
            receipt_path,
            paired_pass_paths=paired_pass_paths,
        )


def test_preregistration_authorization_packaging_projects_no_score_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration_path = tmp_path / "race-preregistration.json"
    preregistration_path.write_text('{"noise_floor_pp":1.5}\n', encoding="utf-8")
    prior_passes = [
        {
            "pass_id": f"aa-{index}",
            "seed": 1300 + index,
            "paired_pass_sha256": f"sha256:{str(index) * 64}",
        }
        for index in range(1, 4)
    ]
    _spec, contracts = race_spec()
    validated = {
        "preregistration_sha256": f"sha256:{'b' * 64}",
        "stack": {"sealed": "stack"},
        "seeds": [1501, 1502, 1503],
        "aa_receipt_sha256": f"sha256:{'a' * 64}",
        "aa_receipt": {
            "passes": [{**item, "accuracy_delta_pp": 2.0} for item in prior_passes],
        },
        "noise_floor_pp": 1.5,
        **contracts,
    }
    real_require = authorization.require_preregistration_authorization
    monkeypatch.setattr(
        authorization.rig,
        "validate_preregistration",
        lambda _raw, *, kind: validated,
    )
    monkeypatch.setattr(
        authorization,
        "require_preregistration_authorization",
        lambda raw, *, kind: raw,
    )
    gate_receipt_path = tmp_path / "anchor-receipt.json"
    gate_receipt_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        authorization_package,
        "_package_anchor_gate",
        lambda path, _preregistration: {
            "kind": "anchor",
            "source_receipt": authorization_package.bind_artifact(
                path,
                name="test anchor",
            ),
            "receipt_sha256": f"sha256:{'d' * 64}",
        },
    )

    projected = authorization_package.package_preregistration_authorization(
        preregistration_path,
        kind="race",
        gate_receipt_path=gate_receipt_path,
    )

    assert projected["source_preregistration"]["sha256"]
    assert projected["aa_passes"] == prior_passes
    assert projected["gate"]["kind"] == "anchor"
    assert projected["policy"] == {}
    with pytest.raises(contract.StagePlanError, match="score-bearing"):
        authorization.reject_score_bearing_keys(validated, name="raw preregistration")
    authorization.reject_score_bearing_keys(projected, name="preregistration authorization")
    monkeypatch.setattr(authorization.rig, "validate_stack", lambda raw: raw)
    real_require(projected, kind="race")
    gate_receipt_path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(contract.StagePlanError, match="digest changed"):
        real_require(projected, kind="race")


def test_render_authorization_policy_controls_not_applicable_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec, _contracts = render_spec()
    artifact = tmp_path / "render-authorization.json"
    artifact.write_text("{}\n", encoding="utf-8")
    spec["upstream"]["preregistration_authorization"] = str(artifact)
    spec["mode"] = "not_applicable"
    spec["passes"] = []
    preregistration = {
        "preregistration_sha256": f"sha256:{'c' * 64}",
        "policy": {"render_applicable": False},
    }
    monkeypatch.setattr(
        contract,
        "require_preregistration_authorization",
        lambda _raw, *, kind: preregistration,
    )

    assert contract.require_stage_spec(spec) == spec
    preregistration["policy"]["render_applicable"] = True
    with pytest.raises(contract.StagePlanError, match="not-applicable"):
        contract.require_stage_spec(spec)


def test_render_policy_rejects_inconsistent_applicability() -> None:
    policy = {
        "selected_render_substrate": "machine",
        "render_applicable": True,
        "included_levers": ["render_group_lanes"],
        "replay_survivors": {"render_group_lanes": True},
    }
    assert authorization._require_policy(policy, kind="render") == policy
    policy["render_applicable"] = False
    with pytest.raises(contract.StagePlanError, match="applicability"):
        authorization._require_policy(policy, kind="render")


def test_authorization_write_is_atomic_and_one_shot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "authorization.json"
    paid_output_root = tmp_path / "paid-output"
    payload = {"kind": "aa", "authorization_sha256": "sealed"}
    monkeypatch.setattr(authorization, "require_aa_authorization", lambda raw: raw)

    authorization_package.write_authorization(
        target,
        payload,
        paid_output_root=paid_output_root,
    )

    assert target.read_text(encoding="utf-8").endswith("\n")
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(contract.StagePlanError, match="already exists"):
        authorization_package.write_authorization(
            target,
            payload,
            paid_output_root=paid_output_root,
        )


def test_authorization_write_rejects_paid_output_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paid_output_root = tmp_path / "paid-output"
    target = paid_output_root / "authorization.json"
    payload = {"kind": "aa", "authorization_sha256": "sealed"}
    monkeypatch.setattr(authorization, "require_aa_authorization", lambda raw: raw)

    with pytest.raises(contract.StagePlanError, match="outside the paid output root"):
        authorization_package.write_authorization(
            target,
            payload,
            paid_output_root=paid_output_root,
        )
    assert not target.exists()


def test_stage_plan_write_is_atomic_and_one_shot_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "authority" / "stage-plan.json"
    payload = {"output_root": str(tmp_path / "paid-output")}
    barrier = Barrier(2)
    original_link = release_io.os.link
    monkeypatch.setattr(release_plan, "require_stage_plan", lambda _raw: [])

    def synchronized_link(source: Path, destination: Path) -> None:
        barrier.wait()
        original_link(source, destination)

    monkeypatch.setattr(release_io.os, "link", synchronized_link)

    def publish() -> str:
        try:
            release_plan.write_stage_plan(target, payload)
        except contract.StagePlanError:
            return "rejected"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _index: publish(), range(2)))

    assert results == ["published", "rejected"]
    assert inputs.load_json(target) == payload


def test_not_applicable_render_memory_bindings_require_no_roots() -> None:
    spec = {
        "stage": "render",
        "mode": "not_applicable",
        "memory_roots": {"baseline": None, "render": None},
    }

    assert release_memory.build_memory_bindings(spec, dataset={}, source={}) == {
        "baseline": None,
        "render": None,
    }
    spec["memory_roots"]["baseline"] = {"web": "unused", "enterprise": "unused"}
    with pytest.raises(contract.StagePlanError, match="cannot bind saved memory"):
        release_memory.build_memory_bindings(spec, dataset={}, source={})


def test_release_dataset_payload_hashes_are_exact() -> None:
    assert inputs.OFFICIAL_DATASET_SHA256 == {
        "questions": ("sha256:0a3ae5ebea938c24d7800e1e0b0828e08ae1646f939a53853b2b8cdc08e292b7"),
        "trajectories": ("sha256:363cec9a8e87aa8d9101ce4e600aadbf7031d674056ebe4f969e8424abc5f3c6"),
        "small_haystack": (
            "sha256:9b5301defb23a088a5f06e45ff8d5f35e569d78305a66d492046a9fff9b46593"
        ),
    }


def test_aa_extension_and_anchor_require_exact_authorized_arm_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "authorization.json"
    artifact.write_text("{}\n", encoding="utf-8")
    prior_passes = [
        {
            "pass_id": f"aa-{index}",
            "seed": 1300 + index,
            "paired_pass_sha256": f"sha256:{str(index) * 64}",
        }
        for index in range(1, 4)
    ]
    aa_contract = arm_contract(aa_spec()["passes"][0]["arms"][0])
    expected_stack = {"sealed": "stack"}
    aa_authorization = {
        "status": "NEEDS_TWO_MORE",
        "passes": prior_passes,
        "arm_contract": aa_contract,
        "stack": expected_stack,
    }
    monkeypatch.setattr(
        authorization,
        "require_aa_authorization",
        lambda _raw: aa_authorization,
    )
    extension = aa_extension_spec()
    extension["upstream"]["aa_authorization"] = str(artifact)
    authorization.build_upstream_bindings(extension, expected_stack=expected_stack)
    extension["passes"][1]["arms"][1]["manifest"]["configuration"]["drift"] = True
    with pytest.raises(contract.StagePlanError, match="A/A authorization"):
        authorization.build_upstream_bindings(extension, expected_stack=expected_stack)

    anchor = anchor_spec()
    anchor["upstream"]["aa_authorization"] = str(artifact)
    aa_authorization["status"] = "PASS"
    authorization.build_upstream_bindings(anchor, expected_stack=expected_stack)
    anchor["passes"][0]["arms"][0]["manifest"]["geometry"]["max_context_total_chars"] = 59_999
    with pytest.raises(contract.StagePlanError, match="A/A authorization"):
        authorization.build_upstream_bindings(anchor, expected_stack=expected_stack)

    aa_authorization["stack"] = {"sealed": "drifted"}
    with pytest.raises(contract.StagePlanError, match="stack differs"):
        authorization.build_upstream_bindings(anchor, expected_stack=expected_stack)


@pytest.mark.parametrize("stage", ["race", "render"])
def test_paid_stages_require_exact_preregistered_arm_contracts(
    stage: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if stage == "race":
        spec, contracts = race_spec()
        drift_arm = spec["passes"][3]["arms"][1]
    else:
        spec, contracts = render_spec()
        drift_arm = spec["passes"][2]["arms"][1]
    artifact = tmp_path / f"{stage}-authorization.json"
    artifact.write_text("{}\n", encoding="utf-8")
    expected_stack = {"sealed": "stack"}
    preregistration_authorization = {
        "contracts": contracts,
        "stack": expected_stack,
        "policy": ({"render_applicable": True} if stage == "render" else {}),
    }
    spec["upstream"]["preregistration_authorization"] = str(artifact)
    monkeypatch.setattr(
        authorization,
        "require_preregistration_authorization",
        lambda _raw, *, kind: preregistration_authorization,
    )
    authorization.build_upstream_bindings(spec, expected_stack=expected_stack)
    drift_arm["manifest"]["configuration"]["drift"] = True

    with pytest.raises(contract.StagePlanError, match="authorization"):
        authorization.build_upstream_bindings(spec, expected_stack=expected_stack)

    geometry_cases = [(0, 0), (3, 1)] if stage == "race" else [(1, 0), (2, 1)]
    for pass_index, arm_index in geometry_cases:
        if stage == "race":
            geometry_spec, geometry_contracts = race_spec()
        else:
            geometry_spec, geometry_contracts = render_spec()
        geometry_spec["passes"][pass_index]["arms"][arm_index]["manifest"]["geometry"][
            "max_context_items"
        ] += 1
        geometry_spec["upstream"]["preregistration_authorization"] = str(artifact)
        preregistration_authorization["contracts"] = geometry_contracts
        with pytest.raises(contract.StagePlanError, match="authorization"):
            authorization.build_upstream_bindings(
                geometry_spec,
                expected_stack=expected_stack,
            )
