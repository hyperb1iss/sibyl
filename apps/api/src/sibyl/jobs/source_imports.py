"""Source import jobs backed by core source adapters."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import uuid4

import structlog

from sibyl.api.event_types import WSEvent
from sibyl.config import settings
from sibyl.services.document_adapters import (
    DOCUMENT_TEXT_ADAPTER_NAME,
    DOCUMENT_URL_ADAPTER_NAME,
    ensure_document_adapters_registered,
)
from sibyl_core.auth import MemoryPolicyContext, authorize_memory_write
from sibyl_core.models.sources import SourceImportCheckpoint, SourceRecord
from sibyl_core.services.mailbox_adapter import IMAP_ADAPTER_NAME, ensure_mailbox_adapter_registered
from sibyl_core.services.source_adapters import (
    RawMemoryRememberer,
    SourceRawMemoryWrite,
    SourceRecordDuplicateChecker,
    SourceRecordImportDecision,
    SourceRecordSupersessionHandler,
    get_source_adapter,
    import_source_batch,
    plan_source_import,
)
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    get_raw_memory,
    get_raw_memory_by_dedupe_key,
    get_raw_memory_by_source_id,
    save_raw_memory,
)
from sibyl_core.services.transcript_adapters import ensure_transcript_adapters_registered

log = structlog.get_logger()


class SourceImportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(slots=True)
class SourceImportRun:
    import_id: str
    organization_id: str
    principal_id: str
    source_uri: str
    adapter_name: str
    options: dict[str, Any]
    policy_context: dict[str, Any]
    batch_size: int
    promotion_preview_approved: bool
    status: SourceImportStatus = SourceImportStatus.PENDING
    adapter_version: str | None = None
    source_identity: str | None = None
    source_version: str | None = None
    privacy_class: str | None = None
    target_memory_scope: str | None = None
    target_scope_key: str | None = None
    checkpoint: SourceImportCheckpoint | None = None
    imported_count: int = 0
    skipped_count: int = 0
    dedupe_count: int = 0
    superseded_count: int = 0
    attachment_count: int = 0
    extraction_pending_count: int = 0
    raw_memory_ids: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    dedupe_keys: list[str] = field(default_factory=list)
    duplicate_dedupe_keys: list[str] = field(default_factory=list)
    skipped_records: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    raw_memory_by_source_id: dict[str, str] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    def status_payload(self) -> dict[str, Any]:
        target_memory_scope = self.target_memory_scope or self.options.get("target_memory_scope")
        target_scope_key = self.target_scope_key or self.options.get("target_scope_key")
        return {
            "import_id": self.import_id,
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "source_identity": self.source_identity,
            "source_version": self.source_version,
            "status": self.status.value,
            "privacy_class": self.privacy_class,
            "target_memory_scope": target_memory_scope,
            "target_scope_key": target_scope_key,
            "checkpoint": self.checkpoint.model_dump(mode="json") if self.checkpoint else None,
            "progress": {
                "imported_count": self.imported_count,
                "skipped_count": self.skipped_count,
                "dedupe_count": self.dedupe_count,
                "superseded_count": self.superseded_count,
                "error_count": len(self.errors),
                "attachment_count": self.attachment_count,
                "extraction_pending_count": self.extraction_pending_count,
                "raw_memory_count": len(self.raw_memory_ids),
            },
            "raw_memory_ids": list(self.raw_memory_ids),
            "dedupe_keys": list(self.dedupe_keys),
            "duplicate_dedupe_keys": list(self.duplicate_dedupe_keys),
            "skipped_records": list(self.skipped_records),
            "errors": list(self.errors),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


_SOURCE_IMPORT_RUNS: dict[str, SourceImportRun] = {}
_NON_PATH_SOURCE_ADAPTERS = {
    DOCUMENT_TEXT_ADAPTER_NAME,
    DOCUMENT_URL_ADAPTER_NAME,
    IMAP_ADAPTER_NAME,
}
_IMAP_PERSISTED_ALLOWED_OPTIONS = frozenset(
    {"privacy_class", "target_memory_scope", "target_scope_key", "username"}
)
_IMAP_SECRET_OPTION_MARKERS = (
    "api_key",
    "bearer",
    "credential",
    "oauth",
    "password",
    "secret",
    "token",
)
_IMAP_PERSISTED_PRIVILEGED_OPTIONS = frozenset({"allow_private_network"})


def _store_run(run: SourceImportRun) -> None:
    run.touch()
    _SOURCE_IMPORT_RUNS[run.import_id] = run


def _source_import_event_payload(run: SourceImportRun) -> dict[str, Any]:
    status_payload = run.status_payload()
    payload = {
        "import_id": status_payload["import_id"],
        "status": status_payload["status"],
        "progress": status_payload["progress"],
        "created_at": status_payload["created_at"],
        "updated_at": status_payload["updated_at"],
        "completed_at": status_payload["completed_at"],
    }
    for field_name in ("created_at", "updated_at", "completed_at"):
        value = payload.get(field_name)
        if isinstance(value, datetime):
            payload[field_name] = value.isoformat()
    return payload


async def _safe_broadcast_source_import(run: SourceImportRun) -> None:
    try:
        from sibyl.api.pubsub import publish_event

        await publish_event(
            WSEvent.SOURCE_IMPORT_UPDATED,
            _source_import_event_payload(run),
            org_id=run.organization_id,
        )
    except Exception:
        log.debug("source_import_broadcast_failed", import_id=run.import_id)


def _datetime_from_record(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _dict_from_record(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _string_list_from_record(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _dict_list_from_record(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_dict_from_record(item) for item in value if isinstance(item, dict)]


def _run_record(run: SourceImportRun) -> dict[str, object]:
    return {
        "uuid": run.import_id,
        "organization_id": run.organization_id,
        "principal_id": run.principal_id,
        "adapter_name": run.adapter_name,
        "adapter_version": run.adapter_version,
        "source_uri": run.source_uri,
        "source_identity": run.source_identity,
        "source_version": run.source_version,
        "privacy_class": run.privacy_class,
        "target_memory_scope": run.target_memory_scope,
        "target_scope_key": run.target_scope_key,
        "status": run.status.value,
        "checkpoint": run.checkpoint.model_dump(mode="json") if run.checkpoint else None,
        "options": dict(run.options),
        "policy_context": dict(run.policy_context),
        "counters": {
            "imported_count": run.imported_count,
            "skipped_count": run.skipped_count,
            "dedupe_count": run.dedupe_count,
            "superseded_count": run.superseded_count,
            "attachment_count": run.attachment_count,
            "extraction_pending_count": run.extraction_pending_count,
        },
        "raw_memory_ids": list(run.raw_memory_ids),
        "source_ids": list(run.source_ids),
        "dedupe_keys": list(run.dedupe_keys),
        "duplicate_dedupe_keys": list(run.duplicate_dedupe_keys),
        "skipped_records": list(run.skipped_records),
        "errors": list(run.errors),
        "raw_memory_by_source_id": dict(run.raw_memory_by_source_id),
        "batch_size": run.batch_size,
        "promotion_preview_approved": run.promotion_preview_approved,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "completed_at": run.completed_at,
    }


def _run_from_record(record: dict[str, object]) -> SourceImportRun:
    counters = _dict_from_record(record.get("counters"))
    checkpoint_payload = record.get("checkpoint")
    checkpoint = (
        SourceImportCheckpoint.model_validate(checkpoint_payload)
        if isinstance(checkpoint_payload, dict)
        else None
    )
    run = SourceImportRun(
        import_id=str(record.get("uuid") or ""),
        organization_id=str(record.get("organization_id") or ""),
        principal_id=str(record.get("principal_id") or ""),
        source_uri=str(record.get("source_uri") or ""),
        adapter_name=str(record.get("adapter_name") or "mbox"),
        options=_dict_from_record(record.get("options")),
        policy_context=_dict_from_record(record.get("policy_context")),
        batch_size=int(record.get("batch_size") or 100),
        promotion_preview_approved=bool(record.get("promotion_preview_approved")),
        status=SourceImportStatus(str(record.get("status") or SourceImportStatus.PENDING)),
        adapter_version=(
            str(record["adapter_version"]) if record.get("adapter_version") is not None else None
        ),
        source_identity=(
            str(record["source_identity"]) if record.get("source_identity") is not None else None
        ),
        source_version=(
            str(record["source_version"]) if record.get("source_version") is not None else None
        ),
        privacy_class=(
            str(record["privacy_class"]) if record.get("privacy_class") is not None else None
        ),
        target_memory_scope=(
            str(record["target_memory_scope"])
            if record.get("target_memory_scope") is not None
            else None
        ),
        target_scope_key=(
            str(record["target_scope_key"]) if record.get("target_scope_key") is not None else None
        ),
        checkpoint=checkpoint,
        imported_count=int(counters.get("imported_count") or 0),
        skipped_count=int(counters.get("skipped_count") or 0),
        dedupe_count=int(counters.get("dedupe_count") or 0),
        superseded_count=int(counters.get("superseded_count") or 0),
        attachment_count=int(counters.get("attachment_count") or 0),
        extraction_pending_count=int(counters.get("extraction_pending_count") or 0),
        raw_memory_ids=_string_list_from_record(record.get("raw_memory_ids")),
        source_ids=_string_list_from_record(record.get("source_ids")),
        dedupe_keys=_string_list_from_record(record.get("dedupe_keys")),
        duplicate_dedupe_keys=_string_list_from_record(record.get("duplicate_dedupe_keys")),
        skipped_records=_dict_list_from_record(record.get("skipped_records")),
        errors=_dict_list_from_record(record.get("errors")),
        raw_memory_by_source_id={
            str(key): str(value)
            for key, value in _dict_from_record(record.get("raw_memory_by_source_id")).items()
        },
        created_at=_datetime_from_record(record.get("created_at")) or datetime.now(UTC),
        updated_at=_datetime_from_record(record.get("updated_at")) or datetime.now(UTC),
        completed_at=_datetime_from_record(record.get("completed_at")),
    )
    _SOURCE_IMPORT_RUNS[run.import_id] = run
    return run


async def _persist_run(run: SourceImportRun) -> None:
    _store_run(run)
    try:
        from sibyl.persistence.surreal.content import _replace_record, surreal_content_client

        async with surreal_content_client() as client:
            await _replace_record(
                client,
                "source_imports",
                uuid=run.import_id,
                record=_run_record(run),
            )
    except Exception as exc:
        log.warning(
            "source_import_state_persist_failed",
            error=str(exc),
            import_id=run.import_id,
        )
    await _safe_broadcast_source_import(run)


async def _load_persisted_run(
    import_id: str,
    *,
    organization_id: str,
) -> SourceImportRun | None:
    try:
        from sibyl.persistence.surreal.content import _select_one, surreal_content_client

        async with surreal_content_client() as client:
            record = await _select_one(
                client,
                "SELECT * FROM source_imports "
                "WHERE uuid = $import_id AND organization_id = $organization_id LIMIT 1;",
                import_id=import_id,
                organization_id=organization_id,
            )
    except Exception as exc:
        log.warning(
            "source_import_state_load_failed",
            error=str(exc),
            import_id=import_id,
        )
        return None
    return _run_from_record(record) if record is not None else None


def clear_source_import_runs() -> None:
    _SOURCE_IMPORT_RUNS.clear()


def _policy_context_from_payload(payload: dict[str, Any] | None) -> MemoryPolicyContext:
    if payload is None:
        raise ValueError("job_policy_context_missing")
    context = MemoryPolicyContext(
        actor_user_id=payload.get("actor_user_id"),
        organization_id=payload.get("organization_id"),
        organization_role=payload.get("organization_role"),
        accessible_projects=payload.get("accessible_projects"),
        accessible_delegations=payload.get("accessible_delegations"),
        delegated_authority=payload.get("delegated_authority"),
        agent_id=payload.get("agent_id"),
        project_id=payload.get("project_id"),
        memory_space=payload.get("memory_space"),
        scope_key=payload.get("scope_key"),
        source_surface=str(payload.get("source_surface") or "source_import"),
    )
    if not context.actor_user_id:
        raise ValueError("missing_actor")
    if not context.organization_id:
        raise ValueError("missing_organization")
    if not context.memory_space:
        raise ValueError("missing_memory_space")
    return context


def memory_policy_context_payload(context: MemoryPolicyContext) -> dict[str, Any]:
    role = context.organization_role
    return {
        "actor_user_id": context.actor_user_id,
        "organization_id": context.organization_id,
        "organization_role": role.value if hasattr(role, "value") else role,
        "accessible_projects": sorted(context.accessible_projects or []),
        "accessible_delegations": sorted(context.accessible_delegations or []),
        "delegated_authority": context.delegated_authority,
        "agent_id": context.agent_id,
        "project_id": context.project_id,
        "memory_space": context.memory_space,
        "scope_key": context.scope_key,
        "source_surface": context.source_surface,
    }


def _authorize_source_import(
    *,
    organization_id: str,
    principal_id: str,
    context_payload: dict[str, Any] | None,
    manifest_target_scope: str,
    manifest_target_scope_key: str | None,
) -> str:
    context = _policy_context_from_payload(context_payload)
    if context.organization_id != organization_id:
        raise ValueError("job_policy_context_stale")
    if context.actor_user_id != principal_id:
        raise ValueError("job_policy_context_stale")
    if context.memory_space != manifest_target_scope:
        raise ValueError("job_policy_context_stale")
    if context.scope_key != manifest_target_scope_key:
        raise ValueError("job_policy_context_stale")

    decision = authorize_memory_write(policy_context=context)
    if not decision.allowed:
        raise ValueError(decision.reason)
    return decision.reason


def _default_duplicate_checker(
    *,
    organization_id: str,
    record_dedupe_keys: dict[str, str],
    record_source_ids: dict[str, str],
) -> SourceRecordDuplicateChecker:
    async def check_duplicate(
        *,
        record: SourceRecord,
        payload: SourceRawMemoryWrite,
    ) -> SourceRecordImportDecision | None:
        if record.dedupe_key in record_dedupe_keys:
            return SourceRecordImportDecision(
                duplicate_raw_memory_id=record_dedupe_keys[record.dedupe_key]
            )
        lookup_principal_id = (
            payload.principal_id if payload.memory_scope is MemoryScope.PRIVATE else None
        )
        existing_duplicate = await get_raw_memory_by_dedupe_key(
            organization_id=organization_id,
            dedupe_key=record.dedupe_key,
            principal_id=lookup_principal_id,
            memory_scope=payload.memory_scope,
            scope_key=payload.scope_key,
        )
        if existing_duplicate is not None:
            return SourceRecordImportDecision(duplicate_raw_memory_id=existing_duplicate.id)

        if record.source_id in record_source_ids:
            return SourceRecordImportDecision(
                superseded_raw_memory_id=record_source_ids[record.source_id]
            )
        existing_source = await get_raw_memory_by_source_id(
            organization_id=organization_id,
            source_id=payload.source_id,
            principal_id=lookup_principal_id,
            memory_scope=payload.memory_scope,
            scope_key=payload.scope_key,
        )
        if existing_source is not None:
            if str(existing_source.metadata.get("content_hash") or "") == record.content_hash:
                return SourceRecordImportDecision(duplicate_raw_memory_id=existing_source.id)
            return SourceRecordImportDecision(superseded_raw_memory_id=existing_source.id)
        return None

    return check_duplicate


def _default_supersession_handler(*, organization_id: str) -> SourceRecordSupersessionHandler:
    async def mark_superseded(
        *,
        record: SourceRecord,
        payload: SourceRawMemoryWrite,
        memory: RawMemory,
        superseded_raw_memory_id: str,
    ) -> bool:
        if superseded_raw_memory_id == memory.id:
            return False
        superseded = await get_raw_memory(
            organization_id=organization_id,
            memory_id=superseded_raw_memory_id,
        )
        if superseded is None:
            return False

        superseded_metadata = dict(superseded.metadata)
        superseded_metadata["superseded_by_raw_memory_id"] = memory.id
        superseded_metadata["superseded_by_source_id"] = payload.source_id
        superseded.review_state = "superseded"
        superseded.metadata = superseded_metadata

        memory_metadata = dict(memory.metadata)
        memory_metadata["supersedes_raw_memory_id"] = superseded.id
        memory_metadata["supersedes_source_id"] = record.source_id
        memory.metadata = memory_metadata

        await save_raw_memory(superseded)
        await save_raw_memory(memory)
        return True

    return mark_superseded


def _resolve_import_source_uri(source_uri: str) -> str:
    raw_path = source_uri.removeprefix("file://")
    source_path = Path(raw_path).expanduser().resolve()
    import_root = settings.source_import_dir.expanduser().resolve()
    try:
        source_path.relative_to(import_root)
    except ValueError as exc:
        raise PermissionError("source_import_path_denied") from exc
    return str(source_path)


def _resolve_import_source_uri_for_adapter(adapter_name: str, source_uri: str) -> str:
    if adapter_name in _NON_PATH_SOURCE_ADAPTERS:
        return source_uri
    return _resolve_import_source_uri(source_uri)


def _source_option_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _validate_source_import_options(adapter_name: str, options: Mapping[str, Any]) -> None:
    if adapter_name != IMAP_ADAPTER_NAME:
        return
    if "ssl" in options and not _source_option_bool(options["ssl"]):
        raise ValueError("imap_source_uri_must_use_tls")
    disallowed_options = set(options) - _IMAP_PERSISTED_ALLOWED_OPTIONS
    if _IMAP_PERSISTED_PRIVILEGED_OPTIONS.intersection(disallowed_options):
        raise ValueError("imap_private_network_not_allowed_in_source_import_options")
    if any(_is_imap_secret_option(option) for option in disallowed_options):
        raise ValueError("imap_credentials_not_allowed_in_source_import_options")
    if disallowed_options:
        raise ValueError("imap_source_import_options_not_allowed")


def _is_imap_secret_option(option: object) -> bool:
    normalized = str(option).strip().lower().replace("-", "_")
    return any(marker in normalized for marker in _IMAP_SECRET_OPTION_MARKERS)


def _has_control_char(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


def _validate_source_import_uri(adapter_name: str, source_uri: str) -> None:
    if adapter_name != IMAP_ADAPTER_NAME:
        return
    if _has_control_char(source_uri):
        raise ValueError("imap_source_uri_must_not_include_control_characters")
    parsed = urlparse(source_uri)
    if parsed.scheme != "imaps":
        raise ValueError("imap_source_uri_must_use_tls")
    if parsed.password:
        raise ValueError("imap_source_uri_must_not_include_password")
    if parsed.query or parsed.fragment:
        raise ValueError("imap_source_uri_must_not_include_query_or_fragment")
    if not parsed.hostname:
        raise ValueError("imap_source_uri_must_include_host")
    if _has_control_char(unquote(parsed.path)):
        raise ValueError("imap_source_uri_must_not_include_control_characters")


def _ensure_builtin_source_adapters() -> None:
    ensure_mailbox_adapter_registered()
    ensure_transcript_adapters_registered()
    ensure_document_adapters_registered()


async def import_source_archive(
    ctx: dict[str, Any],
    source_uri: str,
    *,
    organization_id: str,
    principal_id: str,
    adapter_name: str = "mbox",
    options: dict[str, Any] | None = None,
    checkpoint: dict[str, Any] | None = None,
    batch_size: int = 100,
    promotion_preview_approved: bool = False,
    policy_context: dict[str, Any] | None = None,
    remember: RawMemoryRememberer | None = None,
    duplicate_checker: SourceRecordDuplicateChecker | None = None,
    supersession_handler: SourceRecordSupersessionHandler | None = None,
) -> dict[str, Any]:
    """Import a bounded source archive batch into raw memory."""
    _ensure_builtin_source_adapters()
    option_values = dict(options or {})
    _validate_source_import_uri(adapter_name, source_uri)
    _validate_source_import_options(adapter_name, option_values)
    source_uri = _resolve_import_source_uri_for_adapter(adapter_name, source_uri)
    adapter = get_source_adapter(adapter_name)
    manifest = await adapter.prepare_manifest(
        source_uri=source_uri,
        options=option_values,
    )
    plan = plan_source_import(adapter, manifest)
    context_payload = policy_context if policy_context is not None else ctx.get("policy_context")
    policy_reason = _authorize_source_import(
        organization_id=organization_id,
        principal_id=principal_id,
        context_payload=context_payload,
        manifest_target_scope=plan.policy.target_memory_scope.value,
        manifest_target_scope_key=plan.policy.target_scope_key,
    )
    checkpoint_model = (
        SourceImportCheckpoint.model_validate(checkpoint) if checkpoint is not None else None
    )
    import_kwargs: dict[str, Any] = {}
    if remember is not None:
        import_kwargs["remember"] = remember
    if duplicate_checker is not None:
        import_kwargs["duplicate_checker"] = duplicate_checker
    if supersession_handler is not None:
        import_kwargs["supersession_handler"] = supersession_handler

    result = await import_source_batch(
        adapter,
        plan.manifest,
        organization_id=organization_id,
        principal_id=principal_id,
        checkpoint=checkpoint_model,
        batch_size=batch_size,
        promotion_preview_approved=promotion_preview_approved,
        **import_kwargs,
    )
    result_checkpoint = result.checkpoint or SourceImportCheckpoint(
        source_version=plan.manifest.source_version,
        done=True,
    )
    payload = {
        "adapter_name": plan.manifest.adapter_name,
        "adapter_version": plan.manifest.adapter_version,
        "source_identity": plan.manifest.source_identity,
        "source_uri": plan.manifest.source_uri,
        "source_version": plan.manifest.source_version,
        "imported_count": result.imported_count,
        "skipped_count": result.skipped_count,
        "dedupe_count": result.dedupe_count,
        "superseded_count": result.superseded_count,
        "attachment_count": result.attachment_count,
        "extraction_pending_count": result.extraction_pending_count,
        "raw_memory_ids": list(result.raw_memory_ids),
        "source_ids": list(result.source_ids),
        "dedupe_keys": list(result.dedupe_keys),
        "duplicate_dedupe_keys": list(result.duplicate_dedupe_keys),
        "skipped_records": [skipped.model_dump(mode="json") for skipped in result.skipped_records],
        "checkpoint": result_checkpoint.model_dump(mode="json"),
        "policy": {
            "privacy_class": result.policy.privacy_class.value,
            "target_memory_scope": result.policy.target_memory_scope.value,
            "target_scope_key": result.policy.target_scope_key,
            "requires_promotion_preview": result.policy.requires_promotion_preview,
            "reasons": list(result.policy.reasons),
            "write_reason": policy_reason,
        },
        "sensitivity": {
            "contains_pii": result.contains_pii,
            "contains_secret": result.contains_secret,
            "contains_sensitive": result.contains_sensitive,
            "sensitivity_flags": list(result.sensitivity_flags),
        },
    }
    log.info(
        "source_import_archive_batch_complete",
        adapter_name=plan.manifest.adapter_name,
        source_uri=plan.manifest.source_uri,
        imported_count=result.imported_count,
        skipped_count=result.skipped_count,
        dedupe_count=result.dedupe_count,
        superseded_count=result.superseded_count,
    )
    return payload


async def _get_run(
    import_id: str,
    *,
    organization_id: str,
    principal_id: str | None = None,
) -> SourceImportRun:
    run = _SOURCE_IMPORT_RUNS.get(import_id)
    if run is None:
        run = await _load_persisted_run(import_id, organization_id=organization_id)
    if run is None or run.organization_id != organization_id:
        raise KeyError("source_import_not_found")
    if principal_id is not None and run.principal_id != principal_id:
        raise PermissionError("source_import_forbidden")
    return run


async def get_source_import_status(
    import_id: str,
    *,
    organization_id: str,
    principal_id: str | None = None,
) -> dict[str, Any]:
    return (
        await _get_run(
            import_id,
            organization_id=organization_id,
            principal_id=principal_id,
        )
    ).status_payload()


async def start_source_import(
    *,
    source_uri: str,
    organization_id: str,
    principal_id: str,
    policy_context: dict[str, Any],
    adapter_name: str = "mbox",
    options: dict[str, Any] | None = None,
    batch_size: int = 100,
    promotion_preview_approved: bool = False,
) -> dict[str, Any]:
    option_values = dict(options or {})
    _validate_source_import_uri(adapter_name, source_uri)
    _validate_source_import_options(adapter_name, option_values)
    run = SourceImportRun(
        import_id=f"source_import:{uuid4()}",
        organization_id=organization_id,
        principal_id=principal_id,
        source_uri=source_uri,
        adapter_name=adapter_name,
        options=option_values,
        policy_context=dict(policy_context),
        batch_size=batch_size,
        promotion_preview_approved=promotion_preview_approved,
    )
    await _persist_run(run)
    return run.status_payload()


async def drain_source_import(
    ctx: dict[str, Any],
    import_id: str,
    *,
    organization_id: str,
    principal_id: str,
    policy_context: dict[str, Any],
    batch_size: int | None = None,
    promotion_preview_approved: bool | None = None,
    remember: RawMemoryRememberer | None = None,
) -> dict[str, Any]:
    del ctx
    while True:
        status = await get_source_import_status(
            import_id,
            organization_id=organization_id,
            principal_id=principal_id,
        )
        if status["status"] in {
            SourceImportStatus.CANCELED.value,
            SourceImportStatus.COMPLETED.value,
        }:
            return status

        status = await resume_source_import(
            import_id,
            organization_id=organization_id,
            principal_id=principal_id,
            policy_context=policy_context,
            batch_size=batch_size,
            promotion_preview_approved=promotion_preview_approved,
            remember=remember,
        )
        if status["status"] != SourceImportStatus.PAUSED.value:
            return status


async def resume_source_import(
    import_id: str,
    *,
    organization_id: str,
    principal_id: str,
    policy_context: dict[str, Any],
    batch_size: int | None = None,
    promotion_preview_approved: bool | None = None,
    remember: RawMemoryRememberer | None = None,
) -> dict[str, Any]:
    run = await _get_run(
        import_id,
        organization_id=organization_id,
        principal_id=principal_id,
    )
    if run.status is SourceImportStatus.CANCELED:
        raise ValueError("source_import_canceled")
    if run.status is SourceImportStatus.COMPLETED:
        return run.status_payload()

    run.status = SourceImportStatus.RUNNING
    run.policy_context = dict(policy_context)
    await _persist_run(run)
    try:
        dedupe_checker = _default_duplicate_checker(
            organization_id=run.organization_id,
            record_dedupe_keys=dict(zip(run.dedupe_keys, run.raw_memory_ids, strict=False)),
            record_source_ids=run.raw_memory_by_source_id,
        )
        supersession_handler = _default_supersession_handler(organization_id=run.organization_id)
        result = await import_source_archive(
            {"policy_context": policy_context},
            run.source_uri,
            organization_id=run.organization_id,
            principal_id=run.principal_id,
            adapter_name=run.adapter_name,
            options=run.options,
            checkpoint=run.checkpoint.model_dump(mode="json") if run.checkpoint else None,
            batch_size=batch_size or run.batch_size,
            promotion_preview_approved=(
                run.promotion_preview_approved
                if promotion_preview_approved is None
                else promotion_preview_approved
            ),
            remember=remember,
            duplicate_checker=dedupe_checker,
            supersession_handler=supersession_handler,
        )
    except Exception as exc:
        run.status = SourceImportStatus.FAILED
        run.errors.append(
            {
                "message": str(exc),
                "type": type(exc).__name__,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        await _persist_run(run)
        raise

    if run.status is SourceImportStatus.CANCELED:
        return run.status_payload()
    latest = await _load_persisted_run(import_id, organization_id=run.organization_id)
    if latest is not None and latest.status is SourceImportStatus.CANCELED:
        return latest.status_payload()

    run.adapter_version = str(result["adapter_version"])
    run.source_identity = str(result["source_identity"])
    run.source_version = str(result["source_version"])
    policy = result["policy"]
    run.privacy_class = str(policy["privacy_class"])
    run.target_memory_scope = str(policy["target_memory_scope"])
    target_scope_key = policy["target_scope_key"]
    run.target_scope_key = None if target_scope_key is None else str(target_scope_key)
    run.checkpoint = SourceImportCheckpoint.model_validate(result["checkpoint"])
    run.imported_count += int(result["imported_count"])
    run.skipped_count += int(result["skipped_count"])
    run.dedupe_count += int(result["dedupe_count"])
    run.superseded_count += int(result["superseded_count"])
    run.attachment_count += int(result["attachment_count"])
    run.extraction_pending_count += int(result["extraction_pending_count"])
    run.raw_memory_ids.extend(str(raw_id) for raw_id in result["raw_memory_ids"])
    run.source_ids.extend(str(source_id) for source_id in result["source_ids"])
    run.dedupe_keys.extend(str(key) for key in result["dedupe_keys"])
    run.duplicate_dedupe_keys.extend(str(key) for key in result["duplicate_dedupe_keys"])
    run.skipped_records.extend(result["skipped_records"])
    for source_id, raw_memory_id in zip(
        result["source_ids"], result["raw_memory_ids"], strict=True
    ):
        run.raw_memory_by_source_id[str(source_id)] = str(raw_memory_id)

    if run.checkpoint.done:
        run.status = SourceImportStatus.COMPLETED
        run.completed_at = datetime.now(UTC)
    else:
        run.status = SourceImportStatus.PAUSED
    await _persist_run(run)
    await _enqueue_raw_promotion_after_import(
        organization_id=run.organization_id,
        raw_memory_ids=[str(raw_id) for raw_id in result["raw_memory_ids"]],
    )
    return run.status_payload()


async def _enqueue_raw_promotion_after_import(
    *,
    organization_id: str,
    raw_memory_ids: list[str],
) -> None:
    if not raw_memory_ids:
        return
    try:
        from sibyl.jobs import queue as job_queue

        await job_queue.enqueue_raw_promotion(
            organization_id,
            raw_memory_ids=raw_memory_ids,
        )
    except Exception as exc:
        log.warning(
            "source_import_raw_promotion_enqueue_failed",
            organization_id=organization_id,
            raw_memory_count=len(raw_memory_ids),
            error=str(exc),
        )


async def cancel_source_import(
    import_id: str,
    *,
    organization_id: str,
    principal_id: str,
) -> dict[str, Any]:
    run = await _get_run(
        import_id,
        organization_id=organization_id,
        principal_id=principal_id,
    )
    if run.status is not SourceImportStatus.COMPLETED:
        run.status = SourceImportStatus.CANCELED
        run.completed_at = datetime.now(UTC)
        await _persist_run(run)
    return run.status_payload()


__all__ = [
    "SourceImportRun",
    "SourceImportStatus",
    "cancel_source_import",
    "clear_source_import_runs",
    "drain_source_import",
    "get_source_import_status",
    "import_source_archive",
    "memory_policy_context_payload",
    "resume_source_import",
    "start_source_import",
]
