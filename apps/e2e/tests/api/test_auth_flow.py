"""Live replay of the local-auth lifecycle, from signup through logout."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from tests.api.auth_flow import replay_auth_flow
from tests.conftest import API_BASE_URL

REPO_ROOT = Path(__file__).resolve().parents[4]
# `moon run dev` points the API's email outbox here; CI sets the variable for
# the API and this test alike.
DEFAULT_EMAIL_OUTBOX = REPO_ROOT / ".moon/cache/auth-flow-email-outbox.jsonl"
AUTH_FLOW_PASSWORD = "auth-flow-password-secure-123!"

EXPECTED_STEPS = (
    "signup_primary_user",
    "login_primary_user",
    "switch_to_owned_org",
    "refresh_tokens",
    "create_api_key",
    "authenticate_api_key",
    "revoke_api_key",
    "signup_invited_user",
    "invite_and_accept_user",
    "switch_active_org",
    "device_auth_flow",
    "change_password",
    "password_reset_request_and_consume",
    "list_user_sessions",
    "logout_rejects_access_token",
)


def _email_outbox_path() -> Path:
    configured = os.getenv("SIBYL_EMAIL_OUTBOX_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_EMAIL_OUTBOX


@pytest.mark.api
async def test_local_auth_lifecycle_replays_against_live_api(e2e_auth_token: str) -> None:
    email = f"auth-flow-{uuid.uuid4().hex[:12]}@sibyl.dev"

    result = await replay_auth_flow(
        base_url=API_BASE_URL,
        inviter_access_token=e2e_auth_token,
        email=email,
        password=AUTH_FLOW_PASSWORD,
        email_outbox_path=_email_outbox_path(),
    )

    assert result.steps == EXPECTED_STEPS
    assert result.primary_email == email
    # Eleven token-issuing responses, each carrying an access and refresh pair.
    assert len(result.token_claims) == 22
    observed = {(item.step, item.key): item.value for item in result.observations}
    assert observed[("authenticate api key", "org_role")] == "owner"
    assert observed[("verify revoked api key", "status")] in {"401", "403"}
    assert observed[("poll pending device auth", "error")] == "authorization_pending"
    assert observed[("list sessions", "current_session_present")] == "true"
    assert observed[("verify logged out token", "status")] in {"401", "403"}
