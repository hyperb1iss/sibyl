"""The server URL has exactly one resolver.

Two independent resolutions is the defect this branch exists to remove: auth
ranked SIBYL_API_URL above the selected context while the client ranked it
below, and a login stored its token under a key no other command read. Naming
the modules was not enough either, because a module-wide exemption hands every
future function in that file the same privilege. Ownership is therefore pinned
per function: each site that may reach the raw active context or read
SIBYL_API_URL is listed below with the reason it is allowed, and any other site
anywhere in the package fails these tests.

Reaching the active context at all is the thing being fenced, which covers
reading its ``server_url`` field: a function that never gets the context object
cannot read a URL off it.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "sibyl_cli"

RAW_READERS = {"get_active_context", "get_active_context_name"}

# (module, enclosing function) pairs allowed to reach the raw active context.
ALLOWED_ACTIVE_CONTEXT_READERS = {
    # The resolver itself, and the two settings that travel with the context it
    # picks (credential key, TLS verification).
    ("client_transport.py", "resolve_api_base_url"),
    ("client_transport.py", "_auth_credential_scope"),
    ("client_transport.py", "ClientTransportMixin._get_insecure_from_context"),
    # The store defines the active-context accessors and its own selection order.
    ("config_store.py", "get_active_context"),
    ("config_store.py", "resolve_context_name"),
    # `sibyl config context ...` reports the stored configuration by design; an
    # effective-context view would hide exactly what these commands exist to show.
    ("context.py", "list_cmd"),
    ("context.py", "show_cmd"),
    ("context.py", "update_cmd"),
    ("context.py", "delete_cmd"),
    # Maps a buffered write's recorded base_url back to a context name.
    ("pending.py", "_context_name_for_base_url"),
}

# (module, enclosing function) pairs allowed to read SIBYL_API_URL.
ALLOWED_ENV_READERS = {
    # A paired URL and token form the explicit automation target.
    ("client_transport.py", "_paired_automation_api_url"),
    # Rank 3 of the one resolution order.
    ("client_transport.py", "resolve_api_base_url"),
    # Presence check, not a resolution: an implicit-localhost write is refused
    # unless a context or this legacy variable exists.
    ("client_transport.py", "ClientTransportMixin._request"),
}

ENV_KEY = "SIBYL_API_URL"


class _SiteFinder(ast.NodeVisitor):
    """Collects call sites with the qualified name of the function around them."""

    def __init__(self) -> None:
        self.scope: list[str] = []
        self.raw_readers: set[str] = set()
        self.env_readers: set[str] = set()

    def _qualname(self) -> str:
        return ".".join(self.scope) or "<module>"

    def _enter(self, node: ast.AST) -> None:
        self.scope.append(getattr(node, "name", "<anonymous>"))
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _enter
    visit_AsyncFunctionDef = _enter
    visit_ClassDef = _enter

    def visit_Call(self, node: ast.Call) -> None:
        name: str | None = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr

        if name in RAW_READERS:
            self.raw_readers.add(self._qualname())
        # os.environ.get(KEY), environ.get(KEY), os.getenv(KEY)
        if name in {"get", "getenv"} and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and first.value == ENV_KEY:
                self.env_readers.add(self._qualname())
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # os.environ[KEY] / environ[KEY]; a dict literal key is not a Subscript,
        # so the compose files docker.py and local.py generate stay invisible here.
        target = node.value
        environ_access = isinstance(target, ast.Attribute) and target.attr == "environ"
        environ_access = environ_access or (isinstance(target, ast.Name) and target.id == "environ")
        if environ_access and isinstance(node.slice, ast.Constant) and node.slice.value == ENV_KEY:
            self.env_readers.add(self._qualname())
        self.generic_visit(node)


def _sites() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    raw: set[tuple[str, str]] = set()
    env: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        finder = _SiteFinder()
        finder.visit(ast.parse(path.read_text(encoding="utf-8")))
        raw |= {(path.name, fn) for fn in finder.raw_readers}
        env |= {(path.name, fn) for fn in finder.env_readers}
    return raw, env


def _function(module: str, name: str) -> ast.FunctionDef:
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{module} no longer defines {name}()")


def test_only_named_functions_reach_the_raw_active_context() -> None:
    """A function that reads the active context directly ignores -C and the pin."""
    raw, _ = _sites()
    offenders = sorted(raw - ALLOWED_ACTIVE_CONTEXT_READERS)

    assert not offenders, (
        f"These functions read the active context directly: {offenders}. Route them "
        "through resolve_api_base_url / resolve_effective_context, or add the site "
        "here with the reason it owns a raw read. A staging-pinned directory would "
        "otherwise report production state."
    )


def test_every_named_active_context_reader_still_exists() -> None:
    """A stale allowlist is a silent exemption for whatever replaces the site."""
    raw, _ = _sites()
    stale = sorted(ALLOWED_ACTIVE_CONTEXT_READERS - raw)

    assert not stale, f"These allowlisted readers no longer read the active context: {stale}"


def test_the_effective_url_helper_delegates_to_the_resolver() -> None:
    """get_effective_server_url used to omit SIBYL_API_URL entirely."""
    node = _function("config_store.py", "get_effective_server_url")
    calls = {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }
    calls |= {
        call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }

    assert "resolve_api_base_url" in calls, (
        "get_effective_server_url must delegate to the resolver instead of "
        f"assembling a URL of its own; it calls {sorted(calls)}"
    )
    assert not (calls & RAW_READERS), (
        "get_effective_server_url resolves the active context on its own again"
    )


def test_the_env_var_is_read_only_by_named_functions() -> None:
    """SIBYL_API_URL is a fallback inside the resolver, not a second entry point."""
    _, env = _sites()
    offenders = sorted(env - ALLOWED_ENV_READERS)

    assert not offenders, (
        f"{ENV_KEY} is read outside the resolver by: {offenders}. Every read is a "
        "second resolution order waiting to disagree with the first."
    )


def test_every_named_env_reader_still_exists() -> None:
    _, env = _sites()
    stale = sorted(ALLOWED_ENV_READERS - env)

    assert not stale, f"These allowlisted {ENV_KEY} readers are gone: {stale}"
