"""A `curl` stand-in that answers like the SurrealDB 3.x HTTP API.

The surrealdb chart's ops jobs are shell scripts that only reach the
database through curl. Putting this script first on PATH lets a test run
the rendered job scripts unchanged, with jq and sha256sum doing the real
work, against servers described in a JSON state file
(``FAKE_SURREAL_STATE``):

    {"servers": {"http://source": {"user": "root:secret",
                                   "version": "surrealdb-3.2.4",
                                   "namespaces": {"ns": {"db": {"table": rows}}}}},
     "empty_exports": ["ns/db"], "drop_on_import": ["ns/db"],
     "sql_errors": ["ns/db"], "refuse_import": ["ns/db"],
     "drop_tables_on_import": ["ns/db/table"],
     "shrink_on_import": {"ns/db/table": rows},
     "ns_info_errors": ["ns"], "org_check_error": false,
     "change_before_export": {"ns/db/table": rows or null},
     "recount_errors": ["ns/db"]}

A source server may also carry "org_uuids": [...], the organizations the
export's read-only organization check finds in the auth database.
``change_before_export`` is a write landing between the export's first row
count and its snapshot: the table takes the new row count (null drops it)
just before ``/export`` reads it, so the snapshot and the export's second
count both see the change and the first count does not.
``recount_errors`` refuses the row count for ``ns/db`` once that database
has been exported, so only the export's second count fails.

Response shapes mirror a real v3.2.4 server: ``/sql`` answers HTTP 200
with one ``{"status", "result"}`` entry per statement, ``/import`` answers
``[]`` on success, and ``/export`` of a database that does not exist
still succeeds with only the ``OPTION IMPORT`` header. Identifiers in
DEFINE/USE statements arrive backtick-quoted, as the drill sends them.

Every request must carry --connect-timeout and --max-time: the jobs bound
every call, and the fake refuses one that is not bounded.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

EXPORT_HEADER = "-- ------------------------------\n-- OPTION\n-- ------------------------------\n\nOPTION IMPORT;\n"
TABLES_MARKER = "-- fake-surreal-tables "
TABLE_ROWS_QUERY_PREFIX = "RETURN object::keys((INFO FOR DB).tables)"
_VALUE_FLAGS = {
    "-u",
    "-H",
    "--data-binary",
    "-T",
    "-o",
    "-X",
    "-w",
    "--connect-timeout",
    "--max-time",
}
_IDENT = r"`((?:[^`\\]|\\.)*)`"


class Request:
    def __init__(self, argv: list[str]) -> None:
        self.headers: dict[str, str] = {}
        self.user = ""
        self.body: str | None = None
        self.upload: str | None = None
        self.upload_path: str | None = None
        self.output: str | None = None
        self.fail = False
        self.url = ""
        self.bounded: set[str] = set()
        index = 0
        while index < len(argv):
            flag = argv[index]
            if flag in _VALUE_FLAGS:
                self._take(flag, argv[index + 1])
                index += 2
                continue
            if flag.startswith("--"):
                sys.exit(f"fake curl: unsupported flag {flag}")
            if flag.startswith("-"):
                self.fail = self.fail or "f" in flag
            else:
                self.url = flag
            index += 1

    def _take(self, flag: str, value: str) -> None:
        if flag == "-H":
            name, _, header_value = value.partition(":")
            self.headers[name.strip().lower()] = header_value.strip()
        elif flag == "-u":
            self.user = value
        elif flag == "--data-binary":
            self.body = Path(value[1:]).read_text() if value.startswith("@") else value
        elif flag == "-T":
            self.upload = Path(value).read_text()
            self.upload_path = value
        elif flag == "-o":
            self.output = value
        elif flag in {"--connect-timeout", "--max-time"}:
            self.bounded.add(flag)


def _ok(result: object) -> dict[str, object]:
    return {"status": "OK", "result": result, "time": "1µs", "type": None}


def _err(message: str) -> dict[str, object]:
    return {"status": "ERR", "result": message, "time": "1µs", "type": None}


type Namespaces = dict[str, dict[str, dict[str, int]]]


def _info(namespaces: Namespaces, ns: str, statement: str) -> dict[str, object] | None:
    if statement == "INFO FOR ROOT":
        defined = {name: f"DEFINE NAMESPACE {name}" for name in namespaces}
        return _ok({"namespaces": defined, "users": {}, "accesses": {}})
    if statement != "INFO FOR NS":
        return None
    if ns not in namespaces:
        return _err(f"The namespace '{ns}' does not exist")
    defined = {name: f"DEFINE DATABASE {name}" for name in namespaces[ns]}
    return _ok({"databases": defined, "users": {}, "accesses": {}})


def _unquote(quoted: str) -> str:
    return re.sub(r"\\(.)", r"\1", quoted)


def _define(
    namespaces: Namespaces, ns: str, statement: str, strict: list[str]
) -> dict[str, object] | None:
    if match := re.fullmatch(rf"DEFINE NAMESPACE IF NOT EXISTS {_IDENT}", statement):
        namespaces.setdefault(_unquote(match.group(1)), {})
        return _ok(None)
    if match := re.fullmatch(rf"DEFINE DATABASE IF NOT EXISTS {_IDENT}( STRICT)?", statement):
        database = _unquote(match.group(1))
        namespaces.setdefault(ns, {}).setdefault(database, {})
        if match.group(2) and f"{ns}/{database}" not in strict:
            strict.append(f"{ns}/{database}")
        return _ok(None)
    return None


def _count(namespaces: Namespaces, ns: str, db: str, statement: str) -> dict[str, object] | None:
    match = re.fullmatch(r"SELECT count\(\) AS count FROM (\w+) GROUP ALL", statement)
    if match is None:
        return None
    tables = namespaces.get(ns, {}).get(db, {})
    if match.group(1) not in tables:
        return _err(f"The table '{match.group(1)}' does not exist")
    return _ok([{"count": tables[match.group(1)]}])


def _statement(
    namespaces: Namespaces, ns: str, db: str, statement: str, strict: list[str]
) -> dict[str, object]:
    return (
        _info(namespaces, ns, statement)
        or _define(namespaces, ns, statement, strict)
        or _count(namespaces, ns, db, statement)
        or _err(f"fake surreal: unsupported statement {statement!r}")
    )


def _injected(
    state: dict[str, Any], server: dict[str, Any], ns: str, db: str, body: str
) -> list[dict[str, object]] | None:
    """Faults the test asked for, and the organization check's query."""
    if body.startswith(TABLE_ROWS_QUERY_PREFIX) and f"{ns}/{db}" in state.get("sql_errors", []):
        return [_err("fake surreal: the query was refused")]
    if (
        body.startswith(TABLE_ROWS_QUERY_PREFIX)
        and f"{ns}/{db}" in state.get("recount_errors", [])
        and f"{ns}/{db}" in state.get("exported", [])
    ):
        return [_err("fake surreal: the recount was refused")]
    if body == "INFO FOR NS;" and ns in state.get("ns_info_errors", []):
        return [_err("fake surreal: INFO FOR NS was refused")]
    if body != "SELECT VALUE uuid FROM organizations;":
        return None
    if state.get("org_check_error"):
        return [_err("The table 'organizations' does not exist")]
    return [_ok(server.get("org_uuids", []))]


def _sql(
    state: dict[str, Any], server: dict[str, Any], request: Request
) -> list[dict[str, object]]:
    namespaces: Namespaces = server.setdefault("namespaces", {})
    strict: list[str] = server.setdefault("strict", [])
    ns = request.headers.get("surreal-ns", "")
    db = request.headers.get("surreal-db", "")
    body = (request.body or "").strip()
    injected = _injected(state, server, ns, db, body)
    if injected is not None:
        return injected
    if body.startswith(TABLE_ROWS_QUERY_PREFIX):
        tables = namespaces.get(ns, {}).get(db)
        if tables is None:
            return [_err(f"The database '{db}' does not exist")]
        return [_ok([{"table": name, "rows": rows} for name, rows in sorted(tables.items())])]

    results: list[dict[str, object]] = []
    for statement in (part.strip() for part in body.split(";")):
        if match := re.fullmatch(rf"USE NS {_IDENT}", statement):
            ns = _unquote(match.group(1))
            results.append(_ok({"namespace": ns, "database": None}))
        elif statement:
            results.append(_statement(namespaces, ns, db, statement, strict))
    return results


def _export(state: dict[str, Any], server: dict[str, Any], request: Request) -> str:
    namespace = request.headers.get("surreal-ns", "")
    database = request.headers.get("surreal-db", "")
    if f"{namespace}/{database}" in state.get("empty_exports", []):
        return ""
    tables = server.get("namespaces", {}).get(namespace, {}).get(database)
    if tables is None:
        return EXPORT_HEADER
    state.setdefault("exported", []).append(f"{namespace}/{database}")
    for key, rows in state.get("change_before_export", {}).items():
        changed_ns, changed_db, table = key.split("/")
        if (changed_ns, changed_db) != (namespace, database):
            continue
        if rows is None:
            tables.pop(table, None)
        else:
            tables[table] = rows
    return f"{EXPORT_HEADER}\n{TABLES_MARKER}{json.dumps(tables, sort_keys=True)}\n"


def _import(state: dict[str, Any], server: dict[str, Any], request: Request) -> object:
    namespace = request.headers.get("surreal-ns", "")
    database = request.headers.get("surreal-db", "")
    payload = request.upload if request.upload is not None else request.body or ""
    if "OPTION IMPORT;" not in payload:
        return {"code": 400, "information": "Import requires `OPTION IMPORT;`"}
    namespaces = server.setdefault("namespaces", {})
    if namespace not in namespaces:
        return [_err(f"The namespace '{namespace}' does not exist")]
    if database not in namespaces[namespace]:
        return [_err(f"The database '{database}' does not exist")]
    tables: dict[str, int] = {}
    for line in payload.splitlines():
        if line.startswith(TABLES_MARKER):
            tables = json.loads(line[len(TABLES_MARKER) :])
    key = f"{namespace}/{database}"
    if key in state.get("refuse_import", []):
        return [_err("fake surreal: the import was refused")]
    if key in state.get("drop_on_import", []):
        tables = dict.fromkeys(tables, 0)
    for table in list(tables):
        if f"{key}/{table}" in state.get("drop_tables_on_import", []):
            del tables[table]
        shrunk = state.get("shrink_on_import", {}).get(f"{key}/{table}")
        if shrunk is not None:
            tables[table] = shrunk
    namespaces[namespace][database] = tables
    if request.upload_path is not None:
        siblings = len(list(Path(request.upload_path).parent.glob("*.surql")))
        state["max_import_siblings"] = max(state.get("max_import_siblings", 0), siblings)
    return []


def main(argv: list[str]) -> int:
    request = Request(argv)
    state_path = Path(os.environ["FAKE_SURREAL_STATE"])
    state: dict[str, Any] = json.loads(state_path.read_text())
    base, _, path = request.url.partition("://")
    host, _, path = path.partition("/")
    server = state["servers"].get(f"{base}://{host}")
    if server is None:
        sys.stderr.write(f"curl: (7) Failed to connect to {host}\n")
        return 7

    route = f"/{path}"
    if request.bounded != {"--connect-timeout", "--max-time"}:
        sys.stderr.write(f"fake curl: unbounded request to {route}\n")
        return 2
    if route == "/health":
        body: object = ""
    elif route == "/version":
        body = server.get("version", "surrealdb-3.2.4")
    elif request.user != server.get("user"):
        body = {"code": 401, "information": "There was a problem with authentication"}
        if request.fail:
            sys.stderr.write("curl: (22) The requested URL returned error: 401\n")
            return 22
    elif route == "/sql":
        body = _sql(state, server, request)
    elif route == "/export":
        body = _export(state, server, request)
    elif route == "/import":
        body = _import(state, server, request)
    else:
        sys.stderr.write(f"fake curl: unsupported route {route}\n")
        return 2

    state_path.write_text(json.dumps(state))
    text = body if isinstance(body, str) else json.dumps(body)
    if request.output:
        Path(request.output).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
