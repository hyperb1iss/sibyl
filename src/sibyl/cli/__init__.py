"""Sibyl CLI - Modular command-line interface.

This package provides a comprehensive CLI for Sibyl with subcommand groups:
- task: Task lifecycle management
- project: Project operations
- entity: Generic entity CRUD
- explore: Graph traversal and exploration
- source: Documentation source management
- export: Data export (JSON/CSV)
- db: Database operations
- generate: Synthetic data generation
"""

from sibyl.cli.main import app, main

__all__ = ["app", "main"]
