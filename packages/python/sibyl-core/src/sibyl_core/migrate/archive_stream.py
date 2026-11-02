"""Archive captures that stream a namespace through disk instead of holding it.

Reading a namespace in one statement makes SurrealDB materialize every row at
once (and stringify the lot for a fingerprint), and leaves the API holding the
rows several times over while it assembles the JSON. A capture instead reads
each table in id-ordered pages inside one read transaction, spools every page
to a scratch file as it arrives, and writes the archive JSON from the spool.
Only ledgers, sort keys and per-row digests stay in memory. For the same
snapshot the bytes written match ``json.dumps(..., indent=2, default=str)`` of
the payload the in-memory exports assemble.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from sibyl_core.backends.surreal.schema_version import SurrealExecute
from sibyl_core.memory_pipeline.observations import SourceKind
from sibyl_core.migrate.archive_lineage import LineageSection
from sibyl_core.migrate.source_integrity import (
    INTEGRITY_ARCHIVE_VERSION,
    IntegrityRowValidator,
    SectionDigest,
    decode_record,
    encode_record,
)
from sibyl_core.services.source_archive_store import (
    apply_datetime_texts,
    datetime_paths,
    source_table,
)

# Rows per statement. A graph row carries a 1024-float vector and a raw
# capture a 1536-float one, so a page stays in the low megabytes on both ends.
ARCHIVE_PAGE_SIZE = 200


class ArchiveSpool:
    """Values written once to a scratch file and read back in any order."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[bytes] = path.open("w+b")
        self._entries: list[tuple[int, int]] = []
        self._end = 0

    def append(self, value: Any) -> int:
        data = json.dumps(value, separators=(",", ":"), default=str).encode()
        self._handle.seek(self._end)
        self._handle.write(data)
        self._entries.append((self._end, len(data)))
        self._end += len(data)
        return len(self._entries) - 1

    def get(self, index: int) -> Any:
        offset, length = self._entries[index]
        self._handle.seek(offset)
        return json.loads(self._handle.read(length))

    def close(self) -> None:
        self._handle.close()
        self._path.unlink(missing_ok=True)


@dataclass
class SpooledCollection:
    """One ordered collection inside a spool: spool indexes and their sort keys."""

    spool: ArchiveSpool
    descending: tuple[bool, ...]

    def __post_init__(self) -> None:
        self._items: list[tuple[tuple[Any, ...], int]] = []

    def __len__(self) -> int:
        return len(self._items)

    def add(self, sort_key: tuple[Any, ...], value: Any) -> None:
        self._items.append((sort_key, self.spool.append(value)))

    def ordered(self) -> Iterator[Any]:
        """Values in key order; equal keys keep the order they arrived in."""
        order = list(range(len(self._items)))
        for position in reversed(range(len(self.descending))):
            order.sort(
                key=lambda item: self._items[item][0][position],
                reverse=self.descending[position],
            )
        for item in order:
            yield self.spool.get(self._items[item][1])


_FRACTION = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,](\d+)")


def order_value(value: Any) -> tuple[Any, ...]:
    """Sort a value the way SurrealDB's ORDER BY does for the types archives hold.

    Absent values sort first, then booleans, numbers, strings and datetimes;
    a datetime keeps the nanoseconds its native text carries.
    """
    if value is None:
        return (0,)
    if isinstance(value, bool):
        return (1, value)
    if isinstance(value, int | float):
        return (2, value)
    if isinstance(value, str):
        return (3, value)
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        text = getattr(value, "native_text", "")
        fraction = _FRACTION.match(text) if text else None
        digits = fraction.group(1) if fraction else ""
        return (4, moment, int((digits[6:9] + "000")[:3]))
    return (5, str(value))


class Streamed:
    """A JSON array whose items are produced while it is written."""

    def __init__(self, items: Iterable[Any]) -> None:
        self.items = items


class StreamedObject:
    """A JSON object whose members are written in order, some of them streamed."""

    def __init__(self, members: list[tuple[str, Any]]) -> None:
        self.members = members


class Deferred:
    """A JSON value computed only when the writer reaches it."""

    def __init__(self, compute: Callable[[], Any]) -> None:
        self.compute = compute


def _indent(level: int) -> str:
    return "\n" + "  " * level


def write_json(handle: IO[str], value: Any, level: int = 0) -> None:
    """Write ``value`` exactly as ``json.dumps(value, indent=2, default=str)`` would."""
    if isinstance(value, Deferred):
        value = value.compute()
    if isinstance(value, StreamedObject):
        if not value.members:
            handle.write("{}")
            return
        for index, (key, member) in enumerate(value.members):
            handle.write(("," if index else "{") + _indent(level + 1) + json.dumps(key) + ": ")
            write_json(handle, member, level + 1)
        handle.write(_indent(level) + "}")
        return
    if isinstance(value, Streamed):
        empty = True
        for item in value.items:
            handle.write(("[" if empty else ",") + _indent(level + 1))
            write_json(handle, item, level + 1)
            empty = False
        handle.write("[]" if empty else _indent(level) + "]")
        return
    text = json.dumps(value, indent=2, default=str)
    handle.write(text.replace("\n", _indent(level)) if level else text)


def write_json_file(path: Path, value: Any) -> int:
    with path.open("w", encoding="utf-8") as handle:
        write_json(handle, value)
    return path.stat().st_size


class PageReader:
    """Reads tables in id order, one bounded page per statement.

    The executor must hold one read transaction (``read_snapshot``), so every
    page belongs to the same snapshot. ``WITH NOINDEX`` keeps the plan a table
    scan in record order that stops at the page limit; an index plan would have
    to sort the whole table for every page.
    """

    def __init__(self, execute: SurrealExecute, *, page_size: int | None = None) -> None:
        self._execute = execute
        self._page_size = page_size or ARCHIVE_PAGE_SIZE

    async def pages(
        self,
        table: str,
        *,
        fields: str,
        where: str,
        params: dict[str, Any],
        omit: tuple[str, ...] = (),
        precise_datetimes: bool = False,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield the table's matching rows a page at a time.

        ``fields`` selects ``*``, so each row keeps the ``id`` its page is
        sorted and resumed by, and ``omit`` is dropped here rather than with
        ``OMIT``: the embedded engine sorts after applying ``OMIT`` and loses
        the order of an omitted ``id``.
        """
        if not fields.startswith("*"):
            raise ValueError("archive pages select every field")
        cursor: object = None
        while True:
            seek = " AND id > $archive_cursor" if cursor is not None else ""
            select = (
                f"SELECT {fields} FROM {table} "
                f"WITH NOINDEX WHERE {where}{seek} ORDER BY id LIMIT $archive_page_size"
            )
            bound = {**params, "archive_cursor": cursor, "archive_page_size": self._page_size}
            result = await self._execute(select + ";", **bound)
            rows = [dict(row) for row in result] if isinstance(result, list) else []
            if not rows:
                return
            cursor = rows[-1]["id"]
            for row in rows:
                for name in omit:
                    row.pop(name, None)
            if precise_datetimes:
                await self._capture_datetimes(rows, select, bound)
            yield rows
            if len(rows) < self._page_size:
                return

    async def _capture_datetimes(
        self, rows: list[dict[str, Any]], select: str, params: dict[str, Any]
    ) -> None:
        """Replace the SDK's truncated datetimes with the server's text for them.

        The page is selected again in the same transaction, so it is the same
        page, and only the datetime paths are stringified.
        """
        dates = datetime_paths({"page": rows})
        if not dates:
            return
        captured = await self._execute(
            f"RETURN {{ LET $document = {{ page: ({select}) }}; "
            "RETURN SELECT array::map($paths, |$path| type::string(type::field($path))) "
            "AS datetimes FROM ONLY $document; };",
            **params,
            paths=[path for _, _, path in dates],
        )
        texts = captured.get("datetimes") if isinstance(captured, dict) else None
        apply_datetime_texts(dates, texts)

    async def read_all(
        self,
        table: str,
        *,
        fields: str,
        where: str,
        params: dict[str, Any],
        omit: tuple[str, ...] = (),
        precise_datetimes: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            row
            async for page in self.pages(
                table,
                fields=fields,
                where=where,
                params=params,
                omit=omit,
                precise_datetimes=precise_datetimes,
            )
            for row in page
        ]


class SourceCapture:
    """One kind's integrity section: rows on disk, ledgers and digests in memory.

    It validates and digests exactly what ``build_integrity_archive`` would for
    the same rows, states and derivations, one row at a time.
    """

    def __init__(self, kind: SourceKind, organization_id: str, spool: ArchiveSpool) -> None:
        self.kind = kind
        self.organizations = [organization_id]
        self._org_field = source_table(kind)[1]
        self._rows = SpooledCollection(spool, (False, False))
        self.states: list[dict[str, Any]] = []
        self.derivations: list[dict[str, Any]] = []
        self.lineage = LineageSection(kind=kind, associations=self.derivations)
        self._validator: IntegrityRowValidator | None = None
        self._state_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        self._record_uuids: dict[str, str] = {}

    def begin(self, states: list[dict[str, Any]], derivations: list[dict[str, Any]]) -> None:
        self.states = _ordered(states, ("organization_id", "source_id"))
        self.derivations = _ordered(derivations, ("organization_id", "target_id"))
        self._validator = IntegrityRowValidator(
            kind=self.kind,
            organizations=self.organizations,
            states=self.states,
            derivations=self.derivations,
        )
        self.lineage = LineageSection(kind=self.kind, associations=self.derivations)
        self._state_by_key = {
            (state["organization_id"], state["source_id"]): state for state in self.states
        }

    def add_rows(self, rows: list[dict[str, Any]]) -> None:
        assert self._validator is not None
        for row in rows:
            self._validator.check(row)
            key = (row[self._org_field], row["uuid"])
            self.lineage.add_row(row, self._state_by_key[key])
            self._record_uuids[row["archive_record_key"]] = row["uuid"]
            self._rows.add(
                (order_value(row[self._org_field]), order_value(row["uuid"])),
                encode_record(row),
            )

    def finish(self) -> None:
        assert self._validator is not None
        self._validator.finish()

    def close(self) -> None:
        self._rows.spool.close()

    @property
    def row_count(self) -> int:
        return len(self._rows)

    @property
    def record_uuids(self) -> dict[str, str]:
        return self._record_uuids

    def encoded_rows(self, flagged: set[tuple[str, str]]) -> Iterator[dict[str, Any]]:
        for encoded in self._rows.ordered():
            record = encoded["record"]
            if (record[self._org_field], record["uuid"]) in flagged:
                record["derivation_required"] = True
            yield encoded

    def decoded_rows(self, flagged: set[tuple[str, str]]) -> Iterator[dict[str, Any]]:
        for encoded in self.encoded_rows(flagged):
            yield decode_record(encoded)

    def section(self, flagged: set[tuple[str, str]]) -> StreamedObject:
        """The integrity section, its digest taken as the rows are written."""
        digest = SectionDigest(
            kind=self.kind, organizations=self.organizations, derivations=self.derivations
        )

        def rows() -> Iterator[dict[str, Any]]:
            for encoded in self.encoded_rows(flagged):
                digest.add_row(encoded)
                yield encoded

        return StreamedObject(
            [
                ("version", INTEGRITY_ARCHIVE_VERSION),
                ("kind", self.kind.value),
                ("organizations", sorted(self.organizations)),
                ("source_rows", Streamed(rows())),
                ("source_states", self.states),
                ("derivations", self.derivations),
                ("sha256", Deferred(lambda: digest.finish(self.states))),
            ]
        )


def _ordered(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: tuple(order_value(row.get(field)) for field in fields))


async def read_source_capture(capture: SourceCapture, reader: PageReader) -> None:
    """Page a kind's ledger, derivations and rows into the capture.

    These are the statements of ``read_source_archive_snapshot``, one table at
    a time.
    """
    table, org_field = source_table(capture.kind)
    scope = {"organizations": capture.organizations, "kind": capture.kind.value}
    states = await reader.read_all(
        "source_states",
        fields="*",
        omit=("id",),
        where="organization_id IN $organizations AND source_kind = $kind",
        params=scope,
        precise_datetimes=True,
    )
    derivations = await reader.read_all(
        "memory_derivations",
        fields="*",
        omit=("id",),
        where="organization_id IN $organizations AND target_kind = $kind",
        params=scope,
        precise_datetimes=True,
    )
    capture.begin(states, derivations)
    async for page in reader.pages(
        table,
        fields="*, type::string(id) AS archive_record_key",
        omit=("id",),
        where=f"{org_field} IN $organizations",
        params={"organizations": capture.organizations},
        precise_datetimes=True,
    ):
        capture.add_rows(page)


def capture_snapshot_rows(capture: SourceCapture, snapshot: dict[str, Any]) -> None:
    """Feed a one-statement snapshot (an embedded engine's read) into the capture."""
    capture.begin(snapshot["source_states"], snapshot["derivations"])
    capture.add_rows(snapshot["source_rows"])


def spool_path(workdir: Path, name: str) -> Path:
    return workdir / f".{name}.spool"


GRAPH_ARCHIVE_VERSION = "3.0"


class GraphArchiveCapture:
    """One organization's graph backup, captured to a spool and written as graph.json.

    The document is the ``BackupData`` the in-memory ``create_backup`` returns
    after its own lineage seal, then sealed again beside the content capture,
    which is how the API backup has always written graph.json.
    """

    def __init__(self, organization_id: str, workdir: Path) -> None:
        self.organization_id = organization_id
        self._spool = ArchiveSpool(spool_path(workdir, "graph"))
        self.source = SourceCapture(SourceKind.GRAPH_ENTITY, organization_id, self._spool)
        # The orders of the companion snapshot statements.
        self._episodes = SpooledCollection(self._spool, (True, False))
        self._relationships = SpooledCollection(self._spool, (True, True, False))
        self._mentions = SpooledCollection(self._spool, (True, False))
        self._episode_uuids: dict[str, str] = {}
        self.created_at = ""
        self._standalone_flags: set[tuple[str, str]] = set()
        self._standalone_report: list[dict[str, str]] = []

    @property
    def lineage(self) -> LineageSection:
        return self.source.lineage

    @property
    def quarantined(self) -> list[dict[str, str]]:
        """Standalone quarantines, which the joint seal carries forward."""
        return [row for row in self._standalone_report if row.get("status") == "quarantined"]

    @property
    def entity_count(self) -> int:
        return self.source.row_count

    @property
    def relationship_count(self) -> int:
        return len(self._relationships)

    def add_episodes(self, rows: list[dict[str, Any]]) -> None:
        from sibyl_core.migrate.graph_companions import episode_payload

        for row in rows:
            self._episode_uuids[row["archive_record_key"]] = row["uuid"]
            self._episodes.add(
                (order_value(row.get("uuid")), order_value(row.get("archive_record_key"))),
                episode_payload(row, organization_id=self.organization_id),
            )

    def add_relationships(self, rows: list[dict[str, Any]]) -> None:
        from sibyl_core.migrate.graph_companions import relationship_payload

        for row in rows:
            self._relationships.add(
                (
                    order_value(row.get("created_at")),
                    order_value(row.get("uuid")),
                    order_value(row.get("archive_record_key")),
                ),
                relationship_payload(row),
            )

    def add_mentions(self, rows: list[dict[str, Any]]) -> None:
        from sibyl_core.migrate.graph_companions import mention_payload

        for row in rows:
            self._mentions.add(
                (order_value(row.get("uuid")), order_value(row.get("archive_record_key"))),
                mention_payload(row, self._record_uuid, organization_id=self.organization_id),
            )

    def _record_uuid(self, record_key: str) -> str:
        uuid = self.source.record_uuids.get(record_key)
        return uuid if uuid is not None else self._episode_uuids[record_key]

    def add_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Feed a one-statement snapshot (an embedded engine's read)."""
        capture_snapshot_rows(self.source, snapshot)
        auxiliary = snapshot["graph_auxiliary"]
        self.add_relationships(auxiliary["relates_to"])
        self.add_episodes(auxiliary["episode"])
        self.add_mentions(auxiliary["mentions"])

    def finish(self) -> None:
        """Validate the ledger and seal the graph alone, as ``create_backup`` does."""
        from sibyl_core.migrate.archive_lineage import seal_lineage

        self.source.finish()
        self._standalone_report = seal_lineage([self.lineage], [])
        self._standalone_flags = set(self.lineage.flagged)
        self.created_at = datetime.now(UTC).isoformat()

    def document(self, report: list[dict[str, str]]) -> StreamedObject:
        """graph.json after the joint seal that produced ``report``."""
        from sibyl_core.services.graph_records import entity_from_surreal_row

        entities = (
            entity_from_surreal_row(row).model_dump(mode="json")
            for row in self.source.decoded_rows(self._standalone_flags)
        )
        return StreamedObject(
            [
                ("version", GRAPH_ARCHIVE_VERSION),
                ("created_at", self.created_at),
                ("organization_id", self.organization_id),
                ("entity_count", self.entity_count),
                ("relationship_count", self.relationship_count),
                ("entities", Streamed(entities)),
                ("relationships", Streamed(self._relationships.ordered())),
                ("episode_count", len(self._episodes)),
                ("mention_count", len(self._mentions)),
                ("episodes", Streamed(self._episodes.ordered())),
                ("mentions", Streamed(self._mentions.ordered())),
                ("source_integrity", self.source.section(set(self.lineage.flagged))),
                (
                    "lineage_validation",
                    [row for row in report if row["source_kind"] == self.source.kind.value],
                ),
            ]
        )

    def write(self, path: Path, report: list[dict[str, str]]) -> int:
        return write_json_file(path, self.document(report))

    def close(self) -> None:
        self._spool.close()


async def read_graph_capture(capture: GraphArchiveCapture, reader: PageReader) -> None:
    """Page the graph's sources and companions into the capture.

    Episodes are read before mentions, whose endpoints may name them.
    """
    await read_source_capture(capture.source, reader)
    scope = {"organizations": capture.source.organizations}
    async for page in reader.pages(
        "relates_to",
        fields="*, type::string(id) AS archive_record_key, "
        "type::string(in) AS source_record_key, type::string(out) AS target_record_key",
        omit=("id", "in", "out"),
        where="group_id IN $organizations",
        params=scope,
        precise_datetimes=True,
    ):
        capture.add_relationships(page)
    async for page in reader.pages(
        "episode",
        fields="*, type::string(id) AS archive_record_key",
        omit=("id",),
        where="group_id IN $organizations",
        params=scope,
        precise_datetimes=True,
    ):
        capture.add_episodes(page)
    async for page in reader.pages(
        "mentions",
        fields="*, type::string(id) AS archive_record_key, "
        "type::string(in) AS source_record_key, type::string(out) AS target_record_key",
        omit=("id", "in", "out"),
        where="group_id IN $organizations",
        params=scope,
        precise_datetimes=True,
    ):
        capture.add_mentions(page)
