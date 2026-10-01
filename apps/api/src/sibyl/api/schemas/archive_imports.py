"""Typed request options and redacted checked archive status."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sibyl_core.migrate.personal_archive_plan import (
    ArchiveContractModel,
    ArchiveMappings,
    ArchivePreviewCounts,
)


class ArchiveCheckOptions(ArchiveContractModel):
    mappings: ArchiveMappings
    conflict_policy: Literal["additive"] = "additive"


class ArchiveCheckStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: Literal["checked"]
    contract_version: int
    archive_sha256: str
    artifact_sha256: str
    checked_plan_sha256: str
    mappings_sha256: str
    revision: int
    created_at: datetime
    preview_counts: dict[str, ArchivePreviewCounts]
    replayed: bool = False
