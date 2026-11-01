"""The CI E2E job's sign-ins fit the login route's per-address rate limit."""

import os
import re
from collections import Counter
from pathlib import Path

import pytest

# POST /auth/local/login allows five attempts a minute per client address
# (apps/api/src/sibyl/api/routes/auth.py), counting failed attempts too.
LOGIN_LIMIT_PER_MINUTE = 5
_LOGIN_REQUEST = re.compile(r"\bmethod=POST path=/api/auth/local/login\b.*?\bclient=(\S+)")


def login_attempts_by_client(log_text: str) -> Counter[str]:
    """Count login requests in the API's request log by the client address it resolved."""
    return Counter(match.group(1) for match in _LOGIN_REQUEST.finditer(log_text))


@pytest.mark.browser
@pytest.mark.usefixtures("e2e_auth_token", "authenticated_browser_storage")
def test_job_sign_ins_stay_under_the_login_limit() -> None:
    """No client address signs in as often in the whole job as the limiter allows in a minute.

    The browser run is the job's last step, and both fixtures have signed in
    by the time this counts. A job total under the limit fits every window
    however fast the steps run, so the check cannot pass or fail on timing.
    """
    log_path = os.getenv("SIBYL_E2E_API_LOG", "").strip()
    if not log_path:
        pytest.skip("set SIBYL_E2E_API_LOG to the API server's log to count the job's sign-ins")

    attempts = login_attempts_by_client(Path(log_path).read_text(encoding="utf-8"))
    assert attempts, f"No login requests in {log_path}; the API request log format changed"
    crowded = {
        client: count for client, count in attempts.items() if count >= LOGIN_LIMIT_PER_MINUTE
    }
    assert not crowded, (
        f"Sign-ins per client address over the whole job: {dict(attempts)}. "
        f"{sorted(crowded)} reached the {LOGIN_LIMIT_PER_MINUTE}-per-minute login limit, "
        "so a fast run would answer 429. Reuse e2e_auth_token instead of signing in again."
    )
