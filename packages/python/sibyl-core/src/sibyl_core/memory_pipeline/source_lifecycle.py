"""Revision-ordered source exclusions, independent of a derivative's lifecycle."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

CORRECTION_BLOCKERS_KEY = "correction_blockers"
SOURCE_VALIDATION_PENDING_KEY = "source_validation_pending"
SOURCE_BINDINGS_KEY = "source_bindings"
RAW_SOURCE_IDS_KEY = "raw_source_ids"
CORRECTION_HISTORY_KEY = "correction_history"
# The binding a derived row gets for a source whose content epoch nobody
# recorded. It is smaller than every real capture revision, so any revised
# content blocks it: "we do not know which text this row read" has to read as
# stale, never as current.
UNKNOWN_SOURCE_REVISION = 0

__all__ = [
    "CORRECTION_BLOCKERS_KEY",
    "CORRECTION_HISTORY_KEY",
    "RAW_SOURCE_IDS_KEY",
    "SOURCE_BINDINGS_KEY",
    "SOURCE_VALIDATION_PENDING_KEY",
    "UNKNOWN_SOURCE_REVISION",
    "SourceCorrection",
    "SourceMemoryView",
    "correction_blocked",
    "correction_event",
    "declared_source_ids",
    "merge_source_correction",
    "public_memory_metadata",
    "source_revision_bindings",
]


def public_memory_metadata(metadata: Mapping[str, object] | None) -> dict[str, object]:
    """Copy final response metadata without other sources' IDs and clocks.

    Call only after lifecycle and policy checks. Bindings can name ancestors
    outside the reader's scope, so they stay internal alongside correction
    clocks. The row's admission state and authored nested maps remain visible.
    """
    public = {
        key: value
        for key, value in (metadata or {}).items()
        if key not in {CORRECTION_BLOCKERS_KEY, SOURCE_BINDINGS_KEY, "source_snapshot_sha256"}
    }
    identity = public.get("reflection_identity")
    if isinstance(identity, Mapping):
        public["reflection_identity"] = {
            key: value for key, value in identity.items() if key != "source_snapshot_sha256"
        }
    for key in ("lifecycle_reconciliation_pending", SOURCE_VALIDATION_PENDING_KEY):
        if isinstance(public.get(key), Mapping):
            public[key] = bool(public[key])
    return public


class SourceMemoryView(Protocol):
    """The three fields a corrected or observed capture is read through."""

    @property
    def id(self) -> str: ...

    @property
    def revision(self) -> int: ...

    @property
    def metadata(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class SourceCorrection:
    """One correction of one root source, as the derived rows must see it.

    ``revision`` is the root's capture revision after the correction was saved,
    which is the only monotone clock both lanes agree on. ``blocking`` says the
    source itself left recall. ``content_revision`` is the first revision whose
    text is the revised text, or ``0`` when the source's text was never revised;
    it is what makes a ``revise`` reach the rows that read the old body without
    also hiding the rows that already read the new one.
    """

    root_id: str
    revision: int
    blocking: bool
    content_revision: int = 0


def _valid_revision(value: object, *, minimum: int = 0) -> bool:
    # `bool` is a subclass of `int`, so a flag written into a revision slot
    # would read as revision 1 and silently order itself against real captures.
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _validate_event(event: SourceCorrection) -> None:
    if not isinstance(event.root_id, str) or not event.root_id.strip():
        raise ValueError("source correction root_id must be a non-empty string")
    if not _valid_revision(event.revision, minimum=1):
        raise ValueError("source correction revision must be a positive integer")
    if not isinstance(event.blocking, bool):
        raise ValueError("source correction blocking must be a boolean")
    if not _valid_revision(event.content_revision) or event.content_revision > event.revision:
        raise ValueError(
            "source correction content_revision must be a non-negative integer "
            "no newer than revision"
        )


def _parse_blockers(metadata: Mapping[str, object] | None) -> dict[str, tuple[int, bool]]:
    """Read the blocker bag, or refuse to guess what a malformed one meant.

    A falsy value (absent, ``{}``, ``None``) is a real statement -- this row
    inherits no exclusions -- and parses to nothing. Anything else that is not a
    bag of ``{revision, blocking}`` entries is a bag whose exclusions we cannot
    enumerate, so it raises rather than reading as "not excluded".
    """

    state = metadata.get(CORRECTION_BLOCKERS_KEY) if metadata else None
    if not state:
        return {}
    if not isinstance(state, Mapping):
        raise ValueError(f"{CORRECTION_BLOCKERS_KEY} must be a mapping of root id to blocker state")
    parsed: dict[str, tuple[int, bool]] = {}
    for root, entry in state.items():
        if not isinstance(root, str) or not root.strip():
            raise ValueError(f"{CORRECTION_BLOCKERS_KEY} root id must be a non-empty string")
        if not isinstance(entry, Mapping):
            raise ValueError(f"{CORRECTION_BLOCKERS_KEY} entry must be a mapping")
        revision = entry.get("revision")
        blocking = entry.get("blocking")
        if not _valid_revision(revision, minimum=1):
            raise ValueError(f"{CORRECTION_BLOCKERS_KEY} revision must be a positive integer")
        if not isinstance(blocking, bool):
            raise ValueError(f"{CORRECTION_BLOCKERS_KEY} blocking must be a boolean")
        parsed[root] = (revision, blocking)
    return parsed


def _parse_bindings(metadata: Mapping[str, object] | None) -> dict[str, int]:
    """Read the content epochs a row is bound to, refusing malformed ones.

    A binding is provenance: it asserts which revision of a source this row
    actually read. Dropping an unreadable one would not lose an exclusion, it
    would invent a fresher binding than the row can support, so it raises.
    """

    state = metadata.get(SOURCE_BINDINGS_KEY) if metadata else None
    if not state:
        return {}
    if not isinstance(state, Mapping):
        raise ValueError(f"{SOURCE_BINDINGS_KEY} must be a mapping of source id to revision")
    parsed: dict[str, int] = {}
    for source_id, revision in state.items():
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError(f"{SOURCE_BINDINGS_KEY} source id must be a non-empty string")
        if not _valid_revision(revision):
            raise ValueError(f"{SOURCE_BINDINGS_KEY} revision must be a non-negative integer")
        parsed[source_id] = revision
    return parsed


def declared_source_ids(metadata: Mapping[str, object] | None) -> tuple[str, ...]:
    value = metadata.get(RAW_SOURCE_IDS_KEY) if metadata else None
    if value is None:
        return ()
    # A bare string is one declared id; iterating it would declare one id per
    # character. A Mapping is iterable too and its keys are not a support list.
    if isinstance(value, str):
        candidates: Sequence[object] = (value,)
    elif isinstance(value, list | tuple | set | frozenset):
        candidates = tuple(value)
    else:
        raise ValueError(f"{RAW_SOURCE_IDS_KEY} must be a list of source id strings")
    declared: list[str] = []
    for item in candidates:
        if not isinstance(item, str):
            raise ValueError(f"{RAW_SOURCE_IDS_KEY} entries must be strings")
        # A blank id names nothing and no correction can ever key on it, so
        # skipping it drops no provenance.
        if item.strip():
            declared.append(item)
    return tuple(declared)


def _revised_content_revision(
    metadata: Mapping[str, object] | None,
    revision: int,
    checkpoint: Mapping[str, object] | None = None,
) -> int:
    """The newest revision of this source that carries revised text.

    Read from ``correction_history``, which is append-only: a ``restore`` adds
    an entry and clears lifecycle keys but never removes the ``revise`` that
    happened, so the content epoch is retained across a later restore for free.

    A history entry records ``prior_revision``, the revision *before* the
    correction was saved, so the revised text first exists at ``prior_revision +
    1``. Legacy entries use the storage-owned checkpoint recording when their
    body was observed. An unnormalized source conservatively contributes its
    current revision until storage establishes that checkpoint. An entry claiming a
    revision the capture has not reached is impossible and raises, because
    honouring it would block every binding forever and ignoring it would bless a
    history we cannot explain.
    """

    history = (metadata.get(CORRECTION_HISTORY_KEY) if metadata else None) or []
    if not isinstance(history, list | tuple):
        raise ValueError(f"{CORRECTION_HISTORY_KEY} must be a list of correction entries")
    legacy = [
        entry
        for entry in history
        if isinstance(entry, Mapping)
        and str(entry.get("action") or "").strip().lower() == "revise"
        and entry.get("prior_revision") is None
    ]
    legacy_revision = revision
    if checkpoint is not None:
        epoch = checkpoint.get("observed_revision")
        if (
            checkpoint.get("entries") != legacy
            or not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch < 0
        ):
            raise ValueError("legacy content checkpoint does not cover correction history")
        if epoch > revision or (legacy and epoch < 1):
            raise ValueError("legacy content checkpoint has an invalid observed revision")
        legacy_revision = epoch
    content_revision = UNKNOWN_SOURCE_REVISION
    for entry in history:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("action") or "").strip().lower() != "revise":
            continue
        prior = entry.get("prior_revision")
        if prior is None:
            candidate = legacy_revision
        elif _valid_revision(prior):
            candidate = prior + 1
        else:
            raise ValueError(
                f"{CORRECTION_HISTORY_KEY} prior_revision must be a non-negative integer"
            )
        if candidate > revision:
            raise ValueError(
                f"{CORRECTION_HISTORY_KEY} declares a revision newer than the persisted capture"
            )
        content_revision = max(content_revision, candidate)
    return content_revision


def correction_blocked(metadata: Mapping[str, object] | None) -> bool:
    """Whether inherited correction state excludes this derived row from recall.

    True when any root's entry is still blocking. Entries that are all ``False``
    are tombstones, kept on purpose so a delayed earlier event cannot re-block a
    cleared root, and they do not exclude anything.

    A malformed non-empty blocker bag fails closed. A row whose exclusions cannot
    be read is a row that may be excluded, and serving it would leak content a
    user asked to have retired; the caller's own read of the bag will raise and
    say so. Absent and empty state are not malformed and do not block.

    Only the blocker bag is read. A binding is provenance rather than a verdict,
    so a malformed one is refused where it can do harm -- at propagation, by
    :func:`merge_source_correction` and :func:`source_revision_bindings` -- and
    is not turned into an exclusion nothing actually asked for.
    """

    if metadata and metadata.get(SOURCE_VALIDATION_PENDING_KEY):
        return True
    try:
        blockers = _parse_blockers(metadata)
    except ValueError:
        return True
    return any(blocking for _revision, blocking in blockers.values())


def correction_event(memory: SourceMemoryView, *, blocking: bool) -> SourceCorrection:
    """Build the propagable event for a correction already saved to ``memory``.

    The revision comes from the persisted capture rather than from the caller,
    because that is the value the storage layer bumped and therefore the only one
    that orders two propagations of the same root. Every non-correction metadata
    write bumps it too, which costs nothing here: a later revision only ever
    restates the same verdict, and the extra bump cannot resurrect a blocker that
    a restore already cleared at a higher revision.

    ``blocking`` is the caller's verdict on the source itself (it left recall),
    not on the derived rows; the derived-row verdict is decided per row by
    :func:`merge_source_correction` against that row's bindings.
    """

    if not isinstance(blocking, bool):
        raise ValueError("source correction blocking must be a boolean")
    if not isinstance(memory.id, str) or not memory.id.strip():
        raise ValueError("source correction root_id must be a non-empty string")
    if not _valid_revision(memory.revision, minimum=1):
        raise ValueError("source correction revision must be a positive integer")
    event = SourceCorrection(
        root_id=memory.id,
        revision=memory.revision,
        blocking=blocking,
        content_revision=_revised_content_revision(
            memory.metadata, memory.revision, getattr(memory, "legacy_content_checkpoint", None)
        ),
    )
    # The history is read off the same row as the revision, so this can only
    # fire on a bag that contradicts itself; it must not ship as an event.
    _validate_event(event)
    return event


def merge_source_correction(
    metadata: Mapping[str, object],
    event: SourceCorrection,
) -> dict[str, object]:
    """Apply one root's correction to one derived row's metadata bag.

    Returns a new bag. Unrelated keys -- the row's own lifecycle, its bindings,
    every other root's blocker -- are carried through untouched, so propagating
    root A cannot overwrite the row's own archived state or root B's exclusion.
    Inputs are never mutated.

    The row-level verdict is the event's ``blocking`` *or* a stale content epoch:
    a ``revise`` blocks a row whose binding for this root is older than the
    revised revision, or missing entirely. So a ``revise`` at revision 2 followed
    by a ``restore`` at revision 3 still blocks a row bound to revision 1, while
    a row bound to revision 2 stays clear through both.

    Ordering is resolved against the stored revision, not against arrival: a
    lower revision leaves the entry alone, an equal revision may only strengthen
    ``blocking``, and a cleared entry is written as ``False`` rather than deleted
    so a late-arriving earlier event has something to lose to.

    Raises ``ValueError`` on a malformed event or malformed stored state, and
    writes nothing in that case; the caller reports the propagation as incomplete
    instead of overwriting exclusions it could not read.
    """

    _validate_event(event)
    stored_blockers = _parse_blockers(metadata)
    bound = _parse_bindings(metadata).get(event.root_id)
    stale_content = event.content_revision > UNKNOWN_SOURCE_REVISION and (
        bound is None or bound < event.content_revision
    )
    effective = event.blocking or stale_content

    # Rewritten in canonical form: exactly one revision/blocking pair per root,
    # so the bag stays auditable and no reader has to guess at extra fields.
    entries: dict[str, object] = {
        root: {"revision": revision, "blocking": blocking}
        for root, (revision, blocking) in stored_blockers.items()
    }
    prior = stored_blockers.get(event.root_id)
    if prior is None or prior[0] < event.revision:
        entries[event.root_id] = {"revision": event.revision, "blocking": effective}
    elif prior[0] == event.revision:
        entries[event.root_id] = {"revision": event.revision, "blocking": prior[1] or effective}
    # prior[0] > event.revision: the row already carries a later statement about
    # this root, and this event is the delayed one. Keep the later verdict.

    merged = dict(metadata)
    merged[CORRECTION_BLOCKERS_KEY] = entries
    stored_bindings = metadata.get(SOURCE_BINDINGS_KEY)
    if isinstance(stored_bindings, Mapping):
        # Copied, never recomputed: rebinding a row here would bless it against
        # content it never read. Bindings are set once, where the row is built.
        merged[SOURCE_BINDINGS_KEY] = dict(stored_bindings)
    return merged


def source_revision_bindings(memories: Sequence[SourceMemoryView]) -> dict[str, int]:
    """The content epochs a row built from ``memories`` may claim to have read.

    Each observed capture binds its own id to its own revision, and each observed
    row's existing ``source_bindings`` are inherited so a promotion two hops from
    the capture still names the root it descends from. Overlapping paths take the
    **minimum**: the row's claim can only be as fresh as the oldest text on any
    path that fed it.

    A support declared in ``raw_source_ids`` with no inherited binding
    contributes ``UNKNOWN_SOURCE_REVISION``, which the minimum then keeps even
    when the same source was read directly and newer in this batch. That is the
    point: an old unbound review must not be rebound to source content that was
    corrected after the review was written. Canonical unretained anchors need no
    special case for the same reason -- an anchor never receives a real capture
    correction, so an unknown epoch for it stays honest and inert.

    Source ids are the original canonical strings. No grouping id is resolved and
    no row is fetched: the caller supplies exactly the snapshots it is authorized
    to read, and this helper does not claim otherwise.

    Raises ``ValueError`` on a malformed binding, support list, id or revision,
    rather than emitting provenance no observed row supports.
    """

    bindings: dict[str, int] = {}

    def bind(source_id: str, revision: int) -> None:
        current = bindings.get(source_id)
        bindings[source_id] = revision if current is None else min(current, revision)

    for memory in memories:
        if not isinstance(memory.id, str) or not memory.id.strip():
            raise ValueError("observed source id must be a non-empty string")
        if not _valid_revision(memory.revision):
            raise ValueError("observed source revision must be a non-negative integer")
        inherited = _parse_bindings(memory.metadata)
        bind(memory.id, memory.revision)
        for inherited_id, inherited_revision in inherited.items():
            bind(inherited_id, inherited_revision)
        for declared in declared_source_ids(memory.metadata):
            if declared not in inherited:
                bind(declared, UNKNOWN_SOURCE_REVISION)
    return bindings
