"""Explicit per-request allocation and native transport budgets for archive checks."""

from urllib.parse import urlsplit

from sibyl.api.routes.archive_import_upload import ArchiveUploadBudget
from sibyl.config import settings
from sibyl_core.migrate.personal_archive_intake import (
    ArchiveIntakeBudget,
    ArchiveIntakeCapacityError,
)
from sibyl_core.migrate.personal_archive_plan import CheckedArchivePlan, checked_plan_bytes

_MIB = 1024**2


def archive_import_budgets() -> tuple[ArchiveIntakeBudget, ArchiveUploadBudget]:
    """Respect the configured transport resource without reducing other transports.

    HTTP's default follows the server's configurable 4 MiB RPC body resource.
    An explicitly expanded deployment can override the complete request budget.
    Multipart overhead includes two bounded header sets and framing.
    """
    metadata_bytes = settings.archive_import_metadata_transaction_bytes
    if metadata_bytes is None:
        metadata_bytes = (
            4 * _MIB
            if urlsplit(settings.resolved_surreal_url).scheme in {"http", "https"}
            else 64 * _MIB
        )
    intake = ArchiveIntakeBudget(
        compressed_bytes=settings.archive_import_compressed_bytes,
        inflated_bytes=settings.archive_import_inflated_bytes,
        member_bytes=settings.archive_import_member_bytes,
        members=settings.archive_import_members,
        json_depth=settings.archive_import_json_depth,
        json_scalar_bytes=settings.archive_import_json_scalar_bytes,
        json_nodes=settings.archive_import_json_nodes,
        parsed_rows=settings.archive_import_parsed_rows,
        encoded_artifact_bytes=settings.archive_import_encoded_artifact_bytes,
        encoded_plan_bytes=settings.archive_import_encoded_plan_bytes,
        metadata_transaction_bytes=metadata_bytes,
    )
    request_bytes = settings.archive_import_request_bytes
    if request_bytes is None:
        request_bytes = (
            intake.compressed_bytes
            + settings.archive_import_options_bytes
            + 2 * settings.archive_import_header_bytes
            + 1024
        )
    upload = ArchiveUploadBudget(
        request_bytes=request_bytes,
        options_bytes=settings.archive_import_options_bytes,
        header_bytes=settings.archive_import_header_bytes,
    )
    return intake, upload


def validate_archive_plan_capacity(plan: CheckedArchivePlan, budget: ArchiveIntakeBudget) -> None:
    """Bound a fresh validated canonical plan before metadata persistence."""
    snapshot = CheckedArchivePlan.model_validate(plan.model_dump(mode="python"))
    if len(checked_plan_bytes(snapshot).encode("utf-8")) > budget.encoded_plan_bytes:
        raise ArchiveIntakeCapacityError("archive encoded-plan budget exceeded")
