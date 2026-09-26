"""One table decides how Sibyl treats every SurrealDB URL scheme.

Scheme checks used to live in half a dozen lists that disagreed:
- mem:// was on none of them, so it got a remote pool and auth.
- surrealkv+versioned:// and file:// failed embedded sign-in.
- the production validators only knew memory:// and surrealkv://.
- rocksdb://, which the SDK rejects, sat on every list.

Every check now goes through sibyl_core.backends.surreal.url_schemes, and
this table pins what each scheme gets.
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
    try:
        CoreConfig(
            environment="production",
            surreal_url=url,
            surreal_data_dir="",
            allow_embedded_single_writer=opt_in,
        )
    except ValueError as exc:
        return str(exc)
    return _OK


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
        ("localhost:8000", "is not supported"),
    ],
)
def test_urls_the_sdk_cannot_open_fail_at_configuration(url: str, hint: str) -> None:
    assert not url_schemes.is_embedded_surreal_url(url)
    reason = url_schemes.unsupported_surreal_url_reason(url)
    assert reason is not None
    assert hint in reason
    for environment in ("development", "production"):
        with pytest.raises(ValueError, match="not"):
            CoreConfig(environment=environment, surreal_url=url, surreal_data_dir="")


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
