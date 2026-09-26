"""The client CLI starts whatever server URL the environment carries.

`sibyl` imports sibyl-core, whose config loads at import. A SurrealDB URL
that only the server would open (a `surreal start` argument, an in-memory
store in production, a file store without the single-writer opt-in) must not
stop `sibyl --help` or `sibyl version`, or every agent hook on that machine
fails with it. The server's own Settings refuse those URLs at startup.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_SECRET = "sk-live-cli-0123456789abcdefsecret"


def _run_sibyl(
    home: Path, env_overrides: dict[str, str], *args: str
) -> subprocess.CompletedProcess:
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "SIBYL_OPENAI_API_KEY": _SECRET,
        **env_overrides,
    }
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    return subprocess.run(
        [sys.executable, "-c", "from sibyl_cli.entrypoint import main; main()", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    "env_overrides",
    [
        {"SIBYL_SURREAL_URL": "rocksdb:///data/sibyl.db"},
        {"SIBYL_SURREAL_URL": "tikv://pd:2379"},
        {"SIBYL_ENVIRONMENT": "production"},
        {"SIBYL_ENVIRONMENT": "production", "SIBYL_SURREAL_URL": "mem://"},
        {"SIBYL_ENVIRONMENT": "production", "SIBYL_SURREAL_URL": "file:///data/sibyl"},
        {"SIBYL_ENVIRONMENT": "production", "SIBYL_SURREAL_DATA_DIR": "/data/sibyl"},
        {"SIBYL_SURREAL_URL": "ws://surreal:8000/rpc", "SIBYL_SURREAL_DATA_DIR": "/data/sibyl"},
    ],
    ids=["rocksdb", "tikv", "prod-default", "prod-mem", "prod-file", "prod-data-dir", "both"],
)
@pytest.mark.parametrize("args", [("--help",), ("version",)], ids=["help", "version"])
def test_cli_starts_with_a_server_url_it_does_not_open(
    tmp_path: Path, env_overrides: dict[str, str], args: tuple[str, ...]
) -> None:
    result = _run_sibyl(tmp_path, env_overrides, *args)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ValidationError" not in result.stderr
    assert _SECRET not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("surreal_url", "sql_url"),
    [
        ("ws://surreal:8000/rpc", "http://surreal:8000/sql"),
        ("WS://surreal:8000/rpc", "http://surreal:8000/sql"),
        ("wss://surreal.example.com/rpc", "https://surreal.example.com/sql"),
        ("Wss://surreal.example.com/rpc", "https://surreal.example.com/sql"),
        ("http://surreal:8000", "http://surreal:8000/sql"),
        # Userinfo never reaches the request URL, so an HTTP error quoting it
        # cannot carry the password; credentials travel only through auth.
        ("ws://admin:Hunter2@surreal:8000/rpc", "http://surreal:8000/sql"),
        ("wss://admin:Hunter2@[2001:db8::1]:8443/rpc", "https://[2001:db8::1]:8443/sql"),
    ],
)
def test_migrate_reads_the_sql_endpoint_whatever_the_scheme_case(
    monkeypatch, surreal_url: str, sql_url: str
) -> None:
    from sibyl_cli import migrate

    posted: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict[str, object]]:
            return [{"status": "OK", "result": []}]

    def fake_post(url: str, **_kwargs: object) -> Response:
        posted.append(url)
        return Response()

    monkeypatch.setattr(migrate.httpx, "post", fake_post)
    migrate._source_sql(surreal_url=surreal_url, username="u", password="p", statement="RETURN 1;")

    assert posted == [sql_url]


@pytest.mark.parametrize(
    "surreal_url",
    [
        "ws:///Admin:Hunter2@host:8000/rpc",
        "surrealkv:///srv/Hunter2",
        "Admin:Hunter2@h:8000?x=ws://y",
    ],
)
def test_migrate_refuses_a_non_server_source_without_echoing_it(surreal_url: str) -> None:
    from sibyl_cli import migrate

    with pytest.raises(ValueError, match="must be a server URL") as caught:
        migrate._source_sql(
            surreal_url=surreal_url, username="u", password="p", statement="RETURN 1;"
        )

    assert "hunter2" not in str(caught.value).lower()
