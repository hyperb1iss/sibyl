"""Keep the cyclic collector off a serving process's long-lived heap.

A full collection walks every tracked object and stops every thread while it
does, the event loop's included. After startup the API holds about a million
tracked objects (modules, app and route trees, schemas) that live as long as
the process. With the interpreter's default thresholds a cold build of a
6,000-entity graph set off six or seven full collections, each walking that
heap again and stalling the loop for up to 700 ms.

Freezing once startup has settled moves those objects into the permanent
generation, so no collection walks them again. The thresholds pace what is
left. A young collection runs every 10,000 net allocations and the middle
generation every ten of those, so neither ever walks more than about 100,000
objects (under 75 ms on the bench). A full collection waits for a hundred
middle ones, about ten million allocations, and the interpreter still skips
it until the objects promoted since the last one reach a quarter of the old
generation. Cyclic garbage is still reclaimed: young cycles at the next young
collection, older ones at the next full one, and nothing created after the
freeze is exempt.
"""

from __future__ import annotations

import gc

import structlog

log = structlog.get_logger()

GC_THRESHOLDS = (10_000, 10, 100)


def tune_gc_for_long_running_process() -> None:
    """Freeze the startup heap and pace collections for request allocation.

    Call once, from a long-running process (API server, job worker), after
    its imports and schema bootstrap have finished. A short-lived CLI keeps
    the interpreter's defaults: it exits before a collection could matter.
    """
    # Startup garbage goes now; freezing it would keep it for good.
    gc.collect()
    gc.freeze()
    gc.set_threshold(*GC_THRESHOLDS)
    log.info(
        "gc_tuned_for_long_running_process",
        frozen_objects=gc.get_freeze_count(),
        thresholds=list(GC_THRESHOLDS),
    )


__all__ = ["GC_THRESHOLDS", "tune_gc_for_long_running_process"]
