from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_release_evidence as evidence
from benchmarks import longmemeval_v2_release_runner as runner
from benchmarks import longmemeval_v2_release_state as state
from benchmarks.longmemeval_v2_release_inputs import StagePlanError, bind_artifact
from tools.bench import longmemeval_v2_rig as rig
from tools.bench.longmemeval_v2_artifact_bridge import BridgeInputError

EXPECTED_DOMAIN_EXECUTIONS = 4
EXPECTED_WAVE_DOMAINS = 2
EXPECTED_TOTAL_COST_USD = 2.0
EXPECTED_REDACTIONS = 4
EXPECTED_ATOMIC_REPLACEMENTS = 3
TEMPORARY_WORKER_CAP = 4
SECOND_INVOCATION = 2


def _domain_run(root: Path, arm_id: str, domain: str) -> dict[str, Any]:
    planning = root / "planning" / arm_id / domain
    output = root / "runs" / arm_id / domain
    base = ["fake-official", "--domain", domain, "--output-dir"]
    return {
        "domain": domain,
        "planning_output_dir": str(planning),
        "output_dir": str(output),
        "planning_memory_dir": None,
        "execution_memory_dir": None,
        "plan_command": [*base, str(planning), "--plan-only"],
        "run_command": [*base, str(output)],
    }


def _run(root: Path, arm_id: str, index: int) -> dict[str, Any]:
    return {
        "arm_id": arm_id,
        "pass_id": f"pass-{index}",
        "pass_kind": "paired",
        "pass_index": index,
        "seed": 1000 + index,
        "memory_source": "build_baseline" if index == 0 else "baseline",
        "manifest": {},
        "execution": {
            "schema_version": rig.EXECUTION_IDENTITY_SCHEMA_VERSION,
            "kind": "local",
            "repository": "hyperb1iss/sibyl",
            "ref": "refs/heads/main",
            "sha": "a" * 40,
            "run_id": f"00000000-0000-0000-0000-{index + 1:012d}",
            "run_attempt": 1,
        },
        "domains": {domain: _domain_run(root, arm_id, domain) for domain in ("web", "enterprise")},
        "spend_reservation": {
            "currency": "USD",
            "max_spend_usd_per_domain": 3.0,
            "max_spend_usd_total": 6.0,
            "enforcement": "official plan-only reservation before provider calls",
        },
    }


@pytest.fixture
def stage_plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    root = tmp_path / "release-output"
    runs = [_run(root, "arm-a", 0), _run(root, "arm-b", 1)]
    raw = {
        "stage_plan_sha256": "sha256:" + "a" * 64,
        "source_identity": {
            "repository": "hyperb1iss/sibyl",
            "ref": "refs/heads/main",
            "sha": "a" * 40,
        },
        "output_root": str(root),
        "max_workers_cap": 4,
        "spec": {
            "runtime": {
                "reader_api_key_env": "OPENROUTER_API_KEY",
                "evaluator_api_key_env": "OPENAI_API_KEY",
            }
        },
        "runs": runs,
        "waves": [["arm-a"], ["arm-b"]],
    }
    monkeypatch.setattr(state.release_plan, "require_stage_plan", lambda _raw: runs)
    monkeypatch.setattr(
        state.release_plan,
        "_require_stage_plan",
        lambda _raw, *, check_checkout, claimed_root: runs,
    )

    def planning_binding(_plan: dict[str, Any], run: dict[str, Any], domain: str) -> dict[str, Any]:
        path = (
            Path(run["domains"][domain]["planning_output_dir"])
            / "longmemeval_v2_official_plan.json"
        )
        return bind_artifact(path, name="test planning output")

    monkeypatch.setattr(runner.evidence, "require_planning_output", planning_binding)
    return raw


def _output_dir(command: list[str]) -> Path:
    return Path(command[command.index("--output-dir") + 1])


def _successful_invoke(events: list[tuple[str, str]]) -> Any:
    lock = threading.Lock()

    def invoke(
        command: list[str],
        *,
        log_path: Path,
        secrets: tuple[str, ...],
    ) -> int:
        del secrets
        phase = "plan" if "--plan-only" in command else "paid"
        with lock:
            events.append((phase, command[command.index("--domain") + 1]))
        state.append_log(
            log_path,
            {
                "event": "start",
                "recorded_at": state.now(),
                "command_sha256": rig.canonical_sha256(command),
            },
        )
        _output_dir(command).mkdir(parents=True, exist_ok=True)
        if phase == "plan":
            (_output_dir(command) / "longmemeval_v2_official_plan.json").write_text(
                "{}\n", encoding="utf-8"
            )
        state.append_log(
            log_path,
            {"event": "exit", "recorded_at": state.now(), "returncode": 0},
        )
        return 0

    return invoke


def _successful_completion(
    _plan: dict[str, Any], run: dict[str, Any], domain: str
) -> tuple[float, dict[str, Any]]:
    return 0.5, {}


def test_runner_preflights_every_domain_then_executes_every_wave(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_invoke_command", _successful_invoke(events))
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    result = runner.run_stage_plan(stage_plan, max_workers=4)

    assert result["status"] == "EXECUTED"
    assert [phase for phase, _domain in events[:4]] == ["plan"] * 4
    paid = [domain for phase, domain in events if phase == "paid"]
    assert len(paid) == EXPECTED_DOMAIN_EXECUTIONS
    assert sorted(paid) == ["enterprise", "enterprise", "web", "web"]
    assert result["actual_cost_usd"] == EXPECTED_TOTAL_COST_USD


def test_runner_enforces_temporary_worker_cap_and_declared_wave_width(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    widths: list[int] = []
    real_executor = runner.ThreadPoolExecutor

    def recording_executor(*, max_workers: int) -> Any:
        widths.append(max_workers)
        return real_executor(max_workers=max_workers)

    monkeypatch.setattr(runner, "ThreadPoolExecutor", recording_executor)
    monkeypatch.setattr(runner, "_invoke_command", _successful_invoke(events))
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    with pytest.raises(StagePlanError, match=r"temporary 1\.\.4"):
        runner.run_stage_plan(deepcopy(stage_plan), max_workers=0)
    with pytest.raises(StagePlanError, match=r"temporary 1\.\.4"):
        runner.run_stage_plan(deepcopy(stage_plan), max_workers=5)
    result = runner.run_stage_plan(stage_plan, max_workers=3)

    assert result["status"] == "EXECUTED"
    assert widths == [2, 2]
    assert all(width <= TEMPORARY_WORKER_CAP for width in widths)


def test_runner_finishes_active_wave_but_never_starts_later_wave_after_failure(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_invoke_command", _successful_invoke(events))

    def completion(
        _plan: dict[str, Any], run: dict[str, Any], domain: str
    ) -> tuple[float, dict[str, Any]]:
        if run["arm_id"] == "arm-a" and domain == "web":
            raise StagePlanError("hostile wave failure")
        return 0.5, {}

    monkeypatch.setattr(runner.evidence, "require_completed_domain", completion)

    result = runner.run_stage_plan(stage_plan)

    assert result["status"] == "FAIL"
    paid_paths = [event for event in events if event[0] == "paid"]
    assert len(paid_paths) == EXPECTED_WAVE_DOMAINS
    assert not any("arm-b" in str(path) for path in Path(stage_plan["output_root"]).glob("runs/*"))


def test_dependent_wave_attestations_form_a_barrier_before_paid_workers(
    stage_plan: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts: dict[str, int] = {}
    events: list[tuple[str, str]] = []
    for domain in ("web", "enterprise"):
        domain_run = stage_plan["runs"][1]["domains"][domain]
        memory = tmp_path / f"saved-{domain}"
        memory.mkdir()
        (memory / "memory_manifest.json").write_text("{}\n", encoding="utf-8")
        domain_run["planning_memory_dir"] = str(memory)
        domain_run["plan_command"].extend(["--checkpoint-dir", str(memory)])

    def invoke(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        del secrets
        output = str(_output_dir(command))
        phase = "plan" if "--plan-only" in command else "paid"
        counts[output] = counts.get(output, 0) + 1
        events.append((phase, output))
        state.append_log(
            log_path,
            {
                "event": "start",
                "recorded_at": state.now(),
                "command_sha256": rig.canonical_sha256(command),
            },
        )
        _output_dir(command).mkdir(parents=True, exist_ok=True)
        if phase == "plan":
            (_output_dir(command) / "longmemeval_v2_official_plan.json").write_text(
                "{}\n", encoding="utf-8"
            )
        failed = (
            phase == "plan" and "arm-b/enterprise" in output and counts[output] == SECOND_INVOCATION
        )
        returncode = int(failed)
        state.append_log(
            log_path,
            {"event": "exit", "recorded_at": state.now(), "returncode": returncode},
        )
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", invoke)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    with pytest.raises(StagePlanError, match="attestation failed"):
        runner.run_stage_plan(stage_plan)

    assert not any(phase == "paid" and "arm-b" in output for phase, output in events)
    status = json.loads(
        (Path(stage_plan["output_root"]) / "runner_status.json").read_text(encoding="utf-8")
    )
    assert status["completed_domains"] == ["arm-a:enterprise", "arm-a:web"]
    assert status["actual_cost_usd"] == 1.0


def test_bridge_integrity_failure_preserves_last_trusted_paid_ledger(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)

    def completion(
        _plan: dict[str, Any], run: dict[str, Any], domain: str
    ) -> tuple[float, dict[str, Any]]:
        marker = Path(run["domains"][domain]["output_dir"]) / "aggregated_metrics.json"
        if not marker.exists():
            marker.write_text("sealed\n", encoding="utf-8")
        if marker.read_text(encoding="utf-8") != "sealed\n":
            raise BridgeInputError("canonical prior bridge artifact drifted")
        return 0.5, {"marker": bind_artifact(marker, name="test marker")}

    def drift_prior(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        returncode = invoke(command, log_path=log_path, secrets=secrets)
        if "--plan-only" not in command and "arm-b/web" in str(_output_dir(command)):
            prior = Path(stage_plan["runs"][0]["domains"]["web"]["output_dir"])
            (prior / "aggregated_metrics.json").write_text("tampered\n", encoding="utf-8")
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", drift_prior)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", completion)

    with pytest.raises(BridgeInputError, match="bridge artifact drifted"):
        runner.run_stage_plan(stage_plan)

    status = state.read_status_receipt(stage_plan)
    assert status["status"] == "FAIL"
    assert status["completed_domains"] == ["arm-a:enterprise", "arm-a:web"]
    assert status["actual_cost_usd"] == 1.0
    assert "bridge artifact drifted" in status["failures"][0]["error"]


def test_oserror_during_integrity_check_preserves_last_trusted_paid_ledger(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)
    storage_failed = threading.Event()

    def completion(
        _plan: dict[str, Any], run: dict[str, Any], domain: str
    ) -> tuple[float, dict[str, Any]]:
        if storage_failed.is_set() and run["arm_id"] == "arm-a" and domain == "web":
            raise OSError("evidence storage unavailable")
        return 0.5, {}

    def fail_storage(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        returncode = invoke(command, log_path=log_path, secrets=secrets)
        if "--plan-only" not in command and "arm-b/web" in str(_output_dir(command)):
            storage_failed.set()
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", fail_storage)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", completion)

    with pytest.raises(OSError, match="storage unavailable"):
        runner.run_stage_plan(stage_plan)

    status = state.read_status_receipt(stage_plan)
    assert status["status"] == "FAIL"
    assert status["completed_domains"] == ["arm-a:enterprise", "arm-a:web"]
    assert status["actual_cost_usd"] == 1.0


def test_malformed_prior_exit_writes_fail_with_last_trusted_paid_ledger(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)

    def corrupt_prior(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        returncode = invoke(command, log_path=log_path, secrets=secrets)
        if "--plan-only" not in command and "arm-b/web" in str(_output_dir(command)):
            prior = Path(stage_plan["output_root"]) / "exits" / "arm-a" / "web.json"
            prior.write_text("not-json\n", encoding="utf-8")
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", corrupt_prior)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    with pytest.raises(StagePlanError, match="could not load JSON"):
        runner.run_stage_plan(stage_plan)

    status = state.read_status_receipt(stage_plan)
    assert status["status"] == "FAIL"
    assert status["completed_domains"] == ["arm-a:enterprise", "arm-a:web"]
    assert status["actual_cost_usd"] == 1.0


def test_zero_exit_without_official_receipt_fails_closed(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_invoke_command", _successful_invoke(events))

    result = runner.run_stage_plan(stage_plan)

    assert result["status"] == "FAIL"
    exit_receipt = json.loads(
        (Path(stage_plan["output_root"]) / "exits" / "arm-a" / "web.json").read_text()
    )
    assert exit_receipt["returncode"] == 0
    assert exit_receipt["status"] == "FAIL"
    assert "missing" in exit_receipt["error"]


def test_partial_domain_output_requires_a_fresh_root(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    state.claim_stage(stage_plan, max_workers=4)
    Path(stage_plan["runs"][0]["domains"]["web"]["output_dir"]).mkdir(parents=True)
    monkeypatch.setattr(runner, "_invoke_command", pytest.fail)

    with pytest.raises(StagePlanError, match="partial domain output"):
        runner.run_stage_plan(stage_plan)

    status = json.loads(
        (Path(stage_plan["output_root"]) / "runner_status.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "FAIL"


def test_resume_repeats_every_live_plan_only_attestation_before_paid_work(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    state.claim_stage(stage_plan, max_workers=4)
    state.write_status(stage_plan, status="RUNNING", max_workers=4)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_invoke_command", _successful_invoke(events))
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    result = runner.run_stage_plan(stage_plan)

    assert result["status"] == "EXECUTED"
    assert [phase for phase, _domain in events[:4]] == ["plan"] * 4
    assert [phase for phase, _domain in events[4:]] == ["paid"] * 4


def test_resume_rejects_resealed_stale_command_receipt(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_invoke_command", _successful_invoke(events))
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)
    assert runner.run_stage_plan(stage_plan)["status"] == "EXECUTED"
    exit_path = Path(stage_plan["output_root"]) / "exits" / "arm-a" / "web.json"
    receipt = json.loads(exit_path.read_text())
    receipt["command_sha256"] = rig.canonical_sha256(["stale-command"])
    unsigned = {key: value for key, value in receipt.items() if key != "exit_sha256"}
    receipt["exit_sha256"] = rig.canonical_sha256(unsigned)
    exit_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    with pytest.raises(StagePlanError, match="sealed run"):
        runner.run_stage_plan(stage_plan)


def test_resume_rejects_changed_completed_artifact(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_invoke_command", _successful_invoke(events))

    def completion(
        _plan: dict[str, Any], run: dict[str, Any], domain: str
    ) -> tuple[float, dict[str, Any]]:
        marker = Path(run["domains"][domain]["output_dir"]) / "aggregated_metrics.json"
        marker.write_text("sealed\n", encoding="utf-8") if not marker.exists() else None
        return 0.5, {"marker": bind_artifact(marker, name="test marker")}

    monkeypatch.setattr(runner.evidence, "require_completed_domain", completion)
    assert runner.run_stage_plan(stage_plan)["status"] == "EXECUTED"
    marker = Path(stage_plan["runs"][0]["domains"]["web"]["output_dir"]) / "aggregated_metrics.json"
    marker.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(StagePlanError, match="artifacts or cost changed"):
        runner.run_stage_plan(stage_plan)


def test_runner_rejects_actual_cost_over_sealed_arm_reservation(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_invoke_command", _successful_invoke(events))
    monkeypatch.setattr(
        runner.evidence,
        "require_completed_domain",
        lambda *_args: (3.1, {}),
    )

    with pytest.raises(StagePlanError, match="total reservation"):
        runner.run_stage_plan(stage_plan)

    status = json.loads(
        (Path(stage_plan["output_root"]) / "runner_status.json").read_text(encoding="utf-8")
    )
    assert status["status"] == "FAIL"


def test_command_log_redacts_env_and_inline_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "".join(("provider", "-secret-value"))

    class Process:
        stdout = iter(
            [
                f"Authorization: Bearer {secret}\n",
                f"api_key={secret}\n",
                f"raw {secret}\n",
            ]
        )

        @staticmethod
        def wait() -> int:
            return 0

    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    log_path = tmp_path / "runner.jsonl"

    assert runner._invoke_command(["fake"], log_path=log_path, secrets=(secret,)) == 0

    log = log_path.read_text(encoding="utf-8")
    assert secret not in log
    assert log.count("<redacted>") == EXPECTED_REDACTIONS


def test_redaction_hides_sensitive_urls_paths_and_dynamic_credential_envs(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    email_secret = "".join(("operator", "@example.invalid"))
    file_secret = "/".join(("", "private", "credentials", "release.json"))
    monkeypatch.setenv("SIBYL_RELEASE_EMAIL", email_secret)
    monkeypatch.setenv("SIBYL_RELEASE_FILE", file_secret)
    secrets = state.secret_values(stage_plan)
    raw = (
        "endpoint=https://user:pass@example.invalid/api?session=visible "
        f"credentials_path={file_secret} email={email_secret}"
    )

    redacted = state.redact(raw, secrets=secrets)

    assert "user:pass" not in redacted
    assert "session=visible" not in redacted
    assert file_secret not in redacted
    assert email_secret not in redacted


def test_claimed_tree_rejects_nested_foreign_and_symlinked_domain_artifacts(
    stage_plan: dict[str, Any], tmp_path: Path
) -> None:
    state.claim_stage(stage_plan, max_workers=4)
    runtime_dir = Path(stage_plan["runs"][0]["domains"]["web"]["output_dir"]) / "runtime_inputs"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "foreign.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(StagePlanError, match="unknown entries"):
        state.require_claimed_stage_plan(stage_plan)

    (runtime_dir / "foreign.json").unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}\n", encoding="utf-8")
    (runtime_dir / "questions.json").symlink_to(outside)
    with pytest.raises(StagePlanError, match="unsafe path"):
        state.require_claimed_stage_plan(stage_plan)


def test_completion_evidence_rejects_foreign_files_created_by_paid_command(
    stage_plan: dict[str, Any],
) -> None:
    run = stage_plan["runs"][0]
    output_dir = Path(run["domains"]["web"]["output_dir"])
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "foreign.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(StagePlanError, match="unknown entries"):
        evidence.require_completed_domain(stage_plan, run, "web")


def test_wave_rejects_foreign_root_output_before_complete(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)

    def foreign_invoke(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        returncode = invoke(command, log_path=log_path, secrets=secrets)
        if "--plan-only" not in command:
            foreign = Path(stage_plan["output_root"]) / "packages" / "arbitrary"
            foreign.mkdir(parents=True, exist_ok=True)
            (foreign / "foreign.bin").write_bytes(b"hostile")
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", foreign_invoke)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    with pytest.raises(StagePlanError, match="unknown entries"):
        runner.run_stage_plan(stage_plan)

    assert not list((Path(stage_plan["output_root"]) / "exits").rglob("*.json"))


def test_wave_root_audit_waits_for_concurrent_temporary_files(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)
    temporary_ready = threading.Event()
    peer_observed = threading.Event()
    temporary = Path(stage_plan["output_root"]) / ".provider-inflight.tmp"

    def concurrent_invoke(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        if "--plan-only" in command:
            return invoke(command, log_path=log_path, secrets=secrets)
        if "arm-a" not in str(_output_dir(command)):
            return invoke(command, log_path=log_path, secrets=secrets)
        domain = command[command.index("--domain") + 1]
        if domain == "web":
            temporary.write_text("in flight\n", encoding="utf-8")
            temporary_ready.set()
            assert peer_observed.wait(timeout=2)
            temporary.unlink()
        else:
            assert temporary_ready.wait(timeout=2)
            assert temporary.is_file()
            peer_observed.set()
        return invoke(command, log_path=log_path, secrets=secrets)

    monkeypatch.setattr(runner, "_invoke_command", concurrent_invoke)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    assert runner.run_stage_plan(stage_plan)["status"] == "EXECUTED"


def test_wave_revalidates_current_evidence_after_every_peer_quiesces(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    calls: dict[str, int] = {}
    web_staged = threading.Event()
    monkeypatch.setattr(runner, "_invoke_command", _successful_invoke(events))

    def completion(
        _plan: dict[str, Any], run: dict[str, Any], domain: str
    ) -> tuple[float, dict[str, Any]]:
        key = f"{run['arm_id']}:{domain}"
        calls[key] = calls.get(key, 0) + 1
        artifact = Path(run["domains"][domain]["output_dir"]) / "aggregated_metrics.json"
        artifact.write_text(f"{key}\n", encoding="utf-8") if not artifact.exists() else None
        if run["arm_id"] == "arm-a" and domain == "web" and calls[key] == 1:
            binding = bind_artifact(artifact, name="staged web artifact")
            web_staged.set()
            return 0.5, {"marker": binding}
        if run["arm_id"] == "arm-a" and domain == "enterprise" and calls[key] == 1:
            assert web_staged.wait(timeout=2)
            web_artifact = Path(run["domains"]["web"]["output_dir"])
            (web_artifact / "aggregated_metrics.json").write_text(
                "peer mutation\n", encoding="utf-8"
            )
        return 0.5, {"marker": bind_artifact(artifact, name="current artifact")}

    monkeypatch.setattr(runner.evidence, "require_completed_domain", completion)

    with pytest.raises(StagePlanError, match="current wave evidence changed"):
        runner.run_stage_plan(stage_plan)

    assert not list((Path(stage_plan["output_root"]) / "exits").rglob("*.json"))


def test_wave_rejects_planning_artifact_mutation_after_preflight(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)

    def mutate_planning(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        returncode = invoke(command, log_path=log_path, secrets=secrets)
        if "--plan-only" not in command and "arm-a/web" in str(_output_dir(command)):
            planning = Path(stage_plan["runs"][0]["domains"]["web"]["planning_output_dir"])
            (planning / "longmemeval_v2_official_plan.json").write_text(
                '{"selection_complete": false}\n', encoding="utf-8"
            )
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", mutate_planning)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    with pytest.raises(StagePlanError, match="changed sealed planning evidence"):
        runner.run_stage_plan(stage_plan)

    assert not list((Path(stage_plan["output_root"]) / "exits").rglob("*.json"))


def test_planning_selection_requires_exact_sealed_small_corpus() -> None:
    digest = "sha256:" + "a" * 64
    stage_plan = {
        "dataset": {
            "question_count_by_domain": {"web": 2},
            "question_ids_sha256_by_domain": {"web": digest},
        }
    }
    raw = {
        "selection_complete": True,
        "trajectory_path_exists": True,
        "question_count": 2,
        "official_question_count": 2,
        "selected_question_ids_sha256": digest,
        "official_question_ids_sha256": digest,
    }
    runner.evidence._require_full_small_selection(stage_plan, "web", raw)

    for field, value in (
        ("selection_complete", False),
        ("question_count", 1),
        ("selected_question_ids_sha256", "sha256:" + "0" * 64),
    ):
        drifted = {**raw, field: value}
        with pytest.raises(StagePlanError, match="sealed Small corpus"):
            runner.evidence._require_full_small_selection(stage_plan, "web", drifted)


@pytest.mark.parametrize("control_name", ["runner_claim.json", "runner_status.json"])
def test_wave_rejects_runner_control_overwrite_before_complete(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch, control_name: str
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)

    def corrupt_control(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        returncode = invoke(command, log_path=log_path, secrets=secrets)
        if "--plan-only" not in command:
            (Path(stage_plan["output_root"]) / control_name).write_text("{}\n", encoding="utf-8")
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", corrupt_control)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    with pytest.raises(StagePlanError):
        runner.run_stage_plan(stage_plan)

    assert not list((Path(stage_plan["output_root"]) / "exits").rglob("*.json"))


def test_later_wave_rejects_prior_exit_overwrite(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)

    def corrupt_prior_exit(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        returncode = invoke(command, log_path=log_path, secrets=secrets)
        if "--plan-only" not in command and "arm-b/web" in str(_output_dir(command)):
            prior = Path(stage_plan["output_root"]) / "exits" / "arm-a" / "web.json"
            prior.write_text("{}\n", encoding="utf-8")
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", corrupt_prior_exit)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    with pytest.raises(StagePlanError, match="completed domain exit"):
        runner.run_stage_plan(stage_plan)

    assert not (Path(stage_plan["output_root"]) / "exits" / "arm-b" / "web.json").exists()


def test_later_wave_rejects_resealed_prior_artifact_drift(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)

    def completion(
        _plan: dict[str, Any], run: dict[str, Any], domain: str
    ) -> tuple[float, dict[str, Any]]:
        artifact = Path(run["domains"][domain]["output_dir"]) / "aggregated_metrics.json"
        if not artifact.exists():
            artifact.write_text("sealed\n", encoding="utf-8")
        if artifact.read_text(encoding="utf-8") != "sealed\n":
            raise StagePlanError("canonical prior artifact drifted")
        return 0.5, {"marker": bind_artifact(artifact, name="test marker")}

    def reseal_prior_exit(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        returncode = invoke(command, log_path=log_path, secrets=secrets)
        if "--plan-only" not in command and "arm-b/web" in str(_output_dir(command)):
            artifact = Path(stage_plan["runs"][0]["domains"]["web"]["output_dir"])
            marker = artifact / "aggregated_metrics.json"
            marker.write_text("tampered\n", encoding="utf-8")
            exit_path = Path(stage_plan["output_root"]) / "exits" / "arm-a" / "web.json"
            receipt = json.loads(exit_path.read_text(encoding="utf-8"))
            receipt["artifacts"]["marker"] = bind_artifact(marker, name="test marker")
            unsigned = {key: value for key, value in receipt.items() if key != "exit_sha256"}
            receipt["exit_sha256"] = rig.canonical_sha256(unsigned)
            exit_path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", reseal_prior_exit)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", completion)

    with pytest.raises(StagePlanError, match="canonical prior artifact drifted"):
        runner.run_stage_plan(stage_plan)


@pytest.mark.parametrize("target", ["current", "prior"])
def test_wave_rejects_current_and_prior_run_log_overwrite(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    events: list[tuple[str, str]] = []
    invoke = _successful_invoke(events)

    def corrupt_log(command: list[str], *, log_path: Path, secrets: tuple[str, ...]) -> int:
        returncode = invoke(command, log_path=log_path, secrets=secrets)
        output_dir = str(_output_dir(command))
        if "--plan-only" not in command and (
            (target == "current" and "arm-a/web" in output_dir)
            or (target == "prior" and "arm-b/web" in output_dir)
        ):
            victim = (
                log_path
                if target == "current"
                else Path(stage_plan["output_root"]) / "logs" / "runs" / "arm-a" / "web.jsonl"
            )
            victim.write_text("{}\n", encoding="utf-8")
        return returncode

    monkeypatch.setattr(runner, "_invoke_command", corrupt_log)
    monkeypatch.setattr(runner.evidence, "require_completed_domain", _successful_completion)

    with pytest.raises(StagePlanError, match="runner command log"):
        runner.run_stage_plan(stage_plan)


@pytest.mark.parametrize("foreign_name", ["packages", "stage_receipt.json"])
def test_executor_state_rejects_packaging_until_packaged_lifecycle_exists(
    stage_plan: dict[str, Any], foreign_name: str
) -> None:
    state.claim_stage(stage_plan, max_workers=4)
    output_root = Path(stage_plan["output_root"])
    path = output_root / foreign_name
    if foreign_name == "packages":
        path.mkdir()
    else:
        path.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(StagePlanError, match="unknown entries"):
        state.require_claimed_stage_plan(stage_plan)


def test_completed_plan_preregistration_uses_bridge_normalization() -> None:
    assert evidence._normalized_preregistration({"preregistration_sha256": ""}) == ""
    digest = "b" * 64
    assert evidence._normalized_preregistration({"preregistration_sha256": digest}) == (
        f"sha256:{digest}"
    )


def test_status_replacement_is_atomic_and_claim_inventory_is_strict(
    stage_plan: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    replacements: list[tuple[Path, Path]] = []
    real_replace = state.os.replace

    def replace(source: str | Path, target: str | Path) -> None:
        replacements.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(state.os, "replace", replace)
    state.claim_stage(stage_plan, max_workers=4)
    state.write_status(stage_plan, status="PREFLIGHT_COMPLETE", max_workers=4)

    assert len(replacements) == EXPECTED_ATOMIC_REPLACEMENTS
    assert all(source.name.endswith(".tmp") for source, _target in replacements)
    assert not list(Path(stage_plan["output_root"]).rglob("*.tmp"))
    (Path(stage_plan["output_root"]) / "foreign-output").write_text("stale\n")
    with pytest.raises(StagePlanError, match="unknown entries"):
        state.require_claimed_stage_plan(stage_plan)
