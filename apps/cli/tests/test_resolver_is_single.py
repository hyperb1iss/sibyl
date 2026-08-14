"""The server URL has exactly one resolver.

Two independent resolutions is the defect this branch exists to remove: auth
ranked SIBYL_API_URL above the selected context while the client ranked it
below, and a login stored its token under a key no other command read. Naming
the sites was not enough, because new ones keep appearing, so this pins the
class instead of the instances.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "sibyl_cli"

# The resolver itself, the context store that backs it, and the commands whose
# subject IS the raw configuration rather than the effective server.
ALLOWED_ACTIVE_CONTEXT_READERS = {
    "client.py",  # defines resolve_api_base_url
    "config_store.py",  # defines the context store and the effective helpers
    "context.py",  # `sibyl context` reports the raw configuration by design
    "auth.py",  # scopes credentials per stored context, not per request
    "doctor.py",  # diagnoses the configuration, so it must see it unresolved
    "pending.py",  # matches a buffered write's recorded base_url to a context
}

RAW_READERS = {"get_active_context", "get_active_context_name"}


def _calls(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            found.add(node.func.id)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            found.add(node.func.attr)
    return found


def test_no_module_resolves_the_server_url_on_its_own() -> None:
    """A module that reads the active context directly ignores -C and the pin."""
    offenders: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        if path.name in ALLOWED_ACTIVE_CONTEXT_READERS:
            continue
        if RAW_READERS & _calls(path):
            offenders.append(path.name)

    assert not offenders, (
        "These modules read the active context directly instead of going "
        f"through resolve_api_base_url / resolve_effective_context: {offenders}. "
        "A staging-pinned directory would report production state."
    )


def test_the_effective_url_helper_delegates_to_the_resolver() -> None:
    """get_effective_server_url used to omit SIBYL_API_URL entirely."""
    source = (SRC / "config_store.py").read_text(encoding="utf-8")
    body = source.split("def get_effective_server_url")[1].split("\ndef ")[0]

    assert "resolve_api_base_url" in body


def _reads_env(path: pathlib.Path, key: str) -> bool:
    """True when the module reads os.environ[key], ignoring modules that write it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and first.value == key:
                    return True
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if node.slice.value == key and isinstance(node.value, ast.Attribute):
                if node.value.attr == "environ":
                    return True
    return False


def test_the_env_var_is_read_in_exactly_one_place() -> None:
    """SIBYL_API_URL is a fallback inside the resolver, not a second entry point.

    docker.py and local.py write this key into a generated compose file for the
    web container, which is a different concern and not a resolution path.
    """
    readers = [
        path.name for path in sorted(SRC.glob("*.py")) if _reads_env(path, "SIBYL_API_URL")
    ]

    assert readers == ["client.py"], f"SIBYL_API_URL is read outside the resolver: {readers}"
