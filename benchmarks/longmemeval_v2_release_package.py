"""Immutable stage-outcome transaction for local LongMemEval-V2 releases."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Protocol

from benchmarks import longmemeval_v2_release_authorization_package as authorization_package
from benchmarks import longmemeval_v2_release_outcomes as outcomes
from benchmarks import longmemeval_v2_release_package_claim as package_claim
from benchmarks import longmemeval_v2_release_stage_io as stage_io
from benchmarks import longmemeval_v2_release_stage_receipt as stage_receipt
from benchmarks import longmemeval_v2_release_stage_transaction as stage_transaction
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_handoff import ExecutedStage, require_executed_stage
from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    load_json,
    require_exact_keys,
)

STAGE_RECEIPT_SCHEMA_VERSION = "sibyl-longmemeval-v2-release-stage-receipt-v1"
STAGE_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "stage_plan_sha256",
        "stage",
        "mode",
        "status",
        "actual_cost_usd",
        "package_claim_sha256",
        "official_arms",
        "artifacts",
        "stage_receipt_sha256",
    }
)
FILE_MODE = 0o400
DIRECTORY_MODE = 0o500
_PENDING_PREFIX = "packages.pending."


class _PayloadAuthority(Protocol):
    def json(self, relative: str) -> dict[str, Any]: ...

    def binding(self, relative: str) -> dict[str, Any]: ...


def _outcome_artifacts(
    executed: ExecutedStage,
    outcome: outcomes.StageOutcome,
    transaction: stage_transaction.PendingStageTransaction,
    *,
    template_binding: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    public_root = Path(executed.plan["output_root"]) / "packages"
    bindings: dict[str, dict[str, Any]] = {}
    paired_artifacts: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for paired in outcome.paired_passes:
        relative = f"passes/{paired['pass_id']}.json"
        transaction.write_json(relative, paired)
        bindings[f"pass:{paired['pass_id']}"] = transaction.binding(relative, public=True)
        paired_artifacts.append((transaction.binding(relative, public=False), paired))
    transaction.write_json("outcome.json", outcome.receipt)
    physical_outcome = transaction.binding("outcome.json", public=False)
    public_outcome_path = public_root / "outcome.json"
    bindings["outcome"] = transaction.binding("outcome.json", public=True)
    stage = executed.plan["spec"]["stage"]
    if stage == "aa":
        physical = authorization_package.build_aa_authorization(
            physical_outcome,
            outcome.receipt,
            paired_artifacts=paired_artifacts,
        )
        payload = authorization_package.rebase_aa_authorization(
            physical,
            public_receipt_path=public_outcome_path,
            public_paired_pass_paths=[
                public_root / "passes" / f"{paired['pass_id']}.json"
                for paired in outcome.paired_passes
            ],
        )
        transaction.write_json("authorization.json", payload)
        bindings["authorization"] = transaction.binding("authorization.json", public=True)
    elif stage in {"anchor", "race"}:
        if template_binding is None:
            raise StagePlanError("preregistration template binding is missing")
        template = load_json(Path(template_binding["path"]))
        if stage == "anchor":
            aa_receipt = outcomes.require_bound_aa_receipt(executed)
            race_receipt = None
            kind = "race"
        else:
            source = outcomes.require_bound_preregistration(executed, kind="race")
            aa_receipt = source["aa_receipt"]
            race_receipt = outcome.receipt
            kind = "render"
        preregistration = outcomes.issue_preregistration(
            template,
            kind=kind,
            aa_receipt=aa_receipt,
            race_receipt=race_receipt,
        )
        transaction.write_json("preregistration.json", preregistration)
        physical_preregistration = transaction.binding("preregistration.json", public=False)
        bindings["preregistration"] = transaction.binding("preregistration.json", public=True)
        gate = authorization_package.build_preregistration_gate(
            physical_outcome,
            outcome.receipt,
            preregistration,
            kind=kind,
        )
        physical = authorization_package.build_preregistration_authorization(
            physical_preregistration,
            preregistration,
            kind=kind,
            gate=gate,
        )
        packaged = authorization_package.rebase_preregistration_authorization(
            physical,
            kind=kind,
            public_preregistration_path=public_root / "preregistration.json",
            public_gate_receipt_path=public_outcome_path,
        )
        transaction.write_json("authorization.json", packaged)
        bindings["authorization"] = transaction.binding("authorization.json", public=True)
    return bindings


def _expected_names(bindings: dict[str, dict[str, Any]]) -> tuple[set[str], set[str]]:
    files = {"package_claim.json"}
    for key, binding in bindings.items():
        if key.startswith("pass:"):
            files.add(f"passes/{Path(binding['path']).name}")
        else:
            files.add(Path(binding["path"]).name)
    directories = {"passes"} if any(name.startswith("passes/") for name in files) else set()
    return files, directories


def _expected_preregistration_authorization(
    executed: ExecutedStage,
    outcome: outcomes.StageOutcome,
    *,
    source_binding: dict[str, Any],
    gate_binding: dict[str, Any],
    template_binding: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if template_binding is None:
        raise StagePlanError("preregistration template binding is missing")
    template = load_json(Path(template_binding["path"]))
    stage = executed.plan["spec"]["stage"]
    if stage == "anchor":
        aa_receipt = outcomes.require_bound_aa_receipt(executed)
        race_receipt = None
        kind = "race"
    elif stage == "race":
        source = outcomes.require_bound_preregistration(executed, kind="race")
        aa_receipt = source["aa_receipt"]
        race_receipt = outcome.receipt
        kind = "render"
    else:
        raise StagePlanError("stage does not issue a preregistration")
    preregistration = outcomes.issue_preregistration(
        template,
        kind=kind,
        aa_receipt=aa_receipt,
        race_receipt=race_receipt,
    )
    gate = authorization_package.build_preregistration_gate(
        gate_binding,
        outcome.receipt,
        preregistration,
        kind=kind,
    )
    packaged = authorization_package.build_preregistration_authorization(
        source_binding,
        preregistration,
        kind=kind,
        gate=gate,
    )
    return preregistration, packaged


def _stage_receipt(
    executed: ExecutedStage,
    claim: dict[str, Any],
    bindings: dict[str, dict[str, Any]],
    *,
    outcome_status: str,
) -> dict[str, Any]:
    return package_claim.sealed(
        {
            "schema_version": STAGE_RECEIPT_SCHEMA_VERSION,
            "stage_plan_sha256": executed.plan["stage_plan_sha256"],
            "stage": executed.plan["spec"]["stage"],
            "mode": executed.plan["spec"]["mode"],
            "status": outcome_status,
            "actual_cost_usd": executed.status_receipt["actual_cost_usd"],
            "package_claim_sha256": claim["package_claim_sha256"],
            "official_arms": deepcopy(claim["official_arms"]),
            "artifacts": deepcopy(bindings),
        },
        "stage_receipt_sha256",
    )


def _consume(
    plan: dict[str, Any],
    *,
    allowed_statuses: set[str],
) -> dict[str, Any]:
    output_root = Path(plan["output_root"])
    with stage_io.open_frozen_stage_authority(output_root) as authority:
        claim = outcomes.require_package_claim(
            plan,
            authority.json("package_claim.json"),
        )
        live = state.read_status_receipt(plan)
        if live["status"] not in allowed_statuses or live["package_claim"] != claim:
            raise StagePlanError("stage package lifecycle status is invalid")
        executed = package_claim.require_packaging_handoff(plan, claim, live)
        expected_arms = outcomes.require_official_arms(
            executed,
            packages_root=Path(claim["official_packages_root"]),
            expected=claim["official_arms"],
            packaging_status=live,
        )
        rebuilt = outcomes.build_stage_outcome(
            executed,
            packages_root=Path(claim["official_packages_root"]),
            official_arms=expected_arms,
        )
        bindings = _require_transaction_payloads(executed, claim, rebuilt, authority)
        expected_files, expected_directories = _expected_names(bindings)
        authority.require_inventory(expected_files, expected_directories)
        receipt = authority.receipt_json()
        require_exact_keys(receipt, STAGE_RECEIPT_KEYS, name="stage receipt")
        if receipt != _stage_receipt(
            executed,
            claim,
            bindings,
            outcome_status=rebuilt.receipt["status"],
        ):
            raise StagePlanError("stage receipt differs from the frozen package")
        receipt_binding = authority.receipt_binding()
        final = state.read_status_receipt(plan)
        if final["status"] not in allowed_statuses or final["package_claim"] != claim:
            raise StagePlanError("stage package status changed during consumption")
        package_claim.require_packaging_handoff(plan, claim, final)
        if final["status"] == "PACKAGED" and final["stage_receipt"] != receipt_binding:
            raise StagePlanError("PACKAGED status does not bind the stage receipt")
        authority.require_unchanged()
        return receipt


def _require_transaction_payloads(
    executed: ExecutedStage,
    claim: dict[str, Any],
    outcome: outcomes.StageOutcome,
    authority: _PayloadAuthority,
) -> dict[str, dict[str, Any]]:
    if authority.json("package_claim.json") != claim:
        raise StagePlanError("frozen package claim differs from its lifecycle claim")
    bindings: dict[str, dict[str, Any]] = {"package_claim": authority.binding("package_claim.json")}
    paired_artifacts: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for paired in outcome.paired_passes:
        relative = f"passes/{paired['pass_id']}.json"
        if authority.json(relative) != paired:
            raise StagePlanError("frozen paired pass differs from the canonical outcome")
        binding = authority.binding(relative)
        bindings[f"pass:{paired['pass_id']}"] = binding
        paired_artifacts.append((binding, paired))
    if authority.json("outcome.json") != outcome.receipt:
        raise StagePlanError("frozen outcome differs from the canonical rig receipt")
    bindings["outcome"] = authority.binding("outcome.json")
    stage = executed.plan["spec"]["stage"]
    if stage == "aa":
        expected = authorization_package.build_aa_authorization(
            bindings["outcome"],
            outcome.receipt,
            paired_artifacts=paired_artifacts,
        )
        if authority.json("authorization.json") != expected:
            raise StagePlanError("frozen A/A authorization differs from its outcome")
        bindings["authorization"] = authority.binding("authorization.json")
    elif stage in {"anchor", "race"}:
        preregistration, expected = _expected_preregistration_authorization(
            executed,
            outcome,
            source_binding=authority.binding("preregistration.json"),
            gate_binding=bindings["outcome"],
            template_binding=claim["preregistration_template"],
        )
        if authority.json("preregistration.json") != preregistration:
            raise StagePlanError("frozen preregistration differs from its stage outcome")
        if authority.json("authorization.json") != expected:
            raise StagePlanError("frozen preregistration authorization differs from its source")
        bindings["preregistration"] = authority.binding("preregistration.json")
        bindings["authorization"] = authority.binding("authorization.json")
    return bindings


def _require_resume_template(
    plan: dict[str, Any],
    claim: dict[str, Any],
    path: Path | None,
) -> None:
    if claim["preregistration_template"] != package_claim.expected_template(plan, path):
        raise StagePlanError("preregistration template changed during resume")


def _start_or_resume(
    plan: dict[str, Any],
    live: dict[str, Any],
    *,
    official_packages_root: Path,
    preregistration_template: Path | None,
) -> tuple[
    ExecutedStage,
    tuple[outcomes.OfficialArm, ...],
    dict[str, Any],
    dict[str, Any],
]:
    if live["status"] == "EXECUTED":
        executed = require_executed_stage(plan)
        official_arms = package_claim.require_official_arms(
            executed,
            official_packages_root=official_packages_root,
        )
        claim = package_claim.build_package_claim(
            executed,
            official_arms,
            official_packages_root=official_packages_root,
            template=package_claim.expected_template(plan, preregistration_template),
        )
        ledger = package_claim.ledger(executed.status_receipt)
        state.write_status(
            plan,
            status="PACKAGING",
            package_claim=claim,
            executed_status_artifact=dict(executed.control_artifacts)["runner_status"],
            **ledger,
        )
        return executed, official_arms, claim, ledger
    if live["status"] != "PACKAGING":
        raise StagePlanError("release stage is not package-resumable")
    claim = outcomes.require_package_claim(
        plan,
        live["package_claim"],
        official_packages_root=official_packages_root,
    )
    _require_resume_template(plan, claim, preregistration_template)
    executed = package_claim.require_packaging_handoff(plan, claim, live)
    official_arms = outcomes.require_official_arms(
        executed,
        packages_root=official_packages_root,
        expected=claim["official_arms"],
        packaging_status=live,
    )
    return executed, official_arms, claim, package_claim.ledger(claim["executed_status"])


def _publish_transaction(
    plan: dict[str, Any],
    executed: ExecutedStage,
    official_arms: tuple[outcomes.OfficialArm, ...],
    claim: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    outcome = outcomes.build_stage_outcome(
        executed,
        packages_root=Path(claim["official_packages_root"]),
        official_arms=official_arms,
    )
    output_root = Path(plan["output_root"])
    packages = output_root / "packages"
    if not packages.exists():
        with stage_transaction.open_pending_transaction(
            output_root,
            prefix=_PENDING_PREFIX,
        ) as transaction:
            pending_bindings = _outcome_artifacts(
                executed,
                outcome,
                transaction,
                template_binding=claim["preregistration_template"],
            )
            transaction.write_json("package_claim.json", claim)
            pending_bindings = {
                "package_claim": transaction.binding("package_claim.json", public=True),
                **pending_bindings,
            }
            expected_files, expected_directories = _expected_names(pending_bindings)
            if transaction.inventory() != (expected_files, expected_directories):
                raise StagePlanError("stage package transaction inventory is not exact")
            transaction.publish()
    with stage_transaction.open_published_transaction(output_root) as transaction:
        bindings = _require_transaction_payloads(executed, claim, outcome, transaction)
        expected_files, expected_directories = _expected_names(bindings)
        transaction.require_inventory(expected_files, expected_directories)
        transaction.finish_publication()
    with stage_io.open_frozen_package_authority(output_root) as authority:
        bindings = _require_transaction_payloads(executed, claim, outcome, authority)
        expected_files, expected_directories = _expected_names(bindings)
        authority.require_inventory(expected_files, expected_directories)
        authority.require_unchanged()
    receipt = _stage_receipt(
        executed,
        claim,
        bindings,
        outcome_status=outcome.receipt["status"],
    )
    binding = stage_receipt.publish(output_root, receipt)
    with stage_io.open_frozen_stage_authority(output_root) as authority:
        if authority.receipt_json() != receipt or authority.receipt_binding() != binding:
            raise StagePlanError("existing stage receipt differs from its package")
        authority.require_unchanged()
    return receipt, binding


def package_stage(
    plan: dict[str, Any],
    *,
    official_packages_root: Path,
    preregistration_template: Path | None = None,
) -> dict[str, Any]:
    """Publish and consume one immutable stage outcome transaction."""

    root = official_packages_root.expanduser().resolve()
    if root != official_packages_root:
        raise StagePlanError("official packages root must be canonical")
    output_root = Path(plan["output_root"])
    with state.stage_lock(output_root):
        live = state.read_status_receipt(plan)
        if live["status"] == "PACKAGED":
            claim = outcomes.require_package_claim(
                plan,
                live["package_claim"],
                official_packages_root=root,
            )
            _require_resume_template(plan, claim, preregistration_template)
            return _consume(plan, allowed_statuses={"PACKAGED"})
        trusted_ledger = package_claim.ledger(live)
        try:
            executed, official_arms, claim, ledger = _start_or_resume(
                plan,
                live,
                official_packages_root=root,
                preregistration_template=preregistration_template,
            )
            _receipt, receipt_binding = _publish_transaction(
                plan,
                executed,
                official_arms,
                claim,
            )
            _consume(plan, allowed_statuses={"PACKAGING"})
            state.write_status(
                plan,
                status="PACKAGED",
                package_claim=claim,
                executed_status_artifact=dict(executed.control_artifacts)["runner_status"],
                stage_receipt=receipt_binding,
                **ledger,
            )
            return _consume(plan, allowed_statuses={"PACKAGED"})
        except Exception as exc:
            state.write_status(
                plan,
                status="FAIL",
                failures=[
                    {
                        "phase": "packaging",
                        "error_type": type(exc).__name__,
                        "error": state.redact(exc, secrets=state.secret_values(plan)),
                    }
                ],
                **trusted_ledger,
            )
            raise


def require_packaged_stage(plan: dict[str, Any]) -> dict[str, Any]:
    """Consume the immutable package authority and finish on mutable status."""

    with state.stage_lock(Path(plan["output_root"])):
        return _consume(plan, allowed_statuses={"PACKAGED"})
