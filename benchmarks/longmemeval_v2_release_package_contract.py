"""Score-blind inputs for LongMemEval-V2 release packaging."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmarks.longmemeval_v2_release_inputs import (
    StagePlanError,
    bind_artifact,
    require_artifact,
    require_exact_keys,
)

PACKAGE_INPUT_KEYS = frozenset({"system_description", "adapter"})


def build_package_inputs(
    *,
    system_description_path: Path,
    adapter_path: Path,
) -> dict[str, Any]:
    inputs = {
        "system_description": bind_artifact(
            system_description_path,
            name="release system description",
        ),
        "adapter": bind_artifact(adapter_path, name="release adapter"),
    }
    if any(item["size_bytes"] == 0 for item in inputs.values()):
        raise StagePlanError("release package inputs must not be empty")
    return inputs


def require_package_inputs(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StagePlanError("release package inputs are missing")
    require_exact_keys(raw, PACKAGE_INPUT_KEYS, name="release package inputs")
    inputs = {
        "system_description": require_artifact(
            raw.get("system_description"),
            name="release system description",
        ),
        "adapter": require_artifact(raw.get("adapter"), name="release adapter"),
    }
    if any(item["size_bytes"] == 0 for item in inputs.values()):
        raise StagePlanError("release package inputs must not be empty")
    return inputs
