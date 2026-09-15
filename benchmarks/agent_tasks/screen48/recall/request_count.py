"""Actual initial Controller body and retained Qwen chat-template counts.

Counting is pluggable. `ControllerRequestCounter` owns everything the study
actually pins in-repo: the hash-bound `coding_controller.Controller` body, the
budget, the workspace file digests and the fit rule. Tokenization is the one
piece that is not in this repository, so it is a subclass hook.

`QwenRequestCounter` is the production implementation. It needs the Qwen
`tokenizer.json` and `tokenizer_config.json` assets, which live on the devbox
eval host rather than in the repository, so its asset directory is a required
constructor argument with no default. The devbox runtime is what supplies the
tokenizer-backed counter to the preparation lane; anything else that satisfies
the `RequestCounter` protocol works the same way.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, runtime_checkable

from benchmarks.agent_tasks.coding_controller import Controller, Usage
from benchmarks.agent_tasks.screen48.contract import (
    BUDGET,
    CONTEXT_TOKENS,
    MEMORY_TOKENS,
    bound,
    sha,
)

CONTROLLER_SHA = "08063b06d522d3539645ef9bc8d5c4a0e8ac419b595c3879d57211ac8f4b219b"
TOKENIZER_SHA = "19564a48c4f71a2a1b937cce34c737a1e662b171c5f5d7edf641a15cd896f07d"
CONFIG_SHA = "fc76878832c668e3f0f8be66e6239a475b9093d2fe5cef97c242369779e6c6e6"


@runtime_checkable
class RequestCounter(Protocol):
    """What the preparation lane needs from a counter, and nothing more."""

    def verify(self) -> None:
        """Re-bind every hashed owner this counter reads. Raise on any drift."""

    def count(self, text: str) -> int:
        """Token count of a standalone string under the controller's tokenizer."""

    def request(self, prompt: str, memory: str, workspace: dict[str, bytes]) -> dict:
        """The exact initial request the controller would send, with its counts."""


class ControllerRequestCounter:
    """The hash-bound controller body; subclasses supply tokenization."""

    def __init__(self) -> None:
        self.controller = Controller
        self.usage = Usage
        self.owner = Path(inspect.getfile(Controller))
        bound(self.owner, CONTROLLER_SHA)

    def count(self, text: str) -> int:
        raise NotImplementedError

    def render(self, body: dict) -> str:
        """Render the wire body through the model's chat template."""
        raise NotImplementedError

    def tokenizer_receipt(self) -> dict:
        """The hashed tokenizer owners recorded alongside every count."""
        raise NotImplementedError

    def verify_tokenizer(self) -> None:
        """Re-bind the tokenizer owners. Default: nothing else to bind."""

    def verify(self) -> None:
        bound(self.owner, CONTROLLER_SHA)
        self.verify_tokenizer()

    def body(self, prompt: str, memory: str) -> dict:
        # No constructor, transport, sandbox or model operation is invoked.
        controller = object.__new__(self.controller)
        controller.request = {
            "controller_model": "qwen/qwen3-coder-next",
            "seed": 0,
            "prompt": prompt,
            "memory_pack": memory,
        }
        controller.usage = self.usage()
        controller._budget = dict(BUDGET)
        controller.options = SimpleNamespace(provider_only=False)
        return controller._body(controller._messages(), BUDGET["output_tokens"])

    def request(self, prompt: str, memory: str, workspace: dict[str, bytes]) -> dict:
        bound(self.owner, CONTROLLER_SHA)
        body = self.body(prompt, memory)
        wire = json.dumps(body, allow_nan=False).encode()
        rendered = self.render(body)
        files = [
            {"path": name, "sha256": sha(data), "tokens": self.count(data.decode())}
            for name, data in sorted(workspace.items())
        ]
        initial = self.count(rendered)
        memory_tokens = self.count(memory)
        workspace_tokens = sum(row["tokens"] for row in files)
        total = initial + workspace_tokens + BUDGET["output_tokens"]
        return {
            "wire_utf8": wire.decode(),
            "body_sha256": sha(wire),
            "rendered_sha256": sha(rendered.encode()),
            "memory_sha256": sha(memory.encode()),
            "memory_tokens": memory_tokens,
            "initial_request_tokens": initial,
            "workspace": files,
            "workspace_tokens": workspace_tokens,
            "initial_plus_workspace_plus_output": total,
            "fits": memory_tokens <= MEMORY_TOKENS and total < CONTEXT_TOKENS,
            "controller_sha256": CONTROLLER_SHA,
            **self.tokenizer_receipt(),
            "budget": dict(BUDGET),
            "provider_usage": None,
            "future_history_fit_guaranteed": False,
        }


class QwenRequestCounter(ControllerRequestCounter):
    """Retained Qwen chat-template counts over hash-bound tokenizer assets."""

    def __init__(self, assets: Path) -> None:
        # Deferred: the tokenizer stack is an eval-host dependency, and the
        # preparation contract must import without it.
        from jinja2.sandbox import ImmutableSandboxedEnvironment  # noqa: PLC0415
        from tokenizers import Tokenizer  # noqa: PLC0415

        super().__init__()
        self.assets = Path(assets)
        tokenizer_bytes = bound(self.assets / "tokenizer.json", TOKENIZER_SHA)
        config_bytes = bound(self.assets / "tokenizer_config.json", CONFIG_SHA)
        self.tokenizer = Tokenizer.from_str(tokenizer_bytes.decode())
        environment = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
        environment.filters["tojson"] = lambda value, **kw: json.dumps(
            value, ensure_ascii=False, **kw
        )
        self.template = environment.from_string(json.loads(config_bytes)["chat_template"])

    def verify_tokenizer(self) -> None:
        bound(self.assets / "tokenizer.json", TOKENIZER_SHA)
        bound(self.assets / "tokenizer_config.json", CONFIG_SHA)

    def tokenizer_receipt(self) -> dict:
        return {"tokenizer_sha256": TOKENIZER_SHA, "tokenizer_config_sha256": CONFIG_SHA}

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)

    def render(self, body: dict) -> str:
        return self.template.render(
            messages=body["messages"], tools=body["tools"], add_generation_prompt=True
        )
