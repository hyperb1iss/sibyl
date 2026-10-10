"""Private encrypted completed receipts independent of content-store availability.

Receipts live in a private directory by default (SIBYL_VALIDATION_RECEIPT_DIR)
or in Amazon S3 when SIBYL_VALIDATION_RECEIPT_URL names a bucket. Both stores
hold the same Fernet ciphertext under the same review-digest name and only
ever create a receipt, never replace one.
"""

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from cryptography.fernet import Fernet

from sibyl_core.config import settings
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_review import review_digest


class _ReceiptStore(Protocol):
    def ready(self) -> None: ...

    def get(self, name: str) -> bytes | None: ...

    def get_many(self, names: Iterable[str]) -> dict[str, bytes | None]: ...

    def put_new(self, name: str, ciphertext: bytes) -> bool: ...

    def delete(self, name: str) -> None: ...


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _DirectoryStore:
    """Private files published by hard link, fsynced with their directory."""

    def __init__(self, root: str) -> None:
        self._root = root

    def _directory(self) -> Path:
        path = Path(self._root).expanduser().absolute()
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.resolve() != path or path.stat().st_mode & 0o077:
            raise ValueError("Validation receipt directory must be private and not symlinked")
        return path

    def ready(self) -> None:
        directory = self._directory()
        descriptor, name = tempfile.mkstemp(prefix=".probe-", dir=directory)
        try:
            os.fsync(descriptor)
            _sync_directory(directory)
        finally:
            os.close(descriptor)
            Path(name).unlink()

    def get(self, name: str) -> bytes | None:
        path = self._directory() / name
        if not path.exists():
            return None
        try:
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ValueError("Validation receipt file is not private")
            ciphertext = path.read_bytes()
        except FileNotFoundError:
            return None
        return ciphertext

    def get_many(self, names: Iterable[str]) -> dict[str, bytes | None]:
        return {name: self.get(name) for name in names}

    def put_new(self, name: str, ciphertext: bytes) -> bool:
        path = self._directory() / name
        descriptor, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(ciphertext)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path)
                created = True
            except FileExistsError:
                created = False
            # Also makes a concurrent writer's entry durable before we rely on it.
            _sync_directory(path.parent)
            return created
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, name: str) -> None:
        path = self._directory() / name
        path.unlink(missing_ok=True)
        _sync_directory(path.parent)


def _store() -> _ReceiptStore:
    if settings.validation_receipt_url:
        from sibyl_core.backends.s3_receipt_store import S3ReceiptStore, parse_s3_receipt_url

        return S3ReceiptStore(parse_s3_receipt_url(settings.validation_receipt_url))
    return _DirectoryStore(settings.validation_receipt_dir)


def _name(request: str) -> str:
    return review_digest(json.loads(request)) + ".receipt"


def ready() -> None:
    """Verify durable write access before creating a provider dispatch obligation."""
    _store().ready()


def capture(request: str) -> bytes | None:
    """Read private ciphertext without traversing unrelated receipts."""
    return _store().get(_name(request))


def capture_many(requests: Iterable[str]) -> dict[str, bytes | None]:
    """Read many requests' ciphertext in one pass; absent receipts map to None."""
    names = {request: _name(request) for request in requests}
    found = _store().get_many(names.values())
    return {request: found[name] for request, name in names.items()}


def decode(request: str, key: str, ciphertext: bytes) -> dict:
    """Authenticate ciphertext against its exact immutable request."""
    encoded = Fernet(key.encode()).decrypt(ciphertext).decode("utf-8")
    value = json.loads(encoded)
    if canonical(value) != encoded:
        raise ValueError("Validation receipt JSON is not canonical")
    if not isinstance(value, dict) or set(value) != {"request", "result"}:
        raise ValueError("Validation receipt envelope differs")
    if value["request"] != request or not isinstance(value["result"], dict):
        raise ValueError("Validation receipt request or result differs")
    return value["result"]


def read(request: str, key: str) -> dict | None:
    ciphertext = capture(request)
    return None if ciphertext is None else decode(request, key, ciphertext)


def restore(request: str, key: str, ciphertext: bytes) -> None:
    """Durably publish authenticated archive bytes without replacing local history."""
    decode(request, key, ciphertext)
    store = _store()
    name = _name(request)
    if not store.put_new(name, ciphertext) and store.get(name) != ciphertext:
        raise ValueError("Validation receipt archive conflicts with local history")


def retain(request: str, key: str, result: dict) -> None:
    """Publish one immutable ciphertext durably before database persistence."""
    existing = read(request, key)
    if existing is not None:
        if existing != result:
            raise ValueError("Validation receipt result differs")
        return
    ciphertext = Fernet(key.encode()).encrypt(
        canonical({"request": request, "result": result}).encode()
    )
    if not _store().put_new(_name(request), ciphertext) and read(request, key) != result:
        raise ValueError("Validation receipt concurrent result differs")


def discard(request: str) -> None:
    _store().delete(_name(request))
