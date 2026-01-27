"""sibyl-cli: Command-line interface for Sibyl knowledge graph.

This package provides the client-side CLI for interacting with a Sibyl server.
All commands communicate via REST API - no direct database access.

Subcommand groups:
- task: Task lifecycle management
- epic: Epic/feature grouping
- project: Project operations
- entity: Generic entity CRUD
- explore: Graph traversal and exploration
- source: Documentation source management
- crawl: Web crawling
- auth: Authentication
- org: Organization management
- config: Configuration
- context: Project context

Server commands (serve, db, generate, etc.) are in the sibyl-server package.
"""

import os
from importlib.metadata import version as pkg_version

from sibyl_cli.main import app, main

# Disable Graphiti telemetry
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")


def __getattr__(name: str) -> str:
    """Lazy attribute access for __version__."""
    if name == "__version__":
        try:
            return pkg_version("sibyl-dev")
        except Exception:
            return "unknown"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["__version__", "app", "main"]
