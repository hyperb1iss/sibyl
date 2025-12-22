from datetime import timedelta

import pytest

from sibyl.auth.oauth_state import OAuthStateError, issue_state, verify_state


def test_oauth_state_roundtrip() -> None:
    token, issued = issue_state(secret="secret")
    verified = verify_state(
        secret="secret",
        cookie_value=token,
        returned_state=issued.state,
        max_age=timedelta(minutes=10),
    )
    assert verified.state == issued.state


def test_oauth_state_rejects_mismatch() -> None:
    token, _issued = issue_state(secret="secret")
    with pytest.raises(OAuthStateError):
        verify_state(secret="secret", cookie_value=token, returned_state="nope")


def test_oauth_state_rejects_expired() -> None:
    token, issued = issue_state(secret="secret")
    with pytest.raises(OAuthStateError):
        verify_state(
            secret="secret",
            cookie_value=token,
            returned_state=issued.state,
            max_age=timedelta(seconds=-1),
        )
