"""Sibyl API Server.

SurrealDB-native knowledge graph and task workflow for development
patterns, templates, and hard-won wisdom.
"""

import os
from pathlib import Path

from sibyl_core.logging import configure_logging

# Configure logging FIRST before any other modules use structlog
configure_logging(service_name="api")

from sibyl.config import Settings  # noqa: E402 - must come after logging config


def _read_version() -> str:
    """Read version from VERSION file in repo root."""
    if version := os.environ.get("SIBYL_VERSION", "").strip():
        return version

    # Try multiple locations for VERSION file
    for path in [
        Path(__file__).parent.parent.parent.parent.parent
        / "VERSION",  # From src/sibyl/__init__.py → repo root
        Path("/app/VERSION"),  # Docker container
        Path.cwd() / "VERSION",  # Current working directory
    ]:
        if path.exists():
            return path.read_text().strip()
    return "0.0.0"


__version__ = _read_version()
__all__ = ["Settings", "__version__"]
