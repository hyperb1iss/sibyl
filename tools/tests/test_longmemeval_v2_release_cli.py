from __future__ import annotations

import argparse
import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest
from benchmarks import longmemeval_v2_ablations as ablations
from benchmarks import longmemeval_v2_release_cli as cli
from benchmarks import longmemeval_v2_release_package_root as package_root
from benchmarks.longmemeval_v2_release_inputs import StagePlanError


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


def _json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _plan_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        command="release-plan",
        spec=str(_json(tmp_path / "spec.json", {"stage": "aa"})),
        official_repo=str(tmp_path / "official"),
        data_root=str(tmp_path / "data"),
        output_root=str(tmp_path / "run"),
        output=str(tmp_path / "plan.json"),
    )


def _command_args(tmp_path: Path, command: str, **values: Any) -> argparse.Namespace:
    plan = _json(tmp_path / "plan.json", {"stage_plan_sha256": "sha256:sealed"})
    return argparse.Namespace(command=command, plan=str(plan), **values)


def test_release_arguments_are_wired_into_the_benchmark_cli() -> None:
    args = ablations.parse_args(
        [
            "release-plan",
            "--spec",
            "spec.json",
            "--official-repo",
            "official",
            "--data-root",
            "data",
            "--output-root",
            "run",
            "--output",
            "plan.json",
        ]
    )
    assert args.command == "release-plan"

    run = ablations.parse_args(["release-run", "--plan", "plan.json"])
    assert run.max_workers == cli.MAX_WORKERS_CAP


@pytest.mark.parametrize("workers", [0, cli.MAX_WORKERS_CAP + 1])
def test_release_run_rejects_workers_outside_the_local_contract(workers: int) -> None:
    with pytest.raises(SystemExit):
        ablations.parse_args(["release-run", "--plan", "plan.json", "--max-workers", str(workers)])


def test_release_plan_binds_only_the_reviewed_package_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}
    payload = {"schema_version": "sealed-plan"}

    def build_stage_plan(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return payload

    def write_stage_plan(path: Path, value: dict[str, Any]) -> None:
        captured["plan_path"] = path
        captured["payload"] = value

    monkeypatch.setattr(cli.release_plan, "build_stage_plan", build_stage_plan)
    monkeypatch.setattr(cli.release_plan, "write_stage_plan", write_stage_plan)
    monkeypatch.setattr(
        cli.release_runner,
        "run_stage_plan",
        lambda *_args, **_kwargs: pytest.fail("planning advanced into paid execution"),
    )

    assert cli.run_release_cli_command(_plan_args(tmp_path)) == 0
    assert captured["system_description_path"] == cli.SYSTEM_DESCRIPTION
    assert captured["adapter_path"] == cli.ADAPTER
    assert captured["payload"] == payload
    assert captured["plan_path"] == (tmp_path / "plan.json").resolve()
    assert json.loads(capsys.readouterr().out) == payload


@pytest.mark.parametrize("destination", ["output_root", "output"])
def test_release_plan_rejects_destinations_inside_the_sibyl_checkout_before_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    args = _plan_args(tmp_path)
    setattr(args, destination, str(cli.ROOT / f".hostile-{destination}"))
    monkeypatch.setattr(
        cli.release_plan,
        "build_stage_plan",
        lambda **_kwargs: pytest.fail("overlapping destination reached plan construction"),
    )
    monkeypatch.setattr(
        cli.release_plan,
        "write_stage_plan",
        lambda *_args: pytest.fail("overlapping destination reached plan publication"),
    )

    with pytest.raises(StagePlanError, match="overlaps a sealed release input"):
        cli.run_release_cli_command(args)


def test_release_plan_rejects_a_destination_which_contains_sealed_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _plan_args(tmp_path)
    args.output_root = str(tmp_path)
    monkeypatch.setattr(
        cli.release_plan,
        "build_stage_plan",
        lambda **_kwargs: pytest.fail("ancestor destination reached plan construction"),
    )

    with pytest.raises(StagePlanError, match="overlaps a sealed release input"):
        cli.run_release_cli_command(args)


def test_release_plan_rejects_a_symlinked_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = tmp_path / "external"
    external.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    args = _plan_args(tmp_path)
    args.output_root = str(alias / "run")
    monkeypatch.setattr(
        cli.release_plan,
        "build_stage_plan",
        lambda **_kwargs: pytest.fail("symlinked destination reached plan construction"),
    )

    with pytest.raises(StagePlanError, match="symlink or noncanonical"):
        cli.run_release_cli_command(args)


@pytest.mark.parametrize("destination", ["output_root", "output"])
def test_release_plan_rejects_a_case_alias_inside_the_sibyl_checkout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    destination: str,
) -> None:
    alias = Path(str(cli.ROOT).replace("/Users/", "/users/", 1))
    if alias == cli.ROOT or not alias.exists() or not alias.samefile(cli.ROOT):
        pytest.skip("requires a case-insensitive macOS /Users volume")
    args = _plan_args(tmp_path)
    setattr(args, destination, str(alias / f".hostile-{destination}"))
    monkeypatch.setattr(
        cli.release_plan,
        "build_stage_plan",
        lambda **_kwargs: pytest.fail("case alias reached plan construction"),
    )
    monkeypatch.setattr(
        cli.release_plan,
        "write_stage_plan",
        lambda *_args: pytest.fail("case alias reached plan publication"),
    )

    with pytest.raises(StagePlanError, match="overlaps a sealed release input"):
        cli.run_release_cli_command(args)


def test_release_plan_never_writes_through_a_parent_symlink_created_after_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _plan_args(tmp_path)
    plan_parent = tmp_path / "future-plans"
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    args.output = str(plan_parent / "stage.json")
    payload = {"output_root": str((tmp_path / "run").resolve())}

    def build_stage_plan(**_kwargs: Any) -> dict[str, Any]:
        plan_parent.symlink_to(redirected, target_is_directory=True)
        return payload

    monkeypatch.setattr(cli.release_plan, "build_stage_plan", build_stage_plan)
    monkeypatch.setattr(cli.release_plan, "require_stage_plan", lambda _raw: [])

    with pytest.raises(StagePlanError, match="symlink or noncanonical"):
        cli.run_release_cli_command(args)
    assert not (redirected / "stage.json").exists()


def test_release_run_executes_only_the_sealed_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _command_args(tmp_path, "release-run", max_workers=3)
    calls: list[tuple[dict[str, Any], int]] = []

    def run_stage_plan(plan: dict[str, Any], max_workers: int) -> dict[str, Any]:
        calls.append((plan, max_workers))
        return {"status": "EXECUTED", "actual_cost_usd": 1.0}

    monkeypatch.setattr(cli.release_runner, "run_stage_plan", run_stage_plan)
    monkeypatch.setattr(
        cli.release_package,
        "package_stage",
        lambda *_args, **_kwargs: pytest.fail("execution advanced into stage packaging"),
    )

    assert cli.run_release_cli_command(args) == 0
    assert calls == [({"stage_plan_sha256": "sha256:sealed"}, 3)]
    assert json.loads(capsys.readouterr().out)["status"] == "EXECUTED"


def test_release_run_rejects_a_nonexecuted_owner_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _command_args(tmp_path, "release-run", max_workers=4)
    monkeypatch.setattr(
        cli.release_runner, "run_stage_plan", lambda *_args, **_kwargs: {"status": "FAIL"}
    )

    with pytest.raises(StagePlanError, match="EXECUTED"):
        cli.run_release_cli_command(args)


def test_release_arm_package_builds_exactly_one_missing_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = (tmp_path / "official-packages").resolve()
    args = _command_args(
        tmp_path,
        "release-arm-package",
        arm_id="aa-1-left",
        packages_root=str(packages_root),
    )
    executed = argparse.Namespace(runs=({"arm_id": "aa-1-left"},))
    calls: list[tuple[object, str, Path]] = []
    monkeypatch.setattr(cli, "require_executed_stage", lambda _plan: executed)

    def package_arm(value: object, *, arm_id: str, packages_root: Path) -> dict[str, Any]:
        calls.append((value, arm_id, packages_root))
        return {"status": "PASS"}

    monkeypatch.setattr(cli.official_publication, "package_official_arm", package_arm)
    monkeypatch.setattr(
        cli.official_publication,
        "require_official_arm_package",
        lambda *_args, **_kwargs: pytest.fail("missing arm was treated as resumable"),
    )

    assert cli.run_release_cli_command(args) == 0
    assert calls == [(executed, "aa-1-left", packages_root)]


def test_release_arm_package_resumes_through_the_canonical_consumer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packages_root = (tmp_path / "official-packages").resolve()
    authority = cli.package_object.publication_path(packages_root, "aa-1-left")
    authority.parent.mkdir(parents=True)
    authority.write_text("{}", encoding="utf-8")
    args = _command_args(
        tmp_path,
        "release-arm-package",
        arm_id="aa-1-left",
        packages_root=str(packages_root),
    )
    executed = argparse.Namespace(runs=({"arm_id": "aa-1-left"},))
    monkeypatch.setattr(cli, "require_executed_stage", lambda _plan: executed)
    monkeypatch.setattr(
        cli.official_publication,
        "package_official_arm",
        lambda *_args, **_kwargs: pytest.fail("existing arm was rebuilt"),
    )
    monkeypatch.setattr(
        cli.official_publication,
        "require_official_arm_package",
        lambda value, *, arm_id, packages_root: {
            "status": "PASS",
            "arm_id": arm_id,
            "root": str(packages_root),
            "executed": value is executed,
        },
    )

    assert cli.run_release_cli_command(args) == 0


def test_release_arm_package_rejects_a_nonpass_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _command_args(
        tmp_path,
        "release-arm-package",
        arm_id="aa-1-left",
        packages_root=str(tmp_path / "official-packages"),
    )
    monkeypatch.setattr(
        cli,
        "require_executed_stage",
        lambda _plan: argparse.Namespace(runs=({"arm_id": "aa-1-left"},)),
    )
    monkeypatch.setattr(
        cli.official_publication,
        "package_official_arm",
        lambda *_args, **_kwargs: {"status": "FAIL"},
    )

    with pytest.raises(StagePlanError, match="PASS authority"):
        cli.run_release_cli_command(args)


def test_release_arm_package_rejects_an_arm_outside_the_sealed_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _command_args(
        tmp_path,
        "release-arm-package",
        arm_id="foreign-arm",
        packages_root=str(tmp_path / "official-packages"),
    )
    monkeypatch.setattr(
        cli,
        "require_executed_stage",
        lambda _plan: argparse.Namespace(runs=({"arm_id": "aa-1-left"},)),
    )
    monkeypatch.setattr(
        cli.package_object,
        "publication_path",
        lambda *_args, **_kwargs: pytest.fail("foreign arm reached path selection"),
    )

    with pytest.raises(StagePlanError, match="sealed stage"):
        cli.run_release_cli_command(args)


def test_release_package_does_not_build_missing_arm_authorities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    template = _json(tmp_path / "template.json", {"seeds": [1, 2, 3]})
    packages_root = (tmp_path / "official-packages").resolve()
    args = _command_args(
        tmp_path,
        "release-package",
        packages_root=str(packages_root),
        preregistration_template=str(template),
    )
    calls: list[tuple[dict[str, Any], Path, Path | None]] = []

    def package_stage(
        plan: dict[str, Any],
        *,
        official_packages_root: Path,
        preregistration_template: Path | None,
    ) -> dict[str, Any]:
        calls.append((plan, official_packages_root, preregistration_template))
        return {"status": "PASS"}

    monkeypatch.setattr(cli.release_package, "package_stage", package_stage)
    monkeypatch.setattr(
        cli.official_publication,
        "package_official_arm",
        lambda *_args, **_kwargs: pytest.fail("stage package rebuilt an official arm"),
    )

    assert cli.run_release_cli_command(args) == 0
    assert calls == [
        (
            {"stage_plan_sha256": "sha256:sealed"},
            packages_root,
            template.resolve(),
        )
    ]
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_release_package_accepts_a_canonical_not_applicable_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _command_args(
        tmp_path,
        "release-package",
        packages_root=str(tmp_path / "official-packages"),
        preregistration_template=None,
    )
    monkeypatch.setattr(
        cli.release_package,
        "package_stage",
        lambda *_args, **_kwargs: {"status": "NOT_APPLICABLE"},
    )

    assert cli.run_release_cli_command(args) == 0


def test_release_verify_only_consumes_the_packaged_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    args = _command_args(tmp_path, "release-verify")
    monkeypatch.setattr(
        cli.release_package,
        "require_packaged_stage",
        lambda plan: {"status": "PASS", "plan": plan["stage_plan_sha256"]},
    )
    monkeypatch.setattr(
        cli.release_runner,
        "run_stage_plan",
        lambda *_args, **_kwargs: pytest.fail("verification reran the paid stage"),
    )

    assert cli.run_release_cli_command(args) == 0
    assert json.loads(capsys.readouterr().out) == {
        "plan": "sha256:sealed",
        "status": "PASS",
    }


def test_release_commands_reject_nonobject_plan_json(tmp_path: Path) -> None:
    plan = _json(tmp_path / "plan.json", [])
    args = argparse.Namespace(command="release-run", plan=str(plan), max_workers=4)

    with pytest.raises(StagePlanError, match="JSON object"):
        cli.run_release_cli_command(args)
