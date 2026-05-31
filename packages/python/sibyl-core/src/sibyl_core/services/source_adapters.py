"""Source adapter contracts and generic raw-memory import helpers."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
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
from sibyl_core.services.sensitivity import classify_record
from sibyl_core.services.surreal_content import (
    MemoryScope,
    RawMemory,
    RawMemoryWrite,
    remember_raw_memories,
    remember_raw_memory,
)

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
_PRIVACY_CLASS_RANK = {
    SourcePrivacyClass.PUBLIC: 0,
    SourcePrivacyClass.ORGANIZATION: 1,
    SourcePrivacyClass.PROJECT: 2,
    SourcePrivacyClass.PERSONAL: 3,
    SourcePrivacyClass.PRIVATE: 4,
    SourcePrivacyClass.SENSITIVE: 5,
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
    ) -> SourceRecordImportDecision | str | None: ...


@runtime_checkable
class SourceRecordSupersessionHandler(Protocol):
    async def __call__(
        self,
        *,
        record: SourceRecord,
        payload: SourceRawMemoryWrite,
        memory: RawMemory,
        superseded_raw_memory_id: str,
    ) -> bool: ...


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
    policy: SourceImportPolicy | None = None
    capture_surface: str = "source_import"
    entity_type: str = "raw_memory"


@runtime_checkable
class RawMemoryBatchRememberer(Protocol):
    async def remember_many(
        self,
        payloads: Sequence[SourceRawMemoryWrite],
    ) -> Sequence[RawMemory]: ...


@dataclass(frozen=True, slots=True)
class SourceRecordImportDecision:
    duplicate_raw_memory_id: str | None = None
    superseded_raw_memory_id: str | None = None


@dataclass(frozen=True, slots=True)
class SourceImportResult:
    imported_count: int
    skipped_count: int
    dedupe_count: int
    superseded_count: int
    attachment_count: int
    extraction_pending_count: int
    raw_memory_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    dedupe_keys: tuple[str, ...]
    duplicate_dedupe_keys: tuple[str, ...]
    skipped_records: tuple[SourceSkippedRecord, ...]
    checkpoint: SourceImportCheckpoint | None
    policy: SourceImportPolicy
    contains_pii: bool = False
    contains_secret: bool = False
    sensitivity_flags: tuple[str, ...] = ()

    @property
    def contains_sensitive(self) -> bool:
        return self.contains_pii or self.contains_secret


def _raw_memory_write_for_source_payload(payload: SourceRawMemoryWrite) -> RawMemoryWrite:
    return RawMemoryWrite(
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


class BatchedRawMemoryRememberer:
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
    ) -> RawMemory:
        return await remember_raw_memory(
            organization_id=organization_id,
            principal_id=principal_id,
            source_id=source_id,
            raw_content=raw_content,
            title=title,
            memory_scope=memory_scope,
            scope_key=scope_key,
            tags=tags,
            metadata=metadata,
            provenance=provenance,
            capture_surface=capture_surface,
            entity_type=entity_type,
        )

    async def remember_many(
        self,
        payloads: Sequence[SourceRawMemoryWrite],
    ) -> Sequence[RawMemory]:
        return await remember_raw_memories(
            [_raw_memory_write_for_source_payload(payload) for payload in payloads]
        )


default_raw_memory_rememberer = BatchedRawMemoryRememberer()


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


def _merge_source_import_policy(
    current: SourceImportPolicy,
    update: SourceImportPolicy,
) -> SourceImportPolicy:
    privacy_class = _more_restrictive_privacy_class(current.privacy_class, update.privacy_class)
    reasons = tuple(dict.fromkeys((*current.reasons, *update.reasons)))
    return SourceImportPolicy(
        privacy_class=privacy_class,
        target_memory_scope=update.target_memory_scope,
        target_scope_key=update.target_scope_key,
        requires_promotion_preview=(
            current.requires_promotion_preview or update.requires_promotion_preview
        ),
        reasons=reasons,
    )


def _more_restrictive_privacy_class(
    current: SourcePrivacyClass,
    update: SourcePrivacyClass,
) -> SourcePrivacyClass:
    if _PRIVACY_CLASS_RANK[update] > _PRIVACY_CLASS_RANK[current]:
        return update
    return current


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
    sensitivity = classify_record(record)
    effective_privacy_class = sensitivity.privacy_class or record.privacy_class
    policy = source_import_policy(manifest, privacy_class=effective_privacy_class)
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
        "privacy_class": effective_privacy_class.value,
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
    metadata.update(sensitivity.metadata())
    if effective_privacy_class != record.privacy_class:
        metadata["source_declared_privacy_class"] = record.privacy_class.value
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
        policy=policy,
    )


def _skipped_record_for_manifest(
    *,
    manifest: SourceImportManifest,
    skipped: SourceSkippedRecord,
) -> SourceSkippedRecord:
    metadata = {
        **dict(skipped.metadata),
        "adapter_name": manifest.adapter_name,
        "adapter_version": manifest.adapter_version,
        "source_identity": manifest.source_identity,
        "source_version": manifest.source_version,
    }
    return skipped.model_copy(update={"metadata": metadata})


def _duplicate_skipped_record(
    *,
    manifest: SourceImportManifest,
    record: SourceRecord,
    raw_memory_id: str,
) -> SourceSkippedRecord:
    return _skipped_record_for_manifest(
        manifest=manifest,
        skipped=SourceSkippedRecord(
            adapter_record_id=record.adapter_record_id,
            source_uri=record.source_uri,
            reason="duplicate_dedupe_key",
            metadata={
                "dedupe_key": record.dedupe_key,
                "raw_memory_id": raw_memory_id,
                "source_id": record.source_id,
            },
        ),
    )


def _source_record_import_decision(
    decision: SourceRecordImportDecision | str | None,
) -> SourceRecordImportDecision:
    if isinstance(decision, SourceRecordImportDecision):
        return decision
    if decision is None:
        return SourceRecordImportDecision()
    return SourceRecordImportDecision(duplicate_raw_memory_id=decision)


async def _remember_source_payloads(
    pending_writes: Sequence[tuple[SourceRecord, SourceRawMemoryWrite, SourceRecordImportDecision]],
    remember: RawMemoryRememberer,
) -> list[RawMemory]:
    if not pending_writes:
        return []

    payloads = [payload for _, payload, _ in pending_writes]
    remember_many = getattr(remember, "remember_many", None)
    if callable(remember_many):
        memories = list(await remember_many(payloads))
    else:
        memories = [
            await remember(
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
            for payload in payloads
        ]

    if len(memories) != len(payloads):
        raise ValueError(
            f"raw memory rememberer returned {len(memories)} memories for {len(payloads)} payloads"
        )
    return memories


async def import_source_batch(
    adapter: SourceAdapter,
    manifest: SourceImportManifest,
    *,
    organization_id: str,
    principal_id: str,
    checkpoint: SourceImportCheckpoint | None = None,
    batch_size: int = 100,
    promotion_preview_approved: bool = False,
    remember: RawMemoryRememberer = default_raw_memory_rememberer,
    duplicate_checker: SourceRecordDuplicateChecker | None = None,
    supersession_handler: SourceRecordSupersessionHandler | None = None,
) -> SourceImportResult:
    plan = plan_source_import(adapter, manifest)
    raw_memory_ids: list[str] = []
    source_ids: list[str] = []
    dedupe_keys: list[str] = []
    duplicate_dedupe_keys: list[str] = []
    skipped_records: list[SourceSkippedRecord] = []
    superseded_count = 0
    attachment_count = 0
    extraction_pending_count = 0
    last_checkpoint = checkpoint
    aggregate_policy = plan.policy
    contains_pii = False
    contains_secret = False
    sensitivity_flags: list[str] = []
    written_batch_dedupe_raw_ids: dict[str, str] = {}
    written_batch_source_raw_ids: dict[str, str] = {}
    written_batch_source_content_hashes: dict[str, str] = {}

    async for batch in adapter.iter_records(
        plan.manifest,
        checkpoint=checkpoint,
        batch_size=batch_size,
    ):
        skipped_records.extend(
            _skipped_record_for_manifest(manifest=plan.manifest, skipped=skipped)
            for skipped in batch.skipped
        )
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
            payload_policy = payload.policy or plan.policy
            aggregate_policy = _merge_source_import_policy(aggregate_policy, payload_policy)
            contains_pii = contains_pii or payload.metadata.get("contains_pii") is True
            contains_secret = contains_secret or payload.metadata.get("contains_secret") is True
            payload_flags = payload.metadata.get("sensitivity_flags")
            if not isinstance(payload_flags, list | tuple | set):
                payload_flags = ()
            for flag in payload_flags:
                if isinstance(flag, str) and flag not in sensitivity_flags:
                    sensitivity_flags.append(flag)

        if not promotion_preview_approved and any(
            (payload.policy or plan.policy).requires_promotion_preview
            for _, payload in batch_payloads
        ):
            msg = "source import requires promotion preview before wider visibility"
            raise ValueError(msg)

        pending_writes: list[tuple[SourceRecord, SourceRawMemoryWrite, SourceRecordImportDecision]]
        pending_writes = []
        pending_dedupe_keys: set[str] = set()
        pending_source_ids: set[str] = set()

        async def flush_pending_writes() -> None:
            nonlocal attachment_count, extraction_pending_count, superseded_count
            nonlocal pending_dedupe_keys, pending_source_ids, pending_writes
            if not pending_writes:
                return
            written_memories = await _remember_source_payloads(pending_writes, remember)
            for (record, payload, decision), memory in zip(
                pending_writes,
                written_memories,
                strict=True,
            ):
                attachment_count += len(record.attachments)
                extraction_pending_count += len(record.attachments)
                if record.transform_behavior == SourceTransformBehavior.METADATA_ONLY:
                    extraction_pending_count += 1
                raw_memory_ids.append(memory.id)
                source_ids.append(record.source_id)
                dedupe_keys.append(record.dedupe_key)
                written_batch_dedupe_raw_ids[record.dedupe_key] = memory.id
                written_batch_source_raw_ids[record.source_id] = memory.id
                written_batch_source_content_hashes[record.source_id] = record.content_hash
                if (
                    supersession_handler is not None
                    and decision.superseded_raw_memory_id is not None
                ):
                    superseded = await supersession_handler(
                        record=record,
                        payload=payload,
                        memory=memory,
                        superseded_raw_memory_id=decision.superseded_raw_memory_id,
                    )
                    if superseded:
                        superseded_count += 1
            pending_writes = []
            pending_dedupe_keys = set()
            pending_source_ids = set()

        for record, payload in batch_payloads:
            if record.dedupe_key in pending_dedupe_keys or record.source_id in pending_source_ids:
                await flush_pending_writes()

            batch_duplicate_raw_id = written_batch_dedupe_raw_ids.get(record.dedupe_key)
            if batch_duplicate_raw_id is not None:
                duplicate_dedupe_keys.append(record.dedupe_key)
                skipped_records.append(
                    _duplicate_skipped_record(
                        manifest=plan.manifest,
                        record=record,
                        raw_memory_id=batch_duplicate_raw_id,
                    )
                )
                continue

            decision = SourceRecordImportDecision()
            batch_source_raw_id = written_batch_source_raw_ids.get(record.source_id)
            if batch_source_raw_id is not None:
                if written_batch_source_content_hashes.get(record.source_id) == record.content_hash:
                    duplicate_dedupe_keys.append(record.dedupe_key)
                    skipped_records.append(
                        _duplicate_skipped_record(
                            manifest=plan.manifest,
                            record=record,
                            raw_memory_id=batch_source_raw_id,
                        )
                    )
                    continue
                decision = SourceRecordImportDecision(superseded_raw_memory_id=batch_source_raw_id)
            elif duplicate_checker is not None:
                decision = _source_record_import_decision(
                    await duplicate_checker(
                        record=record,
                        payload=payload,
                    )
                )
            if decision.duplicate_raw_memory_id is not None:
                duplicate_dedupe_keys.append(record.dedupe_key)
                skipped_records.append(
                    _duplicate_skipped_record(
                        manifest=plan.manifest,
                        record=record,
                        raw_memory_id=decision.duplicate_raw_memory_id,
                    )
                )
                continue

            pending_writes.append((record, payload, decision))
            pending_dedupe_keys.add(record.dedupe_key)
            pending_source_ids.add(record.source_id)

        await flush_pending_writes()

    return SourceImportResult(
        imported_count=len(raw_memory_ids),
        skipped_count=len(skipped_records),
        dedupe_count=len(duplicate_dedupe_keys),
        superseded_count=superseded_count,
        attachment_count=attachment_count,
        extraction_pending_count=extraction_pending_count,
        raw_memory_ids=tuple(raw_memory_ids),
        source_ids=tuple(source_ids),
        dedupe_keys=tuple(dedupe_keys),
        duplicate_dedupe_keys=tuple(duplicate_dedupe_keys),
        skipped_records=tuple(skipped_records),
        checkpoint=last_checkpoint,
        policy=aggregate_policy,
        contains_pii=contains_pii,
        contains_secret=contains_secret,
        sensitivity_flags=tuple(sensitivity_flags),
    )


__all__ = [
    "BatchedRawMemoryRememberer",
    "RawMemoryBatchRememberer",
    "RawMemoryRememberer",
    "SourceAdapter",
    "SourceAdapterRegistry",
    "SourceImportPlan",
    "SourceImportPolicy",
    "SourceImportResult",
    "SourceRawMemoryWrite",
    "SourceRecordDuplicateChecker",
    "SourceRecordImportDecision",
    "SourceRecordSupersessionHandler",
    "build_source_content_hash",
    "build_source_dedupe_key",
    "build_source_record_id",
    "clear_source_adapters",
    "default_raw_memory_rememberer",
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
