from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).parents[2]
WORKFLOWS_WITH_MOON_OUTPUT_CACHES = (
    "ci.yml",
    "eval.yml",
    "longmemeval-v2.yml",
    "nightly-regression.yml",
    "okf-memory-changelog.yml",
    "release.yml",
)
MOON_CACHE_KEY = re.compile(
    r"key: (?P<key>moon-[^\n]+)\n"
    r"\s+restore-keys: \|\n"
    r"\s+(?P<restore>moon-[^\n]+)"
)
TOOLCHAIN_DIGEST = "${{ hashFiles('.prototools') }}"


def test_moon_output_caches_restore_only_matching_toolchains() -> None:
    for workflow_name in WORKFLOWS_WITH_MOON_OUTPUT_CACHES:
        workflow_path = REPO_ROOT / ".github" / "workflows" / workflow_name
        workflow = workflow_path.read_text(encoding="utf-8")
        assert isinstance(yaml.safe_load(workflow), dict)
        caches = list(MOON_CACHE_KEY.finditer(workflow))

        assert caches, f"{workflow_name} has no moon output cache contract"
        for cache in caches:
            key = cache.group("key")
            restore = cache.group("restore")
            assert TOOLCHAIN_DIGEST in key, f"unsafe cache key in {workflow_name}: {key}"
            assert TOOLCHAIN_DIGEST in restore, (
                f"unsafe restore prefix in {workflow_name}: {restore}"
            )
