"""An archive written by one process is listed and downloaded through another.

The backup job runs on a worker (Redis coordination) or on whichever API
replica took the job (local coordination); the download request lands on any
replica. These tests play each role in turn against one shared archive store
and one shared record table, with a different local disk per role, so the
only thing the roles share is what a real deployment shares.
"""

from __future__ import annotations

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
from sibyl_core.backends import s3_archive_store

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

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _error(404, "NoSuchKey")
        data = self.objects[Key][0]
        return {"Body": _Body(data), "ContentLength": len(data)}

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
    monkeypatch.setattr(backup_jobs, "_safe_broadcast", AsyncMock())
    monkeypatch.setattr(backup_jobs.settings, "store", "surreal")
    monkeypatch.setattr(backup_jobs.settings, "auth_store", "surreal")
    monkeypatch.setattr(
        backup_jobs,
        "export_auth_archive_payload",
        AsyncMock(return_value={"version": "1.0", "tables": {"users": []}}),
    )
    monkeypatch.setattr(
        backup_jobs,
        "export_content_archive_payload",
        AsyncMock(return_value={"version": "1.0", "tables": {"secret_rows": ["recovery-key"]}}),
    )
    graph = SimpleNamespace(
        success=True,
        backup_data=_GraphPayload(entities=[{"uuid": "e1"}], relationships=[]),
        entity_count=1,
        relationship_count=0,
        message="ok",
    )
    monkeypatch.setattr("sibyl_core.tools.admin.create_backup", AsyncMock(return_value=graph))
    monkeypatch.setattr(backup_routes, "get_backup_record", records.get)
    monkeypatch.setattr(backup_routes, "list_backup_records", records.list)
    return SimpleNamespace(records=records, tmp_path=tmp_path, monkeypatch=monkeypatch)


def _as(deployment, role: str) -> None:
    """Become one process of the deployment: its own empty local disk."""
    deployment.monkeypatch.setattr(
        backup_jobs.settings, "backup_dir", deployment.tmp_path / role / "backups"
    )


async def _write_backup(deployment, org_id) -> str:
    backup_id = f"backup_{org_id.hex[:8]}_20261010_020000_{uuid4().hex[:10]}"
    deployment.records.rows[backup_id] = BackupRecord(organization_id=org_id, backup_id=backup_id)
    result = await backup_jobs.run_backup({}, str(org_id), backup_id=backup_id)
    assert result["success"], result
    return backup_id


async def _download(backup_id: str, org) -> bytes:
    response = await backup_routes.download_backup(backup_id, org=org)
    body = b"".join([chunk async for chunk in response.body_iterator])
    assert response.headers["content-length"] == str(len(body))
    assert response.headers["content-disposition"] == (
        f'attachment; filename="sibyl_{backup_id}.tar.gz"'
    )
    await response.background()
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
