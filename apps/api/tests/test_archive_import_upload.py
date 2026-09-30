from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import uuid4

import pytest
from starlette.requests import ClientDisconnect, Request

from sibyl.api.routes.archive_import_upload import (
    ArchiveUploadBudget,
    stage_archive_upload,
)
from sibyl_core.migrate.archive import build_manifest, write_archive
from sibyl_core.migrate.personal_archive_intake import (
    ArchiveIntakeBudget,
    ArchiveIntakeCapacityError,
    ArchiveIntakeError,
    parse_personal_archive,
)


def intake_budget():
    return ArchiveIntakeBudget(
        compressed_bytes=100_000,
        inflated_bytes=1_000_000,
        member_bytes=500_000,
        members=16,
        json_depth=32,
        json_scalar_bytes=100_000,
        json_nodes=50_000,
        parsed_rows=10_000,
        encoded_artifact_bytes=2_000_000,
        encoded_plan_bytes=1_000_000,
        metadata_transaction_bytes=3_000_000,
    )


def options():
    actor = str(uuid4())
    return json.dumps(
        {
            "mappings": {
                "source_private_owner_id": str(uuid4()),
                "quarantine": {"memory_scope": "private", "scope_key": actor},
            },
            "conflict_policy": "additive",
        }
    ).encode()


def multipart(archive, option_bytes, *, extra=b"", ending=True, archive_header=b""):
    boundary = b"synthetic-archive"
    body = (
        b"--" + boundary + b'\r\nContent-Disposition: form-data; name="archive"; '
        b'filename="../../untrusted.tar.gz"\r\n'
        + archive_header
        + b"\r\n"
        + archive
        + b"\r\n--"
        + boundary
        + b'\r\nContent-Disposition: form-data; name="options"\r\n\r\n'
        + option_bytes
    )
    if extra:
        body += b"\r\n--" + boundary + b"\r\n" + extra
    if ending:
        body += b"\r\n--" + boundary + b"--\r\n"
    return body


def request(body, *, chunk=31, disconnect=False, content_type=None):
    messages = [
        {"type": "http.request", "body": body[i : i + chunk], "more_body": True}
        for i in range(0, len(body), chunk)
    ]
    messages.append(
        {"type": "http.disconnect"}
        if disconnect
        else {"type": "http.request", "body": b"", "more_body": False}
    )

    async def receive():
        return messages.pop(0)

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/archive-imports/check",
            "headers": [
                (
                    b"content-type",
                    content_type or b"multipart/form-data; boundary=synthetic-archive",
                )
            ],
        },
        receive,
    )


@pytest.mark.parametrize("chunk", [1, 31, 100_000])
async def test_archive_upload_streams_actual_writer_bytes_and_ignores_filename(tmp_path, chunk):
    source = tmp_path / "source.tgz"
    org = str(uuid4())
    graph = json.dumps(
        {
            "version": "2.0",
            "organization_id": org,
            "entities": [],
            "relationships": [],
            "entity_count": 0,
            "relationship_count": 0,
        }
    ).encode()
    write_archive(
        source,
        manifest=build_manifest(
            organization_id=org, source_store="surreal", files={"graph.json": graph}
        ),
        files={"graph.json": graph},
    )
    archive, encoded_options = source.read_bytes(), options()
    body = multipart(archive, encoded_options)
    spool = tmp_path / "owned.spool"
    staged = await stage_archive_upload(
        request(body, chunk=chunk),
        spool=spool,
        budget=ArchiveUploadBudget(len(body), len(encoded_options), 1000),
        intake_budget=intake_budget(),
    )
    assert spool.read_bytes() == archive
    assert staged.compressed_sha256 == hashlib.sha256(archive).hexdigest()
    assert staged.options_sha256 == hashlib.sha256(encoded_options).hexdigest()
    assert staged.request_bytes == len(body)
    assert staged.compressed_bytes == len(archive)
    assert parse_personal_archive(spool, intake_budget()).origin.organization_id == org
    assert not (tmp_path.parent / "untrusted.tar.gz").exists()


@pytest.mark.parametrize("reason", ["request", "compressed", "options", "headers"])
async def test_archive_upload_enforces_physical_allocation_budgets_and_cleans_spool(
    tmp_path, reason
):
    encoded_options = options()
    body = multipart(b"synthetic-archive", encoded_options)
    budget = ArchiveUploadBudget(100_000, 10_000, 1000)
    intake = intake_budget()
    if reason == "request":
        budget = replace(budget, request_bytes=10)
    elif reason == "compressed":
        intake = replace(intake, compressed_bytes=5)
    elif reason == "options":
        budget = replace(budget, options_bytes=5)
    else:
        budget = replace(budget, header_bytes=20)
    spool = tmp_path / "owned.spool"
    with pytest.raises(ArchiveIntakeCapacityError):
        await stage_archive_upload(request(body), spool=spool, budget=budget, intake_budget=intake)
    assert not spool.exists()


@pytest.mark.parametrize(
    "extra",
    [
        b'Content-Disposition: form-data; name="archive"\r\n\r\nsecond',
        b'Content-Disposition: form-data; name="options"\r\n\r\n{}',
        b'Content-Disposition: form-data; name="unknown"\r\n\r\nforeign',
    ],
)
async def test_archive_upload_rejects_duplicate_or_unknown_parts_before_decode(tmp_path, extra):
    spool = tmp_path / "owned.spool"
    with pytest.raises(ArchiveIntakeError):
        await stage_archive_upload(
            request(multipart(b"archive", options(), extra=extra)),
            spool=spool,
            budget=ArchiveUploadBudget(100_000, 10_000, 1000),
            intake_budget=intake_budget(),
        )
    assert not spool.exists()


@pytest.mark.parametrize("mode", ["truncated", "disconnect", "duplicate-options", "unknown-grant"])
async def test_archive_upload_rejects_incomplete_or_untrusted_options_and_cleans_spool(
    tmp_path, mode
):
    encoded_options = options()
    if mode == "duplicate-options":
        encoded_options = encoded_options[:-1] + b', "conflict_policy":"additive"}'
    elif mode == "unknown-grant":
        encoded_options = encoded_options[:-1] + b', "organization_id":"caller-grant"}'
    body = multipart(b"archive", encoded_options, ending=mode != "truncated")
    spool = tmp_path / "owned.spool"
    with pytest.raises(ClientDisconnect if mode == "disconnect" else ArchiveIntakeError):
        await stage_archive_upload(
            request(body, disconnect=mode == "disconnect"),
            spool=spool,
            budget=ArchiveUploadBudget(100_000, 10_000, 1000),
            intake_budget=intake_budget(),
        )
    assert not spool.exists()


async def test_archive_upload_never_removes_or_overwrites_an_existing_spool(tmp_path):
    spool = tmp_path / "already-owned"
    spool.write_bytes(b"other owner's bytes")
    with pytest.raises(FileExistsError):
        await stage_archive_upload(
            request(multipart(b"archive", options())),
            spool=spool,
            budget=ArchiveUploadBudget(100_000, 10_000, 1000),
            intake_budget=intake_budget(),
        )
    assert spool.read_bytes() == b"other owner's bytes"
