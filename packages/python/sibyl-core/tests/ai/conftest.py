"""Skip the AI test tree when the optional ``llm`` extra is not installed.

These tests exercise the pydantic-ai-backed extraction, generation, and
client layers, which live behind the ``llm`` (and ``runtime``) optional
dependency group. When that extra is absent (for example a base CI install
that does not sync extras), importing the modules under test raises
``ModuleNotFoundError: pydantic_ai`` at collection time. Skipping the whole
subtree keeps the suite green where the extra is not installed while still
running these tests wherever it is (local dev, the eval jobs).
"""

import pytest

pytest.importorskip("pydantic_ai")
