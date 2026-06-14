from __future__ import annotations

import pytest

from sibyl.core_runtime_ports import install_core_runtime_ports
from sibyl_core.runtime_ports import (
    RuntimePortUnavailable,
    get_audit_port,
    get_content_port,
    get_graph_link_port,
    get_queue_port,
    reset_runtime_ports,
)


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
