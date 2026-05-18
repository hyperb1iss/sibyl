from __future__ import annotations

import pytest

from sibyl_core.tasks.dependencies import _get_graph_managers


def test_get_graph_managers_rejects_non_native_clients() -> None:
    with pytest.raises(RuntimeError, match="native graph client"):
        _get_graph_managers(object(), "org-native")
