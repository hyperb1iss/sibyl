"""Fail-closed CI planning for changed repository paths."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path


class UnmatchedPathsError(ValueError):
    """Raised when a changed path has no explicit CI classification."""

    def __init__(self, paths: tuple[str, ...]) -> None:
        self.paths = paths
        super().__init__(f"unmatched changed paths: {', '.join(paths)}")


@dataclass(slots=True)
class ChangePlan:
    docs_changed: bool = False
    hooks_changed: bool = False
    infra_changed: bool = False
    skills_changed: bool = False
    runtime_changed: bool = False
    web_changed: bool = False
    ci_changed: bool = False
    release_changed: bool = False
    helm_changed: bool = False
    api_image_changed: bool = False
    web_image_changed: bool = False
    changed_files: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def full(cls) -> ChangePlan:
        return cls(
            docs_changed=True,
            hooks_changed=True,
            infra_changed=True,
            skills_changed=True,
            runtime_changed=True,
            web_changed=True,
            ci_changed=True,
            release_changed=True,
            helm_changed=True,
            api_image_changed=True,
            web_image_changed=True,
        )

    @property
    def image_scan_matrix(self) -> list[str]:
        matrix: list[str] = []
        if self.api_image_changed:
            matrix.append("api")
        if self.web_image_changed:
            matrix.append("web")
        return matrix

    def outputs(self) -> dict[str, str]:
        run_static = any(
            (
                self.docs_changed,
                self.hooks_changed,
                self.infra_changed,
                self.skills_changed,
                self.runtime_changed,
                self.ci_changed,
                self.release_changed,
                self.helm_changed,
            )
        )
        run_runtime = self.runtime_changed or self.ci_changed or self.release_changed
        image_matrix = self.image_scan_matrix
        values: dict[str, bool | str] = {
            "docs_changed": self.docs_changed,
            "hooks_changed": self.hooks_changed,
            "infra_changed": self.infra_changed,
            "skills_changed": self.skills_changed,
            "runtime_changed": self.runtime_changed,
            "web_changed": self.web_changed,
            "ci_changed": self.ci_changed,
            "release_changed": self.release_changed,
            "helm_changed": self.helm_changed,
            "image_changed": bool(image_matrix),
            "image_scan_matrix": json.dumps(image_matrix, separators=(",", ":")),
            "run_static": run_static,
            "run_build": run_runtime,
            "run_tests": run_runtime,
            "run_e2e": run_runtime,
            "run_storybook": self.web_changed,
            "run_image_scan": bool(image_matrix),
            "run_release": self.release_changed or self.ci_changed,
            "run_helm": self.helm_changed or self.release_changed or self.ci_changed,
        }
        return {
            key: str(value).lower() if isinstance(value, bool) else value
            for key, value in values.items()
        }


_DOCUMENTATION_ROOTS = {
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "docs.md",
}
_RUNTIME_ROOTS = {
    ".env.example",
    ".env.quickstart.example",
    ".env.quickstart.test",
    ".env.test.example",
    ".irisconfig",
    ".pre-commit-config.yaml",
    ".prettierignore",
    ".prettierrc.yaml",
    ".prototools",
    ".gitignore",
    "compose.e2e.yml",
    "docker-compose.prod.yml",
    "docker-compose.quickstart.test.yml",
    "docker-compose.quickstart.yml",
    "docker-compose.yml",
    "moon.yml",
    "package.json",
    "pnpm-lock.yaml",
    "pnpm-workspace.yaml",
    "pyproject.toml",
    "setup-dev.ps1",
    "setup-dev.sh",
    "uv.lock",
}
_RELEASE_ROOTS = {"Tiltfile", "VERSION", "install.sh"}
_API_IMAGE_INPUTS = {
    "apps/api/Dockerfile",
    "apps/api/pyproject.toml",
    "packages/python/sibyl-core/pyproject.toml",
    "pyproject.toml",
    "uv.lock",
}
_WEB_IMAGE_INPUTS = {
    "apps/web/Dockerfile",
    "apps/web/package.json",
    "apps/web/pnpm-lock.yaml",
    "package.json",
    "pnpm-lock.yaml",
}
_RELEASE_WORKFLOWS = {
    ".github/workflows/ci.yml",
    ".github/workflows/image-cve-gate.yml",
    ".github/workflows/nightly-regression.yml",
    ".github/workflows/publish-dogfood-images.yml",
    ".github/workflows/publish.yml",
    ".github/workflows/release.yml",
}


def _under(path: str, root: str) -> bool:
    return path.startswith(f"{root}/")


def _classify_static_area(plan: ChangePlan, path: str) -> bool:
    matched = False
    if _under(path, "docs") or path in _DOCUMENTATION_ROOTS:
        plan.docs_changed = True
        matched = True
    if _under(path, "hooks"):
        plan.hooks_changed = True
        matched = True
    if _under(path, "skills") or _under(path, ".claude"):
        plan.skills_changed = True
        matched = True
    if _under(path, "infra") or _under(path, ".devcontainer"):
        plan.infra_changed = True
        matched = True
    if _under(path, ".github"):
        plan.ci_changed = True
        plan.api_image_changed = True
        plan.web_image_changed = True
        plan.docs_changed = plan.docs_changed or path == ".github/workflows/docs.yml"
        matched = True
    return matched


def _classify_runtime_area(plan: ChangePlan, path: str) -> bool:
    matched = (
        _under(path, "apps")
        or _under(path, "packages")
        or _under(path, "tools")
        or _under(path, "scripts")
        or _under(path, "benchmarks")
        or _under(path, "baselines")
        or _under(path, ".moon")
        or path in _RUNTIME_ROOTS
    )
    if matched:
        plan.runtime_changed = True
    if _under(path, "apps/web"):
        plan.web_changed = True
    return matched


def _classify_release_area(plan: ChangePlan, path: str) -> bool:
    release_surface = (
        _under(path, "charts") or path in _RELEASE_ROOTS or _under(path, "tools/release")
    )
    release_control = path in _RELEASE_WORKFLOWS or _under(path, ".github/actions")
    if release_surface or release_control:
        plan.runtime_changed = True
        plan.release_changed = True
        plan.helm_changed = True
        plan.api_image_changed = True
        plan.web_image_changed = True
    return release_surface


def _classify_image_area(plan: ChangePlan, path: str) -> bool:
    if path in _API_IMAGE_INPUTS:
        plan.api_image_changed = True
    if path in _WEB_IMAGE_INPUTS:
        plan.web_image_changed = True
    shared_image_input = path in {".dockerignore", ".trivyignore", "Dockerfile"} or path.endswith(
        "/Dockerfile"
    )
    if shared_image_input:
        plan.api_image_changed = True
        plan.web_image_changed = True
        plan.runtime_changed = True
    return shared_image_input


def classify_changed_paths(paths: tuple[str, ...]) -> ChangePlan:
    """Return the complete CI plan or reject the first unowned surface."""
    if not paths:
        return ChangePlan.full()

    plan = ChangePlan(changed_files=paths)
    unmatched: list[str] = []

    for path in paths:
        matched = any(
            (
                _classify_static_area(plan, path),
                _classify_runtime_area(plan, path),
                _classify_release_area(plan, path),
                _classify_image_area(plan, path),
            )
        )
        if not matched:
            unmatched.append(path)

    if unmatched:
        raise UnmatchedPathsError(tuple(unmatched))
    return plan


def _load_paths(path: Path) -> tuple[str, ...]:
    raw = path.read_bytes()
    return tuple(
        item.decode("utf-8", errors="surrogateescape") for item in raw.split(b"\0") if item
    )


def _write_outputs(path: Path, outputs: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for key, value in outputs.items():
            stream.write(f"{key}={value}\n")


def _write_summary(path: Path, plan: ChangePlan, outputs: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write("### Changed Files\n\n")
        if plan.changed_files:
            for changed_path in plan.changed_files:
                stream.write(f"- `{changed_path}`\n")
        else:
            stream.write("No diff detected; defaulting to the full CI path.\n")
        stream.write("\n### CI Plan\n\n")
        for label in (
            "run_static",
            "run_build",
            "run_tests",
            "run_e2e",
            "run_storybook",
            "run_image_scan",
            "run_release",
            "run_helm",
        ):
            stream.write(f"- {label.removeprefix('run_').replace('_', ' ')}: {outputs[label]}\n")
        stream.write(f"- image matrix: {outputs['image_scan_matrix']}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify changed paths for CI.")
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        plan = classify_changed_paths(_load_paths(args.changed_files))
    except UnmatchedPathsError as exc:
        for unmatched in exc.paths:
            sys.stderr.write(f"::error::unmatched CI path: {unmatched}\n")
        return 1

    outputs = plan.outputs()
    _write_outputs(args.github_output, outputs)
    _write_summary(args.summary, plan, outputs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
