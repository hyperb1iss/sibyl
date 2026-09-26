"""One table decides how Sibyl treats every SurrealDB URL scheme.

Scheme checks used to live in half a dozen lists that disagreed:
- mem:// was on none of them, so it got a remote pool and auth.
- surrealkv+versioned:// and file:// failed embedded sign-in.
- the production validators only knew memory:// and surrealkv://.
- rocksdb://, which the SDK rejects, sat on every list.

Every check now goes through sibyl_core.backends.surreal.url_schemes, and
this table pins what each scheme gets.

CoreConfig itself never refuses a URL: the client CLI imports it, and a
server URL it never opens must not break `sibyl --help`. The verdicts here
come from surreal_url_problem(), which core code checks where it opens a
store; the server's Settings refuse the same URLs at startup.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from sibyl_core.backends.surreal import url_schemes
from sibyl_core.backends.surreal.dedicated_client import DedicatedSurrealClient
from sibyl_core.config import CoreConfig

pytest.importorskip("surrealdb")


@dataclass(frozen=True)
class Scheme:
    url: str
    embedded: bool
    memory: bool
    file_backed: bool
    # Production verdicts: without and with SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER.
    production: str
    production_with_opt_in: str


_MEMORY_BAN = "In-memory SurrealDB is forbidden in production"
_OPT_IN = "single-writer opt-in"
_OK = "accepted"

SCHEMES = [
    Scheme("memory://", True, True, False, _MEMORY_BAN, _MEMORY_BAN),
    Scheme("mem://", True, True, False, _MEMORY_BAN, _MEMORY_BAN),
    Scheme("surrealkv:///var/sibyl", True, False, True, _OPT_IN, _OK),
    Scheme("surrealkv+versioned:///var/sibyl", True, False, True, _OPT_IN, _OK),
    Scheme("file:///var/sibyl", True, False, True, _OPT_IN, _OK),
    # The SDK lowercases the scheme, so Sibyl must too.
    Scheme("SurrealKV:///var/sibyl", True, False, True, _OPT_IN, _OK),
    Scheme("ws://surreal:8000/rpc", False, False, False, _OK, _OK),
    Scheme("wss://surreal.example.com/rpc", False, False, False, _OK, _OK),
    Scheme("http://surreal:8000", False, False, False, _OK, _OK),
    Scheme("https://surreal.example.com", False, False, False, _OK, _OK),
]


def _production_verdict(url: str, *, opt_in: bool) -> str:
    config = CoreConfig(
        environment="production",
        surreal_url=url,
        surreal_data_dir="",
        allow_embedded_single_writer=opt_in,
    )
    return config.surreal_url_problem() or _OK


@pytest.mark.parametrize("scheme", SCHEMES, ids=lambda scheme: scheme.url.split("://")[0])
def test_every_scheme_gets_one_consistent_treatment(scheme: Scheme) -> None:
    url = scheme.url
    assert url_schemes.is_embedded_surreal_url(url) is scheme.embedded
    assert url_schemes.is_memory_surreal_url(url) is scheme.memory
    assert url_schemes.is_file_backed_surreal_url(url) is scheme.file_backed
    assert url_schemes.unsupported_surreal_url_reason(url) is None

    client = DedicatedSurrealClient(url=url, namespace="org_scheme", database="graph", pool_size=8)
    # Embedded engines hold one connection and never sign in; servers get the
    # configured pool and authenticate.
    assert client.pool_size == (1 if scheme.embedded else 8)
    assert client._pool[0]._requires_auth() is (not scheme.embedded)
    # Only file-backed stores share a process-wide engine.
    assert client._pool[0]._url == url
    from sibyl_core.backends.surreal.dedicated_client import _shares_embedded_engine

    assert _shares_embedded_engine(url) is scheme.file_backed

    assert scheme.production in _production_verdict(url, opt_in=False)
    assert scheme.production_with_opt_in in _production_verdict(url, opt_in=True)


@pytest.mark.parametrize(
    ("url", "hint"),
    [
        ("rocksdb:///var/sibyl", "storage argument for `surreal start`"),
        ("tikv://pd:2379", "tikv:// is not supported"),
        ("localhost:8000", "has no scheme"),
        ("surrealdb://surreal:8000", "surrealdb:// is not supported"),
    ],
)
def test_urls_the_sdk_cannot_open_are_refused_before_a_store_opens(
    monkeypatch, url: str, hint: str
) -> None:
    assert not url_schemes.is_embedded_surreal_url(url)
    reason = url_schemes.unsupported_surreal_url_reason(url)
    assert reason is not None
    assert hint in reason
    for environment in ("development", "production"):
        # Loading the config never fails on the URL...
        config = CoreConfig(environment=environment, surreal_url=url, surreal_data_dir="")
        # ...but nothing opens a store on it.
        with pytest.raises(ValueError, match=hint.split("`")[0].strip()):
            config.require_serviceable_surreal_url()

    from sibyl_core.config import core_config
    from sibyl_core.services import content_client, graph_client

    monkeypatch.setattr(core_config, "surreal_url", url)
    monkeypatch.setattr(core_config, "surreal_data_dir", "")
    with pytest.raises(ValueError, match="Supported: "):
        graph_client._new_graph_client("org_refused")
    with pytest.raises(ValueError, match="Supported: "):
        content_client.build_surreal_content_client()


def test_scheme_errors_never_echo_the_url_beyond_its_scheme() -> None:
    # A URL can carry credentials; only its scheme may reach a log line.
    for url in ("tikv://admin:hunter2@pd:2379", "admin:hunter2@surreal:8000"):
        reason = url_schemes.unsupported_surreal_url_reason(url)
        assert reason is not None
        assert "hunter2" not in reason


def test_scheme_sets_match_the_sdk(tmp_path) -> None:
    """If an SDK bump adds or drops a scheme, this table has to be revisited."""
    from surrealdb import AsyncSurreal
    from surrealdb.connections.async_embedded import AsyncEmbeddedSurrealConnection
    from surrealdb.connections.url import UrlScheme

    assert {scheme.value for scheme in UrlScheme} == url_schemes.SUPPORTED_SCHEMES
    # Constructing a file-backed client opens its store, so every candidate
    # URL points into tmp_path rather than at a relative path.
    embedded_by_sdk = {
        scheme.value
        for scheme in UrlScheme
        if isinstance(
            AsyncSurreal(f"{scheme.value}://{tmp_path / scheme.value.replace('+', '_')}"),
            AsyncEmbeddedSurrealConnection,
        )
    }
    assert embedded_by_sdk == url_schemes.EMBEDDED_SCHEMES
    # rocksdb:// is refused by the SDK itself, which is why config refuses it.
    with pytest.raises(ValueError, match="rocksdb"):
        AsyncSurreal(f"rocksdb://{tmp_path / 'rocksdb'}")
