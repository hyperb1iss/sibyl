"""Authored repair-family registry with explicit split ownership."""

from .apportionment import apportionment
from .build_planner import build_planner
from .commands import commands
from .configuration import configuration
from .indexing import indexing
from .interval_capacity import interval_capacity
from .model import Family
from .path_routing import path_routing
from .reservations import reservations
from .sessions import sessions
from .stream_framing import stream_framing


def families(seed: int) -> list[Family]:
    return [
        configuration(seed),
        commands(seed),
        sessions(seed),
        indexing(seed),
        reservations(seed),
        build_planner(seed),
        apportionment(seed),
        stream_framing(seed),
        path_routing(seed),
        interval_capacity(seed),
    ]
