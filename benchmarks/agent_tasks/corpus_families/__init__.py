"""Authored repair-family registry with explicit split ownership."""

from .build_planner import build_planner
from .commands import commands
from .configuration import configuration
from .indexing import indexing
from .model import Family
from .reservations import reservations
from .sessions import sessions


def families(seed: int) -> list[Family]:
    return [
        configuration(seed),
        commands(seed),
        sessions(seed),
        indexing(seed),
        reservations(seed),
        build_planner(seed),
    ]
