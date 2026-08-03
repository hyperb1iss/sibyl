"""What a writing agent may declare about the memory it is storing.

Three declarations, one contract. Spans say where the seams are, ``atomic`` says
there are none, and probes say what the memory has to answer later. All three
are validated and then stamped by the server; none of them can be forged through
the metadata bag, because a plan the server did not validate is a plan whose
tiling nothing has checked, and a rehearsal receipt the server did not run is a
claim about retrievability with no measurement behind it.

Probes exist because this campaign was twice burned by features that passed
their unit tests and did nothing in production. A probe is the question the
memory is being stored to answer, rehearsed against the real retrieval path at
write time, so an inert memory is visible at the moment it is written rather
than the first time somebody needed it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sibyl_core.memory_pipeline.spans import (
    AGENT_ATOMIC_METADATA_KEY,
    AGENT_SPANS_METADATA_KEY,
    AgentSpan,
    MemoryStructureError,
    agent_spans_metadata,
    coerce_agent_spans,
    validate_agent_spans,
    validate_atomic_content,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# One rehearsal is one search against the live retrieval path, so the count is
# what bounds write latency. Five is the ceiling on questions worth pre-paying
# for; past that the write is doing the reader's job.
MAX_PROBES_PER_MEMORY = 5

# A probe is a retrieval query, so it inherits the ceiling the refinement loop
# already applies to the queries it synthesizes: this restates
# ``retrieval.refinement.MAX_REFINEMENT_QUERY_CHARS``, which cannot be imported
# here without closing an import cycle, and ``test_memory_spans.py`` pins the two
# together.
MAX_PROBE_CHARS = 500

MEMORY_PROBES_METADATA_KEY = "memory_probes"
PROBE_REHEARSAL_METADATA_KEY = "probe_rehearsal"
PROBE_LAST_REPLAY_METADATA_KEY = "probe_last_replay"

# Keys the server owns outright. A caller that forwards a request body cannot
# name its own span plan or hand itself a passing rehearsal receipt, for the same
# reason it cannot name the principal its row reads as.
STRUCTURE_METADATA_KEYS = frozenset(
    {
        AGENT_ATOMIC_METADATA_KEY,
        AGENT_SPANS_METADATA_KEY,
        MEMORY_PROBES_METADATA_KEY,
        PROBE_LAST_REPLAY_METADATA_KEY,
        PROBE_REHEARSAL_METADATA_KEY,
    }
)


@dataclass(frozen=True, slots=True)
class MemoryStructure:
    """The validated structure one write declared."""

    spans: tuple[AgentSpan, ...] = ()
    atomic: bool = False
    probes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def declared(self) -> bool:
        return bool(self.spans) or self.atomic or bool(self.probes)


def coerce_probes(value: object) -> tuple[str, ...]:
    """Read a wire payload into probes, faulting anything that is not one."""
    if value is None:
        return ()
    if isinstance(value, str):
        msg = "probes must be a list of query strings"
        raise MemoryStructureError(msg, field="probes")
    if not isinstance(value, list | tuple):
        msg = "probes must be a list of query strings"
        raise MemoryStructureError(msg, field="probes")
    probes: list[str] = []
    for position, entry in enumerate(value):
        if not isinstance(entry, str):
            msg = f"probes[{position}] must be a string"
            raise MemoryStructureError(msg, field="probes")
        probes.append(entry.strip())
    return tuple(probes)


def validate_probes(probes: Sequence[str]) -> tuple[str, ...]:
    """Accept probes only if each is a query the retrieval path can run."""
    if len(probes) > MAX_PROBES_PER_MEMORY:
        msg = f"probes must contain at most {MAX_PROBES_PER_MEMORY} probes, got {len(probes)}"
        raise MemoryStructureError(msg, field="probes")
    for index, probe in enumerate(probes):
        if not probe:
            msg = f"probes[{index}] must not be empty"
            raise MemoryStructureError(msg, field="probes")
        if len(probe) > MAX_PROBE_CHARS:
            msg = (
                f"probes[{index}] is {len(probe)} characters, over the "
                f"{MAX_PROBE_CHARS} character limit"
            )
            raise MemoryStructureError(msg, field="probes")
    return tuple(probes)


def build_memory_structure(
    content: str,
    *,
    spans: object = None,
    atomic: bool = False,
    probes: object = None,
) -> MemoryStructure:
    """Validate one write's declarations against the body it will store.

    ``content`` must be the stored body, so callers strip before validating: the
    offsets an agent computed against its own string are only meaningful against
    the string the row ends up holding.
    """
    coerced_spans = coerce_agent_spans(spans)
    coerced_probes = validate_probes(coerce_probes(probes))
    if atomic and coerced_spans:
        msg = "atomic memories cannot also declare spans: pick one"
        raise MemoryStructureError(msg, field="atomic")
    if atomic:
        validate_atomic_content(content)
    validated_spans = validate_agent_spans(content, coerced_spans) if coerced_spans else ()
    return MemoryStructure(spans=validated_spans, atomic=atomic, probes=coerced_probes)


def structure_metadata(structure: MemoryStructure) -> dict[str, Any]:
    """Render the structure for storage on the parent row."""
    metadata: dict[str, Any] = {}
    if structure.spans:
        metadata[AGENT_SPANS_METADATA_KEY] = agent_spans_metadata(structure.spans)
    if structure.atomic:
        metadata[AGENT_ATOMIC_METADATA_KEY] = True
    if structure.probes:
        metadata[MEMORY_PROBES_METADATA_KEY] = list(structure.probes)
    return metadata


def strip_structure_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop every server-owned structure key from an incoming metadata bag."""
    if not metadata:
        return {}
    return {key: value for key, value in metadata.items() if key not in STRUCTURE_METADATA_KEYS}


def probes_from_metadata(metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Read stored probes back, treating anything malformed as absent."""
    if not metadata:
        return ()
    try:
        return coerce_probes(metadata.get(MEMORY_PROBES_METADATA_KEY))
    except MemoryStructureError:
        return ()


__all__ = [
    "MAX_PROBES_PER_MEMORY",
    "MAX_PROBE_CHARS",
    "MEMORY_PROBES_METADATA_KEY",
    "PROBE_LAST_REPLAY_METADATA_KEY",
    "PROBE_REHEARSAL_METADATA_KEY",
    "STRUCTURE_METADATA_KEYS",
    "MemoryStructure",
    "MemoryStructureError",
    "build_memory_structure",
    "coerce_probes",
    "probes_from_metadata",
    "strip_structure_metadata",
    "structure_metadata",
    "validate_probes",
]
