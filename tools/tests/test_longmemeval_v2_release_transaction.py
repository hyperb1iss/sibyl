from __future__ import annotations

import json
import os
import stat
from contextlib import nullcontext, suppress
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_authorization_package as authorization_package
from benchmarks import longmemeval_v2_release_io as release_io
from benchmarks import longmemeval_v2_release_outcomes as outcomes
from benchmarks import longmemeval_v2_release_package as package
from benchmarks import longmemeval_v2_release_package_claim as package_claim
from benchmarks import longmemeval_v2_release_stage_io as stage_io
from benchmarks import longmemeval_v2_release_stage_receipt as stage_receipt
from benchmarks import longmemeval_v2_release_stage_transaction as stage_transaction
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_handoff import ExecutedStage
from benchmarks.longmemeval_v2_release_inputs import StagePlanError, bind_artifact

EXECUTED_COST_USD = 0.5
MUTABLE_FILE_MODE = 0o600


@pytest.fixture(autouse=True)
def _clear_immutable_outputs(tmp_path: Path) -> Any:
    yield
    for current, _directories, files in os.walk(tmp_path):
        root = Path(current)
        with suppress(OSError):
            os.chflags(root, 0)
        with suppress(OSError):
            root.chmod(0o700)
        for name in files:
            path = root / name
            with suppress(OSError):
                os.chflags(path, 0)
            with suppress(OSError):
                path.chmod(0o600)


def _executed(output_root: Path, *, stage: str = "aa") -> ExecutedStage:
    return ExecutedStage(
        plan={
            "stage_plan_sha256": "sha256:" + "a" * 64,
            "output_root": str(output_root),
            "spec": {"stage": stage, "mode": "initial"},
        },
        runs=(),
        domains=(),
        status_receipt={
            "status": "EXECUTED",
            "max_workers": 4,
            "completed_domains": ["arm-a:web"],
            "resumed_domains": [],
            "actual_cost_usd": EXECUTED_COST_USD,
        },
        control_artifacts=(),
    )


def _live_status(status: str = "EXECUTED") -> dict[str, Any]:
    return {
        "status": status,
        "max_workers": 4,
        "completed_domains": ["arm-a:web"],
        "resumed_domains": [],
        "actual_cost_usd": EXECUTED_COST_USD,
        "package_claim": None,
    }


def test_pending_transaction_root_is_exactly_one_canonical_uuid(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    with stage_transaction.open_pending_transaction(
        root,
        prefix="packages.pending.",
    ) as transaction:
        pending = transaction.path

    assert pending.parent == root
    with stage_transaction.open_pending_transaction(
        root,
        prefix="packages.pending.",
    ) as transaction:
        assert transaction.path == pending
    (root / "packages.pending.not-a-uuid").mkdir()
    with pytest.raises(StagePlanError, match="multiple package transaction"):
        stage_transaction.open_pending_transaction(root, prefix="packages.pending.")


def test_json_binding_uses_future_public_path_without_leaking_staging(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    public = tmp_path / "packages" / "outcome.json"
    with stage_transaction.open_pending_transaction(
        root,
        prefix="packages.pending.",
    ) as transaction:
        transaction.write_json("outcome.json", {"status": "PASS"})
        binding = transaction.binding("outcome.json", public=True)
        physical = transaction.binding("outcome.json", public=False)

    assert binding["path"] == str(public)
    assert "packages.pending" not in json.dumps(binding)
    assert binding["sha256"] == physical["sha256"]


def test_aa_authorization_rebases_every_validated_source_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = tmp_path / "pending" / "outcome.json"
    paired = tmp_path / "pending" / "passes" / "aa-1.json"
    receipt.parent.mkdir()
    paired.parent.mkdir()
    receipt.write_text("{}\n", encoding="utf-8")
    paired.write_text("{}\n", encoding="utf-8")
    validated_pass = {
        "pass_id": "aa-1",
        "seed": 1301,
        "paired_pass_sha256": "sha256:" + "b" * 64,
    }
    monkeypatch.setattr(
        authorization_package.rig,
        "validate_aa_receipt",
        lambda _raw: {
            "status": "PASS",
            "aa_receipt_sha256": "sha256:" + "c" * 64,
            "stack": {},
            "arm_contract": {},
            "passes": [validated_pass],
        },
    )
    monkeypatch.setattr(
        authorization_package.rig,
        "validate_pass",
        lambda _raw: validated_pass,
    )
    monkeypatch.setattr(
        authorization_package.authorization,
        "require_aa_authorization",
        lambda raw: raw,
    )
    public_receipt = tmp_path / "packages" / "outcome.json"
    public_pass = tmp_path / "packages" / "passes" / "aa-1.json"

    projected = authorization_package.package_aa_authorization(
        receipt,
        paired_pass_paths=[paired],
        public_receipt_path=public_receipt,
        public_paired_pass_paths=[public_pass],
    )

    assert projected["source_receipt"]["path"] == str(public_receipt)
    assert projected["passes"][0]["paired_pass_artifact"]["path"] == str(public_pass)
    assert "pending" not in json.dumps(projected)


@pytest.mark.parametrize("kind", ["race", "render"])
def test_preregistration_authorization_rebases_only_after_pending_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    pending = tmp_path / "pending"
    pending.mkdir()
    preregistration = pending / "preregistration.json"
    gate = pending / "outcome.json"
    preregistration.write_text("{}\n", encoding="utf-8")
    gate.write_text("{}\n", encoding="utf-8")
    validated = {
        "preregistration_sha256": "sha256:" + "a" * 64,
        "stack": {},
        "seeds": [1, 2, 3],
        "aa_receipt_sha256": "sha256:" + "b" * 64,
        "aa_receipt": {"passes": []},
    }
    monkeypatch.setattr(
        authorization_package.rig,
        "validate_preregistration",
        lambda _raw, *, kind: validated,
    )
    monkeypatch.setattr(authorization_package.authorization, "contract_keys", lambda _kind: ())
    monkeypatch.setattr(authorization_package, "_package_policy", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        authorization_package,
        "_package_anchor_gate" if kind == "race" else "_package_race_gate",
        lambda path, _preregistration: {
            "kind": "anchor" if kind == "race" else "race",
            "source_receipt": bind_artifact(path, name="gate"),
            "receipt_sha256": "sha256:" + "c" * 64,
        },
    )
    validations: list[dict[str, Any]] = []

    def require(raw: dict[str, Any], *, kind: str) -> dict[str, Any]:
        validations.append(json.loads(json.dumps(raw)))
        return raw

    monkeypatch.setattr(
        authorization_package.authorization,
        "require_preregistration_authorization",
        require,
    )
    public_preregistration = tmp_path / "packages" / "preregistration.json"
    public_gate = tmp_path / "packages" / "outcome.json"

    projected = authorization_package.package_preregistration_authorization(
        preregistration,
        kind=kind,
        gate_receipt_path=gate,
        public_preregistration_path=public_preregistration,
        public_gate_receipt_path=public_gate,
    )

    assert validations[0]["source_preregistration"]["path"] == str(preregistration)
    assert validations[0]["gate"]["source_receipt"]["path"] == str(gate)
    assert projected["source_preregistration"]["path"] == str(public_preregistration)
    assert projected["gate"]["source_receipt"]["path"] == str(public_gate)
    assert len(validations) == 1


def test_outcome_transaction_binds_only_final_public_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    executed = _executed(output_root)
    outcome = outcomes.StageOutcome(
        official_arms=(),
        paired_passes=({"pass_id": "aa-1"},),
        receipt={"status": "PASS"},
    )
    captured: dict[str, Any] = {}

    def build(
        receipt_binding: dict[str, Any],
        _receipt: dict[str, Any],
        *,
        paired_artifacts: list[tuple[dict[str, Any], dict[str, Any]]],
    ) -> dict[str, Any]:
        captured.update(
            receipt=receipt_binding,
            paired=paired_artifacts,
        )
        return {"kind": "aa"}

    def rebase(
        raw: dict[str, Any],
        *,
        public_receipt_path: Path,
        public_paired_pass_paths: list[Path],
    ) -> dict[str, Any]:
        captured.update(
            public_receipt=public_receipt_path,
            public_paired=public_paired_pass_paths,
        )
        return raw

    monkeypatch.setattr(authorization_package, "build_aa_authorization", build)
    monkeypatch.setattr(authorization_package, "rebase_aa_authorization", rebase)
    with stage_transaction.open_pending_transaction(
        output_root,
        prefix="packages.pending.",
    ) as transaction:
        pending = transaction.path
        bindings = package._outcome_artifacts(
            executed,
            outcome,
            transaction,
            template_binding=None,
        )

    assert captured["receipt"]["path"] == str(pending / "outcome.json")
    assert captured["public_receipt"] == output_root / "packages" / "outcome.json"
    assert captured["public_paired"] == [output_root / "packages" / "passes" / "aa-1.json"]
    assert all("packages.pending" not in binding["path"] for binding in bindings.values())


@pytest.mark.parametrize("stage", ["anchor", "render"])
def test_zero_pass_outcome_never_creates_an_empty_passes_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    executed = _executed(output_root, stage=stage)
    outcome = outcomes.StageOutcome(
        official_arms=(),
        paired_passes=(),
        receipt={"status": "PASS" if stage == "anchor" else "NOT_APPLICABLE"},
    )
    template_binding = None
    if stage == "anchor":
        template = tmp_path / "template.json"
        template.write_text("{}\n", encoding="utf-8")
        template_binding = bind_artifact(template, name="template")
        monkeypatch.setattr(outcomes, "require_bound_aa_receipt", lambda _executed: {})
        monkeypatch.setattr(outcomes, "issue_preregistration", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(
            authorization_package,
            "build_preregistration_gate",
            lambda *_args, **_kwargs: {},
        )
        monkeypatch.setattr(
            authorization_package,
            "build_preregistration_authorization",
            lambda *_args, **_kwargs: {},
        )
        monkeypatch.setattr(
            authorization_package,
            "rebase_preregistration_authorization",
            lambda *_args, **_kwargs: {"kind": "race"},
        )

    with stage_transaction.open_pending_transaction(
        output_root,
        prefix="packages.pending.",
    ) as transaction:
        pending = transaction.path
        package._outcome_artifacts(
            executed,
            outcome,
            transaction,
            template_binding=template_binding,
        )

    assert not (pending / "passes").exists()


def test_pending_transaction_never_writes_through_descendant_symlink(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    escaped = tmp_path / "escaped"
    escaped.mkdir()

    with stage_transaction.open_pending_transaction(
        output_root,
        prefix="packages.pending.",
    ) as transaction:
        (transaction.path / "passes").symlink_to(escaped, target_is_directory=True)
        with pytest.raises(StagePlanError, match=r"safe|identity"):
            transaction.write_json("passes/aa-1.json", {"pass_id": "aa-1"})

    assert not (escaped / "aa-1.json").exists()


def test_pending_write_interrupt_removes_owned_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    real_write = release_io.os.write

    with stage_transaction.open_pending_transaction(
        output_root,
        prefix="packages.pending.",
    ) as transaction:
        monkeypatch.setattr(
            release_io.os,
            "write",
            lambda *_args: (_ for _ in ()).throw(KeyboardInterrupt),
        )
        with pytest.raises(KeyboardInterrupt):
            transaction.write_json("outcome.json", {"status": "PASS"})
        assert not any(entry.name.endswith(".tmp") for entry in transaction.path.iterdir())
        monkeypatch.setattr(release_io.os, "write", real_write)
        transaction.write_json("outcome.json", {"status": "PASS"})
        transaction.require_inventory({"outcome.json"}, set())


def test_status_write_interrupt_preserves_status_without_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    plan = {
        "stage_plan_sha256": "sha256:" + "a" * 64,
        "output_root": str(output_root),
    }
    state.write_status(
        plan,
        status="EXECUTED",
        max_workers=4,
        completed=["arm-a:web"],
        cost=EXECUTED_COST_USD,
    )
    real_fsync = release_io.os.fsync

    def interrupt_regular(descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise KeyboardInterrupt
        real_fsync(descriptor)

    monkeypatch.setattr(release_io.os, "fsync", interrupt_regular)
    with pytest.raises(KeyboardInterrupt):
        state.write_status(
            plan,
            status="PACKAGING",
            max_workers=4,
            completed=["arm-a:web"],
            cost=EXECUTED_COST_USD,
        )

    assert state.read_status_receipt(plan)["status"] == "EXECUTED"
    assert not any(entry.name.endswith(".tmp") for entry in output_root.iterdir())


def test_public_package_root_freeze_interrupt_is_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    real_freeze = stage_transaction.package_root.freeze_descriptor

    def interrupt_root(
        descriptor: int,
        *,
        mode: int,
        name: str,
    ) -> None:
        if name == "stage package root":
            raise KeyboardInterrupt
        real_freeze(descriptor, mode=mode, name=name)

    transaction = stage_transaction.open_pending_transaction(
        output_root,
        prefix="packages.pending.",
    )
    try:
        transaction.write_json("outcome.json", {"status": "PASS"})
        monkeypatch.setattr(
            stage_transaction.package_root,
            "freeze_descriptor",
            interrupt_root,
        )
        with pytest.raises(KeyboardInterrupt):
            transaction.publish()
    finally:
        transaction.close()

    assert (output_root / "packages" / "outcome.json").is_file()
    assert not list(output_root.glob("packages.pending.*"))
    monkeypatch.setattr(stage_transaction.package_root, "freeze_descriptor", real_freeze)
    with stage_transaction.open_published_transaction(output_root) as recovered:
        assert recovered.json("outcome.json") == {"status": "PASS"}
        recovered.require_inventory({"outcome.json"}, set())
        recovered.finish_publication()
    with stage_io.open_frozen_package_authority(output_root) as authority:
        authority.require_inventory({"outcome.json"}, set())
        authority.require_unchanged()


def test_stage_receipt_freeze_interrupt_reuses_exact_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    payload = {"status": "PASS"}
    real_freeze = stage_receipt.package_root.freeze_descriptor

    def interrupt_receipt(
        _descriptor: int,
        *,
        mode: int,
        name: str,
    ) -> None:
        assert mode == stage_receipt.FILE_MODE
        assert name == "stage receipt authority"
        raise KeyboardInterrupt

    monkeypatch.setattr(stage_receipt.package_root, "freeze_descriptor", interrupt_receipt)
    with pytest.raises(KeyboardInterrupt):
        stage_receipt.publish(output_root, payload)
    assert (output_root / "stage_receipt.json").is_file()
    monkeypatch.setattr(stage_receipt.package_root, "freeze_descriptor", real_freeze)
    binding = stage_receipt.publish(output_root, payload)
    assert binding == bind_artifact(
        output_root / "stage_receipt.json",
        name="stage receipt",
    )


def test_stage_receipt_cleanup_never_closes_reused_foreign_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    victim_path = tmp_path / "victim"
    victim_path.write_text("alive", encoding="utf-8")
    victim: int | None = None

    def reuse_receipt_descriptor(
        descriptor: int,
        *,
        mode: int,
        name: str,
    ) -> None:
        nonlocal victim
        assert mode == stage_receipt.FILE_MODE
        assert name == "stage receipt authority"
        os.close(descriptor)
        victim = os.open(victim_path, os.O_RDONLY)
        assert victim == descriptor
        raise KeyboardInterrupt

    monkeypatch.setattr(
        stage_receipt.package_root,
        "freeze_descriptor",
        reuse_receipt_descriptor,
    )
    with pytest.raises(KeyboardInterrupt):
        stage_receipt.publish(output_root, {"status": "PASS"})
    assert victim is not None
    assert os.read(victim, 5) == b"alive"
    os.close(victim)


def test_stage_directory_open_interrupt_closes_partial_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[int] = []
    real_open = stage_io.os.open

    def record_open(*args: Any, **kwargs: Any) -> int:
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    monkeypatch.setattr(stage_io.os, "open", record_open)
    monkeypatch.setattr(
        stage_io,
        "snapshot_descriptor",
        lambda _descriptor: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    with pytest.raises(KeyboardInterrupt):
        stage_io.open_directory(tmp_path, name="test directory")
    assert opened
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[-1])


def test_stage_child_open_interrupt_closes_partial_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    opened: list[int] = []
    real_open = stage_io.package_root._open_child_directory

    def record_open(descriptor: int, name: str) -> int:
        child_fd = real_open(descriptor, name)
        opened.append(child_fd)
        return child_fd

    monkeypatch.setattr(stage_io.package_root, "_open_child_directory", record_open)
    monkeypatch.setattr(
        stage_io,
        "snapshot_descriptor",
        lambda _descriptor: (_ for _ in ()).throw(KeyboardInterrupt),
    )
    try:
        with pytest.raises(KeyboardInterrupt):
            stage_io.open_child_directory(parent_fd, "child")
    finally:
        os.close(parent_fd)
    assert opened
    with pytest.raises(OSError, match="Bad file descriptor"):
        os.fstat(opened[-1])


@pytest.mark.parametrize("relative", ["packages/outcome.json", "stage_receipt.json"])
def test_frozen_stage_authority_retains_files_through_final_validation(
    tmp_path: Path,
    relative: str,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    with stage_transaction.open_pending_transaction(
        output_root,
        prefix="packages.pending.",
    ) as transaction:
        transaction.write_json("outcome.json", {"status": "PASS"})
        transaction.publish()
    stage_receipt.publish(output_root, {"status": "PASS"})

    target = output_root / relative
    with stage_io.open_frozen_stage_authority(output_root) as authority:
        os.chflags(target, 0)
        target.chmod(MUTABLE_FILE_MODE)
        with pytest.raises(StagePlanError, match=r"changed|immutable|mode"):
            authority.require_unchanged()


def test_packaging_handoff_revalidates_preregistration_template_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = tmp_path / "template.json"
    template.write_text("{}\n", encoding="utf-8")
    template_binding = bind_artifact(template, name="template")
    original = {
        "max_workers": 4,
        "completed_domains": [],
        "resumed_domains": [],
        "actual_cost_usd": 0.0,
    }
    controls = {"runner_status": {"path": "runner_status.json"}}
    claim = {
        "executed_status": original,
        "control_artifacts": controls,
        "domains": [],
        "preregistration_template": template_binding,
    }
    live = {
        **original,
        "package_claim": claim,
        "executed_status_artifact": controls["runner_status"],
    }
    monkeypatch.setattr(package_claim.state, "validate_status_receipt", lambda *_args: original)
    monkeypatch.setattr(package_claim.state, "require_claimed_stage_plan", lambda _plan: [])
    monkeypatch.setattr(package_claim.state, "secret_values", lambda _plan: ())
    monkeypatch.setattr(package_claim.runner, "require_planning_barrier", lambda *_a, **_k: {})
    monkeypatch.setattr(package_claim.runner, "require_arm_costs", lambda *_a, **_k: None)

    def mutate_template(*_args: Any, **_kwargs: Any) -> None:
        template.write_text('{"changed": true}\n', encoding="utf-8")

    monkeypatch.setattr(package_claim.runner, "require_executed_status", mutate_template)

    with pytest.raises(StagePlanError, match="preregistration template"):
        package_claim.require_packaging_handoff({}, claim, live)


def test_frozen_publication_rejects_foreign_and_mutable_entries(tmp_path: Path) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    with stage_transaction.open_pending_transaction(
        output_root,
        prefix="packages.pending.",
    ) as transaction:
        transaction.write_json("outcome.json", {})
        transaction.write_json("passes/aa-1.json", {})
        transaction.publish()
    with stage_io.open_frozen_package_authority(output_root) as authority:
        authority.require_inventory(
            {"outcome.json", "passes/aa-1.json"},
            {"passes"},
        )
        authority.require_unchanged()

    with pytest.raises(OSError, match="Operation not permitted"):
        (output_root / "packages" / "foreign.json").write_text("{}\n", encoding="utf-8")


def test_packaging_status_and_stage_receipt_are_digest_validated(tmp_path: Path) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    plan = {"stage_plan_sha256": "sha256:" + "a" * 64, "output_root": str(output_root)}
    claim = {"sealed": True}
    executed_status_path = output_root / "executed-status.json"
    executed_status_path.write_text("{}\n", encoding="utf-8")
    executed_status = bind_artifact(executed_status_path, name="executed status")
    state.write_status(
        plan,
        status="PACKAGING",
        max_workers=4,
        completed=["arm-a:web"],
        resumed=[],
        cost=EXECUTED_COST_USD,
        package_claim=claim,
        executed_status_artifact=executed_status,
    )
    assert state.read_status_receipt(plan)["package_claim"] == claim

    receipt_path = output_root / "stage_receipt.json"
    receipt_path.write_text("{}\n", encoding="utf-8")
    receipt = bind_artifact(receipt_path, name="stage receipt")
    state.write_status(
        plan,
        status="PACKAGED",
        max_workers=4,
        completed=["arm-a:web"],
        resumed=[],
        cost=EXECUTED_COST_USD,
        package_claim=claim,
        executed_status_artifact=executed_status,
        stage_receipt=receipt,
    )
    assert state.read_status_receipt(plan)["stage_receipt"] == receipt


def test_package_failure_preserves_last_trusted_executed_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    plan = {"output_root": str(output_root)}
    live = _live_status()
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(state, "stage_lock", lambda _root: nullcontext())
    monkeypatch.setattr(state, "read_status_receipt", lambda _plan: live)
    monkeypatch.setattr(state, "write_status", lambda _plan, **payload: writes.append(payload))
    monkeypatch.setattr(state, "secret_values", lambda _plan: ())
    monkeypatch.setattr(
        package,
        "_start_or_resume",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider secret")),
    )

    with pytest.raises(RuntimeError, match="provider secret"):
        package.package_stage(plan, official_packages_root=(tmp_path / "official").resolve())

    assert writes[-1]["status"] == "FAIL"
    assert writes[-1]["completed"] == ["arm-a:web"]
    assert writes[-1]["cost"] == EXECUTED_COST_USD


def test_package_interrupt_leaves_resume_authority_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    plan = {"output_root": str(output_root)}
    writes: list[dict[str, Any]] = []
    monkeypatch.setattr(state, "stage_lock", lambda _root: nullcontext())
    monkeypatch.setattr(state, "read_status_receipt", lambda _plan: _live_status())
    monkeypatch.setattr(state, "write_status", lambda _plan, **payload: writes.append(payload))
    monkeypatch.setattr(
        package,
        "_start_or_resume",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        package.package_stage(plan, official_packages_root=(tmp_path / "official").resolve())
    assert writes == []


def test_packaged_reentry_requires_the_claimed_official_root_and_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paid"
    output_root.mkdir()
    plan = {"output_root": str(output_root)}
    live = {**_live_status("PACKAGED"), "package_claim": {"sealed": True}}
    monkeypatch.setattr(state, "stage_lock", lambda _root: nullcontext())
    monkeypatch.setattr(state, "read_status_receipt", lambda _plan: live)
    monkeypatch.setattr(
        outcomes,
        "require_package_claim",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StagePlanError("official root changed")),
    )

    with pytest.raises(StagePlanError, match="official root changed"):
        package.package_stage(plan, official_packages_root=(tmp_path / "wrong").resolve())


def test_immutable_flag_is_required_even_when_publication_bytes_match(tmp_path: Path) -> None:
    output_root = tmp_path / "paid"
    root = output_root / "packages"
    root.mkdir(parents=True)
    (root / "outcome.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(StagePlanError, match="immutable"):
        stage_io.open_frozen_package_authority(output_root)
