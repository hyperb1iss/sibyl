from __future__ import annotations

import builtins
import importlib
import sys
from types import ModuleType

import pytest

BLOCKED_ROOTS = {
    "community",
    "crawl4ai",
    "google",
    "mistune",
    "pydantic_ai",
    "surrealdb",
}
LIGHT_IMPORT_MODULES = [
    "sibyl_core",
    "sibyl_core.ai",
    "sibyl_core.ai.errors",
    "sibyl_core.ai.llm",
    "sibyl_core.ai.llm.config",
    "sibyl_core.embeddings",
    "sibyl_core.embeddings.gemini",
    "sibyl_core.embeddings.providers",
    "sibyl_core.models.entities",
    "sibyl_core.session_bundle",
]


def test_light_imports_do_not_require_runtime_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> ModuleType:
        if level == 0 and name.partition(".")[0] in BLOCKED_ROOTS:
            raise ImportError(f"blocked optional dependency: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    saved_modules = _evict_modules(LIGHT_IMPORT_MODULES)
    try:
        for module_name in LIGHT_IMPORT_MODULES:
            importlib.import_module(module_name)
    finally:
        _restore_modules(saved_modules)


def test_classify_llm_exception_without_pydantic_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> ModuleType:
        if level == 0 and name.partition(".")[0] == "pydantic_ai":
            raise ImportError(f"blocked optional dependency: {name}")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    saved_modules = _evict_modules(["sibyl_core.ai.errors"])
    try:
        errors = importlib.import_module("sibyl_core.ai.errors")

        classified = errors.classify_llm_exception(RuntimeError("boom"), provider="test")

        assert isinstance(classified, errors.LLMProviderError)
        assert classified.details["exception_type"] == "RuntimeError"
    finally:
        _restore_modules(saved_modules)


def _evict_modules(module_names: list[str]) -> dict[str, ModuleType | None]:
    saved: dict[str, ModuleType | None] = {}
    for module_name in module_names:
        saved[module_name] = sys.modules.pop(module_name, None)
    for module_name in list(sys.modules):
        if any(module_name.startswith(f"{root}.") for root in module_names):
            saved[module_name] = sys.modules.pop(module_name, None)
    return saved


def _restore_modules(saved: dict[str, ModuleType | None]) -> None:
    for module_name in list(sys.modules):
        if module_name in saved or any(module_name.startswith(f"{root}.") for root in saved):
            sys.modules.pop(module_name, None)
    for module_name, module in saved.items():
        if module is not None:
            sys.modules[module_name] = module
