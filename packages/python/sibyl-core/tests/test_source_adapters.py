from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime

import pytest

from sibyl_core.models.sources import (
    SourceAdapterCapability,
    SourceAdapterDescriptor,
    SourceAttachmentRecord,
    SourceImportCheckpoint,
    SourceImportManifest,
    SourcePrivacyClass,
    SourceRecord,
    SourceRecordBatch,
    SourceSkippedRecord,
    SourceTransformBehavior,
)
from sibyl_core.services.source_adapters import (
    SourceAdapterRegistry,
    SourceRawMemoryWrite,
    SourceRecordImportDecision,
    build_source_content_hash,
    build_source_dedupe_key,
    build_source_record_id,
    default_scope_for_privacy,
    import_source_batch,
    plan_source_import,
    raw_memory_write_from_source_record,
    source_import_policy,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory


class FakeSourceAdapter:
    def __init__(self, records: list[SourceRecord]) -> None:
        self.descriptor = SourceAdapterDescriptor(
            name="fake",
            version="1.0",
            source_type="fixture",
            display_name="Fake fixture adapter",
            capabilities=[
                SourceAdapterCapability.ATTACHMENTS,
                SourceAdapterCapability.CHECKPOINTS,
                SourceAdapterCapability.SKIPPED_RECORDS,
            ],
            default_privacy_class=SourcePrivacyClass.PERSONAL,
            transform_behavior=SourceTransformBehavior.RAW,
            metadata_schema={"message_id": "string"},
            supports_incremental=True,
        )
        self._records = records

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        return SourceImportManifest(
            adapter_name=self.descriptor.name,
            adapter_version=self.descriptor.version,
            source_identity=str((options or {}).get("source_identity") or "fixture"),
            source_uri=source_uri,
            source_version="v1",
            privacy_class=self.descriptor.default_privacy_class,
            transform_behavior=self.descriptor.transform_behavior,
        )

    async def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        start = int(checkpoint.cursor) if checkpoint and checkpoint.cursor else 0
        records = self._records[start : start + batch_size]
        cursor = start + len(records)
        yield SourceRecordBatch(
            records=records,
            skipped=[
                SourceSkippedRecord(
                    adapter_record_id="skip-1",
                    source_uri=f"{manifest.source_uri}/skip-1",
                    reason="unsupported_record",
                )
            ],
            checkpoint=SourceImportCheckpoint(
                cursor=str(cursor),
                source_version=manifest.source_version,
                records_seen=cursor + 1,
                records_imported=len(records),
                records_skipped=1,
                done=cursor >= len(self._records),
            ),
        )


class EmptySourceAdapter(FakeSourceAdapter):
    async def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        if False:
            yield SourceRecordBatch(
                records=[],
                skipped=[],
                checkpoint=checkpoint or SourceImportCheckpoint(),
            )


def _manifest(**overrides: object) -> SourceImportManifest:
    values = {
        "adapter_name": "fake",
        "adapter_version": "1.0",
        "source_identity": "fixture",
        "source_uri": "memory://fixture",
        "source_version": "v1",
        "privacy_class": SourcePrivacyClass.PERSONAL,
        "target_memory_scope": "private",
    }
    values.update(overrides)
    return SourceImportManifest(**values)


def _record(manifest: SourceImportManifest, record_id: str = "message-1") -> SourceRecord:
    content_hash = build_source_content_hash("Subject", "Body")
    dedupe_key = build_source_dedupe_key(
        manifest=manifest,
        adapter_record_id=record_id,
        content_hash=content_hash,
    )
    return SourceRecord(
        adapter_record_id=record_id,
        source_id=build_source_record_id(manifest=manifest, adapter_record_id=record_id),
        source_type="mailbox_message",
        source_uri=f"{manifest.source_uri}/{record_id}",
        source_version=manifest.source_version,
        title="Subject",
        body="Body",
        content_hash=content_hash,
        dedupe_key=dedupe_key.value,
        privacy_class=manifest.privacy_class,
        transform_behavior=manifest.transform_behavior,
        transform_version=manifest.adapter_version,
        occurred_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        participants=["bliss@example.com"],
        labels=["inbox"],
        metadata={"message_id": record_id},
        attachments=[
            SourceAttachmentRecord(
                adapter_attachment_id="attachment-1",
                filename="notes.txt",
                media_type="text/plain",
                size_bytes=12,
                content_hash=build_source_content_hash("notes"),
                source_path="mailbox/notes.txt",
            )
        ],
    )


def test_dedupe_keys_and_record_ids_are_stable() -> None:
    manifest = _manifest()
    next_manifest_version = _manifest(source_version="v2")
    content_hash = build_source_content_hash("Subject", "Body")

    first = build_source_dedupe_key(
        manifest=manifest,
        adapter_record_id="message-1",
        content_hash=content_hash,
    )
    second = build_source_dedupe_key(
        manifest=manifest,
        adapter_record_id="message-1",
        content_hash=content_hash,
    )
    next_version = build_source_dedupe_key(
        manifest=next_manifest_version,
        adapter_record_id="message-1",
        content_hash=content_hash,
    )

    assert first == second
    assert first.value == next_version.value
    assert first.value.startswith("source:")
    assert build_source_record_id(manifest=manifest, adapter_record_id="message-1") == (
        build_source_record_id(manifest=manifest, adapter_record_id="message-1")
    )


def test_source_import_policy_defaults_personal_imports_to_private() -> None:
    manifest = _manifest()

    policy = source_import_policy(manifest)

    assert default_scope_for_privacy(SourcePrivacyClass.PERSONAL) is MemoryScope.PRIVATE
    assert policy.target_memory_scope is MemoryScope.PRIVATE
    assert policy.requires_promotion_preview is False
    assert policy.reasons == ("privacy_default_scope",)


def test_source_import_policy_flags_wider_personal_targets() -> None:
    manifest = _manifest(target_memory_scope="project", target_scope_key="project_123")

    policy = source_import_policy(manifest)

    assert policy.target_memory_scope is MemoryScope.PROJECT
    assert policy.target_scope_key == "project_123"
    assert policy.requires_promotion_preview is True
    assert "promotion_preview_required" in policy.reasons


def test_source_import_policy_flags_wider_project_targets() -> None:
    manifest = _manifest(
        privacy_class=SourcePrivacyClass.PROJECT,
        target_memory_scope="organization",
    )

    policy = source_import_policy(manifest)

    assert policy.privacy_class is SourcePrivacyClass.PROJECT
    assert policy.target_memory_scope is MemoryScope.ORGANIZATION
    assert policy.requires_promotion_preview is True
    assert "promotion_preview_required" in policy.reasons


def test_source_import_policy_allows_narrower_targets_without_preview() -> None:
    manifest = _manifest(
        privacy_class=SourcePrivacyClass.ORGANIZATION,
        target_memory_scope="private",
    )

    policy = source_import_policy(manifest)

    assert policy.privacy_class is SourcePrivacyClass.ORGANIZATION
    assert policy.target_memory_scope is MemoryScope.PRIVATE
    assert policy.requires_promotion_preview is False


def test_source_import_policy_uses_record_privacy_override() -> None:
    manifest = _manifest(
        privacy_class=SourcePrivacyClass.PROJECT,
        target_memory_scope="project",
        target_scope_key="project_123",
    )

    policy = source_import_policy(manifest, privacy_class=SourcePrivacyClass.SENSITIVE)

    assert policy.privacy_class is SourcePrivacyClass.SENSITIVE
    assert policy.target_memory_scope is MemoryScope.PROJECT
    assert policy.requires_promotion_preview is True


def test_source_import_policy_requires_scope_key_for_project_targets() -> None:
    manifest = _manifest(target_memory_scope="project", target_scope_key=None)

    with pytest.raises(ValueError, match="project imports require target_scope_key"):
        source_import_policy(manifest)


def test_plan_source_import_uses_descriptor_schema_without_branching() -> None:
    adapter = FakeSourceAdapter([])
    manifest = _manifest(metadata_schema={})

    plan = plan_source_import(adapter, manifest)

    assert plan.adapter.name == "fake"
    assert plan.manifest.metadata_schema == {"message_id": "string"}
    assert plan.policy.target_memory_scope is MemoryScope.PRIVATE


def test_raw_memory_write_preserves_source_metadata() -> None:
    manifest = _manifest()
    record = _record(manifest)

    payload = raw_memory_write_from_source_record(
        manifest=manifest,
        record=record,
        organization_id="org-1",
        principal_id="user-1",
    )

    assert payload.source_id == record.source_id
    assert payload.memory_scope is MemoryScope.PRIVATE
    assert payload.metadata["adapter_name"] == "fake"
    assert payload.metadata["source_adapter_version"] == "1.0"
    assert payload.metadata["privacy_class"] == "personal"
    assert payload.metadata["dedupe_key"] == record.dedupe_key
    assert payload.metadata["attachment_count"] == 1
    assert payload.metadata["adapter_record_id"] == "message-1"
    assert payload.metadata["source_record_id"] == record.source_id
    assert payload.metadata["source_extraction_state"] == "pending"
    assert payload.provenance["adapter_record_id"] == "message-1"
    assert payload.provenance["source_record_id"] == record.source_id


def test_raw_memory_write_flags_sensitive_record_in_wider_manifest() -> None:
    manifest = _manifest(
        privacy_class=SourcePrivacyClass.PROJECT,
        target_memory_scope="project",
        target_scope_key="project_123",
    )
    record = _record(manifest).model_copy(update={"privacy_class": SourcePrivacyClass.SENSITIVE})

    payload = raw_memory_write_from_source_record(
        manifest=manifest,
        record=record,
        organization_id="org-1",
        principal_id="user-1",
    )

    assert payload.memory_scope is MemoryScope.PROJECT
    assert payload.metadata["privacy_class"] == "sensitive"
    assert payload.metadata["import_requires_promotion_preview"] is True


def test_raw_memory_write_classifies_secret_records_as_sensitive() -> None:
    manifest = _manifest(target_memory_scope="project", target_scope_key="project_123")
    record = _record(manifest).model_copy(
        update={"body": "Rotate AWS key AKIAIOSFODNN7EXAMPLE today"}
    )

    payload = raw_memory_write_from_source_record(
        manifest=manifest,
        record=record,
        organization_id="org-1",
        principal_id="user-1",
    )

    assert payload.metadata["contains_secret"] is True
    assert payload.metadata["contains_pii"] is False
    assert payload.metadata["contains_sensitive"] is True
    assert payload.metadata["sensitivity_flags"] == ["api_key"]
    assert payload.metadata["privacy_class"] == "sensitive"
    assert payload.metadata["source_declared_privacy_class"] == "personal"
    assert payload.metadata["import_requires_promotion_preview"] is True


@pytest.mark.asyncio
async def test_import_source_batch_uses_registered_adapter_contract() -> None:
    manifest = _manifest()
    record = _record(manifest)
    adapter = FakeSourceAdapter([record])
    registry = SourceAdapterRegistry()
    registry.register(adapter)
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return RawMemory(
            id=f"raw-{len(writes)}",
            organization_id=str(kwargs["organization_id"]),
            source_id=str(kwargs["source_id"]),
            principal_id=str(kwargs["principal_id"]),
            memory_scope=kwargs["memory_scope"],
            scope_key=kwargs["scope_key"],
            title=str(kwargs["title"]),
            raw_content=str(kwargs["raw_content"]),
            tags=list(kwargs["tags"]),
            metadata=dict(kwargs["metadata"]),
            provenance=dict(kwargs["provenance"]),
            capture_surface=str(kwargs["capture_surface"]),
            entity_type=str(kwargs["entity_type"]),
            captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
            created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )

    result = await import_source_batch(
        registry.get("fake"),
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        batch_size=10,
        remember=fake_remember,
    )

    assert result.imported_count == 1
    assert result.skipped_count == 1
    assert result.raw_memory_ids == ("raw-1",)
    assert result.dedupe_keys == (record.dedupe_key,)
    assert result.checkpoint is not None
    assert result.checkpoint.done is True
    assert result.skipped_records[0].metadata["adapter_name"] == "fake"
    assert result.skipped_records[0].metadata["adapter_version"] == "1.0"
    assert result.skipped_records[0].metadata["source_identity"] == "fixture"
    assert result.skipped_records[0].metadata["source_version"] == "v1"
    assert writes[0]["source_id"] == record.source_id
    assert writes[0]["capture_surface"] == "source_import"


@pytest.mark.asyncio
async def test_import_source_batch_uses_batch_rememberer_for_batch_writes() -> None:
    manifest = _manifest()
    records = [_record(manifest, record_id="message-1"), _record(manifest, record_id="message-2")]
    adapter = FakeSourceAdapter(records)

    class BatchRememberer:
        def __init__(self) -> None:
            self.payload_batches: list[list[SourceRawMemoryWrite]] = []

        async def __call__(self, **_kwargs: object) -> RawMemory:
            raise AssertionError("source imports should use the batch write path")

        async def remember_many(
            self,
            payloads: list[SourceRawMemoryWrite],
        ) -> list[RawMemory]:
            payload_batch = list(payloads)
            self.payload_batches.append(payload_batch)
            return [
                RawMemory(
                    id=f"raw-{index}",
                    organization_id=payload.organization_id,
                    source_id=payload.source_id,
                    principal_id=payload.principal_id,
                    memory_scope=payload.memory_scope,
                    scope_key=payload.scope_key,
                    title=payload.title,
                    raw_content=payload.raw_content,
                    tags=list(payload.tags),
                    metadata=dict(payload.metadata),
                    provenance=dict(payload.provenance),
                    capture_surface=payload.capture_surface,
                    entity_type=payload.entity_type,
                    captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
                    created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
                )
                for index, payload in enumerate(payload_batch, start=1)
            ]

    rememberer = BatchRememberer()

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        batch_size=10,
        remember=rememberer,
    )

    assert result.imported_count == 2
    assert result.raw_memory_ids == ("raw-1", "raw-2")
    assert len(rememberer.payload_batches) == 1
    assert [payload.source_id for payload in rememberer.payload_batches[0]] == [
        record.source_id for record in records
    ]


@pytest.mark.asyncio
async def test_import_source_batch_skips_in_batch_duplicates_after_flush() -> None:
    manifest = _manifest()
    first = _record(manifest, record_id="message-1")
    duplicate = _record(manifest, record_id="message-1")
    adapter = FakeSourceAdapter([first, duplicate])

    class BatchRememberer:
        def __init__(self) -> None:
            self.payload_batches: list[list[SourceRawMemoryWrite]] = []
            self.next_id = 1

        async def __call__(self, **_kwargs: object) -> RawMemory:
            raise AssertionError("source imports should use the batch write path")

        async def remember_many(
            self,
            payloads: list[SourceRawMemoryWrite],
        ) -> list[RawMemory]:
            payload_batch = list(payloads)
            self.payload_batches.append(payload_batch)
            memories: list[RawMemory] = []
            for payload in payload_batch:
                memory_id = f"raw-{self.next_id}"
                self.next_id += 1
                memories.append(
                    RawMemory(
                        id=memory_id,
                        organization_id=payload.organization_id,
                        source_id=payload.source_id,
                        principal_id=payload.principal_id,
                        memory_scope=payload.memory_scope,
                        scope_key=payload.scope_key,
                        title=payload.title,
                        raw_content=payload.raw_content,
                        tags=list(payload.tags),
                        metadata=dict(payload.metadata),
                        provenance=dict(payload.provenance),
                        capture_surface=payload.capture_surface,
                        entity_type=payload.entity_type,
                        captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
                        created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
                    )
                )
            return memories

    rememberer = BatchRememberer()

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        batch_size=10,
        remember=rememberer,
    )

    assert result.imported_count == 1
    assert result.dedupe_count == 1
    assert result.raw_memory_ids == ("raw-1",)
    assert len(rememberer.payload_batches) == 1
    assert [payload.source_id for payload in rememberer.payload_batches[0]] == [first.source_id]
    duplicate_skip = result.skipped_records[1]
    assert duplicate_skip.reason == "duplicate_dedupe_key"
    assert duplicate_skip.metadata["raw_memory_id"] == "raw-1"
    assert duplicate_skip.metadata["source_id"] == first.source_id


@pytest.mark.asyncio
async def test_import_source_batch_supersedes_in_batch_source_updates() -> None:
    manifest = _manifest()
    first = _record(manifest, record_id="message-1")
    changed_hash = build_source_content_hash("Subject", "Updated body")
    changed = first.model_copy(
        update={
            "body": "Updated body",
            "content_hash": changed_hash,
            "dedupe_key": build_source_dedupe_key(
                manifest=manifest,
                adapter_record_id=first.adapter_record_id,
                content_hash=changed_hash,
            ).value,
        }
    )
    adapter = FakeSourceAdapter([first, changed])
    supersession_calls: list[dict[str, object]] = []

    class BatchRememberer:
        def __init__(self) -> None:
            self.payload_batches: list[list[SourceRawMemoryWrite]] = []
            self.next_id = 1

        async def __call__(self, **_kwargs: object) -> RawMemory:
            raise AssertionError("source imports should use the batch write path")

        async def remember_many(
            self,
            payloads: list[SourceRawMemoryWrite],
        ) -> list[RawMemory]:
            payload_batch = list(payloads)
            self.payload_batches.append(payload_batch)
            memories: list[RawMemory] = []
            for payload in payload_batch:
                memory_id = f"raw-{self.next_id}"
                self.next_id += 1
                memories.append(
                    RawMemory(
                        id=memory_id,
                        organization_id=payload.organization_id,
                        source_id=payload.source_id,
                        principal_id=payload.principal_id,
                        memory_scope=payload.memory_scope,
                        scope_key=payload.scope_key,
                        title=payload.title,
                        raw_content=payload.raw_content,
                        tags=list(payload.tags),
                        metadata=dict(payload.metadata),
                        provenance=dict(payload.provenance),
                        capture_surface=payload.capture_surface,
                        entity_type=payload.entity_type,
                        captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
                        created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
                    )
                )
            return memories

    async def supersession_handler(**kwargs: object) -> bool:
        supersession_calls.append(dict(kwargs))
        return True

    rememberer = BatchRememberer()

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        batch_size=10,
        remember=rememberer,
        supersession_handler=supersession_handler,
    )

    assert result.imported_count == 2
    assert result.superseded_count == 1
    assert result.raw_memory_ids == ("raw-1", "raw-2")
    assert len(rememberer.payload_batches) == 2
    assert len(supersession_calls) == 1
    assert supersession_calls[0]["superseded_raw_memory_id"] == "raw-1"
    memory = supersession_calls[0]["memory"]
    assert isinstance(memory, RawMemory)
    assert memory.id == "raw-2"


@pytest.mark.asyncio
async def test_import_source_batch_writes_sensitivity_metadata() -> None:
    manifest = _manifest()
    record = _record(manifest).model_copy(update={"body": "Temporary verification code is 123456"})
    adapter = FakeSourceAdapter([record])
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return RawMemory(
            id="raw-1",
            organization_id=str(kwargs["organization_id"]),
            source_id=str(kwargs["source_id"]),
            principal_id=str(kwargs["principal_id"]),
            memory_scope=kwargs["memory_scope"],
            scope_key=kwargs["scope_key"],
            title=str(kwargs["title"]),
            raw_content=str(kwargs["raw_content"]),
            tags=list(kwargs["tags"]),
            metadata=dict(kwargs["metadata"]),
            provenance=dict(kwargs["provenance"]),
            capture_surface=str(kwargs["capture_surface"]),
            entity_type=str(kwargs["entity_type"]),
            captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
            created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=fake_remember,
    )

    assert result.imported_count == 1
    assert result.policy.privacy_class is SourcePrivacyClass.SENSITIVE
    assert result.contains_secret is True
    assert result.sensitivity_flags == ("two_factor_code",)
    assert writes[0]["metadata"]["contains_secret"] is True
    assert writes[0]["metadata"]["sensitivity_flags"] == ["two_factor_code"]


@pytest.mark.asyncio
async def test_import_source_batch_reports_effective_sensitive_policy() -> None:
    manifest = _manifest(target_memory_scope="project", target_scope_key="project_123")
    record = _record(manifest).model_copy(
        update={"body": "Rotate AWS key AKIAIOSFODNN7EXAMPLE today"}
    )
    adapter = FakeSourceAdapter([record])
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return RawMemory(
            id="raw-1",
            organization_id=str(kwargs["organization_id"]),
            source_id=str(kwargs["source_id"]),
            principal_id=str(kwargs["principal_id"]),
            memory_scope=kwargs["memory_scope"],
            scope_key=kwargs["scope_key"],
            title=str(kwargs["title"]),
            raw_content=str(kwargs["raw_content"]),
            tags=list(kwargs["tags"]),
            metadata=dict(kwargs["metadata"]),
            provenance=dict(kwargs["provenance"]),
            capture_surface=str(kwargs["capture_surface"]),
            entity_type=str(kwargs["entity_type"]),
            captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
            created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        promotion_preview_approved=True,
        remember=fake_remember,
    )

    assert result.policy.privacy_class is SourcePrivacyClass.SENSITIVE
    assert result.policy.requires_promotion_preview is True
    assert result.contains_secret is True
    assert result.sensitivity_flags == ("api_key",)
    assert writes[0]["metadata"]["privacy_class"] == "sensitive"


@pytest.mark.asyncio
async def test_import_source_batch_tracks_metadata_only_records_as_pending() -> None:
    manifest = _manifest(transform_behavior=SourceTransformBehavior.METADATA_ONLY)
    metadata_hash = build_source_content_hash("Subject", "headers-only")
    dedupe_key = build_source_dedupe_key(
        manifest=manifest,
        adapter_record_id="message-1",
        content_hash=metadata_hash,
    )
    record = _record(manifest).model_copy(
        update={
            "body": "",
            "content_hash": metadata_hash,
            "dedupe_key": dedupe_key.value,
            "transform_behavior": SourceTransformBehavior.METADATA_ONLY,
            "transform_version": "headers-v1",
            "attachments": [],
        }
    )
    adapter = FakeSourceAdapter([record])
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        return RawMemory(
            id=f"raw-{len(writes)}",
            organization_id=str(kwargs["organization_id"]),
            source_id=str(kwargs["source_id"]),
            principal_id=str(kwargs["principal_id"]),
            memory_scope=kwargs["memory_scope"],
            scope_key=kwargs["scope_key"],
            title=str(kwargs["title"]),
            raw_content=str(kwargs["raw_content"]),
            tags=list(kwargs["tags"]),
            metadata=dict(kwargs["metadata"]),
            provenance=dict(kwargs["provenance"]),
            capture_surface=str(kwargs["capture_surface"]),
            entity_type=str(kwargs["entity_type"]),
            captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
            created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=fake_remember,
    )

    assert result.imported_count == 1
    assert result.attachment_count == 0
    assert result.extraction_pending_count == 1
    assert writes[0]["raw_content"] == ""
    assert writes[0]["metadata"]["source_extraction_state"] == "pending"
    assert writes[0]["metadata"]["transform_behavior"] == "metadata_only"
    assert writes[0]["metadata"]["transform_version"] == "headers-v1"


@pytest.mark.asyncio
async def test_import_source_batch_records_duplicate_skip_metadata() -> None:
    manifest = _manifest()
    record = _record(manifest)
    adapter = FakeSourceAdapter([record])

    async def fake_remember(**kwargs: object) -> RawMemory:
        raise AssertionError("duplicate records should not be written")

    async def duplicate_checker(**kwargs: object) -> str | None:
        assert kwargs["record"] == record
        return "raw-existing"

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=fake_remember,
        duplicate_checker=duplicate_checker,
    )

    assert result.imported_count == 0
    assert result.dedupe_count == 1
    assert result.duplicate_dedupe_keys == (record.dedupe_key,)
    duplicate_skip = result.skipped_records[1]
    assert duplicate_skip.reason == "duplicate_dedupe_key"
    assert duplicate_skip.metadata["adapter_name"] == "fake"
    assert duplicate_skip.metadata["source_identity"] == "fixture"
    assert duplicate_skip.metadata["source_version"] == "v1"
    assert duplicate_skip.metadata["raw_memory_id"] == "raw-existing"
    assert duplicate_skip.metadata["source_id"] == record.source_id


@pytest.mark.asyncio
async def test_import_source_batch_invokes_supersession_handler_after_write() -> None:
    manifest = _manifest()
    record = _record(manifest)
    adapter = FakeSourceAdapter([record])
    supersession_calls: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        return RawMemory(
            id="raw-new",
            organization_id=str(kwargs["organization_id"]),
            source_id=str(kwargs["source_id"]),
            principal_id=str(kwargs["principal_id"]),
            memory_scope=kwargs["memory_scope"],
            scope_key=kwargs["scope_key"],
            title=str(kwargs["title"]),
            raw_content=str(kwargs["raw_content"]),
            tags=list(kwargs["tags"]),
            metadata=dict(kwargs["metadata"]),
            provenance=dict(kwargs["provenance"]),
            capture_surface=str(kwargs["capture_surface"]),
            entity_type=str(kwargs["entity_type"]),
            captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
            created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )

    async def duplicate_checker(**kwargs: object) -> SourceRecordImportDecision:
        assert kwargs["record"] == record
        return SourceRecordImportDecision(superseded_raw_memory_id="raw-old")

    async def supersession_handler(**kwargs: object) -> bool:
        supersession_calls.append(dict(kwargs))
        return True

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=fake_remember,
        duplicate_checker=duplicate_checker,
        supersession_handler=supersession_handler,
    )

    assert result.imported_count == 1
    assert result.dedupe_count == 0
    assert result.superseded_count == 1
    assert len(supersession_calls) == 1
    memory = supersession_calls[0]["memory"]
    assert isinstance(memory, RawMemory)
    assert memory.id == "raw-new"
    assert supersession_calls[0]["superseded_raw_memory_id"] == "raw-old"


@pytest.mark.asyncio
async def test_import_source_batch_keeps_import_when_batch_supersession_fails() -> None:
    manifest = _manifest()
    record = _record(manifest)
    adapter = FakeSourceAdapter([record])
    supersession_calls: list[dict[str, object]] = []

    class BatchRememberer:
        async def __call__(self, **_kwargs: object) -> RawMemory:
            raise AssertionError("source imports should use the batch write path")

        async def remember_many(
            self,
            payloads: list[SourceRawMemoryWrite],
        ) -> list[RawMemory]:
            return [
                RawMemory(
                    id="raw-new",
                    organization_id=payload.organization_id,
                    source_id=payload.source_id,
                    principal_id=payload.principal_id,
                    memory_scope=payload.memory_scope,
                    scope_key=payload.scope_key,
                    title=payload.title,
                    raw_content=payload.raw_content,
                    tags=list(payload.tags),
                    metadata=dict(payload.metadata),
                    provenance=dict(payload.provenance),
                    capture_surface=payload.capture_surface,
                    entity_type=payload.entity_type,
                    captured_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
                    created_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
                )
                for payload in payloads
            ]

    async def duplicate_checker(**kwargs: object) -> SourceRecordImportDecision:
        assert kwargs["record"] == record
        return SourceRecordImportDecision(superseded_raw_memory_id="raw-old")

    async def supersession_handler(**kwargs: object) -> bool:
        supersession_calls.append(dict(kwargs))
        return False

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=BatchRememberer(),
        duplicate_checker=duplicate_checker,
        supersession_handler=supersession_handler,
    )

    assert result.imported_count == 1
    assert result.superseded_count == 0
    assert result.raw_memory_ids == ("raw-new",)
    assert len(supersession_calls) == 1
    assert supersession_calls[0]["superseded_raw_memory_id"] == "raw-old"


@pytest.mark.asyncio
async def test_import_source_batch_blocks_wider_import_without_preview() -> None:
    manifest = _manifest(target_memory_scope="project", target_scope_key="project_123")
    adapter = FakeSourceAdapter([_record(manifest)])

    async def fake_remember(**kwargs: object) -> RawMemory:
        raise AssertionError("memory write should not run before promotion preview")

    with pytest.raises(ValueError, match="requires promotion preview"):
        await import_source_batch(
            adapter,
            manifest,
            organization_id="org-1",
            principal_id="user-1",
            remember=fake_remember,
        )


@pytest.mark.asyncio
async def test_import_source_batch_preflights_batch_before_writes() -> None:
    manifest = _manifest(
        privacy_class=SourcePrivacyClass.PROJECT,
        target_memory_scope="project",
        target_scope_key="project_123",
    )
    safe_record = _record(manifest, record_id="message-1")
    sensitive_record = _record(manifest, record_id="message-2").model_copy(
        update={"privacy_class": SourcePrivacyClass.SENSITIVE}
    )
    adapter = FakeSourceAdapter([safe_record, sensitive_record])
    writes: list[dict[str, object]] = []

    async def fake_remember(**kwargs: object) -> RawMemory:
        writes.append(dict(kwargs))
        raise AssertionError("batch preflight should run before any memory write")

    with pytest.raises(ValueError, match="requires promotion preview"):
        await import_source_batch(
            adapter,
            manifest,
            organization_id="org-1",
            principal_id="user-1",
            remember=fake_remember,
        )

    assert writes == []


@pytest.mark.asyncio
async def test_import_source_batch_returns_empty_result_for_empty_adapter() -> None:
    manifest = _manifest()
    adapter = EmptySourceAdapter([])

    async def fake_remember(**kwargs: object) -> RawMemory:
        raise AssertionError("empty adapters should not write memory")

    result = await import_source_batch(
        adapter,
        manifest,
        organization_id="org-1",
        principal_id="user-1",
        remember=fake_remember,
    )

    assert result.imported_count == 0
    assert result.skipped_count == 0
    assert result.raw_memory_ids == ()
    assert result.dedupe_keys == ()
    assert result.checkpoint is None
