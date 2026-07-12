"""Crash-safe provider usage accounting for benchmark model calls."""

from __future__ import annotations

import json
import math
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

USAGE_EVENT_SCHEMA_VERSION = "sibyl-provider-usage-event-v1"


class ProviderUsageRecorder:
    def __init__(self, path: Path, *, run_id: str, role: str) -> None:
        self.path = path
        self.run_id = run_id
        self.role = role
        self._lock = threading.Lock()

    def record(self, response: Any, *, requested_model: str | None) -> dict[str, Any]:
        event = {
            "schema_version": USAGE_EVENT_SCHEMA_VERSION,
            "event_id": uuid4().hex,
            "observed_at": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "role": self.role,
            "requested_model": requested_model,
            "provider_model": _string_attr(response, "model"),
            "response_id": _string_attr(response, "id"),
            "usage": usage_from_response(response),
        }
        encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{encoded}\n")
                handle.flush()
                os.fsync(handle.fileno())
        return event


class AsyncUsageTrackingClient:
    def __init__(self, client: Any, recorder: ProviderUsageRecorder) -> None:
        self._client = client
        self.chat = _AsyncChatProxy(client.chat, recorder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class SyncUsageTrackingClient:
    def __init__(self, client: Any, recorder: ProviderUsageRecorder) -> None:
        self._client = client
        self.chat = _SyncChatProxy(client.chat, recorder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _AsyncChatProxy:
    def __init__(self, chat: Any, recorder: ProviderUsageRecorder) -> None:
        self._chat = chat
        self.completions = _AsyncCompletionsProxy(chat.completions, recorder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class _SyncChatProxy:
    def __init__(self, chat: Any, recorder: ProviderUsageRecorder) -> None:
        self._chat = chat
        self.completions = _SyncCompletionsProxy(chat.completions, recorder)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class _AsyncCompletionsProxy:
    def __init__(self, completions: Any, recorder: ProviderUsageRecorder) -> None:
        self._completions = completions
        self._recorder = recorder

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._completions.create(*args, **kwargs)
        self._recorder.record(response, requested_model=_requested_model(args, kwargs))
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._completions, name)


class _SyncCompletionsProxy:
    def __init__(self, completions: Any, recorder: ProviderUsageRecorder) -> None:
        self._completions = completions
        self._recorder = recorder

    def create(self, *args: Any, **kwargs: Any) -> Any:
        response = self._completions.create(*args, **kwargs)
        self._recorder.record(response, requested_model=_requested_model(args, kwargs))
        return response

    def __getattr__(self, name: str) -> Any:
        return getattr(self._completions, name)


def usage_from_response(response: Any) -> dict[str, Any]:
    raw_usage = _mapping(getattr(response, "usage", None))
    usage = _json_value(raw_usage)
    if not isinstance(usage, dict):
        usage = {}
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = _number(raw_usage.get(field))
        if value is not None:
            usage[field] = int(value)
    cost = _first_number(
        raw_usage.get("cost"),
        raw_usage.get("cost_usd"),
        raw_usage.get("total_cost"),
    )
    if cost is not None:
        usage["cost_usd"] = cost
    return usage


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("model_dump", "to_dict", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            dumped = method()
            if isinstance(dumped, Mapping):
                return dict(dumped)
    if value is None:
        return {}
    fields = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
        "cost",
        "cost_usd",
        "total_cost",
        "is_byok",
    )
    return {
        field: field_value
        for field in fields
        if (field_value := getattr(value, field, None)) is not None
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    mapped = _mapping(value)
    return _json_value(mapped) if mapped else str(value)


def _requested_model(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    model = kwargs.get("model")
    if isinstance(model, str) and model.strip():
        return model
    if args and isinstance(args[0], Mapping):
        positional_model = args[0].get("model")
        if isinstance(positional_model, str) and positional_model.strip():
            return positional_model
    return None


def _string_attr(value: Any, name: str) -> str | None:
    candidate = getattr(value, name, None)
    return candidate if isinstance(candidate, str) and candidate.strip() else None


def _first_number(*values: Any) -> float | None:
    for value in values:
        if (number := _number(value)) is not None:
            return number
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None
