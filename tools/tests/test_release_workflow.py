from __future__ import annotations

import json
import os
import subprocess
from shutil import which
from typing import Any, cast

from tools.tests.conftest import REPO_ROOT

RC_GATE_TEST_DEPS = {
    "root:autonomy-gate-test",
    "root:reflection-quality-gate-test",
    "root:auth-session-gate-test",
    "root:overview-perf-gate-test",
}
PUBLISH_ARTIFACT_JOB_COUNT = 2
PYTHON_RELEASE_PACKAGES = {
    "uv build --package sibyl-core --out-dir dist/",
    "uv build --package sibyl-dev --out-dir dist/",
    "uv build --package sibyld --out-dir dist/",
}


def _root_task(task_id: str) -> dict[str, Any]:
    moon = which("moon")
    assert moon is not None

    result = subprocess.run(  # noqa: S603
        [moon, "query", "tasks", "--project", "root", "--id", task_id],
        cwd=REPO_ROOT,
        env={**os.environ, "MOON_COLOR": "false"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = cast(dict[str, Any], json.loads(result.stdout))
    return cast(dict[str, Any], payload["tasks"]["root"][task_id])


def _dep_targets(task_id: str) -> set[str]:
    return {dep["target"] for dep in _root_task(task_id)["deps"]}


def test_root_check_covers_full_rc_gate_test_matrix() -> None:
    deps = _dep_targets("check")

    assert deps >= RC_GATE_TEST_DEPS
    assert "root:release-workflow-test" in deps


def test_root_test_covers_full_rc_gate_test_matrix() -> None:
    deps = _dep_targets("test")

    assert deps >= RC_GATE_TEST_DEPS
    assert "root:release-workflow-test" in deps


def test_release_workflow_gates_before_tag_or_publish() -> None:
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    gate_index = workflow.index("moon run :check")
    nightly_index = workflow.index("Validate nightly regression receipt")
    assert gate_index < workflow.index('git tag -a "v${{ steps.version.outputs.version }}"')
    assert gate_index < workflow.index("gh workflow run publish.yml")
    assert nightly_index < workflow.index('git tag -a "v${{ steps.version.outputs.version }}"')
    assert 'git commit -m "🔖' not in workflow
    assert "chore(release): prepare v${{ steps.version.outputs.version }}" in workflow
    assert "Release gates run on this exact commit before the tag is pushed." in workflow
    assert "RC gate bundle passed against the current checkout." in workflow
    assert "No version commit, tag, release, or publish was created." in workflow
    assert "Build and checks passed. Ready to release." not in workflow
    assert "rc-gate-receipt-${{ steps.candidate.outputs.short_sha }}" in workflow
    assert r"(-[a-zA-Z0-9.]+)?" in workflow
    assert "nightly_run_id" in workflow
    assert 'workflowName") != "Nightly Regression"' in workflow
    assert 'run.get("headSha") != candidate_sha' in workflow
    assert "Prerelease candidates must have VERSION committed" in workflow
    assert "steps.version.outputs.needs_version_commit == 'true'" in workflow
    assert "from: ${{ steps.version.outputs.previous_tag }}" in workflow


def test_nightly_regression_uploads_candidate_sha_receipts() -> None:
    workflow = (REPO_ROOT / ".github/workflows/nightly-regression.yml").read_text(encoding="utf-8")

    assert "baseline-parity-receipt-${{ github.sha }}" in workflow
    assert "live-graph-receipt-${{ github.sha }}" in workflow
    assert "candidate_sha=${GITHUB_SHA}" in workflow
    assert "run_id=${GITHUB_RUN_ID}" in workflow


def test_publish_workflow_gates_direct_dispatches_before_artifacts() -> None:
    workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")

    assert "rc-gate:" in workflow
    assert "moon run :check" in workflow
    assert "moon run python-package-build" in workflow
    assert workflow.index("moon run :check") < workflow.index("moon run python-package-build")
    assert workflow.index("moon run :check") < workflow.index("docker/build-push-action")
    assert workflow.count("needs: rc-gate") == PUBLISH_ARTIFACT_JOB_COUNT
    assert "uv tool install sibyld" in workflow
    assert "[sibyld](https://pypi.org/project/sibyld/" in workflow


def test_python_package_build_covers_cli_core_and_daemon() -> None:
    task = _root_task("python-package-build")
    script = task["script"]

    for package_command in PYTHON_RELEASE_PACKAGES:
        assert package_command in script
