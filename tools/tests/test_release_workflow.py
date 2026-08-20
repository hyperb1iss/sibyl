from __future__ import annotations

import json
import os
import subprocess
from shutil import which
from typing import Any, cast

import pytest
import tomllib
import yaml
from packaging.requirements import Requirement
from tools.release.aur_pkgbuild import render_pkgbuild as render_aur_pkgbuild
from tools.release.homebrew_formula import PackageArtifact, pep440_version, render_formula
from tools.release.version import parse_release_version
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
# Jobs that begin a publish path and must not start before the RC gate.
# Checked structurally rather than by counting a literal, because a job may
# legitimately depend on the gate plus something else, which a string count
# reads as the gate having been removed.
PUBLISH_ENTRYPOINTS_REQUIRING_RC_GATE = ("python", "docker-build")
RELEASE_WORKFLOW_REQUIRED_FRAGMENTS = (
    "No version commit, tag, release, or publish was created.",
    "moon run :check",
    "nightly_run_id",
    "if: ${{ !inputs.dry_run || inputs.nightly_run_id != '' }}",
    'gh run view "$NIGHTLY_RUN_ID"',
    'run.get("workflowName") != "Nightly Regression"',
    'run.get("headSha") != expected_sha',
    "No successful Nightly Regression run found",
    # The workflow cuts the version itself. What replaced the precommitted-RC
    # rule is a proof rather than a refusal: the bump may only touch the pins
    # sync_versions.py generates, so the code Nightly validated at the base
    # SHA is the code being tagged. These fragments hold that proof in place.
    "--list-targets",
    "A version bump may only touch generated pins.",
    "The committed bump differs from the base by non-pin files.",
    "git status --porcelain",
    "rc-gate-receipt-${{ steps.candidate.outputs.sha }}",
    'python -m tools.release.version "$VERSION"',
    "steps.version.outputs.needs_version_commit == 'true'",
    "from: ${{ steps.version.outputs.previous_tag }}",
    "Generate AI release notes",
    "provider: anthropic",
    "version: v2.1.0",
    "model: claude-opus-5",
    "secrets.ANTHROPIC_API_KEY",
    "Prepare release notes",
    "steps.ai_release_notes.outputs.content",
    "Git-Iris generated empty release notes",
    "[^[:space:]]",
    'echo "::error::Git-Iris generated empty release notes; refusing to create a release."\n'
    "            exit 1",
    # The release object is born as a pre-release that is not "latest".
    # Publish promotes it once distribution actually succeeded, which
    # test_release_is_promoted_only_after_publish_succeeds pins end to end.
    "prerelease: true",
    "make_latest: false",
    "uses: ./.github/workflows/image-cve-gate.yml",
    "needs: image-cve-gate",
    "RELEASE_NOTES_CONTENT",
    "printf '%s\\n' \"$RELEASE_NOTES_CONTENT\"",
)
RELEASE_WORKFLOW_FORBIDDEN_FRAGMENTS = (
    'git commit -m "🔖',
    "chore(release): prepare v${{ steps.version.outputs.version }}",
    "Update version",
    "Commit version bump",
    "Build and checks passed. Ready to release.",
    "continue-on-error: true",
    "provider: openai",
    "secrets.OPENAI_API_KEY",
    "deterministic git log fallback",
    "AI-generated release notes were unavailable",
    'git rev-parse --verify --quiet "$PREVIOUS_TAG^{commit}"',
    "git log --no-merges --pretty=format:'- %s (%h)'",
    "is_prerelease",
    "cat << 'EOF' >> $GITHUB_STEP_SUMMARY",
)


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


def _assert_fragments_present(content: str, fragments: tuple[str, ...]) -> None:
    assert [fragment for fragment in fragments if fragment not in content] == []


def _assert_fragments_absent(content: str, fragments: tuple[str, ...]) -> None:
    assert [fragment for fragment in fragments if fragment in content] == []


def _requirement_by_name(dependencies: list[str], name: str) -> Requirement:
    requirements = [Requirement(dependency) for dependency in dependencies]
    matches = [requirement for requirement in requirements if requirement.name == name]
    assert len(matches) == 1
    return matches[0]


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
    _assert_fragments_present(workflow, RELEASE_WORKFLOW_REQUIRED_FRAGMENTS)
    _assert_fragments_absent(workflow, RELEASE_WORKFLOW_FORBIDDEN_FRAGMENTS)


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


def _publish_workflow() -> str:
    return (REPO_ROOT / ".github/workflows/publish.yml").read_text(encoding="utf-8")


def _dogfood_image_workflow() -> str:
    return (REPO_ROOT / ".github/workflows/publish-dogfood-images.yml").read_text(encoding="utf-8")


def test_image_cve_gate_is_shared_by_every_caller() -> None:
    # Two HIGH advisories reached v1.2.1 because the only image scan ran in
    # publish, after release had already created the tag and the release
    # object. The gate now runs in three places, and the whole value of that
    # depends on the three asking the same question, so the configuration
    # lives in one composite action and every caller delegates to it.
    action = (REPO_ROOT / ".github/actions/trivy-image-gate/action.yml").read_text(encoding="utf-8")

    assert "aquasecurity/trivy-action@v0.36.0" in action
    assert "format: cyclonedx" in action
    assert "severity: HIGH,CRITICAL" in action
    assert 'exit-code: "1"' in action
    assert "vuln-type: os,library" in action
    assert "ignore-unfixed: true" in action
    # Trivy reads a .trivyignore from the working directory unless told
    # otherwise, so the suppression file is named explicitly. Without this an
    # untracked file could silence every gate with nobody accountable.
    assert "trivyignores: .trivyignore" in action
    assert (REPO_ROOT / ".trivyignore").is_file()

    reusable = (REPO_ROOT / ".github/workflows/image-cve-gate.yml").read_text(encoding="utf-8")

    assert "uses: ./.github/actions/trivy-image-gate" in reusable
    assert "workflow_call:" in reusable
    # Built into the local daemon, never pushed, so the gate needs no registry
    # credentials and is safe to run on a pull request from a fork.
    assert "load: true" in reusable
    assert "push: false" in reusable
    assert "fromJSON(inputs.images)" in reusable
    assert "fromJSON(inputs.platforms)" in reusable

    release = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    # The release job may not begin until the gate is green, which is the
    # single property that stops a scan verdict from postdating a release.
    assert "uses: ./.github/workflows/image-cve-gate.yml" in release
    assert "needs: image-cve-gate" in release
    # Publish ships both architectures, so the pre-tag gate covers both.
    assert 'platforms: \'["amd64", "arm64"]\'' in release
    assert 'images: \'["api", "web"]\'' in release

    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uses: ./.github/workflows/image-cve-gate.yml" in ci
    assert "images: ${{ needs.changes.outputs.image_scan_matrix }}" in ci
    # Adding a suppression must run the scan it would suppress.
    assert ".trivyignore" in ci


def test_release_is_promoted_only_after_publish_succeeds() -> None:
    # A release object created before distribution is a claim the pipeline has
    # not yet earned. Release creates the version as a pre-release that is not
    # "latest"; only the final publish job, which runs after the images are
    # scanned, signed and pushed and every package has shipped, promotes it.
    release = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")

    assert "prerelease: true" in release
    assert "make_latest: false" in release

    publish = _publish_workflow()

    assert "needs: [python, homebrew, aur, docker-sign, helm-publish]" in publish

    # Order is the property, not coexistence. action-gh-release updates
    # release metadata before it uploads assets, and its post-upload finalize
    # step returns immediately for a non-draft release, so a single call that
    # both promotes and uploads promotes first. A failed upload would then
    # leave a full, latest release with assets missing. Promotion therefore
    # has to be its own final step that carries no files at all.
    steps = cast(
        "list[dict[str, Any]]",
        yaml.safe_load(publish)["jobs"]["release"]["steps"],
    )
    gh_release_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("softprops/action-gh-release")
    ]
    # Exactly two: one that attaches evidence, one that promotes. A third
    # would mean another release mutation nobody accounted for.
    expected_release_mutations = 2
    assert len(gh_release_steps) == expected_release_mutations

    (upload_index, upload_step), (promote_index, promote_step) = gh_release_steps
    upload_with = upload_step.get("with", {})
    promote_with = promote_step.get("with", {})

    # The step carrying assets must leave the pre-release status untouched.
    assert upload_with.get("files")
    assert upload_with.get("prerelease") is True
    assert upload_with.get("make_latest") is False

    # The promoting step must carry nothing that can fail after the flip.
    assert "files" not in promote_with
    assert promote_with.get("prerelease") is False
    assert promote_with.get("make_latest") is True
    assert promote_index > upload_index

    # Nothing may run after promotion, so a late failure cannot strand a
    # release that already presents itself as current.
    assert promote_index == len(steps) - 1


def test_published_images_can_name_their_own_version() -> None:
    # A published API image reported version 0.0.0 and commit "unknown" in
    # production: the Dockerfile copied VERSION into the builder stage only,
    # and the build passed no build-args at all. Clients compare their
    # version against the server's to detect drift, so an image that cannot
    # name itself makes that check meaningless.
    workflow = _publish_workflow()
    build_args = yaml.safe_load(workflow)["jobs"]["docker-build"]["steps"]
    build_step = next(step for step in build_args if step.get("id") == "build")
    declared = build_step["with"]["build-args"]

    assert "SIBYL_VERSION=${{ steps.version.outputs.version }}" in declared
    assert "SIBYL_GIT_COMMIT=${{ steps.version.outputs.commit }}" in declared
    # github.sha is the triggering commit, not necessarily the checked-out
    # tag, so it must not stand in for the built commit.
    assert "SIBYL_GIT_COMMIT=${{ github.sha }}" not in declared

    dockerfile = (REPO_ROOT / "apps/api/Dockerfile").read_text(encoding="utf-8")
    runtime_stage = dockerfile.split("AS runtime", 1)[1]
    assert "COPY --chown=sibyl:sibyl VERSION /app/VERSION" in runtime_stage
    for arg in ("SIBYL_VERSION", "SIBYL_GIT_COMMIT", "SIBYL_GIT_DIRTY"):
        assert f"ARG {arg}" in runtime_stage, f"{arg} must be declared in the runtime stage"
        assert f"{arg}=${{{arg}}}" in runtime_stage, f"{arg} must be promoted to ENV"


def test_publish_workflow_gates_direct_dispatches_before_artifacts() -> None:
    workflow = _publish_workflow()

    assert "rc-gate:" in workflow
    assert "homebrew:" in workflow
    assert "aur:" in workflow
    assert "moon run :check" in workflow
    assert "moon run python-package-build" in workflow
    jobs = yaml.safe_load(workflow)["jobs"]
    for job_name in PUBLISH_ENTRYPOINTS_REQUIRING_RC_GATE:
        needs = jobs[job_name]["needs"]
        needs = [needs] if isinstance(needs, str) else needs
        assert "rc-gate" in needs, f"{job_name} must not start before the RC gate"

    # Nothing may be published before it has been scanned. The scan used to
    # run after the merge had already pushed a public, immutable tag, which
    # is how a high-severity advisory reached two tagged releases while only
    # withholding signing and the chart.
    assert "docker-build" in jobs["docker-security"]["needs"]
    assert "docker-security" in jobs["docker-merge"]["needs"]
    assert "docker-merge" in jobs["docker-sign"]["needs"]
    assert "docker-security" in jobs["python"]["needs"]

    assert workflow.index("rc-gate:") < workflow.index("moon run python-package-build")
    assert workflow.index("rc-gate:") < workflow.index("Docker: ${{ matrix.image }}")
    assert "id-token: write" in workflow
    assert "uv tool install sibyld" not in workflow


def test_publish_workflow_uploads_python_homebrew_and_aur_artifacts() -> None:
    workflow = _publish_workflow()

    assert "Upload Python distribution artifacts" in workflow
    assert "sibyl-python-${{ inputs.tag }}" in workflow
    assert "path: dist/*" in workflow
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
        "KSXGitHub/github-actions-deploy-aur@084b0d9b15415bf9cdb65d44dad1efe37a354050" in workflow
    )
    assert all(
        fragment in workflow
        for fragment in (
            "Check AUR package version",
            "https://aur.archlinux.org/rpc/?v=5&type=info&arg[]=sibyl",
            "steps.aur_state.outputs.publish == 'true'",
            "Upload AUR PKGBUILD artifact",
            "sibyl-aur-${{ steps.version.outputs.version }}",
        )
    )
    assert "# v4.2.0" in workflow
    assert "AUR_SSH_KEY" in workflow
    assert workflow.index("gh-action-pypi-publish") < workflow.index("homebrew_formula.py")
    assert workflow.index("gh-action-pypi-publish") < workflow.index("aur_pkgbuild.py")


def test_publish_workflow_keeps_install_instructions_current() -> None:
    workflow = _publish_workflow()

    assert "install.sh | sh -s -- --version ${{ steps.version.outputs.version }}" in workflow
    assert (
        "install.sh | sh -s -- --remote --version ${{ steps.version.outputs.version }}" in workflow
    )
    assert "paru -S sibyl" in workflow


def test_publish_workflow_attaches_docker_and_release_evidence() -> None:
    workflow = _publish_workflow()

    assert "needs: [python, homebrew, aur, docker-sign, helm-publish]" in workflow
    assert "docker-security:" in workflow
    assert "fail-fast: false" in workflow
    assert "docker-sign:" in workflow
    # The Trivy configuration itself moved into the shared composite action,
    # so publish is checked for delegating to it rather than for inlining it.
    # test_image_cve_gate_is_shared_by_every_caller owns the configuration.
    assert "uses: ./.github/actions/trivy-image-gate" in workflow
    assert "sigstore/cosign-installer@v4.1.2" in workflow
    assert "cosign sign --yes" in workflow
    assert "Upload Cosign receipt" in workflow
    assert "Download Python distributions" in workflow
    assert "Download Homebrew formula" in workflow
    assert "Download AUR PKGBUILD" in workflow
    assert "Download image evidence" in workflow
    assert "Prepare release evidence assets" in workflow
    assert "pattern: sibyl-*-${{ steps.version.outputs.version }}-*" in workflow
    assert "find release-evidence/images -type f" in workflow
    assert "release-evidence/python" in workflow
    assert "release-evidence/homebrew" in workflow
    assert "release-evidence/aur" in workflow
    assert "sibyl-homebrew-${version}.rb" in workflow
    assert "sibyl-${version}-PKGBUILD" in workflow
    assert "sibyl-${version}-checksums.txt" in workflow
    assert "Prepare release body" in workflow
    assert "body_path: release-body.md" in workflow
    assert "append_body:" not in workflow
    assert "release-assets/*" in workflow
    assert "fail_on_unmatched_files: true" in workflow


def test_publish_workflow_dual_publishes_identical_signed_images() -> None:
    workflow = _publish_workflow()

    assert "registry-preflight:" in workflow
    assert "DOCKERHUB_USERNAME" in workflow
    assert "DOCKERHUB_TOKEN" in workflow
    assert "hub.docker.com/v2/repositories/${DOCKERHUB_NAMESPACE}/sibyl-${image}" in workflow
    assert "needs: [rc-gate, registry-preflight]" in workflow
    assert "GHCR_REPO" in workflow
    assert "DOCKERHUB_REPO" in workflow
    assert 'type=image,"name=${{ env.GHCR_REGISTRY }}' in workflow
    assert 'test "$ghcr_digest" = "$dockerhub_digest"' in workflow
    assert workflow.count('cosign sign --yes "$ghcr_ref"') == 1
    assert workflow.count('cosign sign --yes "$dockerhub_ref"') == 1
    assert '"schema_version": "sibyl-cosign-receipt-v2"' in workflow
    assert '"signed_images": [' in workflow
    assert workflow.count("Upload Cosign receipt") == 1


def test_publish_workflow_packages_and_publishes_immutable_helm_charts() -> None:
    workflow = _publish_workflow()

    assert "helm-package:" in workflow
    assert "helm-publish:" in workflow
    assert "azure/setup-helm@v5.0.1" in workflow
    assert "version: v3.20.2" in workflow
    assert "helm dependency list charts/surrealdb" in workflow
    assert "helm package charts/sibyl" in workflow
    assert "helm package charts/surrealdb" in workflow
    assert "Refusing to replace immutable chart" in workflow
    assert "same_chart_contents" in workflow
    assert "helm repo index new-packages" in workflow
    assert "--merge helm-repo/index.yaml" in workflow
    assert "https://raw.githubusercontent.com/hyperb1iss/sibyl/gh-pages" in workflow
    assert "git -C helm-repo push origin HEAD:gh-pages" in workflow
    assert "sibyl-helm-${{ inputs.tag }}" in workflow
    assert "Download Helm charts" in workflow
    assert "-name '*.tgz'" in workflow


def test_publish_workflow_summary_links_all_package_channels() -> None:
    workflow = _publish_workflow()

    assert "[sibyld](https://pypi.org/project/sibyld/" in workflow
    assert "[sibyl](https://aur.archlinux.org/packages/sibyl)" in workflow


def test_dogfood_image_workflow_is_docker_only_and_rc_scoped() -> None:
    workflow = _dogfood_image_workflow()

    assert "Publish Dogfood Images" in workflow
    assert "image_tag:" in workflow
    assert r"^1\.1\.[0-9]+-rc\.[0-9]+$" in workflow
    assert "REPOSITORY_OWNER: ${{ github.repository_owner }}" in workflow
    assert "ghcr.io/${REPOSITORY_OWNER}/sibyl-api" in workflow
    assert "ghcr.io/${REPOSITORY_OWNER}/sibyl-web" in workflow
    assert "moon run :check" in workflow
    assert "moon run python-package-build" not in workflow
    assert "gh-action-pypi-publish" not in workflow
    assert "homebrew_formula.py" not in workflow
    assert "aur_pkgbuild.py" not in workflow
    assert '-t "${REPO}:latest"' not in workflow
    assert "make_latest" not in workflow
    assert "softprops/action-gh-release" not in workflow


def test_dogfood_image_workflow_records_deployment_provenance() -> None:
    workflow = _dogfood_image_workflow()

    required_commits = (
        "36094084",
        "e59e9be1",
        "b9e3ade8",
        "6bf8881f",
        "4bf80afd",
        "2095b616",
        "dcb8d340",
        "98d9043c",
        "f74f23f4",
    )
    assert all(commit in workflow for commit in required_commits)
    assert 'git merge-base --is-ancestor "$commit" HEAD' in workflow
    assert "org.opencontainers.image.revision=${{ needs.gate.outputs.source_sha }}" in workflow
    assert "org.opencontainers.image.version=${{ needs.gate.outputs.image_tag }}" in workflow
    assert "sibyl-dogfood-image-receipt-v1" in workflow
    assert "sibyl-dogfood-deployment-image-receipt-v1" in workflow
    assert "source_revision" in workflow
    assert "source_commits" in workflow
    assert "required_source_commits" in workflow
    assert "image_digests" in workflow
    assert "expected_image_digests" in workflow
    assert "expected_version" in workflow
    assert '"deployment": deployment' in workflow
    assert "dogfood-digests-${{ matrix.image }}-${{ matrix.platform }}" in workflow
    assert "sibyl-dogfood-deployment-${{ needs.gate.outputs.image_tag }}-receipt" in workflow


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


def test_release_version_parser_normalizes_every_supported_form() -> None:
    cases = {
        "1.0.0": "1.0.0",
        "1.0.0-rc.1": "1.0.0rc1",
        "0.0.1": "0.0.1",
        "12.34.56-rc.789": "12.34.56rc789",
    }

    assert {version: pep440_version(version) for version in cases} == cases
    assert {version: parse_release_version(version).tag for version in cases} == {
        version: f"v{version}" for version in cases
    }


@pytest.mark.parametrize(
    "version",
    [
        "v1.0.0",
        "1.0",
        "1.0.0-alpha.1",
        "1.0.0-beta.1",
        "1.0.0-preview.1",
        "1.0.0-rc.0",
        "1.0.0-rc.01",
        "01.0.0",
        "1.0.0+build",
    ],
)
def test_release_version_parser_rejects_unsupported_forms_before_mutation(
    version: str,
) -> None:
    with pytest.raises(ValueError, match=r"expected X.Y.Z"):
        parse_release_version(version)


def test_python_packages_pin_sibyl_core_to_current_release() -> None:
    version = pep440_version((REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip())
    package_dependencies = {
        "apps/cli/pyproject.toml": set(),
        "apps/api/pyproject.toml": {"crawler", "embeddings", "graph", "graphrag", "llm"},
    }

    for path, extras in package_dependencies.items():
        pyproject = tomllib.loads((REPO_ROOT / path).read_text(encoding="utf-8"))
        requirement = _requirement_by_name(pyproject["project"]["dependencies"], "sibyl-core")

        assert requirement.extras == extras
        assert str(requirement.specifier) == f"=={version}"


def test_python_package_build_verifies_cli_bundle_data() -> None:
    task = _root_task("python-package-build")
    input_paths = {cast(str, entry.get("file") or entry.get("glob")) for entry in task["inputs"]}
    script = task["script"]

    assert "set -euo pipefail" in script
    assert "apps/cli/src/sibyl_cli/data/**/*" in input_paths
    assert "sibyl_cli/data/skills/sibyl/SKILL.md" in script
    assert "sibyl_cli/data/skill-packs/core.md" in script
    assert "sibyl_cli/data/skill-packs/workflows.md" in script
    assert "Requires-Dist" in script
    assert "sibyl_dev-*.whl" in script
    assert "sibyld-*.whl" in script
    assert "must declare exactly one sibyl-core dependency" in script
    assert "pep440_version" in script
    assert "expected_requirements" in script
    assert (
        '"sibyld-*.whl": (expected_core, {"crawler", "embeddings", "graph", "graphrag", "llm"})'
        in script
    )
    assert "expected_core" in script


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
