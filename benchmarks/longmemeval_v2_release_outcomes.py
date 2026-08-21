"""Score-aware outcomes built only from immutable official arm authorities."""

from __future__ import annotations

import json
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from benchmarks import longmemeval_v2_release_authorization as authorization
from benchmarks import longmemeval_v2_release_official_publication as publication
from benchmarks import longmemeval_v2_release_package_archive as package_archive
from benchmarks import longmemeval_v2_release_package_object as package_object
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_handoff import ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import (
    DOMAINS,
    StagePlanError,
    bind_artifact,
    load_json,
    require_artifact,
    require_exact_keys,
)
from tools.bench import longmemeval_v2_artifact_bridge as bridge
from tools.bench import longmemeval_v2_rig as rig

PAIRED_ARM_COUNT = 2
RACE_PAIR_COUNT = 4
PACKAGE_CLAIM_SCHEMA_VERSION = "sibyl-longmemeval-v2-release-package-claim-v1"
PACKAGE_CLAIM_KEYS = frozenset(
    {
        "schema_version",
        "stage_plan_sha256",
        "official_packages_root",
        "preregistration_template",
        "executed_status",
        "control_artifacts",
        "domains",
        "official_arms",
        "package_claim_sha256",
    }
)
DOMAIN_CLAIM_KEYS = frozenset({"arm_id", "domain", "actual_cost_usd", "exit_artifact", "artifacts"})
OFFICIAL_ARM_CLAIM_KEYS = frozenset({"publication", "authority_artifact", "object_artifact"})


@dataclass(frozen=True)
class OfficialArm:
    """One validated score-bearing arm read from a 3b authority only."""

    arm_id: str
    authority: dict[str, Any]
    authority_artifact: dict[str, Any]
    object_artifact: dict[str, Any]
    arm_run: dict[str, Any]


@dataclass(frozen=True)
class StageOutcome:
    """Canonical paired passes and rig receipt for one executed stage."""

    official_arms: tuple[OfficialArm, ...]
    paired_passes: tuple[dict[str, Any], ...]
    receipt: dict[str, Any]


def require_package_claim(
    plan: dict[str, Any],
    raw: object,
    *,
    official_packages_root: Path | None = None,
) -> dict[str, Any]:
    """Validate the score-blind durable handoff into package outcomes."""

    if not isinstance(raw, dict):
        raise StagePlanError("release package claim is missing")
    require_exact_keys(raw, PACKAGE_CLAIM_KEYS, name="release package claim")
    unsigned = {key: value for key, value in raw.items() if key != "package_claim_sha256"}
    if (
        raw.get("schema_version") != PACKAGE_CLAIM_SCHEMA_VERSION
        or raw.get("stage_plan_sha256") != plan["stage_plan_sha256"]
        or raw.get("package_claim_sha256") != rig.canonical_sha256(unsigned)
    ):
        raise StagePlanError("release package claim identity is invalid")
    root = Path(str(raw.get("official_packages_root"))).expanduser().resolve()
    if str(root) != raw.get("official_packages_root"):
        raise StagePlanError("official packages root claim is not canonical")
    if official_packages_root is not None and root != official_packages_root:
        raise StagePlanError("official packages root changed after package claim")
    template = raw.get("preregistration_template")
    if template is not None:
        require_artifact(template, name="preregistration template")
    executed_status = state.validate_status_receipt(plan, raw.get("executed_status"))
    if executed_status["status"] != "EXECUTED":
        raise StagePlanError("package claim does not preserve an EXECUTED status")
    controls = raw.get("control_artifacts")
    if not isinstance(controls, dict) or "runner_status" not in controls:
        raise StagePlanError("package claim control artifacts are incomplete")
    for name, binding in controls.items():
        if name != "runner_status":
            require_artifact(binding, name=f"package claim control {name}")
    _require_claim_domains(plan, raw.get("domains"))
    _require_claim_arms(plan, raw.get("official_arms"))
    return raw


def _require_claim_domains(plan: dict[str, Any], raw: object) -> None:
    expected_keys = {f"{run['arm_id']}:{domain}" for run in plan["runs"] for domain in DOMAINS}
    if not isinstance(raw, list):
        raise StagePlanError("package claim domain evidence is missing")
    seen: set[str] = set()
    for row in raw:
        if not isinstance(row, dict):
            raise StagePlanError("package claim domain evidence is invalid")
        require_exact_keys(row, DOMAIN_CLAIM_KEYS, name="package claim domain")
        key = f"{row.get('arm_id')}:{row.get('domain')}"
        if key in seen:
            raise StagePlanError("package claim domain evidence is duplicated")
        seen.add(key)
        require_artifact(row.get("exit_artifact"), name=f"package claim exit {key}")
        artifacts = row.get("artifacts")
        if not isinstance(artifacts, dict) or not artifacts:
            raise StagePlanError("package claim domain artifacts are missing")
        for name, binding in artifacts.items():
            require_artifact(binding, name=f"package claim {key} artifact {name}")
    if seen != expected_keys:
        raise StagePlanError("package claim domain set differs from the sealed stage")


def _require_claim_arms(plan: dict[str, Any], raw: object) -> None:
    expected_arms = {str(run["arm_id"]) for run in plan["runs"]}
    if not isinstance(raw, dict) or set(raw) != expected_arms:
        raise StagePlanError("package claim official arm set differs from the sealed stage")
    for arm_id, item in raw.items():
        if not isinstance(item, dict):
            raise StagePlanError("package claim official arm is invalid")
        require_exact_keys(item, OFFICIAL_ARM_CLAIM_KEYS, name="package claim official arm")
        package_object.require_publication_receipt(item["publication"])
        require_artifact(item["authority_artifact"], name=f"{arm_id} authority")
        require_artifact(item["object_artifact"], name=f"{arm_id} package object")


def _json_member(content: bytes, *, name: str) -> dict[str, Any]:
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StagePlanError(f"official arm member {name!r} is invalid JSON") from exc
    if not isinstance(raw, dict):
        raise StagePlanError(f"official arm member {name!r} is not an object")
    return raw


def _read_frozen_object(path: Path, binding: dict[str, Any]) -> bytes:
    before = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != package_object.OBJECT_FILE_MODE
        or not (before.st_flags & package_root.IMMUTABLE_FLAG)
    ):
        raise StagePlanError("official arm object is not immutable")
    content = path.read_bytes()
    after = path.stat(follow_symlinks=False)
    if (
        before != after
        or bind_artifact(path, name="official arm package object") != binding
        or len(content) != binding["size_bytes"]
    ):
        raise StagePlanError("official arm object changed while reading outcome input")
    return content


def require_official_arm(
    executed: ExecutedStage,
    run: dict[str, Any],
    *,
    packages_root: Path,
    expected: dict[str, Any] | None = None,
    packaging_status: dict[str, Any] | None = None,
) -> OfficialArm:
    """Read one arm only through its immutable official publication authority."""

    arm_id = str(run["arm_id"])
    if expected is None:
        if packaging_status is not None:
            raise StagePlanError("fresh official arm consumption has package lifecycle status")
        authority = publication.require_official_arm_package(
            executed,
            arm_id=arm_id,
            packages_root=packages_root,
        )
        return _require_official_arm_semantics(
            executed,
            run,
            packages_root,
            authority,
            expected=None,
        )
    if packaging_status is None:
        raise StagePlanError("claimed official arm consumption lacks package lifecycle status")
    with publication.open_claimed_official_arm_package(
        executed,
        arm_id=arm_id,
        packages_root=packages_root,
        expected=expected.get("publication"),
        packaging_status=packaging_status,
    ) as authority:
        return _require_official_arm_semantics(
            executed,
            run,
            packages_root,
            authority,
            expected=expected,
        )


def _require_official_arm_semantics(
    executed: ExecutedStage,
    run: dict[str, Any],
    packages_root: Path,
    authority: dict[str, Any],
    *,
    expected: dict[str, Any] | None,
) -> OfficialArm:
    arm_id = str(run["arm_id"])
    authority_path = package_object.publication_path(packages_root, arm_id)
    authority_artifact = bind_artifact(authority_path, name="official arm authority")
    object_binding = require_artifact(
        authority.get("package_object"),
        name="official arm package object",
    )
    content = _read_frozen_object(Path(object_binding["path"]), object_binding)
    members, _manifest = package_archive.require_package_object(content)
    arm_content = members.get("arm_run.json")
    if arm_content is None:
        raise StagePlanError("official arm authority is missing arm_run.json")
    arm_run = _json_member(arm_content, name="arm_run.json")
    expected_binding = package_archive.member_binding("arm_run.json", arm_content)
    public_binding = authority.get("arm_run")
    if not isinstance(public_binding, dict) or any(
        public_binding.get(key) != value for key, value in expected_binding.items() if key != "path"
    ):
        raise StagePlanError("official arm run differs from its publication authority")
    stack = rig.validate_stack(arm_run.get("stack"))
    validated = rig.validate_arm(
        arm_run,
        stack_digest=rig.stack_fingerprint(stack),
        side=arm_id,
    )
    manifest = run["manifest"]
    if (
        validated["experiment_id"] != manifest["experiment_id"]
        or validated["experiment_phase"] != manifest["experiment_phase"]
        or validated["pass_id"] != run["pass_id"]
        or validated["seed"] != run["seed"]
        or validated["execution"] != run["execution"]
        or validated["stack"] != executed.plan["stack_identity"]
    ):
        raise StagePlanError("official arm outcome identity differs from its sealed run")
    if expected is None:
        stable = (
            publication.require_official_arm_package(
                executed,
                arm_id=arm_id,
                packages_root=packages_root,
            )
            == authority
        )
    else:
        stable = (
            expected.get("authority_artifact") == authority_artifact
            and expected.get("object_artifact") == object_binding
        )
    if not stable or (
        bind_artifact(authority_path, name="official arm authority") != authority_artifact
        or require_artifact(object_binding, name="official arm package object") != object_binding
    ):
        raise StagePlanError("official arm authority changed during outcome validation")
    return OfficialArm(
        arm_id=arm_id,
        authority=authority,
        authority_artifact=authority_artifact,
        object_artifact=object_binding,
        arm_run=validated,
    )


def require_official_arms(
    executed: ExecutedStage,
    *,
    packages_root: Path,
    expected: dict[str, Any] | None = None,
    packaging_status: dict[str, Any] | None = None,
) -> tuple[OfficialArm, ...]:
    run_ids = {str(run["arm_id"]) for run in executed.runs}
    if expected is not None and set(expected) != run_ids:
        raise StagePlanError("official arm claim set differs from the executed stage")
    return tuple(
        require_official_arm(
            executed,
            run,
            packages_root=packages_root,
            expected=None if expected is None else expected[str(run["arm_id"])],
            packaging_status=packaging_status,
        )
        for run in executed.runs
    )


def _bound_json(
    binding: object,
    *,
    name: str,
    validator: Callable[[object], dict[str, Any]],
) -> dict[str, Any]:
    artifact = require_artifact(binding, name=name)
    path = Path(artifact["path"])
    raw = load_json(path)
    validated = validator(raw)
    if require_artifact(artifact, name=name) != artifact:
        raise StagePlanError(f"{name} changed during outcome validation")
    return validated


def _aa_authorization(executed: ExecutedStage) -> dict[str, Any]:
    packaged = executed.plan["upstream_bindings"]["aa_authorization"]
    if packaged is None:
        raise StagePlanError("stage outcome requires an A/A authorization")
    return authorization.require_aa_authorization(packaged)


def require_bound_aa_receipt(executed: ExecutedStage) -> dict[str, Any]:
    authority = _aa_authorization(executed)
    return _bound_json(
        authority["source_receipt"],
        name="A/A source receipt",
        validator=rig.validate_aa_receipt,
    )


def require_bound_preregistration(
    executed: ExecutedStage,
    *,
    kind: str,
) -> dict[str, Any]:
    packaged = executed.plan["upstream_bindings"]["preregistration_authorization"]
    if packaged is None:
        raise StagePlanError(f"{kind} outcome requires a preregistration authorization")
    packaged = authorization.require_preregistration_authorization(
        packaged,
        kind=kind,
    )
    return _bound_json(
        packaged["source_preregistration"],
        name=f"{kind} source preregistration",
        validator=lambda raw: rig.validate_preregistration(raw, kind=kind),
    )


def _pair_current(
    executed: ExecutedStage,
    arms: tuple[OfficialArm, ...],
) -> list[dict[str, Any]]:
    by_id = {arm.arm_id: arm.arm_run for arm in arms}
    pairs: list[dict[str, Any]] = []
    for pass_spec in executed.plan["spec"]["passes"]:
        pass_arms = pass_spec["arms"]
        if len(pass_arms) == PAIRED_ARM_COUNT:
            pairs.append(
                bridge.build_paired_pass(
                    by_id[pass_arms[0]["arm_id"]],
                    by_id[pass_arms[1]["arm_id"]],
                )
            )
    return pairs


def _aa_passes(executed: ExecutedStage, current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if executed.plan["spec"]["mode"] == "initial":
        return current
    packaged = _aa_authorization(executed)
    prior = [
        _bound_json(
            item["paired_pass_artifact"],
            name="prior A/A paired pass",
            validator=rig.validate_pass,
        )
        for item in packaged["passes"]
    ]
    return [*prior, *current]


def build_stage_outcome(
    executed: ExecutedStage,
    *,
    packages_root: Path,
    official_arms: tuple[OfficialArm, ...] | None = None,
) -> StageOutcome:
    """Build the canonical rig outcome after the executed-stage score boundary."""

    arms = (
        require_official_arms(executed, packages_root=packages_root)
        if official_arms is None
        else official_arms
    )
    pairs = _pair_current(executed, arms)
    stage = executed.plan["spec"]["stage"]
    if stage == "aa":
        pairs = _aa_passes(executed, pairs)
        receipt = rig.build_aa_receipt(pairs)
    elif stage == "anchor":
        if len(arms) != 1 or pairs:
            raise StagePlanError("anchor outcome requires one official arm")
        receipt = rig.build_anchor_receipt(
            arms[0].arm_run,
            aa_receipt=require_bound_aa_receipt(executed),
        )
    elif stage == "race":
        preregistration = require_bound_preregistration(executed, kind="race")
        if len(pairs) != RACE_PAIR_COUNT:
            raise StagePlanError("race outcome requires three decision passes and one sanity pass")
        matched_spec = executed.plan["spec"]["passes"][-1]
        by_id = {arm.arm_id: arm.arm_run for arm in arms}
        matched = bridge.build_paired_pass(
            by_id[matched_spec["arms"][0]["arm_id"]],
            by_id[matched_spec["arms"][1]["arm_id"]],
        )
        receipt = rig.build_race_receipt(preregistration, pairs[:3], matched)
        pairs = [*pairs[:3], matched]
    elif stage == "render":
        receipt = rig.build_render_receipt(
            require_bound_preregistration(executed, kind="render"),
            pairs,
        )
    else:
        raise StagePlanError("release stage outcome kind is unsupported")
    return StageOutcome(
        official_arms=arms,
        paired_passes=tuple(pairs),
        receipt=receipt,
    )


def issue_preregistration(
    template: dict[str, Any],
    *,
    kind: str,
    aa_receipt: dict[str, Any],
    race_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inject only canonical prior-stage authority into a scoreless template."""

    authorization.reject_score_bearing_keys(template, name=f"{kind} preregistration template")
    forbidden = {
        "schema_version",
        "kind",
        "stack",
        "aa_receipt",
        "aa_receipt_sha256",
        "aa_span_pp",
        "noise_floor_pp",
        "race_receipt",
        "race_receipt_sha256",
        "preregistration_sha256",
    }
    if set(template) & forbidden:
        raise StagePlanError("preregistration template contains producer-owned fields")
    raw = {
        **template,
        "stack": aa_receipt["stack"],
        "aa_receipt": rig.validate_aa_receipt(aa_receipt),
    }
    if kind == "render":
        if race_receipt is None:
            raise StagePlanError("render preregistration requires a race receipt")
        raw["race_receipt"] = rig.validate_race_receipt(race_receipt)
    elif kind != "race" or race_receipt is not None:
        raise StagePlanError("preregistration template kind is invalid")
    return rig.freeze_preregistration(raw, kind=kind)
