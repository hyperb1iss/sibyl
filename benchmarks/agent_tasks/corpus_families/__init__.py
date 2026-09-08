"""Authored repair-family registry with explicit split ownership."""

from .apportionment import apportionment
from .bit_fields import bit_fields
from .build_planner import build_planner
from .commands import commands
from .configuration import configuration
from .indexing import indexing
from .interval_capacity import interval_capacity
from .knapsack import knapsack
from .model import Family
from .outer_join import outer_join
from .path_routing import path_routing
from .reservations import reservations
from .savepoints import savepoints
from .sessions import sessions
from .shortest_paths import shortest_paths
from .stream_framing import stream_framing
from .three_valued_logic import three_valued_logic
from .weighted_lru import weighted_lru
from .worker_matching import worker_matching


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
        worker_matching(seed),
        weighted_lru(seed),
        outer_join(seed),
        three_valued_logic(seed),
        savepoints(seed),
        bit_fields(seed),
        knapsack(seed),
        shortest_paths(seed),
    ]
