from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(scope="session", autouse=True)
def _force_proto_text_reporter() -> Iterator[None]:
    """Keep proto shims from wrapping machine-readable tool output in NDJSON."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setenv("PROTO_REPORTER", "text")
        yield
