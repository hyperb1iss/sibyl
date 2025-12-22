"""Signed OAuth state cookies (CSRF protection)."""

from __future__ import annotations

import base64
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256


@dataclass(frozen=True)
class OAuthState:
    state: str
    issued_at: datetime


class OAuthStateError(ValueError):
    """OAuth state validation error."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode((data + pad).encode("ascii"))


def _sign(secret: str, payload: bytes) -> str:
    sig = hmac.new(secret.encode("utf-8"), payload, sha256).digest()
    return _b64url(sig)


def issue_state(*, secret: str) -> tuple[str, OAuthState]:
    now = datetime.now(UTC)
    state = secrets.token_urlsafe(32)
    payload = json.dumps({"s": state, "iat": int(now.timestamp())}, separators=(",", ":")).encode(
        "utf-8"
    )
    token = f"{_b64url(payload)}.{_sign(secret, payload)}"
    return token, OAuthState(state=state, issued_at=now)


def verify_state(
    *,
    secret: str,
    cookie_value: str | None,
    returned_state: str | None,
    max_age: timedelta = timedelta(minutes=10),
) -> OAuthState:
    if not cookie_value or not returned_state:
        raise OAuthStateError("Missing OAuth state")

    try:
        payload_b64, sig = cookie_value.split(".", 1)
    except ValueError as e:
        raise OAuthStateError("Malformed OAuth state cookie") from e

    payload = _b64url_decode(payload_b64)
    expected = _sign(secret, payload)
    if not hmac.compare_digest(expected, sig):
        raise OAuthStateError("Invalid OAuth state signature")

    try:
        data = json.loads(payload.decode("utf-8"))
        state = str(data["s"])
        issued_at = datetime.fromtimestamp(int(data["iat"]), tz=UTC)
    except Exception as e:
        raise OAuthStateError("Malformed OAuth state payload") from e

    if not hmac.compare_digest(state, returned_state):
        raise OAuthStateError("OAuth state mismatch")

    if datetime.now(UTC) - issued_at > max_age:
        raise OAuthStateError("OAuth state expired")

    return OAuthState(state=state, issued_at=issued_at)

