"""Client addresses behind trusted reverse proxies.

Behind an ingress every request arrives from the proxy's address, so an
address-keyed limit would put all users in one bucket. These tests drive the
real login route through the same uvicorn configuration the sibyld server
builds, so they cover the trust list, uvicorn's X-Forwarded-For resolution,
and the rate-limit key together.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from importlib import import_module
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from starlette.requests import Request
from starlette.types import ASGIApp
from structlog.testing import capture_logs

from sibyl import config as config_module
from sibyl.api.app import create_api_app
from sibyl.api.rate_limit import limiter
from sibyl.config import LOOPBACK_FORWARDED_ALLOW_IPS, Settings
from sibyl.main import uvicorn_config

LOGIN_PATH = "/auth/local/login"
LOGIN_LIMIT = 5
TRUSTED_RANGE = "10.20.0.0/16"
TRUSTED_PROXY = "10.20.0.7"
INNER_TRUSTED_PROXY = "10.20.0.9"
UNTRUSTED_PEER = "203.0.113.50"
CLIENT_A = "198.51.100.10"
CLIENT_B = "198.51.100.20"

TrustSetter = Callable[..., list[str]]


@pytest.fixture(autouse=True)
def _fresh_buckets_and_fast_login(monkeypatch: pytest.MonkeyPatch):
    # A blank JWT secret makes the login handler fail before it touches the
    # database. The limiter counts the attempt before the handler runs, so
    # the 429 boundary is still the real one.
    monkeypatch.setattr(config_module.settings, "jwt_secret", SecretStr(""))
    limiter.reset()
    yield
    limiter.reset()


@pytest.fixture
def trust(monkeypatch: pytest.MonkeyPatch) -> TrustSetter:
    """Resolve the trust list from the environment, as a fresh process would."""

    def apply(value: str | None, *, uvicorn_env: str | None = None) -> list[str]:
        for name, env_value in (
            ("SIBYL_FORWARDED_ALLOW_IPS", value),
            ("FORWARDED_ALLOW_IPS", uvicorn_env),
        ):
            if env_value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, env_value)
        resolved = Settings().forwarded_allow_ips
        monkeypatch.setattr(config_module.settings, "forwarded_allow_ips", resolved)
        return resolved

    return apply


def _served_app() -> ASGIApp:
    """The API app wrapped by uvicorn exactly as the sibyld HTTP server wraps it."""
    app = create_api_app()

    @app.get("/_test/client")
    async def client_address(request: Request) -> dict[str, str | None]:
        return {"host": request.client.host if request.client else None}

    config = uvicorn_config(app, "127.0.0.1", 0)
    config.load()
    return config.loaded_app


def _peer(app: ASGIApp, address: str) -> TestClient:
    return TestClient(app, client=(address, 40000), raise_server_exceptions=False)


def _login(client: TestClient, forwarded_for: str | None = None) -> int:
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for is not None else {}
    response = client.post(
        LOGIN_PATH,
        json={"email": "someone@example.test", "password": "not-the-password"},
        headers=headers,
    )
    return response.status_code


def _resolved_client(client: TestClient, forwarded_for: str | None = None) -> str | None:
    headers = {"X-Forwarded-For": forwarded_for} if forwarded_for is not None else {}
    return client.get("/_test/client", headers=headers).json()["host"]


def _spend_login_budget(client: TestClient, forwarded_for: str | None = None) -> None:
    statuses = [_login(client, forwarded_for) for _ in range(LOGIN_LIMIT)]
    assert 429 not in statuses, statuses


def test_trusted_proxy_gives_each_forwarded_client_its_own_login_bucket(
    trust: TrustSetter,
) -> None:
    trust(TRUSTED_RANGE)
    proxy = _peer(_served_app(), TRUSTED_PROXY)

    _spend_login_budget(proxy, CLIENT_A)
    assert _login(proxy, CLIENT_A) == 429

    # A second user behind the same proxy still has a full budget.
    _spend_login_budget(proxy, CLIENT_B)
    assert _login(proxy, CLIENT_B) == 429


def test_untrusted_peer_cannot_spoof_forwarded_for_to_dodge_the_limit(
    trust: TrustSetter,
) -> None:
    trust(TRUSTED_RANGE)
    attacker = _peer(_served_app(), UNTRUSTED_PEER)

    for attempt in range(LOGIN_LIMIT):
        assert _login(attacker, f"192.0.2.{attempt + 1}") != 429
    assert _login(attacker, "192.0.2.200") == 429
    assert _resolved_client(attacker, CLIENT_A) == UNTRUSTED_PEER


def test_multi_hop_chain_resolves_the_rightmost_untrusted_address(trust: TrustSetter) -> None:
    trust(TRUSTED_RANGE)
    proxy = _peer(_served_app(), TRUSTED_PROXY)

    # The client wrote the leftmost entry, the edge proxy appended the address
    # it saw, and a second trusted proxy appended the edge proxy's address.
    chain = f"6.6.6.6, {CLIENT_A}, {INNER_TRUSTED_PROXY}"
    assert _resolved_client(proxy, chain) == CLIENT_A

    # Rotating the client-written entry does not buy a fresh bucket.
    for attempt in range(LOGIN_LIMIT):
        assert _login(proxy, f"192.0.2.{attempt + 1}, {CLIENT_A}, {INNER_TRUSTED_PROXY}") != 429
    assert _login(proxy, f"192.0.2.99, {CLIENT_A}, {INNER_TRUSTED_PROXY}") == 429

    # Repeated headers join in order, the way uvicorn reads them off the wire.
    joined = proxy.get(
        "/_test/client",
        headers=[("X-Forwarded-For", "6.6.6.6"), ("X-Forwarded-For", CLIENT_B)],
    )
    assert joined.json()["host"] == CLIENT_B

    # When every hop is trusted, uvicorn falls back to the leftmost entry.
    assert _resolved_client(proxy, f"{INNER_TRUSTED_PROXY}, 10.20.3.3") == INNER_TRUSTED_PROXY


def test_default_config_trusts_only_loopback(trust: TrustSetter) -> None:
    assert trust(None) == list(LOOPBACK_FORWARDED_ALLOW_IPS) == ["127.0.0.1", "::1"]
    app = _served_app()

    # A private-network peer is not trusted by default, so its header is ignored.
    private_peer = _peer(app, TRUSTED_PROXY)
    assert _resolved_client(private_peer, CLIENT_A) == TRUSTED_PROXY
    _spend_login_budget(private_peer, CLIENT_A)
    assert _login(private_peer, CLIENT_B) == 429

    # Both loopback forms are trusted, as uvicorn itself does from 0.53.
    for loopback in LOOPBACK_FORWARDED_ALLOW_IPS:
        assert _resolved_client(_peer(app, loopback), CLIENT_A) == CLIENT_A


def test_empty_setting_keeps_the_default(trust: TrustSetter) -> None:
    assert trust("") == ["127.0.0.1", "::1"]
    assert trust(" , ") == ["127.0.0.1", "::1"]


def test_uvicorn_forwarded_allow_ips_applies_only_while_the_sibyl_setting_is_unset(
    trust: TrustSetter,
) -> None:
    assert trust(None, uvicorn_env="172.18.0.0/16") == ["172.18.0.0/16"]
    assert trust(TRUSTED_RANGE, uvicorn_env="172.18.0.0/16") == [TRUSTED_RANGE]
    # Set, even to nothing, the Sibyl setting wins, and empty means loopback only.
    assert trust("", uvicorn_env="172.18.0.0/16") == list(LOOPBACK_FORWARDED_ALLOW_IPS)
    assert trust(" , ", uvicorn_env="172.18.0.0/16") == list(LOOPBACK_FORWARDED_ALLOW_IPS)


def test_empty_sibyl_setting_overrides_uvicorns_variable_end_to_end(trust: TrustSetter) -> None:
    trust("", uvicorn_env="*")

    with capture_logs() as logs:
        app = _served_app()

    # Uvicorn gets the explicit loopback list, so its own "*" never takes effect.
    assert not [
        entry for entry in logs if entry["event"] == "forwarded_allow_ips_trusts_every_peer"
    ]
    assert _resolved_client(_peer(app, UNTRUSTED_PEER), CLIENT_A) == UNTRUSTED_PEER
    assert _resolved_client(_peer(app, "127.0.0.1"), CLIENT_A) == CLIENT_A


def test_star_trusts_every_peer_and_warns(trust: TrustSetter) -> None:
    assert trust("*") == ["*"]

    with capture_logs() as logs:
        app = _served_app()

    warnings = [
        entry for entry in logs if entry["event"] == "forwarded_allow_ips_trusts_every_peer"
    ]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert "choose its own address" in warnings[0]["message"]

    # Any peer is trusted, and the leftmost entry, which the client wrote, wins.
    stranger = _peer(app, UNTRUSTED_PEER)
    assert _resolved_client(stranger, f"{CLIENT_A}, {CLIENT_B}") == CLIENT_A


@pytest.mark.parametrize("catch_all", ["0.0.0.0/0", "::/0", f"{TRUSTED_RANGE},0.0.0.0/0"])
def test_catch_all_ranges_warn_like_star(trust: TrustSetter, catch_all: str) -> None:
    trust(catch_all)

    with capture_logs() as logs:
        app = _served_app()

    warnings = [
        entry for entry in logs if entry["event"] == "forwarded_allow_ips_trusts_every_peer"
    ]
    assert len(warnings) == 1
    assert warnings[0]["entries"] == [entry for entry in catch_all.split(",") if "/0" in entry]

    if "0.0.0.0/0" in catch_all.split(","):
        # Every IPv4 hop is trusted, so the leftmost entry, which the client wrote, wins.
        stranger = _peer(app, UNTRUSTED_PEER)
        assert _resolved_client(stranger, f"{CLIENT_A}, {CLIENT_B}") == CLIENT_A


@pytest.mark.parametrize(
    ("value", "broad"),
    [
        # Two halves that together cover every IPv4 address, with no /0 in sight.
        ("0.0.0.0/1,128.0.0.0/1", ["0.0.0.0/1", "128.0.0.0/1"]),
        (f"{TRUSTED_RANGE},10.0.0.0/7", ["10.0.0.0/7"]),
        ("::/1,8000::/1", ["::/1", "8000::/1"]),
        ("2001:db8::/31", ["2001:db8::/31"]),
    ],
)
def test_broad_ranges_warn_like_star(trust: TrustSetter, value: str, broad: list[str]) -> None:
    trust(value)

    with capture_logs() as logs:
        app = _served_app()

    [warning] = [
        entry for entry in logs if entry["event"] == "forwarded_allow_ips_trusts_every_peer"
    ]
    assert warning["entries"] == broad
    assert "choose its own address" in warning["message"]

    if value == "0.0.0.0/1,128.0.0.0/1":
        # Every IPv4 hop is trusted, so the leftmost entry, which the client wrote, wins.
        stranger = _peer(app, UNTRUSTED_PEER)
        assert _resolved_client(stranger, f"{CLIENT_A}, {CLIENT_B}") == CLIENT_A


def test_warning_names_uvicorns_variable_when_that_set_the_list(trust: TrustSetter) -> None:
    trust(None, uvicorn_env="*")

    with capture_logs() as logs:
        _served_app()

    [warning] = [
        entry for entry in logs if entry["event"] == "forwarded_allow_ips_trusts_every_peer"
    ]
    assert warning["setting"] == "FORWARDED_ALLOW_IPS"


@pytest.mark.parametrize(
    "value",
    [
        TRUSTED_RANGE,
        # The broadest ranges that stay quiet, and the private blocks together.
        "10.0.0.0/8",
        "2001:db8::/32",
        "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
    ],
)
def test_listed_proxies_do_not_warn(trust: TrustSetter, value: str) -> None:
    trust(value)

    with capture_logs() as logs:
        _served_app()

    assert not [
        entry for entry in logs if entry["event"] == "forwarded_allow_ips_trusts_every_peer"
    ]


def test_entries_are_normalized_and_deduplicated(trust: TrustSetter) -> None:
    assert trust(" 10.20.0.0/16 ,::1, 10.20.0.0/16,2001:db8:0::1 ") == [
        TRUSTED_RANGE,
        "::1",
        "2001:db8::1",
    ]


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("caddy", "is not an IP address or CIDR range"),
        ("10.0.0.0/33", "is not an IP address or CIDR range"),
        ("300.1.1.1", "is not an IP address or CIDR range"),
        ("*,10.0.0.0/8", "cannot be combined"),
        # Uvicorn parses ranges strictly, so a range with host bits would never match.
        ("10.20.3.4/16", "did you mean 10.20.0.0/16"),
    ],
)
def test_malformed_entries_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, value: str, reason: str
) -> None:
    monkeypatch.setenv("SIBYL_FORWARDED_ALLOW_IPS", value)

    with pytest.raises(ValidationError, match=reason):
        Settings()


def test_malformed_uvicorn_variable_names_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIBYL_FORWARDED_ALLOW_IPS", raising=False)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "caddy")

    with pytest.raises(ValueError, match="FORWARDED_ALLOW_IPS"):
        Settings()

    # Once the Sibyl setting is set, even to nothing, uvicorn's variable is not read at all.
    monkeypatch.setenv("SIBYL_FORWARDED_ALLOW_IPS", "")
    assert Settings().forwarded_allow_ips == list(LOOPBACK_FORWARDED_ALLOW_IPS)


def test_dev_reload_server_receives_the_trust_list(
    trust: TrustSetter, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust(f"{TRUSTED_RANGE},::1")
    cli_main = import_module("sibyl.cli.main")
    process = MagicMock()
    process.wait.return_value = 0
    popen = MagicMock(return_value=process)
    monkeypatch.setattr("subprocess.Popen", popen)

    cli_main._serve_with_reload("localhost", 3334)

    args = popen.call_args.args[0]
    assert args[args.index("--forwarded-allow-ips") + 1] == f"{TRUSTED_RANGE},::1"


def test_up_foreground_server_receives_the_trust_list(
    trust: TrustSetter, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    trust("*")
    up_cmd = sys.modules.get("sibyl.cli.up_cmd") or import_module("sibyl.cli.up_cmd")
    process = MagicMock()
    process.wait.return_value = 0
    popen = MagicMock(return_value=process)
    monkeypatch.setattr("subprocess.Popen", popen)
    monkeypatch.setattr("signal.signal", lambda *_args: None)

    with capture_logs() as logs:
        up_cmd._start_server_foreground(tmp_path, False, {})

    args = popen.call_args.args[0]
    assert args[args.index("--forwarded-allow-ips") + 1] == "*"
    # The server subprocess warns from its app factory, so the parent stays quiet.
    assert not [
        entry for entry in logs if entry["event"] == "forwarded_allow_ips_trusts_every_peer"
    ]


def test_dev_app_factory_warns_for_every_dev_launcher(
    trust: TrustSetter, monkeypatch: pytest.MonkeyPatch
) -> None:
    trust("*")
    sibyl_main = import_module("sibyl.main")
    monkeypatch.setattr(sibyl_main, "create_combined_app", lambda: "combined-app")

    with capture_logs() as logs:
        assert sibyl_main.create_dev_app() == "combined-app"

    assert [entry["event"] for entry in logs].count("forwarded_allow_ips_trusts_every_peer") == 1
