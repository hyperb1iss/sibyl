#!/usr/bin/env python3
"""Run the v0.11 large-corpus source import rehearsal."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    SourceImportResult,
    SourceRawMemoryWrite,
    SourceRecordDuplicateChecker,
    build_source_content_hash,
    build_source_dedupe_key,
    build_source_record_id,
    import_source_batch,
)
from sibyl_core.services.surreal_content import MemoryScope, RawMemory

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE_PATH = (
    REPO_ROOT / "packages/python/sibyl-core/tests/fixtures/large_corpus/dogfood.json"
)
DEFAULT_ARTIFACT_PATH = REPO_ROOT / ".moon/cache/large-corpus-rehearsal/receipt.json"
ADAPTER_NAME = "large-corpus-dogfood"
ADAPTER_VERSION = "1.0"
ORGANIZATION_ID = "org-rehearsal"
PRINCIPAL_ID = "user-rehearsal"

Echo = Callable[[str], None]
type JsonObject = dict[str, Any]


class RehearsalFailure(RuntimeError):
    """Raised when the rehearsal no longer proves the release claim."""


@dataclass(frozen=True, slots=True)
class FixtureEntry:
    case: str
    adapter_record_id: str
    title: str
    body: str
    privacy_class: SourcePrivacyClass
    transform_behavior: SourceTransformBehavior = SourceTransformBehavior.RAW
    labels: tuple[str, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)
    attachments: tuple[SourceAttachmentRecord, ...] = ()
    skip_reason: str | None = None


@dataclass(slots=True)
class RunAccumulator:
    name: str
    batches: int = 0
    imported_count: int = 0
    skipped_count: int = 0
    dedupe_count: int = 0
    attachment_count: int = 0
    extraction_pending_count: int = 0
    error_count: int = 0
    raw_memory_count: int = 0
    checkpoint_cursors: list[str | None] = field(default_factory=list)
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def merge(self, result: SourceImportResult) -> None:
        self.batches += 1
        self.imported_count += result.imported_count
        self.skipped_count += result.skipped_count
        self.dedupe_count += result.dedupe_count
        self.attachment_count += result.attachment_count
        self.extraction_pending_count += result.extraction_pending_count
        self.raw_memory_count += len(result.raw_memory_ids)
        if result.checkpoint is not None:
            self.checkpoint_cursors.append(result.checkpoint.cursor)
        for skipped in result.skipped_records:
            self.skipped_reasons[skipped.reason] = self.skipped_reasons.get(skipped.reason, 0) + 1
            if "error" in skipped.reason:
                self.error_count += 1

    def as_dict(self) -> dict[str, object]:
        return {
            "batches": self.batches,
            "imported_count": self.imported_count,
            "skipped_count": self.skipped_count,
            "dedupe_count": self.dedupe_count,
            "attachment_count": self.attachment_count,
            "extraction_pending_count": self.extraction_pending_count,
            "error_count": self.error_count,
            "raw_memory_count": self.raw_memory_count,
            "checkpoint_cursors": list(self.checkpoint_cursors),
            "skipped_reasons": dict(sorted(self.skipped_reasons.items())),
        }


class FixtureCorpusAdapter:
    descriptor = SourceAdapterDescriptor(
        name=ADAPTER_NAME,
        version=ADAPTER_VERSION,
        source_type="dogfood_fixture",
        display_name="Large corpus dogfood fixture",
        capabilities=[
            SourceAdapterCapability.ATTACHMENTS,
            SourceAdapterCapability.CHECKPOINTS,
            SourceAdapterCapability.INCREMENTAL,
            SourceAdapterCapability.SKIPPED_RECORDS,
        ],
        default_privacy_class=SourcePrivacyClass.PERSONAL,
        transform_behavior=SourceTransformBehavior.RAW,
        metadata_schema={
            "fixture_case": "string",
            "fixture_section": "string",
            "search_tokens": "string[]",
        },
        supports_incremental=True,
    )

    def __init__(
        self,
        *,
        fixture: Mapping[str, Any],
        section: str,
        entries: tuple[FixtureEntry, ...],
    ) -> None:
        self._fixture = fixture
        self._section = section
        self._entries = entries

    async def prepare_manifest(
        self,
        *,
        source_uri: str,
        options: Mapping[str, object] | None = None,
    ) -> SourceImportManifest:
        option_values = dict(options or {})
        target_memory_scope = str(option_values.get("target_memory_scope") or "private")
        target_scope_key = _optional_str(option_values.get("target_scope_key"))
        privacy_class = SourcePrivacyClass(
            str(option_values.get("privacy_class") or SourcePrivacyClass.PERSONAL)
        )
        source_identity = f"{self._fixture['fixture_id']!s}:{self._section}"
        source_version = f"{self._fixture['version']!s}:entries:{len(self._entries)}"

        return SourceImportManifest(
            adapter_name=self.descriptor.name,
            adapter_version=self.descriptor.version,
            source_identity=source_identity,
            source_uri=f"{source_uri}/{self._section}",
            source_version=source_version,
            target_memory_scope=target_memory_scope,
            target_scope_key=target_scope_key,
            privacy_class=privacy_class,
            transform_behavior=self.descriptor.transform_behavior,
            metadata_schema=dict(self.descriptor.metadata_schema),
            metadata={
                "fixture_id": str(self._fixture["fixture_id"]),
                "fixture_section": self._section,
                "fixture_version": str(self._fixture["version"]),
                "entry_count": len(self._entries),
            },
            options=option_values,
        )

    async def iter_records(
        self,
        manifest: SourceImportManifest,
        *,
        checkpoint: SourceImportCheckpoint | None = None,
        batch_size: int = 100,
    ) -> AsyncIterator[SourceRecordBatch]:
        start = int(checkpoint.cursor) if checkpoint and checkpoint.cursor else 0
        end = min(start + batch_size, len(self._entries))
        records: list[SourceRecord] = []
        skipped: list[SourceSkippedRecord] = []

        for index, entry in enumerate(self._entries[start:end], start=start):
            source_uri = f"{manifest.source_uri}#entry={index}"
            if entry.skip_reason is not None:
                skipped.append(
                    SourceSkippedRecord(
                        adapter_record_id=entry.adapter_record_id,
                        source_uri=source_uri,
                        reason=entry.skip_reason,
                        metadata={
                            "fixture_case": entry.case,
                            "fixture_section": self._section,
                            **entry.metadata,
                        },
                    )
                )
                continue
            records.append(_source_record_from_entry(manifest, entry, source_uri=source_uri))

        done = end >= len(self._entries)
        yield SourceRecordBatch(
            records=records,
            skipped=skipped,
            checkpoint=SourceImportCheckpoint(
                cursor=str(end) if not done else None,
                source_version=manifest.source_version,
                records_seen=end,
                records_imported=len(records),
                records_skipped=len(skipped),
                done=done,
                metadata={
                    "fixture_id": str(self._fixture["fixture_id"]),
                    "fixture_section": self._section,
                },
            ),
        )


class InMemoryRawMemoryStore:
    def __init__(self) -> None:
        self.memories: list[RawMemory] = []
        self._by_source_id: dict[str, RawMemory] = {}

    async def remember(self, **kwargs: object) -> RawMemory:
        memory = RawMemory(
            id=f"raw-rehearsal-{len(self.memories) + 1:04d}",
            organization_id=str(kwargs["organization_id"]),
            source_id=str(kwargs["source_id"]),
            principal_id=str(kwargs["principal_id"]),
            memory_scope=MemoryScope(str(kwargs["memory_scope"])),
            scope_key=_optional_str(kwargs.get("scope_key")),
            title=str(kwargs["title"]),
            raw_content=str(kwargs["raw_content"]),
            tags=[str(tag) for tag in _sequence_value(kwargs["tags"])],
            metadata=_dict_value(kwargs["metadata"]),
            provenance=_dict_value(kwargs["provenance"]),
            capture_surface=str(kwargs["capture_surface"]),
            entity_type=str(kwargs["entity_type"]),
            captured_at=datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
            created_at=datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
        )
        self.memories.append(memory)
        self._by_source_id[memory.source_id] = memory
        return memory

    def duplicate_checker(self) -> SourceRecordDuplicateChecker:
        async def check_duplicate(
            *,
            record: SourceRecord,
            payload: SourceRawMemoryWrite,
        ) -> str | None:
            existing = self._by_source_id.get(record.source_id)
            return existing.id if existing is not None else None

        return check_duplicate

    def search(self, query: str, *, scope: MemoryScope | None = None) -> list[RawMemory]:
        lowered = query.casefold()
        matches: list[RawMemory] = []
        for memory in self.memories:
            if scope is not None and memory.memory_scope is not scope:
                continue
            haystack = " ".join(
                [
                    memory.title,
                    memory.raw_content,
                    " ".join(memory.tags),
                    json.dumps(memory.metadata, sort_keys=True),
                ]
            ).casefold()
            if lowered in haystack:
                matches.append(memory)
        return matches


def load_fixture(path: Path = DEFAULT_FIXTURE_PATH) -> JsonObject:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RehearsalFailure("large corpus fixture must be a JSON object")
    return {str(key): value for key, value in payload.items()}


def run_rehearsal(
    *,
    fixture_path: Path = DEFAULT_FIXTURE_PATH,
    artifact_path: Path = DEFAULT_ARTIFACT_PATH,
    echo: Echo | None = None,
) -> JsonObject:
    active_echo = echo or _echo
    receipt = asyncio.run(
        _run_rehearsal_async(
            fixture=load_fixture(fixture_path),
            artifact_path=artifact_path,
        )
    )
    _print_receipt(receipt, echo=active_echo)
    return receipt


async def _run_rehearsal_async(
    *,
    fixture: JsonObject,
    artifact_path: Path,
) -> JsonObject:
    store = InMemoryRawMemoryStore()
    source_uri = str(fixture["source_uri"])
    project_scope_key = str(fixture["project_scope_key"])
    search_checks: list[JsonObject] = []

    private_entries = _private_entries(fixture)
    private_manifest = await _manifest(
        fixture=fixture,
        section="private",
        entries=private_entries,
        source_uri=source_uri,
        target_memory_scope="private",
        target_scope_key=None,
        privacy_class=SourcePrivacyClass.PERSONAL,
    )
    private = await _run_section(
        fixture=fixture,
        section="private",
        entries=private_entries,
        manifest=private_manifest,
        batch_size=_section_batch_size(fixture, "private"),
        store=store,
        after_first_batch=lambda: search_checks.extend(_early_search_checks(store)),
    )

    project_entries = _project_entries(fixture)
    project_manifest = await _manifest(
        fixture=fixture,
        section="project",
        entries=project_entries,
        source_uri=source_uri,
        target_memory_scope="project",
        target_scope_key=project_scope_key,
        privacy_class=SourcePrivacyClass.PROJECT,
    )
    project = await _run_section(
        fixture=fixture,
        section="project",
        entries=project_entries,
        manifest=project_manifest,
        batch_size=_section_batch_size(fixture, "project"),
        store=store,
    )

    policy_check = await _policy_failure_probe(fixture=fixture, source_uri=source_uri, store=store)
    scope_check = _scope_leak_check(store, project_scope_key=project_scope_key)
    totals = _totals(private, project)
    _validate_expected("private", private.as_dict(), _section_expected(fixture, "private"))
    _validate_expected("project", project.as_dict(), _section_expected(fixture, "project"))
    _validate_expected("total", totals, _expected(fixture))

    receipt: JsonObject = {
        "schema_version": "large-corpus-rehearsal/v1",
        "status": "PASS",
        "fixture_id": str(fixture["fixture_id"]),
        "fixture_version": str(fixture["version"]),
        "artifact_path": _display_path(artifact_path),
        "sections": {
            "private": private.as_dict(),
            "project": project.as_dict(),
        },
        "totals": totals,
        "checks": {
            "early_search": search_checks,
            "policy_failure_probe": policy_check,
            "scope_leak": scope_check,
        },
        "workspace_progress_contract": {
            "imported_count": totals["imported_count"],
            "skipped_count": totals["skipped_count"],
            "dedupe_count": totals["dedupe_count"],
            "error_count": totals["error_count"],
            "attachment_count": totals["attachment_count"],
            "extraction_pending_count": totals["extraction_pending_count"],
            "raw_memory_count": totals["raw_memory_count"],
        },
    }
    _write_receipt(artifact_path, receipt)
    return receipt


async def _manifest(
    *,
    fixture: Mapping[str, Any],
    section: str,
    entries: tuple[FixtureEntry, ...],
    source_uri: str,
    target_memory_scope: str,
    target_scope_key: str | None,
    privacy_class: SourcePrivacyClass,
) -> SourceImportManifest:
    adapter = FixtureCorpusAdapter(fixture=fixture, section=section, entries=entries)
    return await adapter.prepare_manifest(
        source_uri=source_uri,
        options={
            "target_memory_scope": target_memory_scope,
            "target_scope_key": target_scope_key,
            "privacy_class": privacy_class.value,
        },
    )


async def _run_section(
    *,
    fixture: Mapping[str, Any],
    section: str,
    entries: tuple[FixtureEntry, ...],
    manifest: SourceImportManifest,
    batch_size: int,
    store: InMemoryRawMemoryStore,
    after_first_batch: Callable[[], None] | None = None,
) -> RunAccumulator:
    adapter = FixtureCorpusAdapter(fixture=fixture, section=section, entries=entries)
    accumulator = RunAccumulator(name=section)
    checkpoint: SourceImportCheckpoint | None = None
    cursors_seen: set[str] = set()

    while True:
        result = await import_source_batch(
            adapter,
            manifest,
            organization_id=ORGANIZATION_ID,
            principal_id=PRINCIPAL_ID,
            checkpoint=checkpoint,
            batch_size=batch_size,
            promotion_preview_approved=False,
            remember=store.remember,
            duplicate_checker=store.duplicate_checker(),
        )
        accumulator.merge(result)
        if accumulator.batches == 1 and after_first_batch is not None:
            after_first_batch()

        next_checkpoint = result.checkpoint
        if next_checkpoint is None:
            raise RehearsalFailure(f"{section} import did not return a checkpoint")
        if next_checkpoint.done:
            break
        if next_checkpoint.cursor is None:
            raise RehearsalFailure(f"{section} checkpoint is not done but has no cursor")
        if next_checkpoint.cursor in cursors_seen:
            raise RehearsalFailure(
                f"{section} checkpoint did not advance: {next_checkpoint.cursor}"
            )
        cursors_seen.add(next_checkpoint.cursor)
        checkpoint = next_checkpoint

    return accumulator


async def _policy_failure_probe(
    *,
    fixture: Mapping[str, Any],
    source_uri: str,
    store: InMemoryRawMemoryStore,
) -> JsonObject:
    entries = (_normal_entry(section="policy-probe", index=0, privacy=SourcePrivacyClass.PERSONAL),)
    manifest = await _manifest(
        fixture=fixture,
        section="policy-probe",
        entries=entries,
        source_uri=source_uri,
        target_memory_scope="project",
        target_scope_key=str(fixture["project_scope_key"]),
        privacy_class=SourcePrivacyClass.PERSONAL,
    )
    adapter = FixtureCorpusAdapter(fixture=fixture, section="policy-probe", entries=entries)
    before_count = len(store.memories)
    try:
        await import_source_batch(
            adapter,
            manifest,
            organization_id=ORGANIZATION_ID,
            principal_id=PRINCIPAL_ID,
            batch_size=1,
            promotion_preview_approved=False,
            remember=store.remember,
            duplicate_checker=store.duplicate_checker(),
        )
    except ValueError as exc:
        if "promotion preview" not in str(exc):
            raise
        return {
            "status": "PASS",
            "reason": str(exc),
            "writes_blocked": len(store.memories) == before_count,
        }
    raise RehearsalFailure("policy probe allowed personal source import into project scope")


def _early_search_checks(store: InMemoryRawMemoryStore) -> list[JsonObject]:
    metadata_matches = store.search("metadata-probe-token", scope=MemoryScope.PRIVATE)
    attachment_matches = store.search("attachment-body-token", scope=MemoryScope.PRIVATE)
    if not metadata_matches:
        raise RehearsalFailure("metadata-only record was not searchable after first batch")
    if not attachment_matches:
        raise RehearsalFailure("attachment-bearing record was not searchable after first batch")
    return [
        {
            "name": "metadata-only-before-extraction",
            "status": "PASS",
            "raw_memory_id": metadata_matches[0].id,
            "source_extraction_state": metadata_matches[0].metadata["source_extraction_state"],
        },
        {
            "name": "attachment-before-extraction",
            "status": "PASS",
            "raw_memory_id": attachment_matches[0].id,
            "source_extraction_state": attachment_matches[0].metadata["source_extraction_state"],
        },
    ]


def _scope_leak_check(
    store: InMemoryRawMemoryStore,
    *,
    project_scope_key: str,
) -> JsonObject:
    private_leaks = [
        memory.id
        for memory in store.memories
        if str(memory.metadata.get("source_identity", "")).endswith(":private")
        and memory.memory_scope is not MemoryScope.PRIVATE
    ]
    project_leaks = [
        memory.id
        for memory in store.memories
        if str(memory.metadata.get("source_identity", "")).endswith(":project")
        and (
            memory.memory_scope is not MemoryScope.PROJECT or memory.scope_key != project_scope_key
        )
    ]
    if private_leaks or project_leaks:
        raise RehearsalFailure(
            f"scope leak detected: private={private_leaks}, project={project_leaks}"
        )
    return {
        "status": "PASS",
        "private_leaks": private_leaks,
        "project_leaks": project_leaks,
    }


def _private_entries(fixture: Mapping[str, Any]) -> tuple[FixtureEntry, ...]:
    generated = _section_generated_records(fixture, "private")
    entries = [
        FixtureEntry(
            case="metadata_only",
            adapter_record_id="private-metadata-only",
            title="Metadata only import",
            body="",
            privacy_class=SourcePrivacyClass.PRIVATE,
            transform_behavior=SourceTransformBehavior.METADATA_ONLY,
            labels=("mailbox", "metadata"),
            metadata={"search_tokens": ["metadata-probe-token"]},
        ),
        FixtureEntry(
            case="attachment",
            adapter_record_id="private-attachment",
            title="Attachment import",
            body="attachment-body-token source record with pending extraction",
            privacy_class=SourcePrivacyClass.PRIVATE,
            labels=("mailbox", "attachment"),
            attachments=(
                SourceAttachmentRecord(
                    adapter_attachment_id="private-attachment:part:1",
                    filename="packet-c.txt",
                    media_type="text/plain",
                    size_bytes=31,
                    content_hash=build_source_content_hash("attachment-body-token"),
                    source_path="fixture://attachment/packet-c.txt",
                ),
            ),
            metadata={"search_tokens": ["attachment-body-token"]},
        ),
        _duplicate_entry(case="duplicate_original"),
    ]
    entries.extend(
        _normal_entry(section="private", index=index, privacy=SourcePrivacyClass.PRIVATE)
        for index in range(generated)
    )
    entries.extend(
        [
            _duplicate_entry(case="duplicate_copy"),
            FixtureEntry(
                case="unsupported",
                adapter_record_id="private-skipped-unsupported",
                title="Unsupported calendar invite",
                body="",
                privacy_class=SourcePrivacyClass.PRIVATE,
                skip_reason="fixture_skipped",
                metadata={"reason": "unsupported_mime_type"},
            ),
            FixtureEntry(
                case="parse_error",
                adapter_record_id="private-error-corrupt",
                title="Corrupt source record",
                body="",
                privacy_class=SourcePrivacyClass.PRIVATE,
                skip_reason="fixture_parse_error",
                metadata={"error": "invalid message envelope"},
            ),
        ]
    )
    return tuple(entries)


def _project_entries(fixture: Mapping[str, Any]) -> tuple[FixtureEntry, ...]:
    return tuple(
        _normal_entry(section="project", index=index, privacy=SourcePrivacyClass.PROJECT)
        for index in range(_section_generated_records(fixture, "project"))
    )


def _normal_entry(
    *,
    section: str,
    index: int,
    privacy: SourcePrivacyClass,
) -> FixtureEntry:
    token = f"{section}-raw-search-token-{index:02d}"
    return FixtureEntry(
        case="normal",
        adapter_record_id=f"{section}-record-{index:03d}",
        title=f"{section.title()} corpus record {index:03d}",
        body=f"{token} import rehearsal body {index:03d}",
        privacy_class=privacy,
        labels=("dogfood", section),
        metadata={"search_tokens": [token], "ordinal": index},
    )


def _duplicate_entry(*, case: str) -> FixtureEntry:
    return FixtureEntry(
        case=case,
        adapter_record_id="private-duplicate-stable",
        title="Duplicate source identity",
        body="duplicate drift sentinel body",
        privacy_class=SourcePrivacyClass.PRIVATE,
        labels=("dogfood", "duplicate"),
        metadata={"search_tokens": ["duplicate-drift-token"]},
    )


def _source_record_from_entry(
    manifest: SourceImportManifest,
    entry: FixtureEntry,
    *,
    source_uri: str,
) -> SourceRecord:
    content_hash = build_source_content_hash(entry.title, entry.body, entry.adapter_record_id)
    dedupe_key = build_source_dedupe_key(
        manifest=manifest,
        adapter_record_id=entry.adapter_record_id,
        content_hash=content_hash,
    )
    source_id = build_source_record_id(
        manifest=manifest,
        adapter_record_id=entry.adapter_record_id,
    )
    metadata = {
        "fixture_case": entry.case,
        "fixture_section": manifest.metadata["fixture_section"],
        **entry.metadata,
    }
    return SourceRecord(
        adapter_record_id=entry.adapter_record_id,
        source_id=source_id,
        source_type="dogfood_source_record",
        source_uri=source_uri,
        source_version=manifest.source_version,
        title=entry.title,
        body=entry.body,
        content_hash=content_hash,
        dedupe_key=dedupe_key.value,
        privacy_class=entry.privacy_class,
        transform_behavior=entry.transform_behavior,
        transform_version=manifest.adapter_version,
        occurred_at=datetime(2026, 5, 17, 12, 0, tzinfo=UTC),
        participants=["bliss@example.com", "nova@example.com"],
        labels=list(entry.labels),
        metadata=metadata,
        attachments=list(entry.attachments),
    )


def _totals(private: RunAccumulator, project: RunAccumulator) -> dict[str, int]:
    return {
        "total_batches": private.batches + project.batches,
        "imported_count": private.imported_count + project.imported_count,
        "skipped_count": private.skipped_count + project.skipped_count,
        "dedupe_count": private.dedupe_count + project.dedupe_count,
        "attachment_count": private.attachment_count + project.attachment_count,
        "extraction_pending_count": (
            private.extraction_pending_count + project.extraction_pending_count
        ),
        "error_count": private.error_count + project.error_count,
        "raw_memory_count": private.raw_memory_count + project.raw_memory_count,
    }


def _validate_expected(
    label: str,
    actual: Mapping[str, object],
    expected: Mapping[str, object],
) -> None:
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        if actual_value != expected_value:
            raise RehearsalFailure(
                f"{label} {key} drift: expected {expected_value}, got {actual_value}"
            )


def _section_generated_records(fixture: Mapping[str, Any], section: str) -> int:
    return int(_section(fixture, section)["generated_records"])


def _section_batch_size(fixture: Mapping[str, Any], section: str) -> int:
    return int(_section(fixture, section)["batch_size"])


def _section_expected(fixture: Mapping[str, Any], section: str) -> Mapping[str, Any]:
    expected = _section(fixture, section)["expected"]
    if not isinstance(expected, dict):
        raise RehearsalFailure(f"{section} expected counts must be an object")
    return {str(key): value for key, value in expected.items()}


def _expected(fixture: Mapping[str, Any]) -> Mapping[str, Any]:
    expected = fixture["expected"]
    if not isinstance(expected, dict):
        raise RehearsalFailure("total expected counts must be an object")
    return {str(key): value for key, value in expected.items()}


def _section(fixture: Mapping[str, Any], section: str) -> Mapping[str, Any]:
    value = fixture[section]
    if not isinstance(value, dict):
        raise RehearsalFailure(f"{section} fixture section must be an object")
    return {str(key): item for key, item in value.items()}


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _sequence_value(value: object) -> list[object]:
    if isinstance(value, list | tuple):
        return list(value)
    raise RehearsalFailure("expected sequence value")


def _dict_value(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise RehearsalFailure("expected mapping value")
    return {str(key): item for key, item in value.items()}


def _write_receipt(path: Path, receipt: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _print_receipt(receipt: Mapping[str, object], *, echo: Echo) -> None:
    totals = _dict_value(receipt["totals"])
    echo("Large Corpus Rehearsal Receipt")
    echo(f"status: {receipt['status']}")
    echo(f"fixture: {receipt['fixture_id']}@{receipt['fixture_version']}")
    echo(f"artifact: {receipt['artifact_path']}")
    echo(
        "counts: "
        f"{totals['imported_count']} imported, "
        f"{totals['skipped_count']} skipped, "
        f"{totals['dedupe_count']} deduped, "
        f"{totals['error_count']} errored, "
        f"{totals['extraction_pending_count']} pending extraction"
    )


def _echo(message: str = "") -> None:
    sys.stdout.write(f"{message}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the v0.11 large-corpus import rehearsal.")
    parser.add_argument(
        "--fixture-path",
        type=Path,
        default=DEFAULT_FIXTURE_PATH,
        help="Dogfood fixture JSON to rehearse.",
    )
    parser.add_argument(
        "--artifact-path",
        type=Path,
        default=DEFAULT_ARTIFACT_PATH,
        help="Receipt JSON path for release notes.",
    )
    args = parser.parse_args(argv)

    try:
        run_rehearsal(
            fixture_path=args.fixture_path,
            artifact_path=args.artifact_path,
        )
    except RehearsalFailure as exc:
        _echo("Large Corpus Rehearsal Receipt")
        _echo("status: FAIL")
        _echo(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
