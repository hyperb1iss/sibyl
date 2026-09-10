"""Insert one corrected review candidate through the existing raw writer."""

from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sibyl_core.backends.surreal import SurrealContentClient
from sibyl_core.services import content_client
from sibyl_core.services.dream_checkpoints import _IMMUTABLE_CANDIDATE


@dataclass(frozen=True)
class ValidationCandidateWrite:
    execution_id: str
    result_json: str
    source_guard: str
    guard_params: dict[str, Any]

    @property
    def id(self) -> str:
        return str(uuid5(NAMESPACE_URL, "sibyl:validation-correction:" + self.execution_id))


async def insert_validation_candidate(
    client: SurrealContentClient,
    row: dict[str, Any],
    write: ValidationCandidateWrite,
    derivation: dict[str, object],
) -> dict[str, Any]:
    """Preserve lifecycle on replay; never recreate a purged corrected candidate."""
    if row["uuid"] != write.id or derivation["target_id"] != write.id:
        raise ValueError("Corrected candidate identity differs")
    # Schema defaults normalize missing optional values. Compare the same null
    # representation on both the incoming and stored immutable field vectors.
    import re

    fields = re.findall(r"\$memory\.[a-zA-Z_.]+", _IMMUTABLE_CANDIDATE)
    fingerprint = (
        "crypto::sha256(type::string([" + ",".join(f"({field} ?? NULL)" for field in fields) + "]))"
    )
    result = content_client.normalize_records(
        await client.execute_query(
            "RETURN {"
            + write.source_guard
            + """
        LET $stage=(SELECT * FROM memory_validation_executions WHERE uuid=$execution
            AND organization_id=$org AND principal_id=$principal)[0];
        IF $stage=NONE OR $stage.state!='returned' OR $stage.purged
            OR $stage.result_json!=$result { THROW 'Correction stage changed'; };
        LET $existing=(SELECT * FROM raw_captures WHERE uuid=$candidate)[0];
        LET $memory=$row;
        LET $expected="""
            + fingerprint
            + ";"
            + """
        IF $existing!=NONE {
            LET $memory=$existing;
            IF """
            + fingerprint
            + """!=$expected { THROW 'Corrected candidate changed'; };
            RETURN $existing;
        };
        LET $retired=(SELECT * FROM source_states WHERE organization_id=$org
            AND source_kind='raw_capture' AND source_id=$candidate);
        IF array::len($retired)>0 { THROW 'Corrected candidate was purged'; };
        CREATE raw_captures CONTENT $row;
        CREATE memory_derivations CONTENT $derivation;
        RETURN (SELECT * FROM raw_captures WHERE uuid=$candidate)[0];
        };""",
            **write.guard_params,
            execution=write.execution_id,
            result=write.result_json,
            candidate=write.id,
            row={**row, "derivation_required": True},
            derivation=derivation,
        )
    )
    if len(result) != 1:
        raise ValueError("Corrected candidate was not persisted")
    return result[0]
