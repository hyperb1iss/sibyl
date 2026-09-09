"""Deterministic semantic views of immutable controller episode evidence."""

from __future__ import annotations

import base64
import hashlib
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any

from sibyl_core.tasks._evidence_json import (
    ByteRange,
    JsonPath,
    canonical,
    read_original_json,
    share_exact_values,
)

PROJECTION_VERSION = "sibyl-controller-evidence-view-v1"
EPISODE_VERSION = "sibyl-learning-episode-v1"
TRACE_VERSION = "sibyl-coding-trace-v1"

_PAYLOAD_FIELDS = {
    "start": {
        "claims",
        "identity",
        "image",
        "interpreter",
        "memory_pack_message_included",
        "options",
        "prompt_sha256",
        "request",
        "script_sha256",
        "system_prompt_sha256",
        "workspace_initial",
    },
    "model_request": {
        "authorization_header",
        "body",
        "body_base64",
        "body_sha256",
        "headers",
        "method",
        "url",
    },
    "model_response": {
        "body_base64",
        "body_sha256",
        "generation_id",
        "raw",
        "response_model",
        "response_provider",
        "status_code",
    },
    "tool_call": {"argv", "call", "command", "container", "index", "name", "stage", "tool_call_id"},
    "tool_result": {
        "carried_back",
        "cleanup",
        "detail",
        "index",
        "refusal",
        "returncode",
        "stage",
        "stage_removed",
        "status",
        "stderr",
        "stderr_base64",
        "stderr_sha256",
        "stdout",
        "stdout_base64",
        "stdout_sha256",
        "timeout_seconds",
        "tool_call_id",
        "workspace_after",
        "workspace_before",
    },
    "terminal": {"detail", "exit", "reason", "usage"},
}


def _same(left: Any, right: Any) -> bool:
    return canonical(left) == canonical(right)


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _tool_text(outcome: dict[str, Any]) -> str:
    if outcome["status"] == "refused":
        head = f"refused: {outcome['refusal']}\nno file change was carried back"
    elif outcome["status"] == "timeout":
        head = (
            f"timed out after {outcome['timeout_seconds']}s\n"
            "the container was stopped and no file change was carried back"
        )
    else:
        head = f"exit_code: {outcome['returncode']}"
        if not outcome["carried_back"]:
            head += "\nno file change was carried back"
    return f"{head}\n--- stdout ---\n{outcome['stdout']}\n--- stderr ---\n{outcome['stderr']}"


@dataclass(frozen=True)
class EvidenceCitation:
    episode_id: str
    ranges: tuple[ByteRange, ...]


@dataclass(frozen=True)
class EpisodeProjection:
    view: dict[str, Any]
    citations: dict[str, EvidenceCitation]
    coverage: tuple[dict[str, Any], ...]


@dataclass
class _EpisodeBuilder:
    episode_id: str
    prefix: str
    artifact: bytes
    value: dict[str, Any] = field(init=False)
    ranges: dict[JsonPath, ByteRange] = field(init=False)
    coverage: list[dict[str, Any]] = field(default_factory=list)
    citations: dict[str, EvidenceCitation] = field(default_factory=dict)
    history: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        parsed = read_original_json(self.artifact)
        if not isinstance(parsed.value, dict):
            raise ValueError("controller episode must be a JSON object")
        self.value, self.ranges = parsed.value, parsed.ranges
        if self.value.get("schema_version") != EPISODE_VERSION:
            raise ValueError("unsupported controller episode schema")
        if set(self.value) != {
            "assignment",
            "assurance",
            "goal",
            "input_memory_pack_sha256",
            "outcome",
            "schema_version",
            "sealed_isolation",
            "trace",
        }:
            raise ValueError("controller episode contains unclassified fields")

    def at(self, path: JsonPath) -> Any:
        value: Any = self.value
        for part in path:
            value = value[part]
        return value

    def record(self, path: JsonPath, disposition: str, **details: Any) -> None:
        self.coverage.append(
            {"path": list(path), "range": self.ranges[path], "disposition": disposition, **details}
        )

    def cite(self, identity: str, paths: list[JsonPath]) -> str:
        if identity in self.citations or not paths:
            raise ValueError("evidence citation identity is ambiguous or empty")
        self.citations[identity] = EvidenceCitation(
            self.episode_id, tuple(self.ranges[path] for path in paths)
        )
        for path in paths:
            self.record(path, "visible", evidence_id=identity)
        return identity

    def encoded_alias(self, path: JsonPath, target: JsonPath, *, json_value: bool) -> None:
        raw = base64.b64decode(self.at(path), validate=True)
        equivalent = (
            _same(read_original_json(raw).value, self.at(target))
            if json_value
            else raw == self.at(target).encode("utf-8")
        )
        if not equivalent:
            raise ValueError("transport encoding differs from its semantic evidence")
        digest_path = (*path[:-1], str(path[-1]).removesuffix("_base64") + "_sha256")
        if digest_path in self.ranges and self.at(digest_path) != hashlib.sha256(raw).hexdigest():
            raise ValueError("transport byte digest differs from its encoded evidence")
        self.record(path, "verified_encoding_alias", target=list(target))

    def select(
        self, base: JsonPath, names: tuple[str, ...]
    ) -> tuple[dict[str, Any], list[JsonPath]]:
        payload = self.at(base)
        paths: list[JsonPath] = [(*base, name) for name in names if name in payload]
        return {str(path[-1]): self.at(path) for path in paths}, paths

    def request(self, base: JsonPath) -> tuple[dict[str, Any], list[JsonPath]]:
        self.encoded_alias((*base, "body_base64"), (*base, "body"), json_value=True)
        body = self.at((*base, "body"))
        messages = body["messages"]
        if not isinstance(messages, list) or len(messages) < 2:
            raise ValueError("controller request is missing its budget message")
        without_budget = [messages[0], *messages[2:]]
        paths: list[JsonPath] = [(*base, "body", key) for key in body if key != "messages"]
        view = {
            "controls": {key: body[key] for key in body if key != "messages"},
            "budget_message": messages[1],
        }
        paths.append((*base, "body", "messages", 1))
        if self.history is None:
            self.history = list(without_budget)
            view["initial_messages"] = list(without_budget)
            paths.extend(
                (*base, "body", "messages", index) for index in range(len(messages)) if index != 1
            )
        elif not _same(without_budget, self.history):
            raise ValueError("request history differs from the preceding semantic event stream")
        else:
            self.record(
                (*base, "body", "messages"),
                "verified_event_history_alias",
                reconstructed_sha256=_hash(self.history),
            )
        return view, paths

    def response(self, base: JsonPath) -> tuple[dict[str, Any], list[JsonPath]]:
        self.encoded_alias((*base, "body_base64"), (*base, "raw"), json_value=True)
        raw = self.at((*base, "raw"))
        view, paths = self.select(base, ("status_code", "response_model", "response_provider"))
        choices = raw.get("choices")
        if choices:
            view["choices"] = choices
            paths.append((*base, "raw", "choices"))
            message = choices[0]["message"]
            followup = {"role": "assistant", "content": message.get("content")}
            followup.update(
                {key: message[key] for key in ("tool_calls", "reasoning_details") if key in message}
            )
            if self.history is None:
                raise ValueError("model response precedes its request")
            self.history.append(followup)
            for key in raw:
                if key == "choices":
                    continue
                if key in {
                    "id",
                    "object",
                    "created",
                    "model",
                    "provider",
                    "usage",
                    "system_fingerprint",
                }:
                    self.record((*base, "raw", key), "transport_audit")
                else:
                    view.setdefault("additional_response_fields", {})[key] = raw[key]
                    paths.append((*base, "raw", key))
        else:
            # Error responses must remain visible in full, including provider diagnostics.
            view["raw"] = raw
            paths.append((*base, "raw"))
        return view, paths

    def event(self, index: int, event: dict[str, Any]) -> dict[str, Any]:
        base: JsonPath = ("trace", index, "payload")
        kind = event.get("kind")
        if event.get("schema_version") != TRACE_VERSION or kind not in _PAYLOAD_FIELDS:
            raise ValueError("unsupported controller trace event")
        if set(event) != {"attempt_id", "index", "kind", "payload", "request_id", "schema_version"}:
            raise ValueError("controller event contains unclassified fields")
        payload = event["payload"]
        if set(payload) - _PAYLOAD_FIELDS[kind]:
            raise ValueError("controller event payload contains unclassified fields")
        if kind == "model_request":
            view, paths = self.request(base)
        elif kind == "model_response":
            view, paths = self.response(base)
        elif kind == "start":
            view, paths = self.select(
                base, ("request", "options", "workspace_initial", "image", "interpreter")
            )
        elif kind == "tool_call":
            view, paths = self.select(base, ("index", "tool_call_id", "name", "command", "call"))
        elif kind == "tool_result":
            for key in ("stdout", "stderr"):
                self.encoded_alias((*base, key + "_base64"), (*base, key), json_value=False)
            view, paths = self.select(
                base,
                (
                    "index",
                    "tool_call_id",
                    "status",
                    "returncode",
                    "carried_back",
                    "detail",
                    "refusal",
                    "stdout",
                    "stderr",
                    "timeout_seconds",
                    "workspace_before",
                    "workspace_after",
                    "cleanup",
                ),
            )
            if self.history is None:
                raise ValueError("tool result precedes its request")
            self.history.append(
                {
                    "role": "tool",
                    "tool_call_id": payload["tool_call_id"],
                    "content": _tool_text(payload),
                }
            )
        else:
            view, paths = self.select(base, tuple(payload))
        identity = self.cite(f"{self.prefix}.e{index}", paths)
        for key in event:
            if key != "payload":
                self.record(("trace", index, key), "event_identity")
        accounted = {path[len(base)] for path in paths}
        accounted.update(
            row["path"][len(base)]
            for row in self.coverage
            if tuple(row["path"][: len(base)]) == base and len(row["path"]) > len(base)
        )
        for key in sorted(payload.keys() - accounted):
            self.record((*base, key), "transport_audit")
        return {"kind": kind, **view, "evidence_id": identity}


def project_episode(episode_id: str, artifact: bytes, *, prefix: str) -> EpisodeProjection:
    builder = _EpisodeBuilder(episode_id, prefix, artifact)
    view: dict[str, Any] = {"episode_id": episode_id}
    identifiers = {}
    for key in ("goal", "assignment", "outcome"):
        view[key] = builder.value[key]
        identifiers[key] = builder.cite(f"{prefix}.{key}", [(key,)])
    view["evidence_ids"] = identifiers
    view["events"] = [
        builder.event(index, event) for index, event in enumerate(builder.value["trace"])
    ]
    for key in sorted(builder.value.keys() - {"goal", "assignment", "outcome", "trace"}):
        builder.record((key,), "execution_audit")
    _verify_complete_coverage(builder)
    return EpisodeProjection(view, builder.citations, tuple(builder.coverage))


def _verify_complete_coverage(builder: _EpisodeBuilder) -> None:
    intervals: list[list[int]] = []
    for start, end in sorted(row["range"] for row in builder.coverage):
        if intervals and start <= intervals[-1][1]:
            intervals[-1][1] = max(end, intervals[-1][1])
        else:
            intervals.append([start, end])
    starts = [span[0] for span in intervals]
    parents = {path[:-1] for path in builder.ranges if path}
    for path, (start, end) in builder.ranges.items():
        if path in parents:
            continue
        index = bisect_right(starts, start) - 1
        if index < 0 or intervals[index][1] < end:
            raise ValueError("original evidence contains an unclassified value")


def encode_episode_views(projections: list[EpisodeProjection]) -> dict[str, Any]:
    return share_exact_values([projection.view for projection in projections])
