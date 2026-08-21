from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
TOOLCHAIN_DIGEST = "${{ hashFiles('.prototools') }}"


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
        assert TOOLCHAIN_DIGEST in key, f"unsafe cache key in {label}: {key}"

        restore_prefixes = str(cache.get("restore-keys", "")).splitlines()
        for restore_prefix in filter(None, map(str.strip, restore_prefixes)):
            assert TOOLCHAIN_DIGEST in restore_prefix, (
                f"unsafe restore prefix in {label}: {restore_prefix}"
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
