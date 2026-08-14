"""Console-script entry point for the Sibyl CLI."""

from __future__ import annotations

import sys

_QUICK_CONTEXT_FLAGS = {"--quick", "--validate"}
_QUICK_CONTEXT_ALLOWED_ARGS = {"context", "--quick", "--validate", "--json", "-j"}


def _is_quick_context_invocation(argv: list[str]) -> bool:
    args = argv[1:]
    if "--help" in args or "-h" in args:
        return False
    return bool(args and args[0] == "context" and _QUICK_CONTEXT_FLAGS.intersection(args)) and all(
        arg in _QUICK_CONTEXT_ALLOWED_ARGS for arg in args
    )


def main() -> None:
    # The notice lives at the console-script boundary because the fast path
    # below never reaches sibyl_cli.main, and agent hooks take that path.
    from sibyl_cli.common import notify_pending_writes

    try:
        if _is_quick_context_invocation(sys.argv):
            from sibyl_cli.context_quick import quick_context_payload, render_quick_context

            json_out = "--json" in sys.argv or "-j" in sys.argv
            render_quick_context(quick_context_payload(), json_out=json_out)
            return

        from sibyl_cli.main import main as cli_main

        cli_main()
    finally:
        notify_pending_writes()
