import hashlib
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from sibyl.api.routes import users as users_routes
from sibyl.db.models import UserSession


@pytest.mark.asyncio
async def test_list_sessions_marks_current_from_sibyl_access_token_cookie() -> None:
    token = "access-token-value"
    current_hash = hashlib.sha256(token.encode()).hexdigest()

    session_row = UserSession(
        id=uuid4(),
        user_id=uuid4(),
        organization_id=None,
        token_hash=current_hash,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None),
        revoked_at=None,
    )

    request = MagicMock()
    request.headers = {}
    request.cookies = {"sibyl_access_token": token}

    # AuthSession contains both ctx and session
    auth = MagicMock()
    auth.ctx.user.id = session_row.user_id
    auth.session = AsyncMock()

    with patch.object(
        users_routes,
        "list_user_sessions",
        AsyncMock(return_value=[session_row]),
    ):
        rows = await users_routes.list_sessions(request=request, auth=auth)

    assert len(rows) == 1
    assert rows[0].is_current is True
