"""Carry source-reported outcomes without turning agent claims into proof."""

from __future__ import annotations

import hashlib
import json
from typing import Any

OUTCOME_METADATA_KEY = "operational_outcome"
OUTCOME_PROJECTION_VERSION = 7


def outcome_provenance(value: str | None) -> dict[str, Any]:
    """Describe the source field, not an independently verified task result."""
    return {
        "version": 1,
        "basis": "source_report",
        "scope": "trajectory",
        "present": value is not None,
        "value": value if value is not None else "",
    }


def outcome_context(metadata: object) -> str | None:
    """Render only a complete projection-owned outcome envelope.

    Long free-form outcomes remain exact in metadata. Their bounded display
    references that value instead of truncating it into a different claim.
    """
    if (
        not isinstance(metadata, dict)
        or metadata.get("category") != "operational_experience"
        or type(metadata.get("operational_schema_version")) is not int
        or metadata["operational_schema_version"] < OUTCOME_PROJECTION_VERSION
    ):
        return None
    outcome = metadata.get(OUTCOME_METADATA_KEY)
    if not isinstance(outcome, dict) or set(outcome) != {
        "version",
        "basis",
        "scope",
        "present",
        "value",
    }:
        return None
    if (
        type(outcome["version"]) is not int
        or outcome["version"] != 1
        or outcome["basis"] != "source_report"
        or outcome["scope"] != "trajectory"
        or type(outcome["present"]) is not bool
        or not isinstance(outcome["value"], str)
        or (not outcome["present"] and outcome["value"] != "")
    ):
        return None
    value = outcome["value"]
    if not outcome["present"]:
        display = "not recorded"
    elif len(json.dumps(value, ensure_ascii=False)) <= 256:
        display = json.dumps(value, ensure_ascii=False)
    else:
        digest = hashlib.sha256(value.encode()).hexdigest()
        display = f"full value in {OUTCOME_METADATA_KEY} metadata (sha256 {digest})"
    return (
        f"Source-reported trajectory outcome: {display}\n"
        "Actions and reasoning are agent reports; a trajectory outcome does not "
        "establish success of each action."
    )
