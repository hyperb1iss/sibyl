"""Private encrypted completed receipts independent of content-store availability."""

import json
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from sibyl_core.config import settings
from sibyl_core.tasks._evidence_json import canonical
from sibyl_core.tasks.procedure_review import review_digest


def _directory() -> Path:
    path = Path(settings.validation_receipt_dir).expanduser().absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.resolve() != path or path.stat().st_mode & 0o077:
        raise ValueError("Validation receipt directory must be private and not symlinked")
    return path


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def ready() -> None:
    """Verify durable write access before creating a provider dispatch obligation."""
    directory = _directory()
    descriptor, name = tempfile.mkstemp(prefix=".probe-", dir=directory)
    try:
        os.fsync(descriptor)
        _sync_directory(directory)
    finally:
        os.close(descriptor)
        Path(name).unlink()


def _path(request: str) -> Path:
    return _directory() / (review_digest(json.loads(request)) + ".receipt")


def read(request: str, key: str) -> dict | None:
    path = _path(request)
    if not path.exists():
        return None
    try:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Validation receipt file is not private")
        ciphertext = path.read_bytes()
    except FileNotFoundError:
        return None
    value = json.loads(Fernet(key.encode()).decrypt(ciphertext))
    if value.get("request") != request:
        raise ValueError("Validation receipt request differs")
    return value["result"]


def retain(request: str, key: str, result: dict) -> None:
    """Publish one immutable ciphertext with fsync before database persistence."""
    path = _path(request)
    existing = read(request, key)
    if existing is not None:
        if existing != result:
            raise ValueError("Validation receipt result differs")
        return
    ciphertext = Fernet(key.encode()).encrypt(
        canonical({"request": request, "result": result}).encode()
    )
    descriptor, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(ciphertext)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read(request, key) != result:
                raise ValueError("Validation receipt concurrent result differs") from None
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def discard(request: str) -> None:
    path = _path(request)
    path.unlink(missing_ok=True)
    _sync_directory(path.parent)
