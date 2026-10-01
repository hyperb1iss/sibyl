"""Stream a checked archive request into an owned spool before logical parsing."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

from anyio import CancelScope, to_thread
from anyio.lowlevel import checkpoint
from fastapi import Request
from pydantic import ValidationError
from python_multipart import MultipartParser
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header

from sibyl.api.schemas.archive_imports import ArchiveCheckOptions
from sibyl_core.migrate.personal_archive_intake import (
    ArchiveIntakeBudget,
    ArchiveIntakeCapacityError,
    ArchiveIntakeError,
    decode_personal_archive_json,
)


@dataclass(frozen=True)
class ArchiveUploadBudget:
    request_bytes: int
    options_bytes: int
    header_bytes: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value <= 0 for value in asdict(self).values()):
            raise ValueError("archive upload budgets must be positive integers")


@dataclass(frozen=True)
class StagedArchiveUpload:
    options: ArchiveCheckOptions
    options_sha256: str
    compressed_sha256: str
    request_bytes: int
    compressed_bytes: int


class _UploadParts:
    def __init__(
        self, spool: BinaryIO, budget: ArchiveUploadBudget, intake: ArchiveIntakeBudget
    ) -> None:
        self.spool, self.budget, self.intake = spool, budget, intake
        self.headers: dict[bytes, bytes] = {}
        self.field = bytearray()
        self.value = bytearray()
        self.header_bytes = 0
        self.part: bytes | None = None
        self.seen: set[bytes] = set()
        self.options = bytearray()
        self.compressed_bytes = 0
        self.digest = hashlib.sha256()
        self.complete = False

    def part_begin(self) -> None:
        self.headers.clear()
        self.field.clear()
        self.value.clear()
        self.header_bytes = 0
        self.part = None

    def _header(self, data: bytes, start: int, end: int, target: bytearray) -> None:
        self.header_bytes += end - start
        if self.header_bytes > self.budget.header_bytes:
            raise ArchiveIntakeCapacityError("archive multipart header-byte budget exceeded")
        target.extend(data[start:end])

    def header_field(self, data: bytes, start: int, end: int) -> None:
        self._header(data, start, end, self.field)

    def header_value(self, data: bytes, start: int, end: int) -> None:
        self._header(data, start, end, self.value)

    def header_end(self) -> None:
        key = bytes(self.field).lower()
        if key in self.headers:
            raise ArchiveIntakeError("archive multipart headers must be unique")
        self.headers[key] = bytes(self.value)
        self.field.clear()
        self.value.clear()

    def headers_finished(self) -> None:
        disposition, options = parse_options_header(self.headers.get(b"content-disposition", b""))
        name = options.get(b"name")
        if disposition != b"form-data" or name not in {b"archive", b"options"}:
            raise ArchiveIntakeError("archive upload accepts archive and options parts only")
        if name in self.seen:
            raise ArchiveIntakeError("archive upload parts must be unique")
        self.seen.add(name)
        self.part = name

    def part_data(self, data: bytes, start: int, end: int) -> None:
        amount = end - start
        if self.part == b"archive":
            self.compressed_bytes += amount
            if self.compressed_bytes > self.intake.compressed_bytes:
                raise ArchiveIntakeCapacityError("archive compressed-byte budget exceeded")
            payload = data[start:end]
            self.digest.update(payload)
            self.spool.write(payload)
        elif self.part == b"options":
            if len(self.options) + amount > self.budget.options_bytes:
                raise ArchiveIntakeCapacityError("archive options-byte budget exceeded")
            self.options.extend(data[start:end])
        else:
            raise ArchiveIntakeError("archive upload data has no supported part")

    def end(self) -> None:
        self.complete = True

    def callbacks(self):
        return {
            "on_part_begin": self.part_begin,
            "on_header_field": self.header_field,
            "on_header_value": self.header_value,
            "on_header_end": self.header_end,
            "on_headers_finished": self.headers_finished,
            "on_part_data": self.part_data,
            "on_end": self.end,
        }


def _open_spool(path: Path) -> BinaryIO:
    return path.open("xb")


def _remove_owned_spool(path: Path) -> None:
    path.unlink(missing_ok=True)


async def _owned_worker_result[T](worker: asyncio.Task[T]) -> tuple[T, bool]:
    """Finish an owned worker before propagating direct task cancellation."""
    cancelled = False
    with CancelScope(shield=True):
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
    return worker.result(), cancelled


async def stage_archive_upload(
    request: Request,
    *,
    spool: Path,
    budget: ArchiveUploadBudget,
    intake_budget: ArchiveIntakeBudget,
) -> StagedArchiveUpload:
    """Consume physical request bytes before multipart or archive allocation.

    The caller owns a private temporary directory and keeps it alive through
    archive parsing. Filenames supplied by the client never select a path.
    """
    content_type = request.headers.get("content-type", "")
    if len(content_type.encode()) > budget.header_bytes:
        raise ArchiveIntakeCapacityError("archive content-type header budget exceeded")
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise ArchiveIntakeError("archive upload has an unsupported content encoding")
    kind, parameters = parse_options_header(content_type)
    boundary = parameters.get(b"boundary")
    if (
        kind != b"multipart/form-data"
        or not isinstance(boundary, bytes)
        or not 1 <= len(boundary) <= 70
        or any(character < 32 or character >= 127 for character in boundary)
    ):
        raise ArchiveIntakeError("archive upload requires a valid multipart boundary")

    created = False
    closed = False
    try:
        destination, cancelled = await _owned_worker_result(
            asyncio.create_task(to_thread.run_sync(_open_spool, spool))
        )
        created = True
        if cancelled:
            raise asyncio.CancelledError
        try:
            await checkpoint()
            parts = _UploadParts(destination, budget, intake_budget)
            parser = MultipartParser(boundary, parts.callbacks())
            request_bytes = 0
            try:
                async for chunk in request.stream():
                    request_bytes += len(chunk)
                    if request_bytes > budget.request_bytes:
                        raise ArchiveIntakeCapacityError(
                            "archive physical request-byte budget exceeded"
                        )
                    # Multipart callbacks write the spool on this worker, keeping
                    # filesystem latency out of the request event loop.
                    consumed, cancelled = await _owned_worker_result(
                        asyncio.create_task(to_thread.run_sync(parser.write, chunk))
                    )
                    if cancelled:
                        raise asyncio.CancelledError
                    await checkpoint()
                    if consumed != len(chunk):
                        raise ArchiveIntakeError("archive multipart request was not fully consumed")
                parser.finalize()
            except MultipartParseError as exc:
                raise ArchiveIntakeError("archive multipart request is malformed") from exc
            if not parts.complete or parts.seen != {b"archive", b"options"}:
                raise ArchiveIntakeError("archive multipart request is incomplete")
            options_bytes = bytes(parts.options)
            try:
                options = ArchiveCheckOptions.model_validate(
                    decode_personal_archive_json(options_bytes, intake_budget)
                )
            except ValidationError as exc:
                raise ArchiveIntakeError(
                    "archive options do not match their typed contract"
                ) from exc
            return StagedArchiveUpload(
                options=options,
                options_sha256=hashlib.sha256(options_bytes).hexdigest(),
                compressed_sha256=parts.digest.hexdigest(),
                request_bytes=request_bytes,
                compressed_bytes=parts.compressed_bytes,
            )
        finally:
            _, cancelled = await _owned_worker_result(
                asyncio.create_task(to_thread.run_sync(destination.close))
            )
            closed = True
            if cancelled:
                raise asyncio.CancelledError
    except BaseException:
        if created:
            if not closed:
                await _owned_worker_result(
                    asyncio.create_task(to_thread.run_sync(destination.close))
                )
            await _owned_worker_result(
                asyncio.create_task(to_thread.run_sync(_remove_owned_spool, spool))
            )
        raise
