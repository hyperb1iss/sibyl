from __future__ import annotations

import json
import os
import subprocess
from shutil import which
from typing import Any, cast

from tools.release.aur_pkgbuild import render_pkgbuild as render_aur_pkgbuild
from tools.release.homebrew_formula import PackageArtifact, pep440_version, render_formula
from tools.tests.conftest import REPO_ROOT

RELEASE_TEST_DEPS = {
    "root:autonomy-gate-test",
    "root:reflection-quality-gate-test",
    "root:auth-session-gate-test",
    "root:overview-perf-gate-test",
}
PYTHON_RELEASE_PACKAGES = {
    "uv build --package sibyl-core --out-dir dist/",
    "uv build --package sibyl-dev --out-dir dist/",
    "uv build --package sibyld --out-dir dist/",
}
PUBLISH_ENTRYPOINTS_REQUIRING_RC_GATE = 2


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


def test_root_check_covers_release_test_matrix() -> None:
    deps = _dep_targets("check")

    assert deps >= RELEASE_TEST_DEPS
    assert "root:release-workflow-test" in deps


def test_root_test_covers_release_test_matrix() -> None:
    deps = _dep_targets("test")

    assert deps >= RELEASE_TEST_DEPS
    assert "root:release-workflow-test" in deps


def test_release_workflow_validates_before_tag_or_publish() -> None:
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    candidate_index = workflow.index("Record candidate SHA")
    rc_check_index = workflow.index("Run RC gate bundle")
    nightly_index = workflow.index("Validate same-SHA Nightly Regression")
    assert candidate_index < workflow.index('git tag -a "v${{ steps.version.outputs.version }}"')
    assert candidate_index < workflow.index("gh workflow run publish.yml")
    assert rc_check_index < workflow.index('git tag -a "v${{ steps.version.outputs.version }}"')
    assert rc_check_index < workflow.index("gh workflow run publish.yml")
    assert nightly_index < workflow.index('git tag -a "v${{ steps.version.outputs.version }}"')
    assert nightly_index < workflow.index("gh workflow run publish.yml")
    assert 'git commit -m "🔖' not in workflow
    assert "chore(release): prepare v${{ steps.version.outputs.version }}" not in workflow
    assert "Update version" not in workflow
    assert "Commit version bump" not in workflow
    assert "No version commit, tag, release, or publish was created." in workflow
    assert "Build and checks passed. Ready to release." not in workflow
    assert "moon run :check" in workflow
    assert "nightly_run_id" in workflow
    assert "if: ${{ !inputs.dry_run || inputs.nightly_run_id != '' }}" in workflow
    assert 'gh run view "$NIGHTLY_RUN_ID"' in workflow
    assert 'run.get("workflowName") != "Nightly Regression"' in workflow
    assert 'run.get("headSha") != expected_sha' in workflow
    assert "nightly_run_id is required for live releases." in workflow
    assert "Release candidates must have VERSION pre-committed" in workflow
    assert "rc-gate-receipt-${{ steps.candidate.outputs.sha }}" in workflow
    assert r"(-[a-zA-Z0-9.]+)?" in workflow
    assert "steps.version.outputs.needs_version_commit == 'true'" in workflow
    assert "from: ${{ steps.version.outputs.previous_tag }}" in workflow
    assert "Generate AI release notes" in workflow
    assert "continue-on-error: true" in workflow
    assert "Prepare release notes" in workflow
    assert "steps.ai_release_notes.outputs.content" in workflow
    assert "AI-generated release notes were unavailable" in workflow
    assert 'git rev-parse --verify --quiet "$PREVIOUS_TAG^{commit}"' in workflow
    assert "git log --no-merges --pretty=format:'- %s (%h)'" in workflow
    assert "RELEASE_NOTES_CONTENT" in workflow
    assert "printf '%s\\n' \"$RELEASE_NOTES_CONTENT\"" in workflow
    assert "cat << 'EOF' >> $GITHUB_STEP_SUMMARY" not in workflow


def test_nightly_regression_uploads_candidate_sha_receipts() -> None:
    workflow = (REPO_ROOT / ".github/workflows/nightly-regression.yml").read_text(encoding="utf-8")

    assert "baseline-parity-receipt-${{ github.sha }}" in workflow
    assert "live-graph-receipt-${{ github.sha }}" in workflow
    assert "restore-to-scratch-receipt-${{ github.sha }}" in workflow
    assert "backup-restore-gate-receipt-${{ github.sha }}" in workflow
    assert "candidate_sha=${GITHUB_SHA}" in workflow
    assert "run_id=${GITHUB_RUN_ID}" in workflow
    assert '- cron: "0 10 * * 1"' in workflow
    assert "Run backup restore-to-scratch gate" in workflow
    assert "moon run backup-restore-gate" in workflow


def test_publish_workflow_gates_direct_dispatches_before_artifacts() -> None:
    workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")

    assert "rc-gate:" in workflow
    assert "homebrew:" in workflow
    assert "aur:" in workflow
    assert "moon run :check" in workflow
    assert "moon run python-package-build" in workflow
    assert "tools/release/homebrew_formula.py" in workflow
    assert "tools/release/aur_pkgbuild.py" in workflow
    assert "hyperb1iss/homebrew-tap" in workflow
    assert "HOMEBREW_TAP_TOKEN" in workflow
    assert all(
        fragment in workflow
        for fragment in (
            "Upload Homebrew formula artifact",
            "Verify Homebrew tap token",
            "::error::HOMEBREW_TAP_TOKEN is not configured",
            "exit 1",
        )
    )
    assert "gh api repos/hyperb1iss/homebrew-tap --jq '.permissions.push // false'" in workflow
    assert "HOMEBREW_TAP_TOKEN cannot authenticate" in workflow
    assert "HOMEBREW_TAP_TOKEN cannot push to hyperb1iss/homebrew-tap" in workflow
    assert "persist-credentials: true" in workflow
    assert (
        "KSXGitHub/github-actions-deploy-aur@da03e160361ce01bf087e790b6ffd196d7dccff7" in workflow
    )
    assert all(
        fragment in workflow
        for fragment in (
            "Check AUR package version",
            "https://aur.archlinux.org/rpc/?v=5&type=info&arg[]=sibyl",
            "steps.aur_state.outputs.publish == 'true'",
        )
    )
    assert "# v4.1.3" in workflow
    assert "AUR_SSH_KEY" in workflow
    assert workflow.index("gh-action-pypi-publish") < workflow.index("homebrew_formula.py")
    assert workflow.index("gh-action-pypi-publish") < workflow.index("aur_pkgbuild.py")
    assert workflow.count("needs: rc-gate") == PUBLISH_ENTRYPOINTS_REQUIRING_RC_GATE
    assert workflow.index("rc-gate:") < workflow.index("moon run python-package-build")
    assert workflow.index("rc-gate:") < workflow.index("Docker: ${{ matrix.image }}")
    assert "install.sh | sh -s -- --version ${{ steps.version.outputs.version }}" in workflow
    assert (
        "install.sh | sh -s -- --remote --version ${{ steps.version.outputs.version }}" in workflow
    )
    assert "paru -S sibyl" in workflow
    assert "needs: [python, homebrew, aur, docker-sign]" in workflow
    assert "docker-security:" in workflow
    assert "fail-fast: false" in workflow
    assert "docker-sign:" in workflow
    assert "aquasecurity/trivy-action@v0.36.0" in workflow
    assert "sigstore/cosign-installer@v4.1.2" in workflow
    assert "format: cyclonedx" in workflow
    assert "severity: HIGH,CRITICAL" in workflow
    assert "cosign sign --yes" in workflow
    assert "Upload Cosign receipt" in workflow
    assert "Download image evidence" in workflow
    assert "Prepare release evidence assets" in workflow
    assert "pattern: sibyl-*-${{ steps.version.outputs.version }}-*" in workflow
    assert "find release-evidence -type f" in workflow
    assert "release-assets/*.cdx.json" in workflow
    assert "release-assets/*-cosign-receipt.json" in workflow
    assert "fail_on_unmatched_files: true" in workflow
    assert "id-token: write" in workflow
    assert "uv tool install sibyld" not in workflow
    assert "[sibyld](https://pypi.org/project/sibyld/" in workflow
    assert "[sibyl](https://aur.archlinux.org/packages/sibyl)" in workflow


def test_install_script_defaults_to_server_ui_story() -> None:
    installer = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")

    assert 'MODE="${SIBYL_INSTALL_MODE:-server}"' in installer
    assert "set -- up" in installer
    assert 'install_tool "sibyl-dev" "sibyl" "Sibyl CLI"' in installer
    assert 'install_tool "sibyld" "sibyld" "Sibyl local daemon"' in installer
    assert "sibyl skill install --quiet" in installer
    assert "--remote|remote|--cli|cli" in installer
    assert "sibyl local setup" not in installer
    assert "uv tool upgrade" not in installer


def test_python_package_build_verifies_cli_bundle_data() -> None:
    task = _root_task("python-package-build")
    input_paths = {cast(str, entry.get("file") or entry.get("glob")) for entry in task["inputs"]}
    script = task["script"]

    assert "set -euo pipefail" in script
    assert "apps/cli/src/sibyl_cli/data/**/*" in input_paths
    assert "sibyl_cli/data/skills/sibyl/SKILL.md" in script
    assert "sibyl_cli/data/skill-packs/core.md" in script
    assert "sibyl_cli/data/skill-packs/workflows.md" in script


def test_python_package_build_covers_cli_core_and_daemon() -> None:
    task = _root_task("python-package-build")
    script = task["script"]

    for package_command in PYTHON_RELEASE_PACKAGES:
        assert package_command in script


def test_homebrew_formula_renders_cli_and_daemon_formula() -> None:
    artifacts = {
        "sibyl-dev": PackageArtifact(
            name="sibyl-dev",
            url="https://files.pythonhosted.org/sibyl_dev-1.0.0rc1.tar.gz",
            sha256="a" * 64,
        ),
        "sibyld": PackageArtifact(
            name="sibyld",
            url="https://files.pythonhosted.org/sibyld-1.0.0rc1.tar.gz",
            sha256="b" * 64,
        ),
        "sibyl-core": PackageArtifact(
            name="sibyl-core",
            url="https://files.pythonhosted.org/sibyl_core-1.0.0rc1.tar.gz",
            sha256="c" * 64,
        ),
    }

    formula = render_formula(
        release_version="1.0.0-rc.1",
        python_version=pep440_version("1.0.0-rc.1"),
        artifacts=artifacts,
    )

    assert "class Sibyl < Formula" in formula
    assert 'version "1.0.0-rc.1"' in formula
    assert 'PYTHON_PACKAGE_VERSION = "1.0.0rc1"' in formula
    assert 'resource "sibyl-core"' in formula
    assert 'resource "sibyld"' in formula
    assert 'bin.install_symlink libexec/"bin/sibyl"' in formula
    assert 'bin.install_symlink libexec/"bin/sibyld"' in formula


def test_aur_pkgbuild_renders_cli_package() -> None:
    artifacts = {
        "sibyl-dev": PackageArtifact(
            name="sibyl-dev",
            url="https://files.pythonhosted.org/packages/sibyl_dev-1.0.0rc1.tar.gz",
            sha256="a" * 64,
        ),
        "sibyl-core": PackageArtifact(
            name="sibyl-core",
            url="https://files.pythonhosted.org/packages/sibyl_core-1.0.0rc1.tar.gz",
            sha256="b" * 64,
        ),
    }

    pkgbuild = render_aur_pkgbuild(
        python_version=pep440_version("1.0.0-rc.1"),
        artifacts=artifacts,
    )

    assert "pkgname=sibyl" in pkgbuild
    assert "pkgver=1.0.0rc1" in pkgbuild
    assert "provides=('sibyl-cli')" in pkgbuild
    assert "depends=(" in pkgbuild
    assert "'docker'" in pkgbuild
    assert "'docker-compose'" in pkgbuild
    assert "'python-typer'" in pkgbuild
    assert "'python-pydantic-settings'" in pkgbuild
    assert "optdepends=(" not in pkgbuild
    assert (
        '"sibyl-dev-${pkgver}.tar.gz::https://files.pythonhosted.org/packages/sibyl_dev-1.0.0rc1.tar.gz"'
        in pkgbuild
    )
    assert (
        '"sibyl-core-${pkgver}.tar.gz::https://files.pythonhosted.org/packages/sibyl_core-1.0.0rc1.tar.gz"'
        in pkgbuild
    )
    assert "'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'" in pkgbuild
    assert 'python -m build --wheel --no-isolation "sibyl_core-${pkgver}"' in pkgbuild
    assert 'python -m installer --destdir="${pkgdir}" "sibyl_dev-${pkgver}"/dist/*.whl' in pkgbuild
