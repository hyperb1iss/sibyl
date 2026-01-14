"""Sibyl Runner - Distributed agent execution daemon.

Connects to Sibyl Core via WebSocket, receives task assignments,
and executes agents in isolated git worktrees.
"""

from sibyl_runner.cli import main

__all__ = ["main"]
