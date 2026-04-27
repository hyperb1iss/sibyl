"""Tests for the UserPromptSubmit context injection hook."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_PATH = (
    REPO_ROOT / "apps" / "cli" / "src" / "sibyl_cli" / "data" / "hooks" / "user-prompt-submit.py"
)


def _load_hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sibyl_user_prompt_submit_hook", HOOK_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pack() -> dict[str, Any]:
    return {
        "sections": [
            {
                "title": "Decisions",
                "items": [
                    {
                        "name": "Use context packs",
                        "type": "decision",
                        "reason": "decision records a choice",
                        "content": "Agents should receive grouped memory for the current goal.",
                    }
                ],
            }
        ]
    }


def test_user_prompt_hook_formats_context_pack_sections() -> None:
    hook = _load_hook()

    formatted = hook.format_context_pack(_pack())

    assert "**Decisions:**" in formatted
    assert "**Use context packs** (decision)" in formatted
    assert "_Why:_ decision records a choice" in formatted


def test_user_prompt_hook_prefers_server_markdown() -> None:
    hook = _load_hook()

    formatted = hook.format_context_pack(
        {
            "markdown": "# Sibyl Context Pack: boop",
            "sections": [{"title": "Decisions", "items": [{"name": "Use packs"}]}],
        }
    )

    assert formatted == "# Sibyl Context Pack: boop"


def test_user_prompt_hook_ignores_empty_markdown_context_pack() -> None:
    hook = _load_hook()

    formatted = hook.format_context_pack(
        {
            "markdown": "# Sibyl Context Pack: empty\n\n_Hint: Capture memory._",
            "sections": [],
        }
    )

    assert formatted == ""


def test_user_prompt_hook_uses_context_pack_before_search(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook = _load_hook()
    calls: list[tuple[str, ...]] = []

    def fake_run_sibyl(*args: str, timeout: int = 4) -> str | None:
        calls.append(args)
        if args[:2] == ("context", "pack"):
            return json.dumps(_pack())
        if args and args[0] == "search":
            pytest.fail("search fallback should not run when context pack has results")
        return None

    monkeypatch.setattr(hook, "run_sibyl", fake_run_sibyl)
    monkeypatch.setattr(hook, "generate_query_with_haiku", lambda _ctx, _prompt: None)
    monkeypatch.setattr(hook, "fallback_extract_terms", lambda _prompt: "agent context")
    monkeypatch.setattr(
        hook.sys,
        "stdin",
        io.StringIO(json.dumps({"prompt": "please build better context injection for agents"})),
    )

    with pytest.raises(SystemExit) as exc:
        hook.main()

    assert exc.value.code == 0
    assert calls[0][:2] == ("context", "pack")
    assert "--layer" in calls[0]
    assert "wake" in calls[0]
    payload = json.loads(capsys.readouterr().out)
    assert "Use context packs" in payload["hookSpecificOutput"]["additionalContext"]


def test_user_prompt_hook_falls_back_to_search_when_context_pack_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    hook = _load_hook()
    calls: list[tuple[str, ...]] = []

    def fake_run_sibyl(*args: str, timeout: int = 4) -> str | None:
        calls.append(args)
        if args[:2] == ("context", "pack"):
            return json.dumps({"markdown": "# Empty", "sections": []})
        if args and args[0] == "search":
            return json.dumps(
                {
                    "results": [
                        {
                            "name": "Scoped remember captures linked project context",
                            "type": "decision",
                            "content": "Project-scoped memory should still inject via fallback.",
                        }
                    ]
                }
            )
        return None

    monkeypatch.setattr(hook, "run_sibyl", fake_run_sibyl)
    monkeypatch.setattr(hook, "generate_query_with_haiku", lambda _ctx, _prompt: None)
    monkeypatch.setattr(hook, "fallback_extract_terms", lambda _prompt: "scoped remember")
    monkeypatch.setattr(
        hook.sys,
        "stdin",
        io.StringIO(json.dumps({"prompt": "please continue building scoped remember"})),
    )

    with pytest.raises(SystemExit) as exc:
        hook.main()

    assert exc.value.code == 0
    assert calls[0][:2] == ("context", "pack")
    assert calls[1][0] == "search"
    payload = json.loads(capsys.readouterr().out)
    assert (
        "Scoped remember captures linked project context"
        in payload["hookSpecificOutput"]["additionalContext"]
    )
    assert "sibyl remember" in payload["hookSpecificOutput"]["additionalContext"]
