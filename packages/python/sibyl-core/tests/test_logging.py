from __future__ import annotations

import pytest
import structlog

from sibyl_core.logging.config import configure_logging, get_logger


@pytest.fixture(autouse=True)
def _restore_structlog_config():
    # configure_logging installs a level-filtering wrapper globally; later
    # tests in the session must not inherit whichever level ran last here.
    previous = structlog.get_config()
    yield
    structlog.configure(**previous)


def test_configure_logging_respects_force_color(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)

    configure_logging(service_name="api", level="INFO")
    get_logger().info("color_probe", ok=True)

    output = capsys.readouterr().out
    assert "\x1b[" in output
    assert "color_probe" in output


def test_force_color_overrides_no_color(monkeypatch, capsys) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")

    configure_logging(service_name="api", level="INFO")
    get_logger().info("color_probe", ok=True)

    output = capsys.readouterr().out
    assert "\x1b[" in output
    assert "color_probe" in output


def test_no_color_disables_auto_color_without_force(monkeypatch, capsys) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")

    configure_logging(service_name="api", level="INFO")
    get_logger().info("color_probe", ok=True)

    output = capsys.readouterr().out
    assert "\x1b[" not in output
    assert "color_probe" in output


def test_events_below_the_configured_level_are_not_rendered(capsys) -> None:
    # One debug event per SurrealDB query used to be rendered and printed at
    # INFO; the level now gates the processor chain itself.
    configure_logging(service_name="api", level="INFO", colors=False)
    log = structlog.get_logger()

    log.debug("surreal_query_complete", elapsed_ms=0.3)
    log.info("request_complete", path="/api/tasks")

    output = capsys.readouterr().out
    assert "surreal_query_complete" not in output
    assert "request_complete" in output


def test_debug_level_renders_debug_events(capsys) -> None:
    configure_logging(service_name="api", level="DEBUG", colors=False)

    structlog.get_logger().debug("surreal_query_complete", elapsed_ms=0.3)

    assert "surreal_query_complete" in capsys.readouterr().out


def test_level_defaults_from_the_environment(monkeypatch, capsys) -> None:
    monkeypatch.setenv("SIBYL_LOG_LEVEL", "WARNING")
    configure_logging(service_name="api", colors=False)
    log = structlog.get_logger()

    log.info("quiet")
    log.warning("loud")

    output = capsys.readouterr().out
    assert "quiet" not in output
    assert "loud" in output
