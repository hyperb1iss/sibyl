from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TOOLCHAIN_DIGESTS = ("${{ hashFiles('.prototools', '.python-version') }}",)
SETUP_TOOLCHAIN_ACTION = "moonrepo/setup-toolchain@v0.6.4"
PROTO_VERSION = "0.60.2"
NODE_VERSION = "24.19.0"
PYTHON_VERSION = "3.13.15"


def _workflow_paths(workflows_dir: Path) -> list[Path]:
    return sorted((*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")))


def _moon_output_caches(
    workflow_paths: Iterable[Path],
) -> Iterator[tuple[str, dict[str, Any]]]:
    for workflow_path in workflow_paths:
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        assert isinstance(workflow, dict), f"invalid workflow: {workflow_path.name}"

        jobs = workflow.get("jobs", {})
        assert isinstance(jobs, dict), f"invalid jobs mapping: {workflow_path.name}"
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            steps = job.get("steps", [])
            if not isinstance(steps, list):
                continue
            for step_number, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    continue
                uses = str(step.get("uses", ""))
                cache = step.get("with", {})
                if not isinstance(cache, dict):
                    continue
                if uses.startswith("actions/cache@") and ".moon/cache" in str(
                    cache.get("path", "")
                ):
                    label = f"{workflow_path.name}:{job_name}:step-{step_number}"
                    yield label, cache


def test_moon_output_caches_restore_only_matching_toolchains() -> None:
    caches = list(_moon_output_caches(_workflow_paths(WORKFLOWS_DIR)))

    assert caches, "no moon output caches found"
    for label, cache in caches:
        key = str(cache.get("key", ""))
        for digest in TOOLCHAIN_DIGESTS:
            assert digest in key, f"unsafe cache key in {label}: {key}"

        restore_prefixes = str(cache.get("restore-keys", "")).splitlines()
        for restore_prefix in filter(None, map(str.strip, restore_prefixes)):
            for digest in TOOLCHAIN_DIGESTS:
                assert digest in restore_prefix, (
                    f"unsafe restore prefix in {label}: {restore_prefix}"
                )


def test_every_moon_setup_uses_the_repo_proto_version() -> None:
    setup_steps: list[tuple[str, dict[str, Any]]] = []
    for workflow_path in _workflow_paths(WORKFLOWS_DIR):
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        jobs = workflow.get("jobs", {})
        for job_name, job in jobs.items():
            if not isinstance(job, dict):
                continue
            for step_number, step in enumerate(job.get("steps", []), start=1):
                if not isinstance(step, dict) or step.get("uses") != SETUP_TOOLCHAIN_ACTION:
                    continue
                setup_steps.append((f"{workflow_path.name}:{job_name}:step-{step_number}", step))

    assert setup_steps, "no moon setup-toolchain steps found"
    for label, step in setup_steps:
        inputs = step.get("with", {})
        assert inputs.get("proto-version") == PROTO_VERSION, f"unpinned proto version in {label}"


def test_every_moon_task_hashes_repo_toolchain_selectors() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / ".moon" / "tasks" / "toolchain.yml").read_text(encoding="utf-8")
    )

    assert set(config["implicitInputs"]) == {"/.prototools", "/.python-version"}


def test_direct_workflow_toolchains_use_exact_repo_versions() -> None:
    docs = yaml.safe_load((WORKFLOWS_DIR / "docs.yml").read_text(encoding="utf-8"))
    publish = yaml.safe_load((WORKFLOWS_DIR / "publish.yml").read_text(encoding="utf-8"))

    docs_setup = next(
        step
        for step in docs["jobs"]["build"]["steps"]
        if step.get("uses") == "actions/setup-node@v7"
    )
    assert docs_setup["with"]["node-version"] == NODE_VERSION

    python_setups = [
        (job_name, step)
        for job_name, job in publish["jobs"].items()
        for step in job.get("steps", [])
        if step.get("uses") == "actions/setup-python@v7"
    ]
    assert python_setups
    for job_name, python_setup in python_setups:
        assert python_setup["with"]["python-version"] == PYTHON_VERSION, (
            f"unpinned direct Python setup in publish:{job_name}"
        )


def test_moon_output_cache_discovery_is_structural(tmp_path: Path) -> None:
    workflow_path = tmp_path / "new-workflow.yaml"
    workflow_path.write_text(
        """
jobs:
  cache-order:
    steps:
      - uses: actions/cache@v6
        with:
          path: .moon/cache
          key: safe
          restore-keys: safe-
      - uses: actions/cache@v6
        with:
          restore-keys: unsafe-
          path: .moon/cache
          key: unsafe
""".lstrip(),
        encoding="utf-8",
    )

    caches = list(_moon_output_caches(_workflow_paths(tmp_path)))
    assert [label for label, _ in caches] == [
        "new-workflow.yaml:cache-order:step-1",
        "new-workflow.yaml:cache-order:step-2",
    ]
