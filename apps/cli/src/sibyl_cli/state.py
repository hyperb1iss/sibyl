"""Global CLI state for context override and other session-level settings.

This module provides a way to pass global options (like --context) from the main
CLI to subcommands without modifying every command signature.
"""

from __future__ import annotations

import os

# Global context override set by --context flag
_context_override: str | None = None


def set_context_override(context_name: str | None) -> None:
    """Set the global context override for this CLI session."""
    global _context_override
    _context_override = context_name


def get_context_override() -> str | None:
    """Get the current context override.

    Priority:
    1. --context flag (set via set_context_override)
    2. SIBYL_CONTEXT environment variable
    3. None (use active context from config)
    """
    if _context_override:
        return _context_override

    env_context = os.environ.get("SIBYL_CONTEXT", "").strip()
    if env_context:
        return env_context

    return None


def clear_context_override() -> None:
    """Clear the context override (mainly for testing)."""
    global _context_override
    _context_override = None
