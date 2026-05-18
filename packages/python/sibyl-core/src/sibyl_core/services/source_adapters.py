"""Source adapter contracts and generic raw-memory import helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from hashlib import sha256
from typing import Protocol, runtime_checkable

from sibyl_core.models.sources import (
    SourceAdapterDescriptor,
    SourceDedupeKey,
    SourceImportCheckpoint,
    SourceImportManifest,
    SourcePrivacyClass,
    SourceRecord,
    SourceRecordBatch,
    SourceSkippedRecord,
    SourceTransformBehavior,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory, remember_raw_memory

_SCOPES_REQUIRING_KEY = {
    MemoryScope.DELEGATED,
    MemoryScope.PROJECT,
    MemoryScope.TEAM,
    MemoryScope.SHARED,
}
_PRIVATE_DEFAULT_PRIVACY_CLASSES = {
    SourcePrivacyClass.PERSONAL,
    SourcePrivacyClass.PRIVATE,
    SourcePrivacyClass.SENSITIVE,
}
_SCOPE_RANK = {
    MemoryScope.PRIVATE: 0,
    MemoryScope.DELEGATED: 1,
    MemoryScope.PROJECT: 2,
    MemoryScope.TEAM: 3,
    MemoryScope.ORGANIZATION: 4,
    MemoryScope.SHARED: 5,
    MemoryScope.PUBLIC: 6,
}


@runtime_checkable
class SourceAdapter(Protocol):
    """Protocol every source adapter implements."""

    @property
    def descriptor(self) -> SourceAdapterDescriptor: ...

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest: ...

    def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]: ...


@runtime_checkable
class RawMemoryRememberer(Protocol):
    async def __call__(
        self,
        *,
        organization_id: str,
        principal_id: str,
        source_id: str,
        raw_content: str,
        title: str = "",
        memory_scope: MemoryScope | str = MemoryScope.PRIVATE,
        scope_key: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        provenance: dict[str, object] | None = None,
        capture_surface: str | None = None,
        entity_type: str = "raw_memory",
    ) -> RawMemory: ...


@runtime_checkable
class SourceRecordDuplicateChecker(Protocol):
    async def __call__(
        self,
        *,
        record: SourceRecord,
        payload: SourceRawMemoryWrite,
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class SourceImportPolicy:
    privacy_class: SourcePrivacyClass
    target_memory_scope: MemoryScope
    target_scope_key: str | None
    requires_promotion_preview: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SourceImportPlan:
    adapter: SourceAdapterDescriptor
    manifest: SourceImportManifest
    policy: SourceImportPolicy


@dataclass(frozen=True, slots=True)
class SourceRawMemoryWrite:
    organization_id: str
    principal_id: str
    source_id: str
    raw_content: str
    title: str
    memory_scope: MemoryScope
    scope_key: str | None
    tags: list[str]
    metadata: dict[str, object]
    provenance: dict[str, object]
    capture_surface: str = "source_import"
    entity_type: str = "raw_memory"


@dataclass(frozen=True, slots=True)
class SourceImportResult:
    imported_count: int
    skipped_count: int
    dedupe_count: int
    attachment_count: int
    extraction_pending_count: int
    raw_memory_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    dedupe_keys: tuple[str, ...]
    duplicate_dedupe_keys: tuple[str, ...]
    skipped_records: tuple[SourceSkippedRecord, ...]
    checkpoint: SourceImportCheckpoint | None
    policy: SourceImportPolicy


class SourceAdapterRegistry:
    """In-memory adapter registry used by API and jobs."""

    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}

    def register(self, adapter: SourceAdapter) -> None:
        name = adapter.descriptor.name
        if name in self._adapters:
            msg = f"Source adapter already registered: {name}"
            raise ValueError(msg)
        self._adapters[name] = adapter

    def get(self, name: str) -> SourceAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            msg = f"Unknown source adapter: {name}"
            raise KeyError(msg) from exc

    def has(self, name: str) -> bool:
        return name in self._adapters

    def descriptors(self) -> list[SourceAdapterDescriptor]:
        return [adapter.descriptor for adapter in self._adapters.values()]

    def clear(self) -> None:
        self._adapters.clear()


source_adapter_registry = SourceAdapterRegistry()


def register_source_adapter(adapter: SourceAdapter) -> None:
    source_adapter_registry.register(adapter)


def get_source_adapter(name: str) -> SourceAdapter:
    return source_adapter_registry.get(name)


def list_source_adapters() -> list[SourceAdapterDescriptor]:
    return source_adapter_registry.descriptors()


def clear_source_adapters() -> None:
    source_adapter_registry.clear()


def build_source_content_hash(*values: str | None) -> str:
    hasher = sha256()
    for value in values:
        hasher.update((value or "").encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()


def build_source_dedupe_key(
    *,
    manifest: SourceImportManifest,
    adapter_record_id: str,
    content_hash: str,
) -> SourceDedupeKey:
    raw_value = "\0".join(
        [
            manifest.adapter_name,
            manifest.source_identity,
            manifest.source_version,
            adapter_record_id,
            content_hash,
        ]
    )
    value = "source:" + sha256(raw_value.encode("utf-8")).hexdigest()
    return SourceDedupeKey(
        adapter_name=manifest.adapter_name,
        source_identity=manifest.source_identity,
        source_version=manifest.source_version,
        adapter_record_id=adapter_record_id,
        content_hash=content_hash,
        value=value,
    )


def build_source_record_id(
    *,
    manifest: SourceImportManifest,
    adapter_record_id: str,
) -> str:
    raw_value = "\0".join([manifest.adapter_name, manifest.source_identity, adapter_record_id])
    return "source-record:" + sha256(raw_value.encode("utf-8")).hexdigest()


def default_scope_for_privacy(privacy_class: SourcePrivacyClass) -> MemoryScope:
    if privacy_class in _PRIVATE_DEFAULT_PRIVACY_CLASSES:
        return MemoryScope.PRIVATE
    if privacy_class is SourcePrivacyClass.PROJECT:
        return MemoryScope.PROJECT
    if privacy_class is SourcePrivacyClass.ORGANIZATION:
        return MemoryScope.ORGANIZATION
    if privacy_class is SourcePrivacyClass.PUBLIC:
        return MemoryScope.PUBLIC
    return MemoryScope.PRIVATE


def source_import_policy(
    manifest: SourceImportManifest,
    *,
    privacy_class: SourcePrivacyClass | None = None,
) -> SourceImportPolicy:
    try:
        target_scope = MemoryScope(manifest.target_memory_scope)
    except ValueError as exc:
        msg = f"Unsupported target memory scope: {manifest.target_memory_scope}"
        raise ValueError(msg) from exc

    if target_scope in _SCOPES_REQUIRING_KEY and not manifest.target_scope_key:
        msg = f"{target_scope.value} imports require target_scope_key"
        raise ValueError(msg)

    effective_privacy_class = privacy_class or manifest.privacy_class
    default_scope = default_scope_for_privacy(effective_privacy_class)
    requires_preview = _SCOPE_RANK[target_scope] > _SCOPE_RANK[default_scope]
    reasons: list[str] = []
    if target_scope is default_scope:
        reasons.append("privacy_default_scope")
    else:
        reasons.append("explicit_target_scope")
    if requires_preview:
        reasons.append("promotion_preview_required")

    return SourceImportPolicy(
        privacy_class=effective_privacy_class,
        target_memory_scope=target_scope,
        target_scope_key=manifest.target_scope_key,
        requires_promotion_preview=requires_preview,
        reasons=tuple(reasons),
    )


def plan_source_import(adapter: SourceAdapter, manifest: SourceImportManifest) -> SourceImportPlan:
    descriptor = adapter.descriptor
    if manifest.adapter_name != descriptor.name:
        msg = f"Manifest adapter {manifest.adapter_name} does not match {descriptor.name}"
        raise ValueError(msg)
    if manifest.adapter_version != descriptor.version:
        msg = f"Manifest adapter version {manifest.adapter_version} does not match {descriptor.version}"
        raise ValueError(msg)

    normalized = manifest
    if not normalized.metadata_schema and descriptor.metadata_schema:
        normalized = normalized.model_copy(update={"metadata_schema": descriptor.metadata_schema})
    return SourceImportPlan(
        adapter=descriptor,
        manifest=normalized,
        policy=source_import_policy(normalized),
    )


def raw_memory_write_from_source_record(
    *,
    manifest: SourceImportManifest,
    record: SourceRecord,
    organization_id: str,
    principal_id: str,
) -> SourceRawMemoryWrite:
    policy = source_import_policy(manifest, privacy_class=record.privacy_class)
    attachment_payload = [attachment.model_dump(mode="json") for attachment in record.attachments]
    occurred_at = record.occurred_at.isoformat() if record.occurred_at else None
    metadata_only = record.transform_behavior == SourceTransformBehavior.METADATA_ONLY
    extraction_pending = metadata_only or bool(record.attachments)
    metadata: dict[str, object] = {
        "adapter_record_id": record.adapter_record_id,
        "adapter_name": manifest.adapter_name,
        "adapter_version": manifest.adapter_version,
        "attachment_count": len(record.attachments),
        "attachments": attachment_payload,
        "content_hash": record.content_hash,
        "dedupe_key": record.dedupe_key,
        "import_requires_promotion_preview": policy.requires_promotion_preview,
        "import_policy_reasons": list(policy.reasons),
        "privacy_class": record.privacy_class.value,
        "source_adapter_version": manifest.adapter_version,
        "source_identity": manifest.source_identity,
        "source_import_metadata": dict(manifest.metadata),
        "source_record_id": record.source_id,
        "source_record_metadata": dict(record.metadata),
        "source_type": record.source_type,
        "source_uri": record.source_uri,
        "source_version": record.source_version,
        "source_extraction_state": "pending" if extraction_pending else "complete",
        "transform_behavior": record.transform_behavior.value,
        "transform_version": record.transform_version or manifest.adapter_version,
    }
    if occurred_at is not None:
        metadata["occurred_at"] = occurred_at
    if record.participants:
        metadata["participants"] = list(record.participants)
    if record.labels:
        metadata["labels"] = list(record.labels)

    provenance: dict[str, object] = {
        "adapter_record_id": record.adapter_record_id,
        "dedupe_key": record.dedupe_key,
        "source_adapter": manifest.adapter_name,
        "source_adapter_version": manifest.adapter_version,
        "source_identity": manifest.source_identity,
        "source_record_id": record.source_id,
        "source_uri": record.source_uri,
        "source_version": record.source_version,
    }
    if attachment_payload:
        provenance["attachments"] = attachment_payload

    return SourceRawMemoryWrite(
        organization_id=organization_id,
        principal_id=principal_id,
        source_id=record.source_id,
        raw_content=record.body,
        title=record.title,
        memory_scope=policy.target_memory_scope,
        scope_key=policy.target_scope_key,
        tags=list(record.labels),
        metadata=metadata,
        provenance=provenance,
    )


async def import_source_batch(
    adapter: SourceAdapter,
    manifest: SourceImportManifest,
    *,
    organization_id: str,
    principal_id: str,
    checkpoint: SourceImportCheckpoint | None = None,
    batch_size: int = 100,
    promotion_preview_approved: bool = False,
    remember: RawMemoryRememberer = remember_raw_memory,
    duplicate_checker: SourceRecordDuplicateChecker | None = None,
) -> SourceImportResult:
    plan = plan_source_import(adapter, manifest)
    raw_memory_ids: list[str] = []
    source_ids: list[str] = []
    dedupe_keys: list[str] = []
    duplicate_dedupe_keys: list[str] = []
    skipped_records: list[SourceSkippedRecord] = []
    attachment_count = 0
    extraction_pending_count = 0
    last_checkpoint = checkpoint

    async for batch in adapter.iter_records(
        plan.manifest,
        checkpoint=checkpoint,
        batch_size=batch_size,
    ):
        skipped_records.extend(batch.skipped)
        last_checkpoint = batch.checkpoint
        batch_payloads: list[tuple[SourceRecord, SourceRawMemoryWrite]] = []
        for record in batch.records:
            payload = raw_memory_write_from_source_record(
                manifest=plan.manifest,
                record=record,
                organization_id=organization_id,
                principal_id=principal_id,
            )
            batch_payloads.append((record, payload))

        if not promotion_preview_approved and any(
            payload.metadata["import_requires_promotion_preview"] is True
            for _, payload in batch_payloads
        ):
            msg = "source import requires promotion preview before wider visibility"
            raise ValueError(msg)

        for record, payload in batch_payloads:
            existing_raw_memory_id = None
            if duplicate_checker is not None:
                existing_raw_memory_id = await duplicate_checker(
                    record=record,
                    payload=payload,
                )
            if existing_raw_memory_id is not None:
                duplicate_dedupe_keys.append(record.dedupe_key)
                skipped_records.append(
                    SourceSkippedRecord(
                        adapter_record_id=record.adapter_record_id,
                        source_uri=record.source_uri,
                        reason="duplicate_dedupe_key",
                        metadata={
                            "dedupe_key": record.dedupe_key,
                            "raw_memory_id": existing_raw_memory_id,
                            "source_id": record.source_id,
                        },
                    )
                )
                continue

            attachment_count += len(record.attachments)
            extraction_pending_count += len(record.attachments)
            if record.transform_behavior == SourceTransformBehavior.METADATA_ONLY:
                extraction_pending_count += 1
            memory = await remember(
                organization_id=payload.organization_id,
                principal_id=payload.principal_id,
                source_id=payload.source_id,
                raw_content=payload.raw_content,
                title=payload.title,
                memory_scope=payload.memory_scope,
                scope_key=payload.scope_key,
                tags=payload.tags,
                metadata=payload.metadata,
                provenance=payload.provenance,
                capture_surface=payload.capture_surface,
                entity_type=payload.entity_type,
            )
            raw_memory_ids.append(memory.id)
            source_ids.append(record.source_id)
            dedupe_keys.append(record.dedupe_key)

    return SourceImportResult(
        imported_count=len(raw_memory_ids),
        skipped_count=len(skipped_records),
        dedupe_count=len(duplicate_dedupe_keys),
        attachment_count=attachment_count,
        extraction_pending_count=extraction_pending_count,
        raw_memory_ids=tuple(raw_memory_ids),
        source_ids=tuple(source_ids),
        dedupe_keys=tuple(dedupe_keys),
        duplicate_dedupe_keys=tuple(duplicate_dedupe_keys),
        skipped_records=tuple(skipped_records),
        checkpoint=last_checkpoint,
        policy=plan.policy,
    )


__all__ = [
    "RawMemoryRememberer",
    "SourceAdapter",
    "SourceAdapterRegistry",
    "SourceImportPlan",
    "SourceImportPolicy",
    "SourceImportResult",
    "SourceRawMemoryWrite",
    "SourceRecordDuplicateChecker",
    "build_source_content_hash",
    "build_source_dedupe_key",
    "build_source_record_id",
    "clear_source_adapters",
    "default_scope_for_privacy",
    "get_source_adapter",
    "import_source_batch",
    "list_source_adapters",
    "plan_source_import",
    "raw_memory_write_from_source_record",
    "register_source_adapter",
    "source_adapter_registry",
    "source_import_policy",
]
