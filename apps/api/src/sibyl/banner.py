"""Sibyl startup banner.

✦ SIBYL — knowledge echoes forward
"""

import structlog
from rich.console import Console
from rich.text import Text

from sibyl import __version__

# SilkCircuit colors
ELECTRIC_PURPLE = "#e135ff"
NEON_CYAN = "#80ffea"
CORAL = "#ff6ac1"
DIM = "#555566"


def print_banner(*, component: str | None = None) -> None:
    """Print the Sibyl startup banner (Rich styled, no timestamp).

    Args:
        component: Optional component name (e.g., "worker", "api")
    """
    console = Console(stderr=True)

    # Build the banner
    banner = Text()
    banner.append("✦ ", style=f"bold {ELECTRIC_PURPLE}")
    banner.append("SIBYL", style=f"bold {NEON_CYAN}")
    banner.append(" — ", style=DIM)
    banner.append("knowledge echoes forward", style=f"italic {CORAL}")
    banner.append("  ", style="default")
    banner.append(f"v{__version__}", style=f"dim {ELECTRIC_PURPLE}")

    if component:
        banner.append(f"  [{component}]", style=f"dim {NEON_CYAN}")

    console.print(banner)


def log_banner(*, component: str | None = None) -> None:
    """Log the Sibyl startup banner via structlog (with timestamp).

    Args:
        component: Optional component name (e.g., "worker", "api")
    """
    log = structlog.get_logger()

    # Build banner string for structlog
    banner = f"✦ SIBYL — knowledge echoes forward  v{__version__}"
    if component:
        banner += f"  [{component}]"

    log.info(banner)
