"""Resolve a candidate's producing execution from its private derivation."""

import json
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sibyl_core.services.source_observations import SourceUnavailableError
from sibyl_core.services.validation_dependencies import dependency_reference, validate_dependency
from sibyl_core.services.validation_execution import ValidationExecution


def validate_origin_row(
    derivation: dict[str, Any], row: dict[str, Any] | None, *, historical: bool = False
) -> None:
    origin = derivation.get("origin_execution_id")
    if (
        not isinstance(origin, str)
        or row is None
        or row.get("uuid") != origin
        or row.get("organization_id") != derivation.get("organization_id")
        or row.get("principal_id") != derivation.get("principal_id")
        or derivation.get("target_id")
        != str(uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + origin))
        or row.get("state") != "returned"
        or (row.get("purged") and not historical)
        or (not row.get("result_json") and not (historical and row.get("purged")))
    ):
        raise SourceUnavailableError()
    if not (historical and row.get("purged")):
        validate_dependency(
            row,
            dependency_reference(row),
            str(derivation["organization_id"]),
            str(derivation["principal_id"]),
        )
        from sibyl_core.services.validation_result_codec import decode_validation_result
        from sibyl_core.tasks.ordinary_proposal_result import OrdinaryProposalResult
        from sibyl_core.tasks.reflection_correction import ReflectionCorrectionResult

        result = decode_validation_result(json.loads(row["result_json"]))
        if isinstance(result, OrdinaryProposalResult):
            if result.proposal.procedure is None or result.validation_error is not None:
                raise SourceUnavailableError()
        elif isinstance(result, ReflectionCorrectionResult):
            if result.status != "corrected" or not result.content:
                raise SourceUnavailableError()
        else:
            raise SourceUnavailableError()


async def load_validation_origin(derivation: dict[str, Any] | None) -> dict[str, Any] | None:
    if derivation is None or derivation.get("origin_execution_id") is None:
        return None
    stage = ValidationExecution(
        str(derivation["origin_execution_id"]),
        str(derivation["organization_id"]),
        str(derivation["principal_id"]),
    )
    row = await stage.load()
    validate_origin_row(derivation, row)
    await stage.result()
    return row
