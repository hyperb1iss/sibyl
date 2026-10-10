"""An archive written by one process is listed and downloaded through another.

The backup job runs on a worker (Redis coordination) or on whichever API
replica took the job (local coordination); the download request lands on any
replica. These tests play each role in turn against one shared archive store
and one shared record table, with a different local disk per role, so the
only thing the roles share is what a real deployment shares.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from sibyl.api.routes import backups as backup_routes
from sibyl.jobs import backup as backup_jobs
from sibyl.persistence.backups_common import BackupListResult, BackupRecord
from sibyl_core.backends import archive_store, s3_archive_store
from sibyl_core.backends.archive_store import RangeNotSatisfiable, parse_byte_range

BUCKET = "sibyl-backups"


def _error(status: int, code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "S3"
    )


class _Body(io.BytesIO):
    def iter_chunks(self, size):
        while chunk := self.read(size):
            yield chunk


class Bucket:
    """Create-only S3 objects, multipart uploads invisible until complete."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str], datetime]] = {}
        self.uploads: dict[str, dict] = {}
        # MinIO answers some ranges with 200 and the whole object.
        self.ignore_ranges = False

    def _create(self, key, data, metadata, if_none_match):
        assert if_none_match == "*"
        if key in self.objects:
            raise _error(412, "PreconditionFailed")
        self.objects[key] = (data, dict(metadata or {}), datetime.now(UTC))

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, ContentType=None, Metadata=None):  # noqa: N803
        self._create(Key, bytes(Body), Metadata, IfNoneMatch)

    def create_multipart_upload(self, *, Bucket, Key, ContentType, Metadata):  # noqa: N803
        upload_id = str(uuid4())
        self.uploads[upload_id] = {"parts": {}, "metadata": Metadata}
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body):  # noqa: N803
        self.uploads[UploadId]["parts"][PartNumber] = bytes(Body)
        return {"ETag": f'"{PartNumber}"'}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch):  # noqa: N803
        upload = self.uploads.pop(UploadId)
        data = b"".join(upload["parts"][part["PartNumber"]] for part in MultipartUpload["Parts"])
        self._create(Key, data, upload["metadata"], IfNoneMatch)

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):  # noqa: N803
        self.uploads.pop(UploadId, None)

    def get_object(self, *, Bucket, Key, Range=None):  # noqa: N803
        if Key not in self.objects:
            raise _error(404, "NoSuchKey")
        data, metadata, _ = self.objects[Key]
        answer = {"Metadata": dict(metadata)}
        if Range is not None and not self.ignore_ranges:
            try:
                start, end = parse_byte_range(Range).resolve(len(data))
            except RangeNotSatisfiable:
                raise _error(416, "InvalidRange") from None
            answer["ContentRange"] = f"bytes {start}-{end}/{len(data)}"
            data = data[start : end + 1]
        return {**answer, "Body": _Body(data), "ContentLength": len(data)}

    def head_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _error(404, "404")
        data, metadata, _ = self.objects[Key]
        return {"ContentLength": len(data), "Metadata": dict(metadata)}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)

    def list_objects_v2(self, *, Bucket, Prefix, Delimiter=None, ContinuationToken=None):  # noqa: N803
        keys = [key for key in self.objects if key.startswith(Prefix)]
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[key][0]),
                    "LastModified": self.objects[key][2],
                }
                for key in sorted(keys)
            ],
            "IsTruncated": False,
        }


class Anonymous:
    def get_object(self, **_params):
        raise _error(403, "AccessDenied")


@dataclass
class _GraphPayload:
    version: str
    organization_id: str
    entities: list[dict[str, str]]
    relationships: list[dict[str, str]]


class Records:
    """The backups table every replica reads, keyed by backup id."""

    def __init__(self) -> None:
        self.rows: dict[str, BackupRecord] = {}

    async def update(self, backup_id, **fields):
        record = self.rows[backup_id]
        for name, value in fields.items():
            if value is not None:
                setattr(record, name, value)
        return record

    async def find(self, backup_id):
        return self.rows.get(backup_id)

    async def get(self, org_id, backup_id):
        record = self.rows.get(backup_id)
        if record is None or record.organization_id != org_id:
            raise HTTPException(status_code=404, detail="Backup not found")
        return record

    async def list(self, org_id, *, limit, offset):
        rows = [row for row in self.rows.values() if row.organization_id == org_id]
        return BackupListResult(backups=rows[offset : offset + limit], total=len(rows))


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    records = Records()
    monkeypatch.setattr(backup_jobs, "update_backup_record", records.update)
    monkeypatch.setattr(backup_jobs, "find_backup_record", records.find)
    monkeypatch.setattr(backup_jobs, "list_backup_records", records.list)
    monkeypatch.setattr(backup_jobs, "_safe_broadcast", AsyncMock())
    monkeypatch.setattr(backup_jobs.settings, "store", "surreal")
    monkeypatch.setattr(backup_jobs.settings, "auth_store", "surreal")
    monkeypatch.setattr(backup_jobs.settings, "backup_free_space_reserve_mb", 0)
    exports = SimpleNamespace(
        auth=AsyncMock(
            side_effect=lambda organization_id: {
                "version": "1.0",
                "organization_id": organization_id,
                "tables": {"users": []},
            }
        ),
        content=AsyncMock(
            side_effect=lambda organization_id: {
                "version": "1.0",
                "organization_id": organization_id,
                "tables": {
                    "secret_rows": [
                        {"uuid": "r1", "organization_id": organization_id, "key": "recovery"}
                    ]
                },
            }
        ),
        graph=AsyncMock(
            side_effect=lambda organization_id: SimpleNamespace(
                success=True,
                backup_data=_GraphPayload("2.0", organization_id, [{"uuid": "e1"}], []),
                entity_count=1,
                relationship_count=0,
                message="ok",
            )
        ),
    )
    monkeypatch.setattr(backup_jobs, "export_auth_archive_payload", exports.auth)
    monkeypatch.setattr(backup_jobs, "export_content_archive_payload", exports.content)
    monkeypatch.setattr("sibyl_core.tools.admin.create_backup", exports.graph)
    monkeypatch.setattr(backup_routes, "get_backup_record", records.get)
    monkeypatch.setattr(backup_routes, "list_backup_records", records.list)
    return SimpleNamespace(
        records=records, exports=exports, tmp_path=tmp_path, monkeypatch=monkeypatch
    )


def _exported(deployment) -> int:
    return deployment.exports.auth.await_count + deployment.exports.graph.await_count


def _as(deployment, role: str) -> None:
    """Become one process of the deployment: its own empty local disk."""
    deployment.monkeypatch.setattr(
        backup_jobs.settings, "backup_dir", deployment.tmp_path / role / "backups"
    )


def _new_backup(deployment, org_id) -> str:
    backup_id = f"backup_{org_id.hex[:8]}_20261010_020000_{uuid4().hex[:10]}"
    deployment.records.rows[backup_id] = BackupRecord(organization_id=org_id, backup_id=backup_id)
    return backup_id


async def _write_backup(deployment, org_id) -> str:
    backup_id = _new_backup(deployment, org_id)
    result = await backup_jobs.run_backup({}, str(org_id), backup_id=backup_id)
    assert result["success"], result
    return backup_id


async def _fetch(backup_id: str, org, range_header: str | None = None):
    response = await backup_routes.download_backup(backup_id, org=org, range_header=range_header)
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert response.headers["content-length"] == str(len(body))
    assert response.headers["content-disposition"] == (
        f'attachment; filename="sibyl_{backup_id}.tar.gz"'
    )
    assert response.headers["accept-ranges"] == "bytes"
    await response.background()
    return response, body


async def _download(backup_id: str, org) -> bytes:
    response, body = await _fetch(backup_id, org)
    assert response.status_code == 200
    return body


def _members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {member.name: tar.extractfile(member).read() for member in tar.getmembers()}


@pytest.mark.asyncio
@pytest.mark.parametrize("part_size", [64, 16 * 1024 * 1024])
async def test_s3_archive_from_the_worker_downloads_through_another_replica(
    deployment, monkeypatch, part_size
):
    bucket = Bucket()
    monkeypatch.setattr(s3_archive_store, "s3_clients", lambda region: (bucket, Anonymous()))
    monkeypatch.setattr(s3_archive_store, "PART_SIZE", part_size)
    monkeypatch.setattr(backup_jobs.settings, "backup_archive_url", f"s3://{BUCKET}/prod/archives")
    org = SimpleNamespace(id=uuid4())

    _as(deployment, "worker")
    backup_id = await _write_backup(deployment, org.id)
    key = f"prod/archives/sibyl_{backup_id}.tar.gz"
    assert list(bucket.objects) == [key]
    assert bucket.uploads == {}
    record = deployment.records.rows[backup_id]
    assert record.status == "completed"
    assert record.file_path == f"s3://{BUCKET}/{key}"
    assert not (deployment.tmp_path / "worker").exists(), "S3 mode writes nothing to local disk"

    _as(deployment, "api-replica-2")
    listed = await backup_routes.list_backups(org=org, limit=10, offset=0)
    assert [item.backup_id for item in listed.backups] == [backup_id]
    downloaded = await _download(backup_id, org)
    assert downloaded == bucket.objects[key][0]
    members = _members(downloaded)
    assert set(members) == {"metadata.json", "auth.json", "content.json", "graph.json"}
    assert json.loads(members["metadata.json"])["organization_id"] == str(org.id)

    # Another organization cannot reach the archive through any replica.
    with pytest.raises(HTTPException) as foreign:
        await backup_routes.download_backup(backup_id, org=SimpleNamespace(id=uuid4()))
    assert foreign.value.status_code == 404

    _as(deployment, "api-replica-3")
    monkeypatch.setattr(backup_routes, "delete_backup_record", AsyncMock())
    await backup_routes.delete_backup(backup_id, org=org)
    assert bucket.objects == {}


@pytest.mark.asyncio
async def test_a_shared_directory_serves_every_replica_and_per_pod_disks_do_not(deployment):
    deployment.monkeypatch.setattr(backup_jobs.settings, "backup_archive_url", "")
    org = SimpleNamespace(id=uuid4())

    # The defect: each pod's own directory, as an emptyDir or container
    # layer gives it, cannot serve an archive another pod wrote.
    _as(deployment, "worker")
    backup_id = await _write_backup(deployment, org.id)
    _as(deployment, "api-replica-2")
    with pytest.raises(HTTPException) as missing:
        await backup_routes.download_backup(backup_id, org=org)
    assert missing.value.status_code == 404

    # One volume mounted by every process is a shared store.
    shared = deployment.tmp_path / "claim" / "backups"
    deployment.monkeypatch.setattr(backup_jobs.settings, "backup_dir", shared)
    shared_id = await _write_backup(deployment, org.id)
    assert not any(path.name.startswith(".staging-") for path in shared.iterdir())
    assert await _download(shared_id, org) == (shared / f"sibyl_{shared_id}.tar.gz").read_bytes()


@pytest.fixture(params=["directory", "s3"])
def shared_store(request, deployment, monkeypatch):
    """Point every role at one shared store of each kind; returns its archive bytes."""
    bucket = None
    if request.param == "s3":
        bucket = Bucket()
        monkeypatch.setattr(s3_archive_store, "s3_clients", lambda region: (bucket, Anonymous()))
        monkeypatch.setattr(backup_jobs.settings, "backup_archive_url", f"s3://{BUCKET}/prod")
        _as(deployment, "worker")

        def objects() -> dict[str, bytes]:
            return {key.removeprefix("prod/"): data for key, (data, _, _) in bucket.objects.items()}

        def plant(name: str, data: bytes, metadata: dict[str, str]) -> None:
            bucket.objects[f"prod/{name}"] = (data, metadata, datetime.now(UTC))

        def remove(name: str) -> None:
            bucket.objects.pop(f"prod/{name}")
    else:
        shared = deployment.tmp_path / "claim" / "backups"
        monkeypatch.setattr(backup_jobs.settings, "backup_archive_url", "")
        monkeypatch.setattr(backup_jobs.settings, "backup_dir", shared)

        def objects() -> dict[str, bytes]:
            return {
                path.name: path.read_bytes()
                for path in shared.iterdir()
                if not path.name.startswith(".")
            }

        def plant(name: str, data: bytes, metadata: dict[str, str]) -> None:
            del metadata
            (shared / name).write_bytes(data)

        def remove(name: str) -> None:
            (shared / name).unlink()

    return SimpleNamespace(
        kind=request.param, objects=objects, plant=plant, remove=remove, bucket=bucket
    )


def _sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_a_retried_job_never_rebuilds_a_completed_backup(deployment, shared_store):
    org = SimpleNamespace(id=uuid4())
    backup_id = await _write_backup(deployment, org.id)
    record = deployment.records.rows[backup_id]
    before = (record.status, record.size_bytes, record.completed_at, dict(shared_store.objects()))
    exported = _exported(deployment)

    # arq runs the job again after the worker died between recording the
    # result and acknowledging the job.
    result = await backup_jobs.run_backup({}, str(org.id), backup_id=backup_id)

    assert result["success"] is True
    assert result["already_completed"] is True
    assert result["archive_size_bytes"] == record.size_bytes
    assert _exported(deployment) == exported, "a finished backup is not exported again"
    after = (record.status, record.size_bytes, record.completed_at, dict(shared_store.objects()))
    assert after == before


@pytest.mark.asyncio
async def test_a_retry_adopts_the_archive_an_earlier_attempt_published(deployment, shared_store):
    org = SimpleNamespace(id=uuid4())
    backup_id = await _write_backup(deployment, org.id)
    archive = dict(shared_store.objects())
    # The earlier attempt published, then died before recording completion.
    record = deployment.records.rows[backup_id]
    record.status, record.size_bytes, record.completed_at = "in_progress", 0, None
    exported = _exported(deployment)

    result = await backup_jobs.run_backup({}, str(org.id), backup_id=backup_id)

    assert result["success"] is True, result
    assert _exported(deployment) == exported, "the published archive is kept, not rebuilt"
    assert record.status == "completed"
    assert record.size_bytes == len(archive[f"sibyl_{backup_id}.tar.gz"])
    assert record.entity_count == 1
    assert shared_store.objects() == archive


@pytest.mark.asyncio
async def test_a_retry_that_loses_the_publish_race_adopts_the_winner(
    deployment, shared_store, monkeypatch
):
    org = SimpleNamespace(id=uuid4())
    backup_id = await _write_backup(deployment, org.id)
    name = f"sibyl_{backup_id}.tar.gz"
    earlier = shared_store.objects()[name]
    shared_store.remove(name)
    record = deployment.records.rows[backup_id]
    record.status = "in_progress"
    assemble = backup_jobs._create_backup_archive_async

    async def earlier_attempt_lands_first(*args, **kwargs):
        size = await assemble(*args, **kwargs)
        # The cancelled attempt's upload thread finishes while this one runs.
        shared_store.plant(name, earlier, {"sha256": _sha(earlier)})
        return size

    monkeypatch.setattr(backup_jobs, "_create_backup_archive_async", earlier_attempt_lands_first)

    result = await backup_jobs.run_backup({}, str(org.id), backup_id=backup_id)

    assert result["success"] is True, result
    assert record.status == "completed"
    assert record.size_bytes == len(earlier)
    assert shared_store.objects() == {name: earlier}


@pytest.mark.asyncio
@pytest.mark.parametrize("planted", ["another_org", "corrupt", "wrong_digest"])
async def test_a_foreign_or_damaged_archive_under_the_name_fails_untouched(
    deployment, shared_store, planted
):
    org = SimpleNamespace(id=uuid4())
    other = await _write_backup(deployment, uuid4())
    other_bytes = shared_store.objects()[f"sibyl_{other}.tar.gz"]
    backup_id = _new_backup(deployment, org.id)
    name = f"sibyl_{backup_id}.tar.gz"
    if planted == "another_org":
        data, metadata = other_bytes, {"sha256": _sha(other_bytes)}
    elif planted == "corrupt":
        data, metadata = b"not a tarball", {"sha256": _sha(b"not a tarball")}
    else:
        if shared_store.kind != "s3":
            pytest.skip("only S3 records a digest at publish time")
        data, metadata = other_bytes, {"sha256": "0" * 64}
    shared_store.plant(name, data, metadata)
    exported = _exported(deployment)

    result = await backup_jobs.run_backup({}, str(org.id), backup_id=backup_id)

    assert result["success"] is False
    record = deployment.records.rows[backup_id]
    assert record.status == "failed"
    assert name in record.error
    assert _exported(deployment) == exported, "nothing is exported over an archive in the way"
    assert shared_store.objects()[name] == data, "the archive in the way is never touched"


@pytest.mark.asyncio
async def test_a_full_directory_refuses_the_backup_and_keeps_the_reserve(deployment, monkeypatch):
    shared = deployment.tmp_path / "claim" / "backups"
    monkeypatch.setattr(backup_jobs.settings, "backup_archive_url", "")
    monkeypatch.setattr(backup_jobs.settings, "backup_dir", shared)
    monkeypatch.setattr(backup_jobs.settings, "backup_free_space_reserve_mb", 256)
    org = SimpleNamespace(id=uuid4())
    real = archive_store.shutil.disk_usage
    free = {"bytes": 10 * 1024**3}

    def disk_usage(path):
        return real(path)._replace(free=free["bytes"])

    monkeypatch.setattr(archive_store.shutil, "disk_usage", disk_usage)
    await _write_backup(deployment, org.id)

    # Too little room before the export starts: nothing is exported.
    free["bytes"] = 200 * 1024**2
    refused = _new_backup(deployment, org.id)
    exported = _exported(deployment)
    result = await backup_jobs.run_backup({}, str(org.id), backup_id=refused)
    assert result["success"] is False
    assert "256 MiB kept free" in deployment.records.rows[refused].error
    assert _exported(deployment) == exported

    # Room for the estimate, but the staged export leaves none for the
    # archive: refused before the archive is written, staging removed.
    calls = {"n": 0}

    def shrinking(path):
        calls["n"] += 1
        usage = real(path)
        return usage._replace(free=10 * 1024**3 if calls["n"] == 1 else 256 * 1024**2)

    monkeypatch.setattr(archive_store.shutil, "disk_usage", shrinking)
    late = _new_backup(deployment, org.id)
    result = await backup_jobs.run_backup({}, str(org.id), backup_id=late)
    assert result["success"] is False
    assert "kept free" in deployment.records.rows[late].error
    assert calls["n"] == 2
    assert sorted(path.name for path in shared.iterdir()) == [
        f"sibyl_{backup_id}.tar.gz"
        for backup_id, record in deployment.records.rows.items()
        if record.status == "completed"
    ]


@pytest.mark.asyncio
async def test_downloads_resume_from_a_byte_range(deployment, shared_store):
    org = SimpleNamespace(id=uuid4())
    backup_id = await _write_backup(deployment, org.id)
    archive = shared_store.objects()[f"sibyl_{backup_id}.tar.gz"]
    total = len(archive)

    for header, (start, end) in (
        ("bytes=0-99", (0, 99)),
        (f"bytes={total - 10}-", (total - 10, total - 1)),
        ("bytes=-25", (total - 25, total - 1)),
        (f"bytes=100-{total + 500}", (100, total - 1)),
    ):
        response, body = await _fetch(backup_id, org, header)
        assert response.status_code == 206, header
        assert response.headers["content-range"] == f"bytes {start}-{end}/{total}"
        assert body == archive[start : end + 1]

    # A malformed or multi-range header is ignored and the whole archive sent.
    for ignored in ("bytes=9-2", "bytes=0-1,5-6", "items=0-1"):
        response, body = await _fetch(backup_id, org, ignored)
        assert response.status_code == 200
        assert "content-range" not in response.headers
        assert body == archive

    with pytest.raises(HTTPException) as unsatisfiable:
        await backup_routes.download_backup(backup_id, org=org, range_header=f"bytes={total}-")
    assert unsatisfiable.value.status_code == 416
    assert unsatisfiable.value.headers == {"Content-Range": f"bytes */{total}"}


@pytest.mark.asyncio
async def test_a_failing_archive_directory_answers_503_and_keeps_the_record(
    deployment, monkeypatch
):
    shared = deployment.tmp_path / "claim" / "backups"
    monkeypatch.setattr(backup_jobs.settings, "backup_archive_url", "")
    monkeypatch.setattr(backup_jobs.settings, "backup_dir", shared)
    org = SimpleNamespace(id=uuid4())
    backup_id = await _write_backup(deployment, org.id)
    # The volume goes away: the mount point is no longer a directory.
    blocked = deployment.tmp_path / "unmounted"
    blocked.write_text("not a directory")
    monkeypatch.setattr(backup_jobs.settings, "backup_dir", blocked)
    delete_record = AsyncMock()
    monkeypatch.setattr(backup_routes, "delete_backup_record", delete_record)

    for call in (
        backup_routes.download_backup(backup_id, org=org),
        backup_routes.delete_backup(backup_id, org=org),
    ):
        with pytest.raises(HTTPException) as unavailable:
            await call
        assert unavailable.value.status_code == 503
    delete_record.assert_not_awaited()
    assert backup_id in deployment.records.rows


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", [1, 8], ids=["last_byte", "gzip_trailer"])
async def test_an_archive_with_a_damaged_gzip_end_is_not_adopted(deployment, shared_store, cut):
    from sibyl_core.migrate.archive import load_archive, validate_archive

    org = SimpleNamespace(id=uuid4())
    backup_id = await _write_backup(deployment, org.id)
    name = f"sibyl_{backup_id}.tar.gz"
    damaged = shared_store.objects()[name][:-cut]
    # The restore loader stops at the tar end marker and accepts this, which
    # is why adoption must read the gzip stream to its end itself.
    probe = deployment.tmp_path / f"probe-{cut}.tar.gz"
    probe.write_bytes(damaged)
    assert validate_archive(load_archive(probe)) == []

    shared_store.remove(name)
    shared_store.plant(name, damaged, {"sha256": _sha(damaged)})
    record = deployment.records.rows[backup_id]
    record.status = "in_progress"

    result = await backup_jobs.run_backup({}, str(org.id), backup_id=backup_id)

    assert result["success"] is False
    assert record.status == "failed"
    assert record.error == f"{name} is not a readable backup archive"
    assert shared_store.objects()[name] == damaged, "the damaged archive is never touched"


@pytest.mark.asyncio
async def test_a_range_the_store_ignores_is_answered_with_the_whole_archive(
    deployment, shared_store
):
    if shared_store.bucket is None:
        pytest.skip("a directory store always honours a valid range")
    org = SimpleNamespace(id=uuid4())
    backup_id = await _write_backup(deployment, org.id)
    archive = shared_store.objects()[f"sibyl_{backup_id}.tar.gz"]
    shared_store.bucket.ignore_ranges = True

    response, body = await _fetch(backup_id, org, "bytes=10-19")

    assert response.status_code == 200
    assert "content-range" not in response.headers
    assert body == archive


@pytest.mark.asyncio
async def test_concurrent_backups_on_one_volume_leave_room_for_each_other(deployment, monkeypatch):
    shared = deployment.tmp_path / "claim" / "backups"
    monkeypatch.setattr(backup_jobs.settings, "backup_archive_url", "")
    monkeypatch.setattr(backup_jobs.settings, "backup_dir", shared)
    monkeypatch.setattr(backup_jobs.settings, "backup_free_space_reserve_mb", 1)
    org = SimpleNamespace(id=uuid4())
    # A completed backup gives later ones an estimate to claim.
    await _write_backup(deployment, org.id)
    estimate = deployment.records.rows[next(iter(deployment.records.rows))].size_bytes * 11
    real = archive_store.shutil.disk_usage
    # Room for the reserve and one backup's claim, not two.
    room = 1024**2 + estimate + estimate // 2
    monkeypatch.setattr(
        archive_store.shutil, "disk_usage", lambda path: real(path)._replace(free=room)
    )

    exporting = asyncio.Event()
    finish = asyncio.Event()
    export_auth = deployment.exports.auth.side_effect

    async def slow_auth_export(organization_id):
        exporting.set()
        await finish.wait()
        return export_auth(organization_id)

    monkeypatch.setattr(backup_jobs, "export_auth_archive_payload", slow_auth_export)
    first_id = _new_backup(deployment, org.id)
    first = asyncio.create_task(backup_jobs.run_backup({}, str(org.id), backup_id=first_id))
    await exporting.wait()
    assert [path.name for path in shared.iterdir() if path.name.startswith(".reserve-")] == [
        f".reserve-{first_id}"
    ]

    second_id = _new_backup(deployment, org.id)
    second = await backup_jobs.run_backup({}, str(org.id), backup_id=second_id)
    assert second["success"] is False
    assert "other running backups hold" in deployment.records.rows[second_id].error

    finish.set()
    assert (await first)["success"] is True
    assert not any(path.name.startswith(".reserve-") for path in shared.iterdir())
    # With the first finished and its claim released, the next run fits.
    third = await backup_jobs.run_backup({}, str(org.id), backup_id=_new_backup(deployment, org.id))
    assert third["success"] is True
