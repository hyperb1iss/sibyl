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
    websocket: bool
    # Production verdicts: without and with SIBYL_ALLOW_EMBEDDED_SINGLE_WRITER.
    production: str
    production_with_opt_in: str


_MEMORY_BAN = "In-memory SurrealDB is forbidden in production"
_OPT_IN = "single-writer opt-in"
_OK = "accepted"

SCHEMES = [
    Scheme("memory://", True, True, False, False, _MEMORY_BAN, _MEMORY_BAN),
    Scheme("mem://", True, True, False, False, _MEMORY_BAN, _MEMORY_BAN),
    Scheme("surrealkv:///var/sibyl", True, False, True, False, _OPT_IN, _OK),
    Scheme("surrealkv+versioned:///var/sibyl", True, False, True, False, _OPT_IN, _OK),
    Scheme("file:///var/sibyl", True, False, True, False, _OPT_IN, _OK),
    # Schemes are case-insensitive. The SDK's embedded engine rejects an
    # uppercase one, so the client lowercases it before the SDK sees it.
    Scheme("SURREALKV:///var/sibyl", True, False, True, False, _OPT_IN, _OK),
    Scheme("MEMORY://", True, True, False, False, _MEMORY_BAN, _MEMORY_BAN),
    Scheme("ws://surreal:8000/rpc", False, False, False, True, _OK, _OK),
    Scheme("WS://surreal:8000/rpc", False, False, False, True, _OK, _OK),
    Scheme("wss://surreal.example.com/rpc", False, False, False, True, _OK, _OK),
    Scheme("http://surreal:8000", False, False, False, False, _OK, _OK),
    Scheme("https://surreal.example.com", False, False, False, False, _OK, _OK),
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

    assert url_schemes.is_websocket_surreal_url(url) is scheme.websocket

    client = DedicatedSurrealClient(url=url, namespace="org_scheme", database="graph", pool_size=8)
    # The client hands the SDK a lowercase scheme and keeps the rest verbatim.
    scheme_part, _, rest = url.partition("://")
    assert client._url == f"{scheme_part.lower()}://{rest}"
    assert client._pool[0]._url == client._url
    # Embedded engines hold one connection and never sign in; servers get the
    # configured pool and authenticate.
    assert client.pool_size == (1 if scheme.embedded else 8)
    assert client._pool[0]._requires_auth() is (not scheme.embedded)
    assert client.supports_live_queries is scheme.websocket
    # Only file-backed stores share a process-wide engine.
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


@pytest.mark.parametrize(
    "url",
    [
        # No scheme at all, but a later "://" in the query string.
        "Admin:Hunter2@host:8000/rpc?next=http://x",
        # Userinfo only, with a later "://" in the path.
        "admin:Hunter2@surreal:8000/proxy/ws://inner",
        " admin:Hunter2@surreal:8000?redirect=wss://elsewhere ",
    ],
)
def test_a_later_separator_is_not_mistaken_for_a_scheme(url: str) -> None:
    # Only an RFC 3986 scheme token at the start counts as a scheme.
    assert url_schemes.surreal_url_scheme(url) == ""
    assert not url_schemes.is_embedded_surreal_url(url)
    reason = url_schemes.unsupported_surreal_url_reason(url)
    assert reason is not None
    assert "has no scheme" in reason
    assert "hunter2" not in reason.lower()
    # Normalization leaves a URL without a valid scheme alone, beyond trimming.
    assert url_schemes.normalize_surreal_url(url) == url.strip()
    config = CoreConfig(environment="development", surreal_url=url, surreal_data_dir="")
    with pytest.raises(ValueError, match="has no scheme") as caught:
        config.require_serviceable_surreal_url()
    assert "hunter2" not in str(caught.value).lower()


@pytest.mark.parametrize(
    ("url", "scheme", "normalized"),
    [
        ("ws://admin:Hunter2@surreal:8000/rpc?next=http://x", "ws", None),
        ("WSS://admin:Hunter2@surreal:8000/rpc", "wss", "wss://admin:Hunter2@surreal:8000/rpc"),
        ("SurrealKV+Versioned:///var/sibyl", "surrealkv+versioned", None),
    ],
)
def test_a_valid_scheme_still_parses_with_credentials_in_the_url(
    url: str, scheme: str, normalized: str | None
) -> None:
    assert url_schemes.surreal_url_scheme(url) == scheme
    assert url_schemes.unsupported_surreal_url_reason(url) is None
    expected = normalized or f"{scheme}://{url.split('://', 1)[1]}"
    assert url_schemes.normalize_surreal_url(url) == expected
    # Only the scheme is lowercased; the password keeps its case.
    if "Hunter2" in url:
        assert "Hunter2" in url_schemes.normalize_surreal_url(url)
    config = CoreConfig(environment="development", surreal_url=url, surreal_data_dir="")
    assert config.surreal_url_problem() is None


def test_an_unsupported_scheme_names_only_its_token() -> None:
    reason = url_schemes.unsupported_surreal_url_reason("tikv://admin:Hunter2@pd:2379?x=ws://y")
    assert reason is not None
    assert "tikv:// is not supported" in reason
    assert "hunter2" not in reason.lower()


async def test_uppercase_embedded_schemes_open_a_real_store(tmp_path) -> None:
    for url in (f"SURREALKV://{tmp_path / 'upper'}", "MEMORY://", "Mem://"):
        client = DedicatedSurrealClient(url=url, namespace="org_case", database="graph")
        try:
            assert await client.execute_query("RETURN 1;") == 1
        finally:
            await client.close()


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


@pytest.mark.parametrize(
    ("url", "redacted"),
    [
        ("ws://surreal:8000/rpc", "ws://surreal:8000"),
        ("WS://admin:Hunter2@surreal:8000/rpc?token=Hunter2#Hunter2", "ws://surreal:8000"),
        ("wss://admin:Hunter2@surreal.example.com/rpc", "wss://surreal.example.com"),
        ("http://admin:Hunter2@10.0.0.5:8000", "http://10.0.0.5:8000"),
        ("https://admin:Hunter2@[2001:db8::1]:8443/sub/rpc", "https://[2001:db8::1]:8443"),
        ("ws://admin:Hunter2@[::1]/rpc", "ws://[::1]"),
        # No host to show: the scheme only.
        ("ws:///Admin:Hunter2@host:8000/rpc", "ws:// (host not parsed)"),
        ("http://admin:Hunter2@host:notaport/rpc", "http:// (host not parsed)"),
        # Embedded stores show no path, which could itself hold a secret.
        ("surrealkv:///srv/Hunter2/sibyl", "surrealkv:// (embedded)"),
        ("surrealkv+versioned:///srv/Hunter2", "surrealkv+versioned:// (embedded)"),
        ("file:///srv/Hunter2", "file:// (embedded)"),
        ("memory://", "memory:// (embedded)"),
        ("mem://", "mem:// (embedded)"),
        # Unsupported or missing schemes.
        ("tikv://admin:Hunter2@pd:2379", "tikv://pd:2379"),
        ("Admin:Hunter2@host:8000/rpc?next=http://x", "(no URL scheme)"),
    ],
)
def test_redacted_urls_keep_only_scheme_host_and_port(url: str, redacted: str) -> None:
    assert url_schemes.redact_surreal_url(url) == redacted
    assert "hunter2" not in url_schemes.redact_surreal_url(url).lower()


@pytest.mark.parametrize(
    ("url", "base", "credentials"),
    [
        ("ws://surreal:8000/rpc", "http://surreal:8000", None),
        (
            "wss://admin:Hunter2@surreal.example.com/rpc",
            "https://surreal.example.com",
            ("admin", "Hunter2"),
        ),
        (
            "HTTP://a%40b:p%3Aw@[::1]:8000/sub/rpc/?q=Hunter2",
            "http://[::1]:8000/sub",
            ("a@b", "p:w"),
        ),
        ("ws:///Admin:Hunter2@host:8000/rpc", None, None),
        ("surrealkv:///var/sibyl", None, None),
    ],
)
def test_http_base_urls_never_carry_userinfo(
    url: str, base: str | None, credentials: tuple[str, str] | None
) -> None:
    assert url_schemes.surreal_http_base_url(url) == base
    assert "hunter2" not in (url_schemes.surreal_http_base_url(url) or "").lower()
    assert url_schemes.surreal_url_credentials(url) == credentials


_SECRET_URL = "https://admin:Pw%2BSecret@host:8443/private/Deploy%20Path/rpc?token=Tok+En#Frag42"


@pytest.mark.parametrize(
    "message",
    [
        "admin",
        "Pw+Secret",  # percent-decoded password
        "pw%2bsecret",  # raw, other case
        "/private/Deploy Path/rpc",  # decoded path
        "Deploy%20Path",  # raw segment
        "Deploy+Path",  # "+" for a space
        "token=Tok En",  # query value with "+" decoded to a space
        "Tok%2BEn",  # query value percent-encoded
        "Frag42",
        "url='https://host:8443/private/Deploy%20Path?token=Tok+En/rpc'",
    ],
)
def test_every_spelling_of_a_secret_piece_is_caught(message: str) -> None:
    assert url_schemes.error_mentions_url_secret(RuntimeError(f"failed: {message}"), _SECRET_URL)
    assert url_schemes.safe_error_detail(RuntimeError(f"failed: {message}"), _SECRET_URL) == ""


def test_secrets_are_caught_anywhere_in_the_exception_chain() -> None:
    try:
        try:
            raise OSError("cannot reach /private/Deploy Path")
        except OSError as inner:
            raise RuntimeError("request failed") from inner
    except RuntimeError as outer:
        assert url_schemes.error_mentions_url_secret(outer, _SECRET_URL)


@pytest.mark.parametrize(
    "message",
    [
        "Database record `entity:one` already exists",
        "Failed to commit transaction due to a read or write conflict. This transaction can be retried",
        "503 at /rpc",  # endpoint segments are not secret
        "host:8443",  # a clean host and port are not secret
    ],
)
def test_clean_messages_are_left_alone(message: str) -> None:
    assert not url_schemes.error_mentions_url_secret(RuntimeError(message), _SECRET_URL)
    assert url_schemes.safe_error_detail(RuntimeError(message), _SECRET_URL) == message


async def test_the_client_boundary_keeps_clean_errors_and_replaces_leaky_ones(monkeypatch) -> None:
    from sibyl_core.backends.surreal.connection import SurrealTransportError

    class Conflict(Exception):
        pass

    raised: list[Exception] = []

    class FakeAsyncSurreal:
        def __init__(self, _url: str) -> None:
            pass

        async def use(self, _namespace: str, _database: str) -> None:
            return None

        async def query_raw(self, query: str, _params: object | None = None) -> object:
            if query == "RETURN true;":
                return {"result": [{"status": "OK", "result": True}]}
            raise raised.pop(0)

        async def close(self) -> None:
            return None

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)
    client = DedicatedSurrealClient(
        url="wss://host:8443/private/Deploy%20Path/rpc", namespace="org_b", database="graph"
    )

    clean = Conflict("Database record `entity:one` already exists")
    raised.append(clean)
    with pytest.raises(Conflict) as kept:
        await client.execute_query("CREATE entity:one;")
    assert kept.value is clean

    raised.append(Conflict("503 for url='https://host:8443/private/Deploy Path/rpc'"))
    with pytest.raises(SurrealTransportError) as replaced:
        await client.execute_query("CREATE entity:two;")
    assert "Deploy" not in str(replaced.value)
    assert replaced.value.__cause__ is None
    assert replaced.value.__context__ is None
    assert replaced.value.cause_type == "Conflict"


def test_partly_normalized_urls_are_caught_by_canonical_comparison() -> None:
    url = "http://host:8000/Path%3AToken%2FCanary42/rpc"
    # aiohttp decodes %3A but keeps %2F; no fixed list of spellings has this.
    printed = RuntimeError("503, url='http://host:8000/Path:Token%2FCanary42/rpc'")
    assert url_schemes.error_mentions_url_secret(printed, url)
    assert url_schemes.canonical_url_text("Path:Token%2FCanary42") == "path:token/canary42"
    assert url_schemes.canonical_url_text("A%252Fb+c") == "a/b c"


def test_raw_envelopes_withhold_only_url_quoting_error_text() -> None:
    from sibyl_core.backends.surreal.connection import withhold_url_secrets_in_envelope

    url = "http://host:8000/private/Deploy%20Path/rpc"
    clean = {
        "id": "q1",
        "result": [
            {"status": "OK", "result": [{"ref": "/private/Deploy Path"}]},
            {"status": "ERR", "result": "Database record `entity:one` already exists"},
        ],
    }
    # Clean envelopes, including OK data that happens to match, pass as is.
    assert withhold_url_secrets_in_envelope(clean, url) is clean

    leaky = {
        "id": "q2",
        "result": [
            {"status": "OK", "result": []},
            {
                "status": "ERR",
                "result": "cannot serve /private/Deploy%20Path?x/rpc",
                "details": {"kind": "Internal"},
            },
        ],
        "error": {"code": -32000, "message": "proxy said /private/Deploy Path"},
    }
    scrubbed = withhold_url_secrets_in_envelope(leaky, url)
    assert isinstance(scrubbed, dict)
    assert scrubbed["id"] == "q2"
    assert scrubbed["result"][0] is leaky["result"][0]
    assert scrubbed["result"][1]["status"] == "ERR"
    assert scrubbed["result"][1]["details"] == {"kind": "Internal"}
    assert "withheld" in scrubbed["result"][1]["result"]
    assert scrubbed["error"]["code"] == -32000
    assert "Deploy" not in str(scrubbed)


def test_deeply_nested_escapes_are_fully_decoded() -> None:
    # Twelve levels of percent-encoding: "%3A" becomes "%25...253A".
    nesting = "25" * 11
    url = f"http://host:8000/Path%{nesting}3AToken%{nesting}2FCanary42/rpc"
    for printed in (
        url,
        f"503, url='{url}'",
        "503, url='http://host:8000/Path:Token/Canary42/rpc'",
        f"503, url='http://host:8000/Path%3AToken%{nesting[:4]}2FCanary42/rpc'",
    ):
        error = RuntimeError(printed)
        assert url_schemes.error_mentions_url_secret(error, url), printed
        assert url_schemes.safe_error_detail(error, url) == ""
    assert url_schemes.canonical_url_text(f"%{nesting}3A") == ":"

    reason = url_schemes.unsupported_surreal_url_reason(f"tikv://h:1/Path%{nesting}3ACanary42")
    assert reason is not None
    assert "canary42" not in reason.lower()


async def test_deeply_nested_escapes_never_leave_the_client(monkeypatch) -> None:
    nesting = "25" * 11
    url = f"http://host:8000/Path%{nesting}3AToken%{nesting}2FCanary42/rpc"

    class FakeAsyncSurreal:
        def __init__(self, _url: str) -> None:
            pass

        async def use(self, _namespace: str, _database: str) -> None:
            return None

        async def query_raw(self, query: str, _params: object | None = None) -> object:
            if query == "RETURN true;":
                return {"result": [{"status": "OK", "result": True}]}
            # Only the fully decoded path, as a stack that decodes every
            # level would print it; the raw URL never appears.
            decoded = "http://host:8000/Path:Token/Canary42/rpc"
            if query.startswith("RAW"):
                return {"result": [{"status": "ERR", "result": f"cannot serve {decoded}"}]}
            raise RuntimeError(f"503, url='{decoded}'")

        async def close(self) -> None:
            return None

    monkeypatch.setattr("surrealdb.AsyncSurreal", FakeAsyncSurreal)
    client = DedicatedSurrealClient(url=url, namespace="org_nested", database="graph")

    import traceback

    with pytest.raises(Exception) as caught:
        await client.execute_query("CREATE entity:one;")
    text = (
        str(caught.value) + repr(caught.value) + "".join(traceback.format_exception(caught.value))
    )
    assert "canary42" not in text.lower()

    raw = await client.execute_query_raw("RAW SELECT * FROM entity;")
    assert "canary42" not in str(raw).lower()
    assert raw["result"][0]["status"] == "ERR"
