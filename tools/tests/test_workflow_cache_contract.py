from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import tomllib
import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TOOLCHAIN_SELECTORS = frozenset((".prototools", ".python-version"))
HASH_FILES_PATTERN = re.compile(r"hashFiles\s*\(([^)]*)\)")
QUOTED_ARGUMENT_PATTERN = re.compile(r"(['\"])([^'\"]+)\1")
SETUP_TOOLCHAIN_ACTION = "moonrepo/setup-toolchain@v0.6.4"
SETUP_NODE_ACTION = "actions/setup-node@v7"
SETUP_PYTHON_ACTION = "actions/setup-python@v7"
PROTO_VERSION = "0.60.2"
NODE_VERSION = "24.19.0"
PYTHON_VERSION = "3.13.15"
EXPECTED_API_PYTHON_STAGES = 2


def _workflow_paths(workflows_dir: Path) -> list[Path]:
    return sorted((*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")))


def _workflow_steps(
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
                label = f"{workflow_path.name}:{job_name}:step-{step_number}"
                yield label, step


def _action_steps(
    workflow_paths: Iterable[Path],
    action_prefix: str,
) -> Iterator[tuple[str, dict[str, Any]]]:
    for label, step in _workflow_steps(workflow_paths):
        if str(step.get("uses", "")).startswith(action_prefix):
            yield label, step


def _moon_output_caches(
    workflow_paths: Iterable[Path],
) -> Iterator[tuple[str, dict[str, Any]]]:
    for label, step in _action_steps(workflow_paths, "actions/cache@"):
        cache = step.get("with", {})
        if isinstance(cache, dict) and ".moon/cache" in str(cache.get("path", "")):
            yield label, cache


def _hashes_toolchain_selectors(value: str) -> bool:
    for match in HASH_FILES_PATTERN.finditer(value):
        arguments = {
            argument.group(2) for argument in QUOTED_ARGUMENT_PATTERN.finditer(match.group(1))
        }
        if arguments >= TOOLCHAIN_SELECTORS:
            return True
    return False


def test_moon_output_caches_restore_only_matching_toolchains() -> None:
    caches = list(_moon_output_caches(_workflow_paths(WORKFLOWS_DIR)))

    assert caches, "no moon output caches found"
    for label, cache in caches:
        key = str(cache.get("key", ""))
        assert _hashes_toolchain_selectors(key), f"unsafe cache key in {label}: {key}"

        restore_prefixes = str(cache.get("restore-keys", "")).splitlines()
        for restore_prefix in filter(None, map(str.strip, restore_prefixes)):
            assert _hashes_toolchain_selectors(restore_prefix), (
                f"unsafe restore prefix in {label}: {restore_prefix}"
            )


def test_every_moon_setup_uses_the_repo_proto_version() -> None:
    setup_steps = list(_action_steps(_workflow_paths(WORKFLOWS_DIR), "moonrepo/setup-toolchain@"))

    assert setup_steps, "no moon setup-toolchain steps found"
    for label, step in setup_steps:
        assert step.get("uses") == SETUP_TOOLCHAIN_ACTION, f"unpinned setup action in {label}"
        inputs = step.get("with", {})
        assert isinstance(inputs, dict), f"invalid setup inputs in {label}"
        assert inputs.get("proto-version") == PROTO_VERSION, f"unpinned proto version in {label}"


def test_every_moon_task_hashes_repo_toolchain_selectors() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / ".moon" / "tasks" / "toolchain.yml").read_text(encoding="utf-8")
    )

    assert set(config["implicitInputs"]) == {"/.prototools", "/.python-version"}


def test_direct_workflow_toolchains_use_exact_repo_versions() -> None:
    docs_path = WORKFLOWS_DIR / "docs.yml"
    publish_path = WORKFLOWS_DIR / "publish.yml"

    docs_setups = list(_action_steps((docs_path,), "actions/setup-node@"))
    assert len(docs_setups) == 1, "expected exactly one docs setup-node step"
    docs_label, docs_setup = docs_setups[0]
    assert docs_setup.get("uses") == SETUP_NODE_ACTION, f"unpinned Node action in {docs_label}"
    docs_inputs = docs_setup.get("with", {})
    assert isinstance(docs_inputs, dict), f"invalid Node setup inputs in {docs_label}"
    assert docs_inputs.get("node-version") == NODE_VERSION

    python_setups = list(_action_steps((publish_path,), "actions/setup-python@"))
    assert python_setups
    for label, python_setup in python_setups:
        assert python_setup.get("uses") == SETUP_PYTHON_ACTION, f"unpinned Python action in {label}"
        python_inputs = python_setup.get("with", {})
        assert isinstance(python_inputs, dict), f"invalid Python setup inputs in {label}"
        assert python_inputs.get("python-version") == PYTHON_VERSION, (
            f"unpinned direct Python setup in {label}"
        )


def test_api_dockerfile_uses_repo_python_and_uv_versions() -> None:
    prototools = tomllib.loads((REPO_ROOT / ".prototools").read_text(encoding="utf-8"))
    dockerfile = (REPO_ROOT / "apps" / "api" / "Dockerfile").read_text(encoding="utf-8")

    python_image = f"FROM python:{prototools['python']}-slim-bookworm"
    uv_image = f"COPY --from=ghcr.io/astral-sh/uv:{prototools['uv']} /uv /uvx /bin/"
    assert dockerfile.count(python_image) == EXPECTED_API_PYTHON_STAGES
    assert uv_image in dockerfile


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


def test_toolchain_hash_discovery_tolerates_formatting_and_order() -> None:
    assert _hashes_toolchain_selectors('${{ hashFiles( ".python-version" , ".prototools" ) }}')
    assert not _hashes_toolchain_selectors(
        "${{ hashFiles('.prototools') }}-${{ hashFiles('.python-version') }}"
    )
