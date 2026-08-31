from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from benchmarks import longmemeval_v2_official as official
from benchmarks import longmemeval_v2_release_authorization as authorization
from benchmarks import (
    longmemeval_v2_release_authorization_package as authorization_package,
)
from benchmarks import longmemeval_v2_release_contract as contract
from benchmarks import longmemeval_v2_release_inputs as inputs
from benchmarks import longmemeval_v2_release_memory as release_memory
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_plan as release_plan
from tools.bench import longmemeval_v2_rig as rig
from tools.tests.longmemeval_v2_release_support import (
    aa_extension_spec,
    aa_spec,
    anchor_spec,
    arm_contract,
    manifest,
    race_spec,
    render_spec,
)
from tools.tests.test_longmemeval_v2_rig import _dispatch_attempt, _dispatch_ledger, _github_stub

REPO_ROOT = Path(__file__).resolve().parents[2]
MAX_CAP_HEADROOM_USD = 0.15


@pytest.fixture(autouse=True)
def _clear_immutable_plan_files(tmp_path: Path) -> Any:
    yield
    for current, _directories, files in os.walk(tmp_path, topdown=False):
        for name in files:
            with suppress(OSError):
                package_root.set_path_flags(Path(current) / name, 0)
        with suppress(OSError):
            package_root.set_path_flags(current, 0)
            os.chmod(current, 0o700)


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


def test_release_caps_cover_pinned_full_corpus_reservations(tmp_path: Path) -> None:
    domain_counts = {
        "web": (240, 86),
        "enterprise": (211, 70),
    }
    for role, cap in contract.RELEASE_ROLE_CAPS_USD.items():
        treatment = role == "render_treatment"
        reservations = []
        for domain, (question_count, llm_eval_count) in domain_counts.items():
            argv = [
                "--data-root",
                str(tmp_path / "data"),
                "--domain",
                domain,
                "--output-dir",
                str(tmp_path / role / domain),
                "--plan-only",
                "--max-spend-usd",
                str(cap),
                "--max-context-total-chars",
                "72000" if treatment else "60000",
            ]
            if treatment:
                argv.append("--note-distillation")
            args = official.parse_args(argv)
            reservation = official.build_spend_reservation(
                args=args,
                question_count=question_count,
                llm_eval_count=llm_eval_count,
                required_trajectory_count=100,
            )
            reserved_total = reservation["reserved_total_usd"]
            assert isinstance(reserved_total, (int, float))
            reservations.append(float(reserved_total))
            assert reservation["status"] == "PASS"

        assert 0 <= cap - max(reservations) < MAX_CAP_HEADROOM_USD


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


@pytest.mark.parametrize("factory", [race_spec, render_spec])
def test_paid_followup_stage_requires_preregistration_authority(
    factory: Any,
) -> None:
    spec, _contracts = factory()
    spec["upstream"]["preregistration_authorization"] = None

    with pytest.raises(contract.StagePlanError, match="require preregistration"):
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


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="requires Darwin immutable file flags",
)
def test_stage_plan_write_is_atomic_and_one_shot_under_concurrency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "authority" / "stage-plan.json"
    payload = {"output_root": str(tmp_path / "paid-output")}
    barrier = Barrier(2)
    monkeypatch.setattr(release_plan, "require_stage_plan", lambda _raw: [])

    def publish() -> str:
        barrier.wait()
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


def _verified_receipt(ledger: dict[str, Any]) -> dict[str, Any]:
    return rig.build_verified_rig_blocked_receipt(ledger, fetch=_github_stub(ledger))


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _resealed_receipt(receipt: dict[str, Any], **changes: Any) -> dict[str, Any]:
    tampered = {**receipt, **changes}
    unsigned = {k: v for k, v in tampered.items() if k != "rig_blocked_receipt_sha256"}
    tampered["rig_blocked_receipt_sha256"] = rig.canonical_sha256(unsigned)
    return tampered


def _resealed_authorization(projected: dict[str, Any], **changes: Any) -> dict[str, Any]:
    tampered = {**projected, **changes}
    unsigned = {k: v for k, v in tampered.items() if k != "authorization_sha256"}
    tampered["authorization_sha256"] = rig.canonical_sha256(unsigned)
    return tampered


def _remap_run_ids(ledger: dict[str, Any], offset: int) -> dict[str, Any]:
    """Invent a self-consistent ledger whose runs and jobs GitHub has never seen."""
    forged = deepcopy(ledger)
    for attempt in forged["attempts"]:
        controller = attempt["controller"]
        controller["id"] += offset
        controller["html_url"] = (
            f"https://github.com/{controller['repository']}/actions/runs/{controller['id']}"
        )
        for builder in attempt["builders"]:
            builder["id"] += offset
            builder["display_title"] = (
                f"{rig.BUILDER_WORKFLOW_NAME} aa-{controller['id']} {rig.DISPATCH_BUILDER_ARM_ID}"
            )
            builder["name"] = builder["display_title"]
            builder["html_url"] = (
                f"https://github.com/{builder['repository']}/actions/runs/{builder['id']}"
            )
            for job in builder["jobs"]:
                job["id"] += offset
                job["run_id"] = builder["id"]
                job["html_url"] = f"{builder['html_url']}/job/{job['id']}"
    return forged


class _Packaged:
    def __init__(self, tmp_path: Path, *, name: str = "genuine") -> None:
        self.ledger = _dispatch_ledger()
        self.fetch = _github_stub(self.ledger)
        self.receipt = _verified_receipt(self.ledger)
        self.receipt_path = _write(tmp_path / name / "rig-blocked-receipt.json", self.receipt)
        self.ledger_path = _write(tmp_path / name / "dispatch-ledger.json", self.ledger)
        self.projected = authorization_package.package_rig_blocked_authorization(
            self.receipt_path,
            ledger_path=self.ledger_path,
            fetch=self.fetch,
        )


def test_rig_blocked_authorization_rejects_an_unverified_ledger(tmp_path: Path) -> None:
    ledger = _dispatch_ledger()
    fetch = _github_stub(ledger)
    receipt = rig.build_rig_blocked_receipt(ledger)
    assert receipt["ledger_provenance"] == rig.LEDGER_PROVENANCE_UNVERIFIED
    receipt_path = _write(tmp_path / "rig-blocked-receipt.json", receipt)
    ledger_path = _write(tmp_path / "dispatch-ledger.json", ledger)

    with pytest.raises(inputs.StagePlanError, match="requires a GitHub-verified ledger"):
        authorization_package.package_rig_blocked_authorization(
            receipt_path, ledger_path=ledger_path, fetch=fetch
        )
    assert fetch.calls == []

    _write(receipt_path, _verified_receipt(ledger))
    projected = authorization_package.package_rig_blocked_authorization(
        receipt_path, ledger_path=ledger_path, fetch=fetch
    )
    assert projected["ledger_provenance"] == rig.LEDGER_PROVENANCE_VERIFIED
    assert len(fetch.calls) == 4 * rig.EXTENDED_AA_PASS_COUNT
    forged = _resealed_authorization(projected, ledger_provenance=rig.LEDGER_PROVENANCE_UNVERIFIED)
    with pytest.raises(inputs.StagePlanError, match="requires a GitHub-verified ledger"):
        authorization.require_rig_blocked_authorization(forged, fetch=fetch)


def test_rig_blocked_authorization_projects_exhausted_dispatches(tmp_path: Path) -> None:
    bundle = _Packaged(tmp_path)
    ledger, receipt, projected = bundle.ledger, bundle.receipt, bundle.projected
    receipt_path, ledger_path, fetch = bundle.receipt_path, bundle.ledger_path, bundle.fetch

    assert projected["kind"] == "rig_blocked"
    assert projected["status"] == "RIG_BLOCKED"
    assert projected["blocked_reason"] == rig.BLOCKED_REASON_DISPATCH_EXHAUSTED
    assert projected["rig_blocked_receipt_sha256"] == receipt["rig_blocked_receipt_sha256"]
    assert projected["ledger_sha256"] == rig.canonical_sha256(ledger)
    assert projected["source_receipt"]["path"] == str(receipt_path.resolve())
    assert projected["source_ledger"]["path"] == str(ledger_path.resolve())
    assert projected["attempt_count"] == rig.EXTENDED_AA_PASS_COUNT
    assert projected["completed_pass_count"] == 0
    assert [item["controller_run_id"] for item in projected["attempts"]] == [
        item["controller"]["run_id"] for item in receipt["attempts"]
    ]
    assert "paid_benchmark_allowed" not in projected
    authorization.reject_score_bearing_keys(projected, name="rig-blocked authorization")
    calls_before = len(fetch.calls)
    assert authorization.require_rig_blocked_authorization(projected, fetch=fetch) == projected
    assert len(fetch.calls) == calls_before + 4 * rig.EXTENDED_AA_PASS_COUNT

    public_path = tmp_path / "public" / "rig-blocked-receipt.json"
    public_ledger = tmp_path / "public" / "dispatch-ledger.json"
    rebased = authorization_package.rebase_rig_blocked_authorization(
        projected,
        public_receipt_path=public_path.resolve(),
        public_ledger_path=public_ledger.resolve(),
    )
    assert rebased["source_receipt"]["path"] == str(public_path.resolve())
    assert rebased["source_ledger"]["path"] == str(public_ledger.resolve())
    assert rebased["authorization_sha256"] != projected["authorization_sha256"]
    with pytest.raises(inputs.StagePlanError, match="paths are incomplete"):
        authorization_package.rebase_rig_blocked_authorization(
            projected, public_receipt_path=public_path.resolve(), public_ledger_path=None
        )

    output = tmp_path / "authority" / "rig-blocked-authorization.json"
    with pytest.raises(inputs.StagePlanError, match="needs a GitHub fetcher"):
        authorization_package.write_authorization(
            output, projected, paid_output_root=(tmp_path / "paid").resolve()
        )
    authorization_package.write_authorization(
        output,
        projected,
        paid_output_root=(tmp_path / "paid").resolve(),
        fetch=fetch,
    )
    assert json.loads(output.read_text(encoding="utf-8")) == projected

    with pytest.raises(inputs.StagePlanError, match="status or reason is invalid"):
        authorization.require_rig_blocked_authorization(
            {**projected, "blocked_reason": rig.BLOCKED_REASON_SPAN_UNSTABLE}, fetch=fetch
        )
    with pytest.raises(inputs.StagePlanError, match="A/A authorization"):
        authorization.require_aa_authorization(projected)
    receipt_path.write_text("{}\n", encoding="utf-8")
    with pytest.raises(inputs.StagePlanError, match="path or size changed"):
        authorization.require_rig_blocked_authorization(projected, fetch=fetch)


def test_rig_blocked_authorization_rejects_completed_pass_and_short_ledgers(
    tmp_path: Path,
) -> None:
    bundle = _Packaged(tmp_path)
    projected, fetch = bundle.projected, bundle.fetch

    with pytest.raises(inputs.StagePlanError, match="claims a completed pass"):
        authorization.require_rig_blocked_authorization(
            _resealed_authorization(projected, completed_pass_count=1), fetch=fetch
        )
    with pytest.raises(inputs.StagePlanError, match="fewer than five dispatches"):
        authorization.require_rig_blocked_authorization(
            _resealed_authorization(projected, attempt_count=4, attempts=projected["attempts"][:4]),
            fetch=fetch,
        )
    with pytest.raises(inputs.StagePlanError, match="head SHAs differ from its attempts"):
        authorization.require_rig_blocked_authorization(
            _resealed_authorization(projected, head_shas=["9" * 40]), fetch=fetch
        )
    with pytest.raises(inputs.StagePlanError, match="score-bearing"):
        authorization.require_rig_blocked_authorization(
            _resealed_authorization(
                projected, attempts=[{**projected["attempts"][0], "accuracy": 0.5}]
            ),
            fetch=fetch,
        )


def test_rig_blocked_packaging_rejects_a_receipt_citing_another_ledger(tmp_path: Path) -> None:
    bundle = _Packaged(tmp_path)
    receipt, receipt_path, ledger_path, fetch = (
        bundle.receipt,
        bundle.receipt_path,
        bundle.ledger_path,
        bundle.fetch,
    )

    # Codex probe: a resealed receipt whose ledger digest names a ledger that
    # holds a successful two-domain pass must not package as RIG_BLOCKED.
    passing = _dispatch_ledger(
        [
            *[_dispatch_attempt(index) for index in range(4)],
            _dispatch_attempt(
                4,
                domain_conclusions={"enterprise": "success", "web": "success"},
                combined_conclusion="success",
            ),
        ]
    )
    passing_path = _write(tmp_path / "passing-ledger.json", passing)
    citing = _resealed_receipt(receipt, ledger_sha256=rig.canonical_sha256(passing))
    assert rig.validate_rig_blocked_receipt(citing) == citing
    citing_path = _write(tmp_path / "citing-receipt.json", citing)
    with pytest.raises(rig.RigInputError, match="A/A data, not dispatch exhaustion"):
        authorization_package.package_rig_blocked_authorization(
            citing_path, ledger_path=passing_path, fetch=_github_stub(passing)
        )
    with pytest.raises(inputs.StagePlanError, match="not derived from its bound ledger"):
        authorization_package.package_rig_blocked_authorization(
            citing_path, ledger_path=ledger_path, fetch=fetch
        )

    # A real ledger digest mismatch against the genuine ledger.
    wrong_digest = _resealed_receipt(receipt, ledger_sha256=f"sha256:{'f' * 64}")
    wrong_path = _write(tmp_path / "wrong-digest-receipt.json", wrong_digest)
    with pytest.raises(inputs.StagePlanError, match="not derived from its bound ledger"):
        authorization_package.package_rig_blocked_authorization(
            wrong_path, ledger_path=ledger_path, fetch=fetch
        )

    # A different valid ledger than the one the receipt was sealed from.
    other = _dispatch_ledger([_dispatch_attempt(index, head_sha="e" * 40) for index in range(5)])
    other_path = _write(tmp_path / "other-ledger.json", other)
    with pytest.raises(inputs.StagePlanError, match="not derived from its bound ledger"):
        authorization_package.package_rig_blocked_authorization(
            receipt_path, ledger_path=other_path, fetch=_github_stub(other)
        )


def test_rig_blocked_authorization_rejects_projection_drift_from_its_receipt(
    tmp_path: Path,
) -> None:
    bundle = _Packaged(tmp_path)
    projected, ledger_path, fetch = bundle.projected, bundle.ledger_path, bundle.fetch
    assert authorization.require_rig_blocked_authorization(projected, fetch=fetch) == projected

    # Codex probe: keep the genuine source receipt, change projected fields.
    drifted = [
        _resealed_authorization(projected, ledger_sha256=f"sha256:{'e' * 64}"),
        _resealed_authorization(
            projected,
            attempts=[
                {**projected["attempts"][0], "builder_run_ids": [9999]},
                *projected["attempts"][1:],
            ],
        ),
        _resealed_authorization(
            projected,
            attempts=[{**item, "head_sha": "9" * 40} for item in projected["attempts"]],
            head_shas=["9" * 40],
        ),
    ]
    for tampered in drifted:
        with pytest.raises(inputs.StagePlanError, match="differs from its bound receipt"):
            authorization.require_rig_blocked_authorization(tampered, fetch=fetch)
    with pytest.raises(inputs.StagePlanError, match="trusted release repository"):
        authorization.require_rig_blocked_authorization(
            _resealed_authorization(projected, repository="someone/else"), fetch=fetch
        )

    # The bound ledger on disk is swapped for one that does not derive the receipt.
    other = _dispatch_ledger([_dispatch_attempt(index, head_sha="e" * 40) for index in range(5)])
    _write(ledger_path, other)
    swapped = _resealed_authorization(
        projected,
        source_ledger=inputs.bind_artifact(ledger_path, name="rig-blocked source ledger"),
    )
    with pytest.raises(inputs.StagePlanError, match="not derived from its bound ledger"):
        authorization.require_rig_blocked_authorization(swapped, fetch=_github_stub(other))


def test_rig_blocked_authority_rejects_invented_dispatches_with_a_forged_verification(
    tmp_path: Path,
) -> None:
    """Codex probe: invented run and job ids plus a plausible verification block."""
    genuine = _dispatch_ledger()
    github = _github_stub(genuine)
    forged_ledger = _remap_run_ids(genuine, 500_000)
    offline = rig.build_rig_blocked_receipt(forged_ledger)
    forged = _resealed_receipt(
        offline,
        ledger_provenance=rig.LEDGER_PROVENANCE_VERIFIED,
        github_verification={
            "verified_at": forged_ledger["fetched_at"],
            "run_count": 2 * rig.EXTENDED_AA_PASS_COUNT,
            "job_count": 3 * rig.EXTENDED_AA_PASS_COUNT,
            "builder_query": rig.GITHUB_BUILDER_RUNS_ENDPOINT,
        },
    )
    # The label alone passes receipt validation; it is a record, not evidence.
    assert rig.validate_rig_blocked_receipt(forged) == forged
    receipt_path = _write(tmp_path / "forged-receipt.json", forged)
    ledger_path = _write(tmp_path / "forged-ledger.json", forged_ledger)

    with pytest.raises(inputs.StagePlanError, match="HTTP 404"):
        authorization_package.package_rig_blocked_authorization(
            receipt_path, ledger_path=ledger_path, fetch=github
        )

    source = inputs.bind_artifact(receipt_path, name="rig-blocked source receipt")
    source_ledger = inputs.bind_artifact(ledger_path, name="rig-blocked source ledger")
    hand_built = authorization.rig_blocked_projection(source, source_ledger, forged)
    hand_built["authorization_sha256"] = rig.canonical_sha256(hand_built)
    with pytest.raises(inputs.StagePlanError, match="HTTP 404"):
        authorization.require_rig_blocked_authorization(hand_built, fetch=github)


def test_rig_blocked_authority_rejects_drift_at_authorization_time(tmp_path: Path) -> None:
    """A receipt sealed against one GitHub state is rejected once GitHub disagrees."""
    bundle = _Packaged(tmp_path)
    drifted = deepcopy(bundle.ledger)
    web = next(
        job
        for job in drifted["attempts"][4]["builders"][0]["jobs"]
        if job["name"] == rig.OFFICIAL_JOB_NAMES["web"]
    )
    web["conclusion"] = "success"
    github_now = _github_stub(drifted)

    with pytest.raises(inputs.StagePlanError, match="jobs differ from GitHub"):
        authorization_package.package_rig_blocked_authorization(
            bundle.receipt_path, ledger_path=bundle.ledger_path, fetch=github_now
        )
    with pytest.raises(inputs.StagePlanError, match="jobs differ from GitHub"):
        authorization.require_rig_blocked_authorization(bundle.projected, fetch=github_now)

    def unavailable(endpoint: str) -> list[Any]:
        raise rig.RigInputError(f"gh api {endpoint} failed: HTTP 504")

    with pytest.raises(inputs.StagePlanError, match="HTTP 504"):
        authorization.require_rig_blocked_authorization(bundle.projected, fetch=unavailable)


RELEASE_RESULTS = REPO_ROOT / "benchmarks" / "results" / "longmemeval-v2-release"
V1_3_DISPATCH_LEDGER = RELEASE_RESULTS / "sibyl-v1-3-aa-dispatch-ledger.json"
V1_3_RIG_BLOCKED_RECEIPT = RELEASE_RESULTS / "sibyl-v1-3-aa-rig-blocked-receipt.json"
V1_3_CONTROLLER_RUN_IDS = [32888217656, 32897996847, 32911050360, 32921881380, 32998699090]
V1_3_BUILDER_RUN_IDS = [32888310148, 32898089824, 32911112960, 32921948833, 32998783818]


def test_committed_v1_3_rig_blocked_receipt_is_bound_to_its_ledger() -> None:
    ledger = inputs.load_json(V1_3_DISPATCH_LEDGER)
    committed = inputs.load_json(V1_3_RIG_BLOCKED_RECEIPT)

    rig.require_receipt_from_ledger(committed, ledger)
    assert rig.validate_rig_blocked_receipt(committed) == committed
    assert committed["ledger_provenance"] == rig.LEDGER_PROVENANCE_VERIFIED
    assert committed["blocked_reason"] == rig.BLOCKED_REASON_DISPATCH_EXHAUSTED
    assert committed["paid_benchmark_allowed"] is False
    assert committed["score_claim_allowed"] is False
    assert committed["completed_pass_count"] == 0
    assert committed["head_branch"] == rig.TRUSTED_BRANCH
    assert committed["repository"] == rig.TRUSTED_REPOSITORY
    assert [item["controller"]["run_id"] for item in committed["attempts"]] == (
        V1_3_CONTROLLER_RUN_IDS
    )
    assert [
        builder["run_id"] for item in committed["attempts"] for builder in item["builders"]
    ] == V1_3_BUILDER_RUN_IDS

    # Hermetic: the stub replays the committed ledger as GitHub. The runbook
    # command runs the same packaging against live GitHub.
    projected = authorization_package.package_rig_blocked_authorization(
        V1_3_RIG_BLOCKED_RECEIPT,
        ledger_path=V1_3_DISPATCH_LEDGER,
        fetch=_github_stub(ledger),
    )
    assert projected["rig_blocked_receipt_sha256"] == committed["rig_blocked_receipt_sha256"]
    assert projected["ledger_sha256"] == rig.canonical_sha256(ledger)
    assert projected["source_ledger"]["sha256"]
    assert projected["attempt_count"] == rig.EXTENDED_AA_PASS_COUNT
