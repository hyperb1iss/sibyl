from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from starlette.requests import Request

from sibyl.auth.audit import AuditLogger


@pytest.mark.asyncio
async def test_audit_logger_captures_ip_and_user_agent() -> None:
    session = AsyncMock()
    session.add = Mock()
    session.flush = AsyncMock()

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"user-agent", b"test-agent")],
        "client": ("203.0.113.9", 1234),
    }
    request = Request(scope)

    event = await AuditLogger(session).log(
        action="auth.local.login",
        user_id=uuid4(),
        organization_id=uuid4(),
        request=request,
        details={"k": "v"},
    )

    session.add.assert_called_once()
    session.flush.assert_awaited_once()
    assert event.ip_address == "203.0.113.9"
    assert event.user_agent == "test-agent"
    assert event.details == {"k": "v"}


@pytest.mark.asyncio
async def test_audit_logger_allows_missing_request() -> None:
    session = AsyncMock()
    session.add = Mock()
    session.flush = AsyncMock()

    event = await AuditLogger(session).log(
        action="auth.logout",
        user_id=None,
        organization_id=None,
        request=None,
        details=None,
    )

    session.add.assert_called_once()
    session.flush.assert_awaited_once()
    assert event.ip_address is None
    assert event.user_agent is None
    assert event.details == {}
