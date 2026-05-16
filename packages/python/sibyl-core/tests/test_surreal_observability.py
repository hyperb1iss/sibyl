from __future__ import annotations

from typing import Any

from sibyl_core.backends.surreal import observability
from sibyl_core.backends.surreal.driver import SurrealQueryError
from sibyl_core.observability import telemetry_registry


class FakeLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def warning(self, event: str, **fields: Any) -> None:
        self.events.append(("warning", event, fields))

    def debug(self, event: str, **fields: Any) -> None:
        self.events.append(("debug", event, fields))


def test_slow_query_log_uses_safe_fingerprint(monkeypatch) -> None:
    fake_log = FakeLog()
    monkeypatch.setattr(observability, "log", fake_log)
    monkeypatch.setattr(observability, "_slow_query_threshold_ms", lambda: 500.0)

    observability.log_query(
        "LET $document_ids = ("
        "SELECT VALUE uuid FROM crawled_documents WHERE source_id INSIDE $source_ids"
        ");"
        "SELECT * FROM ("
        "SELECT * FROM document_chunks WHERE content @0@ $search_query"
        ") WHERE score > 0;",
        client_kind="content",
        namespace="sibyl_content",
        database="content",
        raw=True,
        elapsed=1200.12,
        retry_count=1,
    )

    level, event, fields = fake_log.events[0]
    assert level == "warning"
    assert event == "surreal_query_slow"
    assert fields == {
        "client": "content",
        "namespace": "sibyl_content",
        "database": "content",
        "raw": True,
        "elapsed_ms": 1200.12,
        "retry_count": 1,
        "statement": "let_select",
        "statement_count": 2,
        "tables": ["crawled_documents", "document_chunks"],
        "query_hash": fields["query_hash"],
    }
    assert len(fields["query_hash"]) == 12
    assert "query" not in fields
    assert "params" not in fields
    assert "search_query" not in fields


def test_fast_query_logs_at_debug(monkeypatch) -> None:
    fake_log = FakeLog()
    monkeypatch.setattr(observability, "log", fake_log)
    monkeypatch.setattr(observability, "_slow_query_threshold_ms", lambda: 500.0)

    observability.log_query(
        "SELECT * FROM crawl_sources;",
        client_kind="content",
        namespace="sibyl_content",
        database="content",
        raw=False,
        elapsed=12.4,
    )

    level, event, fields = fake_log.events[0]
    assert level == "debug"
    assert event == "surreal_query_complete"
    assert fields["statement"] == "select"
    assert fields["tables"] == ["crawl_sources"]


def test_fast_schema_define_query_is_quiet(monkeypatch) -> None:
    fake_log = FakeLog()
    monkeypatch.setattr(observability, "log", fake_log)
    monkeypatch.setattr(observability, "_slow_query_threshold_ms", lambda: 500.0)

    observability.log_query(
        "DEFINE TABLE IF NOT EXISTS users SCHEMAFULL;",
        client_kind="auth",
        namespace="sibyl_auth",
        database="auth",
        raw=False,
        elapsed=0.7,
    )

    assert fake_log.events == []


def test_fast_schema_alter_query_is_quiet(monkeypatch) -> None:
    fake_log = FakeLog()
    monkeypatch.setattr(observability, "log", fake_log)
    monkeypatch.setattr(observability, "_slow_query_threshold_ms", lambda: 500.0)

    observability.log_query(
        "ALTER TABLE IF EXISTS users SCHEMAFULL;",
        client_kind="auth",
        namespace="sibyl_auth",
        database="auth",
        raw=False,
        elapsed=0.7,
    )

    assert fake_log.events == []


def test_slow_schema_define_query_still_warns(monkeypatch) -> None:
    fake_log = FakeLog()
    monkeypatch.setattr(observability, "log", fake_log)
    monkeypatch.setattr(observability, "_slow_query_threshold_ms", lambda: 500.0)

    observability.log_query(
        "DEFINE TABLE IF NOT EXISTS users SCHEMAFULL;",
        client_kind="auth",
        namespace="sibyl_auth",
        database="auth",
        raw=False,
        elapsed=1200.0,
    )

    level, event, fields = fake_log.events[0]
    assert level == "warning"
    assert event == "surreal_query_slow"
    assert fields["statement"] == "define"


def test_failed_query_logs_warning(monkeypatch) -> None:
    fake_log = FakeLog()
    monkeypatch.setattr(observability, "log", fake_log)

    observability.log_query(
        "UPDATE system_settings SET value = $value;",
        client_kind="auth",
        namespace="sibyl_auth",
        database="auth",
        raw=False,
        elapsed=3.2,
        error=RuntimeError("boom"),
    )

    level, event, fields = fake_log.events[0]
    assert level == "warning"
    assert event == "surreal_query_failed"
    assert fields["error_type"] == "RuntimeError"
    assert fields["error_category"] == "query_error"
    assert len(fields["error_hash"]) == 12
    assert fields["error_length"] == 4
    assert fields["statement"] == "update"
    assert fields["tables"] == ["system_settings"]


def test_failed_query_log_does_not_include_surreal_query_error_text(monkeypatch) -> None:
    fake_log = FakeLog()
    monkeypatch.setattr(observability, "log", fake_log)
    query = "SELECT * FROM private_notes WHERE content = 'secret literal token';"

    observability.log_query(
        query,
        client_kind="graph",
        namespace="org_secret",
        database="graph",
        raw=False,
        elapsed=3.2,
        error=SurrealQueryError(query, "bad query"),
    )

    _level, event, fields = fake_log.events[0]
    assert event == "surreal_query_failed"
    assert fields["error_type"] == "SurrealQueryError"
    assert fields["error_category"] == "query_error"
    assert len(fields["error_hash"]) == 12
    assert fields["error_length"] == len("bad query")
    assert "error" not in fields
    assert "secret literal token" not in str(fields)
    assert query not in str(fields)


def test_failed_query_log_classifies_safe_surreal_errors(monkeypatch) -> None:
    fake_log = FakeLog()
    monkeypatch.setattr(observability, "log", fake_log)

    observability.log_query(
        "DEFINE FIELD attributes ON entity TYPE object FLEXIBLE DEFAULT {};",
        client_kind="graph",
        namespace="org_safe",
        database="graph",
        raw=False,
        elapsed=0.4,
        error=SurrealQueryError(
            "DEFINE FIELD attributes ON entity TYPE object FLEXIBLE DEFAULT {};",
            "Parse error: FLEXIBLE must be specified after TYPE",
        ),
    )

    _level, event, fields = fake_log.events[0]
    assert event == "surreal_query_failed"
    assert fields["error_category"] == "parse_error"
    assert len(fields["error_hash"]) == 12
    assert "FLEXIBLE must be specified" not in str(fields)


def test_log_query_records_runtime_telemetry(monkeypatch) -> None:
    fake_log = FakeLog()
    registry = telemetry_registry()
    registry.reset()
    monkeypatch.setattr(observability, "log", fake_log)
    monkeypatch.setattr(observability, "_slow_query_threshold_ms", lambda: 500.0)

    observability.log_query(
        "SELECT * FROM raw_captures WHERE title = 'secret';",
        client_kind="content",
        namespace="sibyl_content",
        database="content",
        raw=False,
        elapsed=750.0,
        retry_count=2,
    )

    snapshot = registry.snapshot()

    assert snapshot["summaries"]["surreal"]["count"] == 1
    assert snapshot["summaries"]["surreal"]["slow"] == 1
    assert "secret" not in str(snapshot)
    assert any(
        metric["name"] == "sibyl_surreal_query_retries_total" for metric in snapshot["metrics"]
    )
