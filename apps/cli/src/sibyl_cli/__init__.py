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

# Disable Graphiti telemetry
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

__version__ = "0.1.0"

from sibyl_cli.main import app, main

__all__ = ["__version__", "app", "main"]
