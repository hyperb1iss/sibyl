from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml
from tools.release.ci_changes import UnmatchedPathsError, classify_changed_paths, main
from tools.tests.conftest import REPO_ROOT


@pytest.mark.parametrize(
    "path",
    [
        "charts/sibyl/values.yaml",
        "VERSION",
        ".github/workflows/publish.yml",
    ],
)
def test_release_surfaces_run_every_release_integrity_gate(path: str) -> None:
    outputs = classify_changed_paths((path,)).outputs()

    assert outputs["run_static"] == "true"
    assert outputs["run_build"] == "true"
    assert outputs["run_tests"] == "true"
    assert outputs["run_e2e"] == "true"
    assert outputs["run_image_scan"] == "true"
    assert outputs["image_scan_matrix"] == '["api","web"]'
    assert outputs["run_release"] == "true"
    assert outputs["run_helm"] == "true"


def test_documentation_only_change_keeps_runtime_jobs_off() -> None:
    outputs = classify_changed_paths(("docs/guide/quick-start.md",)).outputs()

    assert outputs["run_static"] == "true"
    assert outputs["run_build"] == "false"
    assert outputs["run_tests"] == "false"
    assert outputs["run_e2e"] == "false"
    assert outputs["run_image_scan"] == "false"
    assert outputs["run_release"] == "false"
    assert outputs["run_helm"] == "false"


def test_classifier_fails_closed_with_the_unmatched_path() -> None:
    path = "new-release-surface.toml"

    with pytest.raises(UnmatchedPathsError) as exc_info:
        classify_changed_paths((path,))

    assert exc_info.value.paths == (path,)
    assert path in str(exc_info.value)


def test_classifier_cli_prints_the_unmatched_path(tmp_path, capsys) -> None:
    changed_files = tmp_path / "changed-files"
    github_output = tmp_path / "github-output"
    summary = tmp_path / "summary"
    changed_files.write_bytes(b"new-release-surface.toml\0")

    result = main(
        [
            "--changed-files",
            str(changed_files),
            "--github-output",
            str(github_output),
            "--summary",
            str(summary),
        ]
    )

    assert result == 1
    assert "::error::unmatched CI path: new-release-surface.toml" in capsys.readouterr().err


def test_every_tracked_repository_path_has_a_ci_owner() -> None:
    git = shutil.which("git")
    assert git is not None
    result = subprocess.run(  # noqa: S603
        [git, "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    )
    paths = tuple(
        path.decode("utf-8", errors="surrogateescape")
        for path in result.stdout.split(b"\0")
        if path
    )

    classify_changed_paths(paths)


def test_ci_runs_release_and_helm_contract_jobs() -> None:
    workflow = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "tools/release/ci_changes.py" in workflow
    assert "moon run release-workflow-test" in workflow
    assert "uses: azure/setup-helm@v5.0.1" in workflow
    assert "moon run helm-test" in workflow
    assert "moon run e2e:test-browser" in workflow
    assert "profile: defaults" in workflow
    assert "profile: production-redis" in workflow


def test_e2e_ci_tasks_are_finite_tasks() -> None:
    config = yaml.safe_load((REPO_ROOT / "apps/e2e/moon.yml").read_text(encoding="utf-8"))

    for task_name in (
        "test",
        "test-api",
        "test-perf",
        "test-browser",
        "playwright-install",
        "format",
    ):
        assert config["tasks"][task_name].get("preset") != "server"
