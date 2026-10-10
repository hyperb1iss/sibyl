"""Logical content restores move pending receipts from a volume into S3."""

import io
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from botocore.exceptions import ClientError

from sibyl.persistence import content_archive
from sibyl_core.backends import s3_receipt_store
from sibyl_core.backends.surreal import SurrealContentClient, bootstrap_content_schema
from sibyl_core.config import settings
from sibyl_core.services import content_client
from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.services.validation_stages import run_validation_stage
from tests.test_validation_execution_archive import history as history  # noqa: PLC0414
from tests.test_validation_receipt_archive import completed_result, retain_completed


def _error(status: int, code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code}, "ResponseMetadata": {"HTTPStatusCode": status}}, "S3"
    )


class Bucket:
    """Create-only S3 objects: If-None-Match on an existing key answers 412."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, ContentType=None):  # noqa: N803
        assert IfNoneMatch == "*"
        if Key in self.objects:
            raise _error(412, "PreconditionFailed")
        self.objects[Key] = bytes(Body)

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if Key not in self.objects:
            raise _error(404, "NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)


class Anonymous:
    def get_object(self, **_params):
        raise _error(403, "AccessDenied")


async def test_content_archive_moves_pending_receipt_from_volume_to_bucket(
    history, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "old-volume"))
    _source, execution = history
    await retain_completed(execution, monkeypatch)
    archive = await content_archive.export_content_archive_payload("org")
    assert archive["validation_receipts"]["executions"][0]["status"] == "journal"
    ciphertext = next((tmp_path / "old-volume").glob("*.receipt")).read_bytes()
    (tmp_path / "old-volume").rename(tmp_path / "retired-volume")

    bucket = Bucket()
    monkeypatch.setattr(s3_receipt_store, "s3_clients", lambda region: (bucket, Anonymous()))
    monkeypatch.setattr(settings, "validation_receipt_url", "s3://sibyl-receipts/team")
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "never-used"))
    destination = SurrealContentClient(url="memory://")
    await bootstrap_content_schema(destination)
    close = destination.close
    monkeypatch.setattr(destination, "close", AsyncMock())
    monkeypatch.setattr(content_archive, "build_surreal_content_client", lambda: destination)

    @asynccontextmanager
    async def session():
        yield destination

    monkeypatch.setattr(content_client, "surreal_content_client", session)
    try:
        restored = await content_archive.restore_content_archive_payload(archive, clean=True)
        assert restored.success, restored.errors
        assert bucket.objects == {f"team/{execution.id}.receipt": ciphertext}

        # A second import of the same archive finds the identical receipt in place.
        again = await content_archive.restore_content_archive_payload(archive, clean=True)
        assert again.success, again.errors
        assert bucket.objects == {f"team/{execution.id}.receipt": ciphertext}

        fresh = ValidationExecution(execution.id, "org", "owner")
        row = await fresh.load()
        forbidden = AsyncMock(side_effect=AssertionError("provider dispatch forbidden"))
        result = await run_validation_stage(
            execution=fresh,
            parent_id=row["parent_id"],
            source_ids=row["source_ids"],
            request=json.loads(row["request_json"]),
            policy=row["policy_json"],
            check_current=AsyncMock(),
            run=forbidden,
        )
        assert result["usage"] == completed_result().usage.model_dump(mode="json")
        forbidden.assert_not_awaited()
        assert bucket.objects == {}
        assert not (tmp_path / "never-used").exists()
    finally:
        await close()
