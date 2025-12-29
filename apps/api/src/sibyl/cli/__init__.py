"""Sibyld CLI - Server daemon commands.

This module provides the server-side CLI for operators:
- serve: Start the MCP server
- worker: Start the background job worker
- db: Database operations (backup, restore, migrations)
- generate: Synthetic data generation
- up/down/status: Local development services

For client commands (task, search, add, etc.), use the `sibyl` CLI.
"""

import os

# Disable Graphiti telemetry before any imports
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

from sibyl.cli.main import app, main

__all__ = ["app", "main"]
