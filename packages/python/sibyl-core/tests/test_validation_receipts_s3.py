"""S3 receipts keep the directory store's guarantees without a shared volume."""

import io
import json
import threading
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError
from cryptography.fernet import Fernet, InvalidToken

from sibyl_core.backends import s3_receipt_store as s3_store
from sibyl_core.backends.s3_receipt_store import (
    ReceiptStoreUnavailable,
    S3ReceiptLocation,
    S3ReceiptStore,
    parse_s3_receipt_url,
)
from sibyl_core.config import settings
from sibyl_core.migrate import validation_receipt_archive
from sibyl_core.services import validation_execution as execution_module
from sibyl_core.services import validation_receipts as receipts
from sibyl_core.services.validation_execution import ValidationExecution
from sibyl_core.services.validation_stages import run_validation_stage
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.memory_validation import run_memory_validation
from sibyl_core.tasks.procedure_review import review_digest
from tests.test_eval_publication import content_store as content_store
from tests.test_memory_validation import candidate as candidate
from tests.test_memory_validation import citations as citations
from tests.test_memory_validation import extractor
from tests.test_memory_validation import prepared as prepared
from tests.test_memory_validation import sources as sources

BUCKET = "sibyl-receipts"
PREFIX = "prod/receipts"


def _error(status: int, code: str, operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        operation,
    )


class FakeS3:
    """In-process S3 with create-only PutObject semantics (If-None-Match: *)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts: list[tuple[str, str | None]] = []
        self.enforce_conditional = True
        self.list_bucket = True
        self.versioned = False
        self.delete_markers: set[tuple[str, str]] = set()
        # The next create is committed but answered 412, as a botocore retry
        # of a PutObject that already landed would see it.
        self.land_then_refuse = False
        self.conflicts = 0
        self.refuse: dict[str, ClientError] = {}
        self.before_put: Callable[[str], None] | None = None
        self.before_get: Callable[[str], None] | None = None
        self.page_size = 1000
        self.listings = 0
        self.gets: list[str] = []
        self.deletes: list[str] = []
        self.anonymous = FakeAnonymous(self)

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch=None, ContentType=None):
        self.puts.append((Key, IfNoneMatch))
        if "put_object" in self.refuse:
            raise self.refuse["put_object"]
        if self.before_put is not None:
            hook, self.before_put = self.before_put, None
            hook(Key)
        if self.conflicts:
            self.conflicts -= 1
            raise _error(409, "ConditionalRequestConflict", "PutObject")
        if IfNoneMatch == "*" and self.enforce_conditional and (Bucket, Key) in self.objects:
            raise _error(412, "PreconditionFailed", "PutObject")
        self.objects[(Bucket, Key)] = bytes(Body)
        self.delete_markers.discard((Bucket, Key))
        if self.land_then_refuse:
            self.land_then_refuse = False
            raise _error(412, "PreconditionFailed", "PutObject")
        return {"ETag": '"etag"'}

    def get_object(self, *, Bucket, Key):
        self.gets.append(Key)
        if "get_object" in self.refuse:
            raise self.refuse["get_object"]
        if self.before_get is not None:
            self.before_get(Key)
        if (Bucket, Key) not in self.objects:
            # Without s3:ListBucket, S3 answers a missing key with 403, not 404,
            # unless a delete marker is the key's current version.
            if not self.list_bucket and (Bucket, Key) not in self.delete_markers:
                raise _error(403, "AccessDenied", "GetObject")
            raise _error(404, "NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.deletes.append(Key)
        if "delete_object" in self.refuse:
            raise self.refuse["delete_object"]
        self.objects.pop((Bucket, Key), None)
        if self.versioned:
            self.delete_markers.add((Bucket, Key))
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        self.listings += 1
        keys = sorted(key for bucket, key in self.objects if bucket == Bucket)
        keys = [key for key in keys if key.startswith(Prefix)]
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        more = start + self.page_size < len(keys)
        result = {"Contents": [{"Key": key} for key in page], "IsTruncated": more}
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
def bucket(tmp_path, monkeypatch):
    signed = FakeS3()
    monkeypatch.setattr(
        settings, "validation_receipt_url", f"s3://{BUCKET}/{PREFIX}/?region=us-west-2"
    )
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "unused-volume"))
    regions = []

    def clients(region):
        regions.append(region)
        return signed, signed.anonymous

    monkeypatch.setattr(s3_store, "s3_clients", clients)
    yield signed
    assert set(regions) <= {"us-west-2"}
    assert not (tmp_path / "unused-volume").exists(), "S3 mode must not touch the directory"


def _key(request: str) -> tuple[str, str]:
    return BUCKET, f"{PREFIX}/{review_digest(json.loads(request))}.receipt"


def test_receipt_is_encrypted_written_once_and_never_overwritten(bucket):
    request = canonical({"org": "org", "principal": "owner"})
    key = Fernet.generate_key().decode()
    receipts.ready()
    assert bucket.objects == {}, "the readiness probe must clean up after itself"
    receipts.retain(request, key, {"private": "known completed evidence"})
    stored = bucket.objects[_key(request)]
    assert list(bucket.objects) == [_key(request)]
    assert b"known completed evidence" not in stored
    assert all(condition == "*" for _, condition in bucket.puts)
    assert receipts.read(request, key) == {"private": "known completed evidence"}

    puts = len(bucket.puts)
    receipts.retain(request, key, {"private": "known completed evidence"})
    assert len(bucket.puts) == puts, "an existing identical receipt needs no write"
    receipts.restore(request, key, stored)
    assert bucket.puts[-1] == (_key(request)[1], "*")
    assert bucket.objects[_key(request)] == stored, "a 412 must leave the first bytes in place"
    with pytest.raises(ValueError, match="differs"):
        receipts.retain(request, key, {"private": "different"})
    assert bucket.objects[_key(request)] == stored

    bucket.objects[_key(request)] = stored[:20]
    with pytest.raises(InvalidToken):
        receipts.read(request, key)


@pytest.mark.parametrize("same", [True, False])
def test_concurrent_writer_wins_and_is_never_replaced(bucket, same):
    request = canonical({"org": "org", "principal": "owner"})
    key = Fernet.generate_key().decode()
    theirs = {"private": "their result" if not same else "ours"}
    rival = Fernet(key.encode()).encrypt(canonical({"request": request, "result": theirs}).encode())

    def rival_lands_first(object_key):
        bucket.objects[(BUCKET, object_key)] = rival

    bucket.before_put = rival_lands_first
    if same:
        receipts.retain(request, key, {"private": "ours"})
    else:
        with pytest.raises(ValueError, match="concurrent result differs"):
            receipts.retain(request, key, {"private": "ours"})
    assert bucket.objects[_key(request)] == rival


def test_read_authenticates_against_the_exact_request(bucket):
    request = canonical({"org": "org", "principal": "owner"})
    other = canonical({"org": "org", "principal": "someone-else"})
    key = Fernet.generate_key().decode()
    receipts.retain(request, key, {"private": "owner result"})
    # A receipt copied under another request's name still names its own request.
    bucket.objects[_key(other)] = bucket.objects[_key(request)]
    with pytest.raises(ValueError, match="request or result differs"):
        receipts.read(other, key)
    with pytest.raises(InvalidToken):
        receipts.read(request, Fernet.generate_key().decode())
    assert receipts.read(canonical({"org": "org", "principal": "nobody"}), key) is None


def test_restore_publishes_archive_bytes_without_replacing_history(bucket):
    request = canonical({"org": "org", "principal": "owner"})
    key = Fernet.generate_key().decode()
    envelope = canonical({"request": request, "result": {"usage": {"requests": 1}}}).encode()
    archived = Fernet(key.encode()).encrypt(envelope)
    validation_receipt_archive.publish([(request, key, archived)])
    assert receipts.capture(request) == archived
    validation_receipt_archive.publish([(request, key, archived)])
    assert receipts.capture(request) == archived

    # Same plaintext, fresh Fernet token: different bytes are a history conflict.
    conflicting = Fernet(key.encode()).encrypt(envelope)
    with pytest.raises(ValueError, match="conflicts with local history"):
        validation_receipt_archive.publish([(request, key, conflicting)])
    with pytest.raises(ValueError, match="conflicts with local history"):
        receipts.restore(request, key, conflicting)
    assert receipts.capture(request) == archived

    forged = Fernet(Fernet.generate_key()).encrypt(envelope)
    other = canonical({"org": "org", "principal": "fresh"})
    with pytest.raises(InvalidToken):
        receipts.restore(other, key, forged)
    assert receipts.capture(other) is None


def test_discard_removes_only_the_named_receipt(bucket):
    first = canonical({"org": "org", "principal": "first"})
    second = canonical({"org": "org", "principal": "second"})
    key = Fernet.generate_key().decode()
    receipts.retain(first, key, {"n": 1})
    receipts.retain(second, key, {"n": 2})
    receipts.discard(first)
    receipts.discard(first)
    assert receipts.capture(first) is None
    assert receipts.read(second, key) == {"n": 2}


def test_capture_many_lists_once_and_reads_only_present_receipts_concurrently(bucket):
    key = Fernet.generate_key().decode()
    requests = [canonical({"org": "org", "principal": f"p{n}"}) for n in range(40)]
    for request in requests[:12]:
        receipts.retain(request, key, {"request": request})
    bucket.objects[(BUCKET, f"{PREFIX}/.probe-leftover")] = b"probe"
    bucket.objects[(BUCKET, "elsewhere/unrelated.receipt")] = b"other"
    bucket.page_size = 5
    bucket.listings = 0
    bucket.gets.clear()
    # The first eight reads must be in flight together, or the barrier breaks.
    together, arrivals, lock = threading.Barrier(8, timeout=10), [], threading.Lock()

    def rendezvous(object_key):
        with lock:
            arrivals.append(object_key)
            first = len(arrivals) <= 8
        if first:
            together.wait()

    bucket.before_get = rendezvous

    found = receipts.capture_many(requests)

    assert bucket.listings == 3, "13 keys under the prefix in pages of five"
    assert sorted(bucket.gets) == sorted(_key(request)[1] for request in requests[:12])
    assert not together.broken
    assert set(found) == set(requests)
    for request in requests[:12]:
        assert receipts.decode(request, key, found[request]) == {"request": request}
    assert all(found[request] is None for request in requests[12:])


def test_capture_many_treats_a_receipt_removed_after_listing_as_absent(bucket):
    key = Fernet.generate_key().decode()
    kept, removed = (canonical({"org": "org", "principal": name}) for name in ("kept", "gone"))
    receipts.retain(kept, key, {"n": 1})
    receipts.retain(removed, key, {"n": 2})

    def discard_after_listing(object_key):
        if object_key == _key(removed)[1]:
            bucket.objects.pop(_key(removed), None)

    bucket.before_get = discard_after_listing
    found = receipts.capture_many([kept, removed])
    assert found[removed] is None
    assert receipts.decode(kept, key, found[kept]) == {"n": 1}
    bucket.refuse["get_object"] = _error(503, "SlowDown", "GetObject")
    with pytest.raises(ReceiptStoreUnavailable, match="SlowDown"):
        receipts.capture_many([kept])


def test_capture_many_on_the_directory_keeps_per_file_privacy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "validation_receipt_url", "")
    monkeypatch.setattr(settings, "validation_receipt_dir", str(tmp_path / "receipts"))
    key = Fernet.generate_key().decode()
    present, absent = (canonical({"org": "org", "principal": name}) for name in ("a", "b"))
    receipts.retain(present, key, {"n": 1})
    found = receipts.capture_many([present, absent])
    assert found == {present: receipts.capture(present), absent: None}
    next((tmp_path / "receipts").glob("*.receipt")).chmod(0o644)
    with pytest.raises(ValueError, match="not private"):
        receipts.capture_many([present])


def test_conditional_conflict_is_retried_then_surfaced(bucket):
    request = canonical({"org": "org", "principal": "owner"})
    key = Fernet.generate_key().decode()
    bucket.conflicts = 1
    receipts.retain(request, key, {"n": 1})
    assert receipts.read(request, key) == {"n": 1}
    other = canonical({"org": "org", "principal": "other"})
    bucket.conflicts = 3
    with pytest.raises(ReceiptStoreUnavailable, match="ConditionalRequestConflict"):
        receipts.retain(other, key, {"n": 2})
    assert receipts.capture(other) is None


@pytest.mark.parametrize(
    ("breakage", "message"),
    [
        ("denied", "refused PutObject: AccessDenied"),
        ("unconditional", "ignored If-None-Match"),
        ("no_list", "grant s3:ListBucket"),
        ("no_list_versioned", "grant s3:ListBucket"),
        ("public", "serves objects to unsigned requests"),
    ],
)
def test_ready_refuses_a_bucket_that_cannot_keep_the_contract(bucket, breakage, message):
    if breakage == "denied":
        bucket.refuse["put_object"] = _error(403, "AccessDenied", "PutObject")
    elif breakage == "unconditional":
        bucket.enforce_conditional = False
    elif breakage == "no_list":
        bucket.list_bucket = False
    elif breakage == "no_list_versioned":
        bucket.list_bucket = False
        bucket.versioned = True
    else:
        bucket.anonymous.public = True
    with pytest.raises(ReceiptStoreUnavailable, match=message):
        receipts.ready()
    assert bucket.objects == {}, "a failed probe must not leave objects behind"


def test_ready_proves_delete_before_creating_anything(bucket):
    bucket.refuse["delete_object"] = _error(403, "AccessDenied", "DeleteObject")
    with pytest.raises(ReceiptStoreUnavailable, match="refused DeleteObject: AccessDenied"):
        receipts.ready()
    assert bucket.puts == [] and bucket.objects == {}, "no probe may leak without delete"


@pytest.mark.parametrize("stage", ["create", "read_back"])
def test_ready_skips_cleanup_when_the_endpoint_stops_answering(bucket, stage):
    from botocore.exceptions import ReadTimeoutError

    timeout = ReadTimeoutError(endpoint_url="https://s3.example.test")
    if stage == "create":
        bucket.refuse["put_object"] = timeout
    else:
        reads = iter([None, timeout])

        def hang_on_read_back(_key):
            failure = next(reads, None)
            if failure is not None:
                raise failure

        bucket.before_get = hang_on_read_back
    with pytest.raises(ReceiptStoreUnavailable, match="ReadTimeoutError"):
        receipts.ready()
    probe_deletes = [key for key in bucket.deletes if not key.endswith(".unwritten")]
    assert probe_deletes == [], "a hung endpoint must not be waited out again"


def test_ready_accepts_its_own_create_answered_412_after_a_retry(bucket):
    bucket.land_then_refuse = True
    receipts.ready()
    assert bucket.objects == {}


async def test_unready_bucket_blocks_dispatch(content_store, bucket):
    bucket.refuse["put_object"] = _error(403, "AccessDenied", "PutObject")
    request = {"org": "org", "principal": "owner"}
    execution = ValidationExecution(review_digest(request), "org", "owner")
    run = AsyncMock(side_effect=AssertionError("must not dispatch"))
    with pytest.raises(OSError, match="refused PutObject"):
        await run_validation_stage(
            execution=execution,
            parent_id="parent",
            source_ids=[],
            request=request,
            policy="{}",
            check_current=AsyncMock(),
            run=run,
        )
    assert await execution.load() is None
    run.assert_not_awaited()


async def test_database_outage_then_new_execution_recovers_from_bucket(
    content_store, prepared, monkeypatch, bucket
):
    request = {"org": "org", "principal": "owner", "parent": "parent", "policy": "{}"}
    identity = review_digest(request)
    execution = ValidationExecution(identity, "org", "owner")
    original_query = execution_module._query
    result = await run_memory_validation(prepared, extractor({"findings": []}))

    async def completed_before_outage():
        monkeypatch.setattr(
            execution_module, "_query", AsyncMock(side_effect=OSError("database unavailable"))
        )
        return result

    args = dict(
        execution=execution,
        parent_id="parent",
        source_ids=[],
        request=request,
        policy="{}",
        check_current=AsyncMock(),
        run=AsyncMock(side_effect=completed_before_outage),
    )
    with pytest.raises(OSError, match="database unavailable"):
        await run_validation_stage(**args)
    assert [key for _, key in bucket.objects] == [f"{PREFIX}/{identity}.receipt"]
    monkeypatch.setattr(execution_module, "_query", original_query)
    args["execution"] = ValidationExecution(identity, "org", "owner")
    args["run"] = AsyncMock(side_effect=AssertionError("duplicate provider call"))
    resumed = await run_validation_stage(**args)
    assert resumed["usage"] == result.usage.model_dump(mode="json")
    args["run"].assert_not_awaited()
    assert bucket.objects == {}


@pytest.mark.parametrize(
    ("url", "location"),
    [
        ("s3://bucket", S3ReceiptLocation("bucket", "", None)),
        ("s3://bucket/", S3ReceiptLocation("bucket", "", None)),
        ("s3://my.bucket-1/a/b/", S3ReceiptLocation("my.bucket-1", "a/b", None)),
        ("s3://bucket/p?region=eu-central-1", S3ReceiptLocation("bucket", "p", "eu-central-1")),
        ("s3://bucket?region=us-gov-west-1", S3ReceiptLocation("bucket", "", "us-gov-west-1")),
    ],
)
def test_receipt_url_parses_bucket_prefix_and_region(url, location):
    assert parse_s3_receipt_url(url) == location


@pytest.mark.parametrize(
    "url",
    [
        "",
        "/var/lib/receipts",
        "https://bucket.s3.amazonaws.com/p",
        "s3://",
        "s3://Bucket",
        "s3://ab",
        "s3://user@bucket",
        "s3://bucket:9000",
        "s3://bucket..name",
        "s3://bucket/a//b",
        "s3://bucket/a/../b",
        "s3://bucket/p#frag",
        "s3://bucket/p?endpoint=http://minio",
        "s3://bucket/p?region=us-west-2&region=us-east-1",
        "s3://bucket/p?region=US_WEST",
        "s3://bucket/p?region",
    ],
)
def test_receipt_url_refuses_anything_else(url):
    with pytest.raises(ValueError):
        parse_s3_receipt_url(url)


def test_real_client_sends_create_only_puts_and_maps_answers(monkeypatch):
    """botocore's own S3 model must accept the conditional parameters we send."""
    import boto3
    from botocore.stub import ANY, Stubber

    session = boto3.session.Session(
        aws_access_key_id="testing", aws_secret_access_key="testing", region_name="us-west-2"
    )
    client = session.client("s3")
    anonymous = session.client("s3")
    monkeypatch.setattr(s3_store, "s3_clients", lambda region: (client, anonymous))
    store = S3ReceiptStore(parse_s3_receipt_url("s3://bucket/prefix"))
    put = {
        "Bucket": "bucket",
        "Key": "prefix/digest.receipt",
        "Body": b"ciphertext",
        "IfNoneMatch": "*",
        "ContentType": ANY,
    }
    with Stubber(client) as stub:
        stub.add_response("put_object", {"ETag": '"etag"'}, put)
        stub.add_client_error("put_object", "PreconditionFailed", http_status_code=412)
        stub.add_client_error("put_object", "AccessDenied", http_status_code=403)
        stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
        stub.add_client_error("get_object", "AccessDenied", http_status_code=403)
        stub.add_client_error("get_object", "NoSuchBucket", http_status_code=404)
        stub.add_client_error("get_object", "404", http_status_code=404)
        stub.add_response("delete_object", {}, {"Bucket": "bucket", "Key": "prefix/x"})
        assert store.put_new("digest.receipt", b"ciphertext") is True
        assert store.put_new("digest.receipt", b"ciphertext") is False
        with pytest.raises(ReceiptStoreUnavailable, match=r"AccessDenied \(403\)"):
            store.put_new("digest.receipt", b"ciphertext")
        assert store.get("digest.receipt") is None
        with pytest.raises(ReceiptStoreUnavailable, match="refused GetObject"):
            store.get("digest.receipt")
        # A missing bucket or a stray endpoint is an outage, never "no receipt".
        with pytest.raises(ReceiptStoreUnavailable, match="NoSuchBucket"):
            store.get("digest.receipt")
        with pytest.raises(ReceiptStoreUnavailable, match=r"404 \(404\)"):
            store.get("digest.receipt")
        store.delete("x")
        stub.assert_no_pending_responses()


def test_client_factory_signs_with_sigv4_and_probes_unsigned():
    from botocore import UNSIGNED

    s3_store._CLIENTS.pop("ap-southeast-2", None)
    signed, anonymous = s3_store.s3_clients("ap-southeast-2")
    try:
        assert signed.meta.region_name == "ap-southeast-2"
        assert signed.meta.config.signature_version == "s3v4"
        assert signed.meta.config.max_pool_connections == 32
        assert signed.meta.config.connect_timeout == s3_store._CONNECT_TIMEOUT
        assert signed.meta.config.read_timeout == s3_store._READ_TIMEOUT
        assert signed.meta.config.retries == {"mode": "standard", "total_max_attempts": 3}
        assert anonymous.meta.config.signature_version is UNSIGNED
        assert s3_store.s3_clients("ap-southeast-2") == (signed, anonymous)
    finally:
        s3_store._CLIENTS.pop("ap-southeast-2", None)


def test_ready_against_a_black_hole_endpoint_is_bounded(monkeypatch):
    """An endpoint that accepts TCP and never answers must fail fast, not in minutes."""
    import socket
    import time

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    accepted: list[socket.socket] = []

    def swallow():
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            accepted.append(connection)

    threading.Thread(target=swallow, daemon=True).start()
    port = listener.getsockname()[1]
    for name, value in {
        "AWS_ENDPOINT_URL_S3": f"http://127.0.0.1:{port}",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(s3_store, "_CONNECT_TIMEOUT", 0.5)
    monkeypatch.setattr(s3_store, "_READ_TIMEOUT", 0.3)
    monkeypatch.setattr(settings, "validation_receipt_url", "s3://black-hole/p?region=eu-west-3")
    s3_store._CLIENTS.pop("eu-west-3", None)
    started = time.monotonic()
    try:
        with pytest.raises(ReceiptStoreUnavailable, match="ReadTimeoutError"):
            receipts.ready()
    finally:
        s3_store._CLIENTS.pop("eu-west-3", None)
        listener.close()
        for connection in accepted:
            connection.close()
    elapsed = time.monotonic() - started
    # Three attempts of one request plus standard-mode backoff; nothing after it.
    assert elapsed < 15, elapsed
    assert len(accepted) <= 3
