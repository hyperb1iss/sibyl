"""Backup archives are published once, atomically, and read back by any process."""

from __future__ import annotations

import errno
import hashlib
import io
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from sibyl_core.backends import archive_store, s3_archive_store
from sibyl_core.backends.archive_store import (
    ArchiveConflict,
    ArchiveStoreUnavailable,
    DirectoryArchiveStore,
    archive_name,
    backup_id_of,
)
from sibyl_core.backends.s3_archive_store import S3ArchiveStore, parse_s3_archive_url
from sibyl_core.backends.s3_client import S3Location

BUCKET = "sibyl-backups"
PREFIX = "prod/archives"
NAME = "sibyl_backup_0b8e5a3c_20261010_020000_a1b2c3d4e5.tar.gz"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _archive(tmp_path: Path, data: bytes, name: str = NAME) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    path = staging / name
    path.write_bytes(data)
    return path


def _read(reader) -> bytes:
    return b"".join(reader.chunks())


# -- names ---------------------------------------------------------------------


def test_archive_names_refuse_anything_path_like():
    assert archive_name("backup_0b8e5a3c_20261010_020000_a1b2c3d4e5") == NAME
    assert backup_id_of(NAME) == "backup_0b8e5a3c_20261010_020000_a1b2c3d4e5"
    for bad in ("", "../etc", "a/b", ".hidden", "backup id", "backup\x00"):
        with pytest.raises(ValueError):
            archive_name(bad)
    for bad in ("sibyl_../x.tar.gz", "other.tar.gz", "sibyl_x.tar", ".partial-x"):
        with pytest.raises(ValueError):
            backup_id_of(bad)


# -- directory store -------------------------------------------------------------


def test_directory_store_publishes_once_and_never_replaces(tmp_path):
    root = tmp_path / "archives"
    store = DirectoryArchiveStore(root)
    store.ready()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700, "archives hold secrets"

    source = _archive(root, b"first archive bytes")
    assert store.put_new(NAME, source, sha256=_sha(b"first archive bytes")) is True
    published = root / NAME
    assert published.read_bytes() == b"first archive bytes"
    assert stat.S_IMODE(published.stat().st_mode) == 0o600

    # A retried publish of the same bytes is "already there", not an error.
    again = _archive(tmp_path, b"first archive bytes")
    assert store.put_new(NAME, again, sha256=_sha(b"first archive bytes")) is False

    rival = _archive(tmp_path, b"different archive bytes")
    with pytest.raises(ArchiveConflict):
        store.put_new(NAME, rival, sha256=_sha(b"different archive bytes"))
    assert published.read_bytes() == b"first archive bytes"


def test_directory_store_copies_across_filesystems_without_exposing_a_partial(
    tmp_path, monkeypatch
):
    root = tmp_path / "archives"
    store = DirectoryArchiveStore(root)
    source = _archive(tmp_path, b"x" * (3 * archive_store.CHUNK_SIZE + 7))
    real_link = os.link
    seen_partials: list[list[str]] = []

    def link(src, dst):
        if Path(src) == source:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        # The copy is complete, hidden, and not yet listed when it is published.
        seen_partials.append(sorted(p.name for p in root.iterdir()))
        assert store.archives() == []
        return real_link(src, dst)

    monkeypatch.setattr(os, "link", link)
    assert store.put_new(NAME, source, sha256=_sha(source.read_bytes())) is True
    assert seen_partials and all(name.startswith(".partial-") for name in seen_partials[0])
    assert sorted(p.name for p in root.iterdir()) == [NAME]
    assert (root / NAME).read_bytes() == source.read_bytes()


def test_directory_store_lists_only_published_archives(tmp_path):
    root = tmp_path / "archives"
    store = DirectoryArchiveStore(root)
    store.ready()
    (root / ".staging-abc").mkdir()
    (root / ".staging-abc" / NAME).write_bytes(b"half")
    (root / ".partial-123").write_bytes(b"half")
    (root / "notes.txt").write_text("not an archive")
    (root / "sibyl_owned_bundle.tar.gz").write_bytes(b"fixture names are not swept")
    (root / "sibyl_backup_link.tar.gz").symlink_to(root / "notes.txt")
    store.put_new(NAME, _archive(tmp_path, b"done"), sha256=_sha(b"done"))

    listed = store.archives()
    assert [item.name for item in listed] == [NAME]
    assert listed[0].size == 4
    assert listed[0].modified.tzinfo is not None


def test_directory_store_streams_and_deletes(tmp_path):
    store = DirectoryArchiveStore(tmp_path / "archives")
    data = os.urandom(archive_store.CHUNK_SIZE * 2 + 11)
    store.put_new(NAME, _archive(tmp_path, data), sha256=_sha(data))

    reader = store.open(NAME)
    assert reader is not None
    assert reader.size == len(data)
    chunks = list(reader.chunks())
    assert len(chunks) == 3
    assert b"".join(chunks) == data

    store.delete(NAME)
    store.delete(NAME)
    assert store.open(NAME) is None
    with pytest.raises(ValueError):
        store.open("../../etc/passwd")


def test_two_processes_on_one_directory_share_archives(tmp_path):
    shared = tmp_path / "shared-volume"
    writer = DirectoryArchiveStore(shared)
    reader = DirectoryArchiveStore(shared)
    writer.put_new(NAME, _archive(tmp_path, b"from the worker"), sha256=_sha(b"from the worker"))
    assert [item.name for item in reader.archives()] == [NAME]
    opened = reader.open(NAME)
    assert opened is not None
    assert _read(opened) == b"from the worker"


# -- S3 store --------------------------------------------------------------------


def _error(status: int, code: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class _Body:
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)
        self.closed = False

    def iter_chunks(self, chunk_size):
        while chunk := self._stream.read(chunk_size):
            yield chunk

    def read(self):
        return self._stream.read()

    def close(self):
        self.closed = True


class FakeS3:
    """S3 with create-only conditional writes and invisible multipart uploads."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str], datetime]] = {}
        self.uploads: dict[str, dict] = {}
        self.aborted: list[str] = []
        self.calls: list[str] = []
        self.enforce_conditional = True
        self.conflicts = 0
        self.fail: dict[str, Exception] = {}
        self.before_complete = None
        self.page_size = 1000
        self.bodies: list[_Body] = []
        self.anonymous = FakeAnonymous(self)

    def _record(self, operation: str) -> None:
        self.calls.append(operation)
        if operation in self.fail:
            raise self.fail[operation]

    def _create(self, key: str, data: bytes, metadata: dict[str, str], if_none_match, op: str):
        if self.conflicts:
            self.conflicts -= 1
            raise _error(409, "ConditionalRequestConflict", op)
        if if_none_match == "*" and self.enforce_conditional and key in self.objects:
            raise _error(412, "PreconditionFailed", op)
        self.objects[key] = (data, dict(metadata), datetime.now(UTC))

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, ContentType=None, Metadata=None):
        assert Bucket == BUCKET
        self._record("put_object")
        self._create(Key, bytes(Body), Metadata or {}, IfNoneMatch, "PutObject")
        return {"ETag": '"etag"'}

    def create_multipart_upload(self, *, Bucket, Key, ContentType=None, Metadata=None):
        self._record("create_multipart_upload")
        upload_id = f"upload-{len(self.uploads)}"
        self.uploads[upload_id] = {"key": Key, "parts": {}, "metadata": Metadata or {}}
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body):
        self._record("upload_part")
        self.uploads[UploadId]["parts"][PartNumber] = bytes(Body)
        return {"ETag": f'"part-{PartNumber}"'}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch):
        self._record("complete_multipart_upload")
        if self.before_complete is not None:
            hook, self.before_complete = self.before_complete, None
            hook(Key)
        upload = self.uploads[UploadId]
        numbers = [part["PartNumber"] for part in MultipartUpload["Parts"]]
        assert numbers == sorted(upload["parts"])
        data = b"".join(upload["parts"][number] for number in numbers)
        self._create(Key, data, upload["metadata"], IfNoneMatch, "CompleteMultipartUpload")
        del self.uploads[UploadId]
        return {"ETag": '"multipart-etag"'}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self._record("abort_multipart_upload")
        self.aborted.append(UploadId)
        self.uploads.pop(UploadId, None)
        return {}

    def head_object(self, *, Bucket, Key):
        self._record("head_object")
        if Key not in self.objects:
            raise _error(404, "404", "HeadObject")
        data, metadata, _ = self.objects[Key]
        return {"ContentLength": len(data), "Metadata": metadata}

    def get_object(self, *, Bucket, Key):
        self._record("get_object")
        if Key not in self.objects:
            raise _error(404, "NoSuchKey", "GetObject")
        body = _Body(self.objects[Key][0])
        self.bodies.append(body)
        return {"Body": body, "ContentLength": len(self.objects[Key][0])}

    def delete_object(self, *, Bucket, Key):
        self._record("delete_object")
        self.objects.pop(Key, None)
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, Delimiter=None, ContinuationToken=None):
        self._record("list_objects_v2")
        keys = sorted(key for key in self.objects if key.startswith(Prefix))
        if Delimiter:
            keys = [key for key in keys if Delimiter not in key[len(Prefix) :]]
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        more = start + self.page_size < len(keys)
        result = {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(self.objects[key][0]),
                    "LastModified": self.objects[key][2],
                }
                for key in page
            ],
            "IsTruncated": more,
        }
        if more:
            result["NextContinuationToken"] = str(start + self.page_size)
        return result


class FakeAnonymous:
    def __init__(self, signed: FakeS3) -> None:
        self.signed = signed
        self.public = False

    def get_object(self, *, Bucket, Key):
        if not self.public:
            raise _error(403, "AccessDenied", "GetObject")
        return self.signed.get_object(Bucket=Bucket, Key=Key)


@pytest.fixture
def bucket(monkeypatch):
    fake = FakeS3()
    regions: list[str | None] = []

    def clients(region):
        regions.append(region)
        return fake, fake.anonymous

    monkeypatch.setattr(s3_archive_store, "s3_clients", clients)
    yield fake
    assert set(regions) <= {"us-west-2"}


def _store() -> S3ArchiveStore:
    return S3ArchiveStore(parse_s3_archive_url(f"s3://{BUCKET}/{PREFIX}/?region=us-west-2"))


KEY = f"{PREFIX}/{NAME}"


def test_s3_single_put_is_create_only_and_resolves_412_by_digest(bucket, tmp_path):
    store = _store()
    assert store.staging_dir() is None
    assert store.location(NAME) == f"s3://{BUCKET}/{KEY}"
    source = _archive(tmp_path, b"small archive")
    assert store.put_new(NAME, source, sha256=_sha(b"small archive")) is True
    data, metadata, _ = bucket.objects[KEY]
    assert data == b"small archive"
    assert metadata == {"sha256": _sha(b"small archive")}

    # A retried write that already landed reads as "already there".
    assert store.put_new(NAME, source, sha256=_sha(b"small archive")) is False
    rival = _archive(tmp_path, b"other archive")
    with pytest.raises(ArchiveConflict):
        store.put_new(NAME, rival, sha256=_sha(b"other archive"))
    assert bucket.objects[KEY][0] == b"small archive", "a 412 must leave the first bytes"


def test_s3_multipart_upload_is_invisible_until_complete(bucket, tmp_path, monkeypatch):
    monkeypatch.setattr(s3_archive_store, "PART_SIZE", 10)
    store = _store()
    data = bytes(range(256)) * 2 + b"tail"
    source = _archive(tmp_path, data)
    observed = {}

    def look(_key):
        observed["listed"] = store.archives()
        observed["opened"] = store.open(NAME)

    bucket.before_complete = look
    assert store.put_new(NAME, source, sha256=_sha(data)) is True
    assert observed == {"listed": [], "opened": None}, "a half-written archive must not show"
    assert bucket.calls.count("upload_part") == (len(data) + 9) // 10
    assert bucket.objects[KEY][0] == data
    assert bucket.objects[KEY][1] == {"sha256": _sha(data)}
    assert bucket.uploads == {}


@pytest.mark.parametrize("same", [True, False])
def test_s3_multipart_loses_to_a_rival_without_replacing_it(bucket, tmp_path, monkeypatch, same):
    monkeypatch.setattr(s3_archive_store, "PART_SIZE", 8)
    store = _store()
    data = b"ours: a multipart archive"
    theirs = data if same else b"theirs: a different archive"

    def rival_lands_first(key):
        bucket.objects[key] = (theirs, {"sha256": _sha(theirs)}, datetime.now(UTC))

    bucket.before_complete = rival_lands_first
    source = _archive(tmp_path, data)
    if same:
        assert store.put_new(NAME, source, sha256=_sha(data)) is False
    else:
        with pytest.raises(ArchiveConflict):
            store.put_new(NAME, source, sha256=_sha(data))
    assert bucket.objects[KEY][0] == theirs
    assert bucket.aborted == ["upload-0"], "the losing upload must be aborted"
    assert bucket.uploads == {}


def test_s3_failed_part_aborts_the_upload_and_publishes_nothing(bucket, tmp_path, monkeypatch):
    monkeypatch.setattr(s3_archive_store, "PART_SIZE", 8)
    bucket.fail["upload_part"] = _error(403, "AccessDenied", "UploadPart")
    store = _store()
    with pytest.raises(ArchiveStoreUnavailable, match=r"AccessDenied \(403\)"):
        store.put_new(NAME, _archive(tmp_path, b"x" * 40), sha256=_sha(b"x" * 40))
    assert bucket.aborted == ["upload-0"]
    assert bucket.objects == {}
    assert store.archives() == []


def test_s3_dead_endpoint_is_not_waited_out_again_by_an_abort(bucket, tmp_path, monkeypatch):
    monkeypatch.setattr(s3_archive_store, "PART_SIZE", 8)
    bucket.fail["upload_part"] = EndpointConnectionError(endpoint_url="https://s3.invalid")
    with pytest.raises(ArchiveStoreUnavailable, match="EndpointConnectionError"):
        _store().put_new(NAME, _archive(tmp_path, b"y" * 40), sha256=_sha(b"y" * 40))
    assert "abort_multipart_upload" not in bucket.calls


@pytest.mark.parametrize("part_size", [8, 1024])
def test_s3_conditional_conflict_is_retried_then_surfaced(bucket, tmp_path, monkeypatch, part_size):
    monkeypatch.setattr(s3_archive_store, "PART_SIZE", part_size)
    store = _store()
    data = b"z" * 40
    bucket.conflicts = 2
    assert store.put_new(NAME, _archive(tmp_path, data), sha256=_sha(data)) is True
    bucket.objects.clear()
    bucket.conflicts = 3
    with pytest.raises(ArchiveStoreUnavailable, match="ConditionalRequestConflict"):
        store.put_new(NAME, _archive(tmp_path, data), sha256=_sha(data))
    assert bucket.objects == {}


def test_s3_streams_downloads_and_tells_absence_from_outage(bucket, tmp_path):
    store = _store()
    data = os.urandom(s3_archive_store.CHUNK_SIZE + 5)
    store.put_new(NAME, _archive(tmp_path, data), sha256=_sha(data))
    reader = store.open(NAME)
    assert reader is not None
    assert reader.size == len(data)
    chunks = list(reader.chunks())
    assert [len(chunk) for chunk in chunks] == [s3_archive_store.CHUNK_SIZE, 5]
    assert b"".join(chunks) == data
    assert bucket.bodies[-1].closed

    assert store.open("sibyl_backup_missing.tar.gz") is None
    bucket.fail["get_object"] = _error(404, "NoSuchBucket", "GetObject")
    with pytest.raises(ArchiveStoreUnavailable, match="NoSuchBucket"):
        store.open(NAME)


def test_s3_list_pages_and_skips_probes_partials_and_nested_keys(bucket, tmp_path):
    bucket.page_size = 2
    store = _store()
    now = datetime.now(UTC)
    names = [f"sibyl_backup_org_{index}.tar.gz" for index in range(5)]
    for name in names:
        bucket.objects[f"{PREFIX}/{name}"] = (b"a", {}, now)
    bucket.objects[f"{PREFIX}/.probe-abc"] = (b"p", {}, now)
    bucket.objects[f"{PREFIX}/nested/{NAME}"] = (b"n", {}, now)
    bucket.objects[f"{PREFIX}/sibyl_owned_bundle.tar.gz"] = (b"f", {}, now)
    bucket.objects[f"elsewhere/{NAME}"] = (b"e", {}, now)
    listed = store.archives()
    assert sorted(item.name for item in listed) == names
    # Seven keys sit directly under the prefix (the nested one is folded away).
    assert bucket.calls.count("list_objects_v2") == 4


def test_s3_delete_is_idempotent_and_surfaces_refusal(bucket, tmp_path):
    store = _store()
    store.put_new(NAME, _archive(tmp_path, b"gone"), sha256=_sha(b"gone"))
    store.delete(NAME)
    store.delete(NAME)
    assert bucket.objects == {}
    bucket.fail["delete_object"] = _error(403, "AccessDenied", "DeleteObject")
    with pytest.raises(ArchiveStoreUnavailable, match="refused DeleteObject: AccessDenied"):
        store.delete(NAME)


def test_s3_ready_proves_create_only_and_privacy_then_cleans_up(bucket):
    store = _store()
    store.ready()
    assert bucket.objects == {}
    assert bucket.calls.count("put_object") == 2
    assert bucket.calls[-1] == "delete_object"

    bucket.calls.clear()
    bucket.enforce_conditional = False
    with pytest.raises(ArchiveStoreUnavailable, match="ignored If-None-Match"):
        store.ready()
    assert bucket.objects == {}, "a failed check still removes its probe"

    bucket.enforce_conditional = True
    bucket.anonymous.public = True
    with pytest.raises(ArchiveStoreUnavailable, match="serves objects to unsigned requests"):
        store.ready()
    assert bucket.objects == {}


def test_archive_url_parsing_and_prefix_overlap():
    location = parse_s3_archive_url("s3://sibyl-backups/prod/archives/?region=eu-central-1")
    assert location == S3Location("sibyl-backups", "prod/archives", "eu-central-1")
    with pytest.raises(ValueError, match="Backup archive URL must use the s3:// scheme"):
        parse_s3_archive_url("/var/lib/sibyl-backups")
    with pytest.raises(ValueError, match="Backup archive URL"):
        parse_s3_archive_url("s3://bucket/a/../b")

    def overlaps(left, right):
        return parse_s3_archive_url(left).overlaps(parse_s3_archive_url(right))

    assert overlaps("s3://bkt/sibyl", "s3://bkt/sibyl/archives")
    assert overlaps("s3://bkt", "s3://bkt/receipts")
    assert overlaps("s3://bkt/x", "s3://bkt/x/")
    assert not overlaps("s3://bkt/sibyl/receipts", "s3://bkt/sibyl/archives")
    assert not overlaps("s3://bkt/sib", "s3://bkt/sibyl")
    assert not overlaps("s3://other/sibyl", "s3://bkt/sibyl")


def test_real_client_accepts_every_parameter_the_store_sends(monkeypatch, tmp_path):
    """botocore's own S3 model must accept the conditional and multipart calls."""
    import boto3
    from botocore.stub import Stubber

    session = boto3.session.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-west-2"
    )
    client = session.client("s3")
    anonymous = session.client("s3")
    monkeypatch.setattr(s3_archive_store, "s3_clients", lambda region: (client, anonymous))
    monkeypatch.setattr(s3_archive_store, "PART_SIZE", 5)
    store = S3ArchiveStore(parse_s3_archive_url("s3://bucket/prefix"))
    key = f"prefix/{NAME}"
    small = _archive(tmp_path, b"tiny", name="small.tar.gz")
    large = _archive(tmp_path, b"0123456789AB", name="large.tar.gz")
    digest = _sha(b"0123456789AB")
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"etag"'},
            {
                "Bucket": "bucket",
                "Key": key,
                "Body": b"tiny",
                "IfNoneMatch": "*",
                "ContentType": "application/gzip",
                "Metadata": {"sha256": _sha(b"tiny")},
            },
        )
        stub.add_response(
            "create_multipart_upload",
            {"UploadId": "u1"},
            {
                "Bucket": "bucket",
                "Key": key,
                "ContentType": "application/gzip",
                "Metadata": {"sha256": digest},
            },
        )
        for number, body in ((1, b"01234"), (2, b"56789"), (3, b"AB")):
            stub.add_response(
                "upload_part",
                {"ETag": f'"p{number}"'},
                {
                    "Bucket": "bucket",
                    "Key": key,
                    "UploadId": "u1",
                    "PartNumber": number,
                    "Body": body,
                },
            )
        stub.add_client_error(
            "complete_multipart_upload",
            "PreconditionFailed",
            http_status_code=412,
            expected_params={
                "Bucket": "bucket",
                "Key": key,
                "UploadId": "u1",
                "MultipartUpload": {
                    "Parts": [
                        {"ETag": '"p1"', "PartNumber": 1},
                        {"ETag": '"p2"', "PartNumber": 2},
                        {"ETag": '"p3"', "PartNumber": 3},
                    ]
                },
                "IfNoneMatch": "*",
            },
        )
        stub.add_response(
            "abort_multipart_upload",
            {},
            {"Bucket": "bucket", "Key": key, "UploadId": "u1"},
        )
        stub.add_response(
            "head_object",
            {"ContentLength": 12, "Metadata": {"sha256": digest}},
            {"Bucket": "bucket", "Key": key},
        )
        stub.add_response(
            "list_objects_v2",
            {
                "Contents": [
                    {
                        "Key": key,
                        "Size": 12,
                        "LastModified": datetime(2026, 10, 10, tzinfo=UTC),
                    }
                ],
                "IsTruncated": False,
            },
            {"Bucket": "bucket", "Prefix": "prefix/", "Delimiter": "/"},
        )
        stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
        stub.add_client_error("get_object", "AccessDenied", http_status_code=403)
        stub.add_response("delete_object", {}, {"Bucket": "bucket", "Key": key})
        assert store.put_new(NAME, small, sha256=_sha(b"tiny")) is True
        assert store.put_new(NAME, large, sha256=digest) is False
        listed = store.archives()
        assert [(item.name, item.size) for item in listed] == [(NAME, 12)]
        assert store.open(NAME) is None
        with pytest.raises(ArchiveStoreUnavailable, match=r"AccessDenied \(403\)"):
            store.open(NAME)
        store.delete(NAME)
        stub.assert_no_pending_responses()


def test_s3_listing_compares_against_aware_cutoffs(bucket):
    store = _store()
    old = datetime.now(UTC) - timedelta(days=40)
    bucket.objects[KEY] = (b"old", {}, old.replace(tzinfo=None))
    (listed,) = store.archives()
    assert listed.modified.tzinfo is not None
    assert listed.modified < datetime.now(UTC) - timedelta(days=30)
