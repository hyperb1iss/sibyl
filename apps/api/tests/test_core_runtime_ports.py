from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from sibyl.core_runtime_ports import install_core_runtime_ports
from sibyl.persistence.surreal import content as api_content
from sibyl_core.runtime_ports import (
    RuntimePortUnavailable,
    get_audit_port,
    get_content_port,
    get_graph_link_port,
    get_queue_port,
    reset_runtime_ports,
)
from sibyl_core.services import content_client as core_content


def test_install_core_runtime_ports_registers_all_adapters() -> None:
    reset_runtime_ports()

    with pytest.raises(RuntimePortUnavailable):
        get_queue_port()

    install_core_runtime_ports()

    assert type(get_queue_port()).__name__ == "ApiQueuePort"
    assert type(get_content_port()).__name__ == "ApiContentPort"
    assert type(get_graph_link_port()).__name__ == "ApiGraphLinkPort"
    assert type(get_audit_port()).__name__ == "ApiAuditPort"

    reset_runtime_ports()


@pytest.mark.asyncio
async def test_core_content_uses_the_application_shared_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = object()
    get_api_client = AsyncMock(return_value=client)
    monkeypatch.setattr(api_content, "get_shared_surreal_content_client", get_api_client)
    reset_runtime_ports()

    try:
        install_core_runtime_ports()
        resolved = await core_content.get_shared_surreal_content_client()
    finally:
        reset_runtime_ports()

    assert resolved is client
    get_api_client.assert_awaited_once_with()
