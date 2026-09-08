"""Offline checks for the containerized OpenRouter coding controller.

Nothing here reaches a provider, a docker daemon or a network: the HTTPS
transport is replaced beneath the controller's own opener, and the docker client
is replaced at the subprocess boundary. Everything else is real: real temporary
workspaces, a real staging tree, real modes and a real trace file.
"""

from __future__ import annotations

import base64
import email.message
import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import urllib.response
from dataclasses import dataclass
from pathlib import Path

import pytest
from benchmarks.agent_tasks import coding_controller as controller
from benchmarks.agent_tasks.runner import ControllerResult

# The controller refuses to hand a container a root UID, so a root session could
# only ever assert that refusal.
pytestmark = pytest.mark.skipif(
    os.getuid() == 0, reason="the controller refuses to run containers as uid 0"
)

KEY = "sk-or-v1-b0ddc0ffee-this-must-never-be-recorded"
PROMPT = "Make tests/test_answer.py pass without editing the test."
MEMORY = "Conditional note: earlier attempts wrote the value to answer.txt.\nCafé 💜"
MEMORY_DIGEST = hashlib.sha256(MEMORY.encode()).hexdigest()
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()

IMAGE = "sha256:" + "b" * 64
TOOL_TIMEOUT = 45.0
MEMORY_MB = 512
REDIRECT_STATUS = 302
ARGV = ["--image", IMAGE, "--tool-timeout", str(TOOL_TIMEOUT), "--memory-mb", str(MEMORY_MB)]

BUDGET = {"input_tokens": 4000, "output_tokens": 600, "tool_calls": 3, "cost_usd": 0.5}
REQUEST = {
    "run_id": "run-a",
    "attempt_id": "attempt-b",
    "request_id": "request-c",
    "task_id": "task-one",
    "task_sha256": "a" * 64,
    "pack_id": MEMORY_DIGEST,
    "seed": 7,
    "controller_model": "vendor/model-declared",
    "controller_tools": ["shell"],
    "controller_budget": BUDGET,
    "prompt": PROMPT,
    "memory_pack": MEMORY,
    "memory_pack_sha256": MEMORY_DIGEST,
}

SUCCESS_KINDS = [
    "start",
    "model_request",
    "model_response",
    "tool_call",
    "tool_result",
    "model_request",
    "model_response",
    "terminal",
]
WORKSPACE_PATHS = {".", "answer.txt", "notes.txt", "tests", "tests/test_answer.py"}
SECOND_CALL = 2


# ---------------------------------------------------------------------------
# Doubles for the two process boundaries
# ---------------------------------------------------------------------------


class ProviderResponse(urllib.response.addinfourl):
    msg = "served"


def provider_response(payload=None, *, code=200, body=None, headers=None):
    """A minimal urllib response, so the controller's real opener chain runs."""
    raw = json.dumps(payload).encode() if body is None else body
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    response = ProviderResponse(io.BytesIO(raw), message, controller.ENDPOINT, code)
    return response


def usage(prompt=101, output=17, cost=0.125):
    return {"prompt_tokens": prompt, "completion_tokens": output, "cost": cost}


def tool_call(command, identifier="call-1"):
    return {
        "id": identifier,
        "type": "function",
        "function": {"name": "shell", "arguments": json.dumps({"command": command})},
    }


def completion_payload(message, *, finish_reason="stop", reported=None):
    payload = {
        "id": "gen-abc123",
        "model": "vendor/model-actual",
        "provider": "TestProvider",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
    }
    if reported is not None:
        payload["usage"] = reported
    return payload


def completion(message, *, finish_reason="stop", reported=None):
    return provider_response(
        completion_payload(message, finish_reason=finish_reason, reported=reported)
    )


def calls_tool(command, *, identifier="call-1", reported=None):
    return completion(
        {"role": "assistant", "content": None, "tool_calls": [tool_call(command, identifier)]},
        finish_reason="tool_calls",
        reported=usage() if reported is None else reported,
    )


def replies(content="fixed", *, reported=None):
    return completion(
        {"role": "assistant", "content": content},
        reported=usage(output=23, cost=0.25) if reported is None else reported,
    )


class Provider:
    """Replaces the HTTPS transport beneath the controller's own opener."""

    def __init__(self, *responses):
        self.queued = list(responses)
        self.requests = []
        self.watchers = []

    def install(self, monkeypatch):
        provider = self

        def https_open(handler, request):
            return provider.serve(request)

        monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)

    def serve(self, request):
        self.requests.append(request)
        for watcher in self.watchers:
            watcher(len(self.requests))
        if not self.queued:
            pytest.fail("the controller made an unexpected provider call")
        return self.queued.pop(0)

    def body(self, index):
        return json.loads(self.requests[index].data)

    def messages(self, index):
        return self.body(index)["messages"]


def stage_of(argv):
    """The staging directory the container was actually given."""
    mount = dict(part.split("=", 1) for part in argv[argv.index("--mount") + 1].split(","))
    assert mount["type"] == "bind"
    assert mount["dst"] == "/workspace"
    return Path(mount["src"])


def container(changes=None, *, returncode=0, stdout=b"", stderr=b"", fail=None):
    """One `docker run`: change the staging copy the way a command would."""

    def behaviour(stage):
        if changes is not None:
            changes(stage)
        if fail is not None:
            raise fail
        return returncode, stdout, stderr

    return behaviour


class Docker:
    """Replaces the docker client at the subprocess boundary."""

    def __init__(self, *runs, cleanup=(0, b"", b"")):
        self.runs = list(runs)
        self.cleanup = cleanup
        self.calls = []
        self.environments = []
        self.keywords = []
        self.last_exit = 0
        self.state_error = ""
        self.security_options: object = []

    def install(self, monkeypatch):
        monkeypatch.setattr(controller.subprocess, "run", self._run)

    def _run(self, argv, **keywords):
        argv = list(argv)
        if argv[1] == "info":
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(self.security_options).encode(), b""
            )
        self.calls.append(argv)
        self.environments.append(keywords.get("env"))
        self.keywords.append(keywords)
        if argv[1] == "inspect":
            state = {
                "Status": "exited",
                "ExitCode": self.last_exit,
                "Error": self.state_error,
                "OOMKilled": False,
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(state).encode(), b"")
        if argv[1] != "run":
            return subprocess.CompletedProcess(argv, *self.cleanup)
        if not self.runs:
            pytest.fail("the controller started an unexpected container")
        returncode, stdout, stderr = self.runs.pop(0)(stage_of(argv))
        self.last_exit = returncode
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    def commands(self):
        return [argv[1] for argv in self.calls]


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@dataclass
class Harness:
    home: Path
    workspace: Path
    staging: Path
    docker_path: Path

    @property
    def trace_path(self):
        return self.home / controller.TRACE_NAME

    def run_raw(self, text):
        return controller.main(list(ARGV), io.StringIO(text))

    def run(self, **overrides):
        return self.run_raw(json.dumps({**REQUEST, **overrides}))

    def run_argv(self, argv):
        return controller.main(argv, io.StringIO(json.dumps(REQUEST)))

    def records(self):
        return [json.loads(line) for line in self.trace_path.read_text().splitlines()]

    def kinds(self):
        return [record["kind"] for record in self.records()]

    def payloads(self, kind):
        return [record["payload"] for record in self.records() if record["kind"] == kind]

    def payload(self, kind):
        return self.payloads(kind)[0]

    def snapshot(self):
        """An independent view of the authoritative workspace."""
        return {
            path.relative_to(self.workspace).as_posix(): (
                stat.S_IMODE(path.lstat().st_mode),
                path.read_bytes() if path.is_file() and not path.is_symlink() else None,
            )
            for path in sorted(self.workspace.rglob("*"))
        }


@pytest.fixture
def harness(tmp_path, monkeypatch):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    staging = tmp_path / "staging"
    binaries = tmp_path / "bin"
    for path in (home, workspace, staging, binaries):
        path.mkdir()
    (workspace / "tests").mkdir()
    # Fixed directory modes keep the staged and carried back modes checkable
    # under any umask.
    workspace.chmod(0o755)
    (workspace / "tests").chmod(0o755)
    for name, content in (
        ("answer.txt", b"0\n"),
        ("notes.txt", b"keep\n"),
        ("tests/test_answer.py", b"assert open('../answer.txt').read() == '42\\n'\n"),
    ):
        (workspace / name).write_bytes(content)
        (workspace / name).chmod(0o644)
    docker = binaries / "docker"
    docker.write_text("#!/bin/sh\nexit 66\n")
    docker.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(staging))
    monkeypatch.setenv("PATH", str(binaries))
    monkeypatch.setenv(controller.KEY_VARIABLE, KEY)
    monkeypatch.chdir(workspace)
    return Harness(home, workspace.resolve(), staging.resolve(), docker)


def edit_answer(stage):
    (stage / "answer.txt").write_bytes(b"42\n")


def result_of(capsys):
    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1, "stdout must carry exactly one JSON result"
    return json.loads(captured.out)


# ---------------------------------------------------------------------------
# The successful loop
# ---------------------------------------------------------------------------


def test_a_successful_attempt_traces_the_whole_sequence(harness, monkeypatch, capsys):
    provider = Provider(
        calls_tool("echo 42 > answer.txt", reported=usage(prompt=101, output=17, cost=0.125)),
        replies(reported=usage(prompt=131, output=23, cost=0.25)),
    )
    provider.install(monkeypatch)
    Docker(container(edit_answer, stdout=b"ok\n")).install(monkeypatch)

    code = harness.run()

    result = result_of(capsys)
    assert code == 0
    assert harness.kinds() == SUCCESS_KINDS
    records = harness.records()
    assert [record["index"] for record in records] == list(range(len(SUCCESS_KINDS)))
    assert {record["attempt_id"] for record in records} == {"attempt-b"}
    assert {record["request_id"] for record in records} == {"request-c"}
    assert {record["schema_version"] for record in records} == {"sibyl-coding-trace-v1"}
    assert result == {
        "input_tokens": 232,
        "output_tokens": 40,
        "cost_usd": 0.375,
        "tool_calls": 1,
        "model": "vendor/model-declared",
        "synthetic": False,
        "trace_sha256": hashlib.sha256(harness.trace_path.read_bytes()).hexdigest(),
    }
    assert (harness.workspace / "answer.txt").read_bytes() == b"42\n"
    assert harness.payload("terminal")["reason"] == "stop"


def test_the_start_record_pins_the_script_prompt_and_options(harness, monkeypatch, capsys):
    Provider(replies()).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == 0

    start = harness.payload("start")
    source = Path(controller.__file__).read_bytes()
    assert start["script_sha256"] == hashlib.sha256(source).hexdigest()
    assert start["image"] == IMAGE
    assert start["options"]["endpoint"] == "https://openrouter.ai/api/v1/chat/completions"
    assert start["options"]["tool_timeout_seconds"] == TOOL_TIMEOUT
    assert start["options"]["memory_mb"] == MEMORY_MB
    assert start["options"]["retries"] == 0
    assert start["options"]["redirects"] is False
    assert start["options"]["stream"] is False
    assert start["interpreter"]["version"]
    assert start["identity"]["uid"] == os.getuid()
    assert start["identity"]["workspace"] == str(harness.workspace)
    # No claim of a sealed sandbox, guaranteed cleanup or attested usage.
    assert start["claims"] == {
        "sealed_isolation": False,
        "container_cleanup_guaranteed": False,
        "usage_attested": False,
    }
    assert "prompt" not in start["request"]
    assert "memory_pack" not in start["request"]
    assert start["request"]["controller_budget"] == BUDGET


def test_the_prompt_and_the_memory_pack_stay_separate_and_exact(harness, monkeypatch, capsys):
    provider = Provider(replies())
    provider.install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == 0

    messages = provider.messages(0)
    assert [message["role"] for message in messages] == ["system", "user", "user"]
    assert messages[1]["content"] == PROMPT
    assert messages[2]["content"] == MEMORY
    # Neither text is folded into the other, and memory is not the authority.
    assert MEMORY not in messages[0]["content"]
    assert MEMORY not in messages[1]["content"]
    assert PROMPT not in messages[0]["content"]
    assert "instruction authority" in messages[0]["content"]
    system_digest = hashlib.sha256(messages[0]["content"].encode()).hexdigest()
    assert harness.payload("start")["system_prompt_sha256"] == system_digest
    assert harness.payload("start")["memory_pack_message_included"] is True


def test_the_request_body_declares_the_fixed_call_shape(harness, monkeypatch, capsys):
    provider = Provider(replies())
    provider.install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == 0

    body = provider.body(0)
    assert body["model"] == REQUEST["controller_model"]
    assert body["seed"] == REQUEST["seed"]
    assert body["stream"] is False
    assert body["max_tokens"] == BUDGET["output_tokens"]
    assert body["tool_choice"] == "auto"
    assert [tool["function"]["name"] for tool in body["tools"]] == ["shell"]
    assert body["tools"][0]["function"]["parameters"]["additionalProperties"] is False
    request = harness.payload("model_request")
    assert request["url"] == controller.ENDPOINT
    assert request["method"] == "POST"
    assert request["body"] == body
    assert request["body_sha256"] == hashlib.sha256(provider.requests[0].data).hexdigest()
    assert "Authorization" not in request["headers"]


def test_an_empty_memory_control_sends_no_memory_message(harness, monkeypatch, capsys):
    provider = Provider(replies())
    provider.install(monkeypatch)
    Docker().install(monkeypatch)

    code = harness.run(memory_pack="", memory_pack_sha256=EMPTY_DIGEST, pack_id=EMPTY_DIGEST)

    assert code == 0
    assert [message["role"] for message in provider.messages(0)] == ["system", "user"]
    assert harness.payload("start")["memory_pack_message_included"] is False


def test_the_response_provenance_is_retained_beside_the_declared_model(
    harness, monkeypatch, capsys
):
    raw = json.dumps(
        completion_payload({"role": "assistant", "content": "fixed"}, reported=usage())
    ).encode()
    Provider(provider_response(body=raw)).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == 0

    response = harness.payload("model_response")
    assert response["status_code"] == controller.HTTP_OK
    assert response["response_model"] == "vendor/model-actual"
    assert response["response_provider"] == "TestProvider"
    assert response["generation_id"] == "gen-abc123"
    assert response["body_sha256"] == hashlib.sha256(raw).hexdigest()
    assert response["raw"] == json.loads(raw)
    # The result declares the manifest's model, not whatever answered.
    assert result_of(capsys)["model"] == REQUEST["controller_model"]


def test_reasoning_details_return_in_the_followup_assistant_message(harness, monkeypatch, capsys):
    details = [{"type": "reasoning.text", "text": "private chain about answer.txt"}]
    message = {
        "role": "assistant",
        "content": None,
        "reasoning_details": details,
        "tool_calls": [tool_call("echo 42 > answer.txt")],
    }
    provider = Provider(
        completion(message, finish_reason="tool_calls", reported=usage()), replies()
    )
    provider.install(monkeypatch)
    Docker(container(edit_answer)).install(monkeypatch)

    assert harness.run() == 0

    followup = provider.messages(1)
    assert followup[-2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [tool_call("echo 42 > answer.txt")],
        "reasoning_details": details,
    }
    assert followup[-1]["role"] == "tool"
    assert followup[-1]["tool_call_id"] == "call-1"
    assert "private chain" not in capsys.readouterr().out


def test_every_record_is_durable_before_the_next_provider_call(harness, monkeypatch, capsys):
    provider = Provider(calls_tool("echo 42 > answer.txt"), replies())
    seen = {}

    def watcher(count):
        if count == SECOND_CALL:
            seen["kinds"] = harness.kinds()

    provider.watchers.append(watcher)
    provider.install(monkeypatch)
    Docker(container(edit_answer)).install(monkeypatch)

    assert harness.run() == 0
    # Everything before the second request is already complete JSON on disk.
    assert seen["kinds"] == SUCCESS_KINDS[:6]


def test_the_result_matches_the_runner_result_contract(harness, monkeypatch, capsys):
    Provider(replies()).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == 0

    result = result_of(capsys)
    assert set(result) == {
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "tool_calls",
        "model",
        "synthetic",
        "trace_sha256",
    }
    parsed = ControllerResult.model_validate(result)
    assert parsed.synthetic is False
    assert parsed.model == REQUEST["controller_model"]
    assert parsed.trace_sha256 == hashlib.sha256(harness.trace_path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Credential handling
# ---------------------------------------------------------------------------


def test_the_key_reaches_only_the_provider_request(harness, monkeypatch, capsys):
    provider = Provider(calls_tool("echo 42 > answer.txt"), replies())
    provider.install(monkeypatch)
    docker = Docker(container(edit_answer))
    docker.install(monkeypatch)

    assert harness.run() == 0

    assert provider.requests[0].get_header("Authorization") == f"Bearer {KEY}"
    assert KEY not in capsys.readouterr().out
    assert KEY.encode() not in harness.trace_path.read_bytes()
    for argv in docker.calls:
        assert KEY not in " ".join(argv)
    for environment in docker.environments:
        assert controller.KEY_VARIABLE not in environment
        assert KEY not in environment.values()
        assert set(environment) <= set(controller.CLIENT_ENVIRONMENT_KEYS)
    # The key is read once from the environment and dropped from this process.
    assert controller.KEY_VARIABLE not in os.environ


@pytest.mark.parametrize("value", ["with space", "line\nbreak", "tab\tseparated"])
def test_an_unusable_key_stops_before_any_call(harness, monkeypatch, capsys, value):
    monkeypatch.setenv(controller.KEY_VARIABLE, value)
    provider = Provider()
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["invalid_request"]

    assert provider.requests == []
    assert docker.calls == []
    assert harness.kinds() == ["terminal"]
    assert value not in harness.trace_path.read_text()
    assert result_of(capsys)["model"] is None


def test_a_missing_key_stops_before_any_call(harness, monkeypatch, capsys):
    monkeypatch.delenv(controller.KEY_VARIABLE)
    provider = Provider()
    provider.install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["invalid_request"]

    assert provider.requests == []
    assert controller.KEY_VARIABLE in harness.payload("terminal")["detail"]
    assert result_of(capsys)["input_tokens"] == 0


def test_the_opener_refuses_to_follow_a_redirect(monkeypatch):
    opener = controller._new_opener()
    seen = []

    def https_open(handler, request):
        seen.append(request.full_url)
        return provider_response(
            code=REDIRECT_STATUS,
            body=b"",
            headers={"Location": "https://elsewhere.example/v1/chat"},
        )

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    request = urllib.request.Request(  # noqa: S310
        controller.ENDPOINT, data=b"{}", method="POST", headers={"Authorization": "Bearer x"}
    )

    with pytest.raises(urllib.error.HTTPError) as raised:
        opener.open(request, timeout=1)

    assert raised.value.code == REDIRECT_STATUS
    # The credential was offered once, to the pinned endpoint only.
    assert seen == [controller.ENDPOINT]


def test_a_redirect_ends_the_attempt_without_a_second_destination(harness, monkeypatch, capsys):
    provider = Provider(
        provider_response(
            code=REDIRECT_STATUS,
            body=b"moved along",
            headers={"Location": "https://elsewhere.example/v1/chat"},
        )
    )
    provider.install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["provider_error"]

    assert len(provider.requests) == 1
    assert harness.payload("model_response") == {
        "status_code": REDIRECT_STATUS,
        "error_class": "HTTPError",
        "raw": None,
    }
    trace = harness.trace_path.read_bytes()
    assert b"elsewhere.example" not in trace
    assert b"moved along" not in trace


@pytest.mark.parametrize("code", [400, 401, 429, 500])
def test_a_failed_provider_call_records_only_class_and_status(harness, monkeypatch, capsys, code):
    body = json.dumps({"error": {"message": f"rejected key {KEY}"}}).encode()
    Provider(provider_response(code=code, body=body)).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["provider_error"]

    assert harness.payload("model_response") == {
        "status_code": code,
        "error_class": "HTTPError",
        "raw": None,
    }
    trace = harness.trace_path.read_bytes()
    assert KEY.encode() not in trace
    assert b"rejected key" not in trace
    # A failed call may still have been billed, so nothing is claimed as zero.
    result = result_of(capsys)
    assert result["input_tokens"] is None
    assert result["output_tokens"] is None
    assert result["cost_usd"] is None
    assert result["tool_calls"] == 0
    assert harness.payload("terminal")["reason"] == "provider_error"


def test_a_transport_failure_is_not_retried(harness, monkeypatch, capsys):
    def https_open(handler, request):
        raise urllib.error.URLError("connection reset by peer")

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["provider_error"]

    assert harness.kinds() == ["start", "model_request", "model_response", "terminal"]
    assert harness.payload("model_response")["error_class"] == "URLError"
    assert harness.payload("model_response")["status_code"] is None
    assert b"connection reset" not in harness.trace_path.read_bytes()


# ---------------------------------------------------------------------------
# Reported usage and budgets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("missing", "unknown"),
    [
        ("cost", "cost_usd"),
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
    ],
)
def test_unreported_usage_stops_before_another_paid_call(
    harness, monkeypatch, capsys, missing, unknown
):
    reported = usage()
    del reported[missing]
    provider = Provider(calls_tool("echo 42 > answer.txt", reported=reported))
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["usage_unreported"]

    assert len(provider.requests) == 1
    assert docker.calls == []
    assert harness.kinds() == ["start", "model_request", "model_response", "terminal"]
    result = result_of(capsys)
    assert result[unknown] is None
    known = {"input_tokens", "output_tokens", "cost_usd"} - {unknown}
    assert all(result[name] is not None for name in known)
    assert harness.payload("terminal")["reason"] == "usage_unreported"


@pytest.mark.parametrize("value", [True, "0.5", -1, None, [1]])
def test_a_non_number_is_never_counted_as_usage(harness, monkeypatch, capsys, value):
    Provider(calls_tool("ls", reported={**usage(), "prompt_tokens": value})).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["usage_unreported"]

    result = result_of(capsys)
    assert result["input_tokens"] is None
    assert result["cost_usd"] == usage()["cost"]


def test_a_fractional_token_count_is_not_a_token_count(harness, monkeypatch, capsys):
    Provider(calls_tool("ls", reported={**usage(), "completion_tokens": 17.5})).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["usage_unreported"]
    assert result_of(capsys)["output_tokens"] is None


@pytest.mark.parametrize("limit", ["input_tokens", "output_tokens", "cost_usd"])
def test_a_reported_excess_stops_with_the_true_numbers(harness, monkeypatch, capsys, limit):
    reported = {
        "input_tokens": usage(prompt=4001),
        "output_tokens": usage(output=601),
        "cost_usd": usage(cost=0.75),
    }[limit]
    provider = Provider(calls_tool("echo 42 > answer.txt", reported=reported))
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)
    before = harness.snapshot()

    assert harness.run() == controller.EXIT_CODES["budget"]

    assert len(provider.requests) == 1
    assert docker.calls == []
    assert harness.snapshot() == before
    result = result_of(capsys)
    assert result["input_tokens"] == reported["prompt_tokens"]
    assert result["output_tokens"] == reported["completion_tokens"]
    assert result["cost_usd"] == reported["cost"]
    terminal = harness.payload("terminal")
    assert terminal["reason"] == "budget"
    assert limit in terminal["detail"]


@pytest.mark.parametrize("limit", ["input_tokens", "output_tokens"])
def test_an_exhausted_token_budget_stops_before_the_first_call(harness, monkeypatch, capsys, limit):
    provider = Provider()
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)

    code = harness.run(controller_budget={**BUDGET, limit: 0})

    assert code == controller.EXIT_CODES["budget"]
    assert provider.requests == []
    assert docker.calls == []
    assert harness.kinds() == ["start", "terminal"]
    assert result_of(capsys)["tool_calls"] == 0


def test_max_tokens_never_exceeds_the_remaining_output_budget(harness, monkeypatch, capsys):
    budget = {**BUDGET, "output_tokens": 100}
    provider = Provider(
        calls_tool("echo 42 > answer.txt", reported=usage(prompt=10, output=60, cost=0.0)),
        replies(reported=usage(prompt=10, output=5, cost=0.0)),
    )
    provider.install(monkeypatch)
    Docker(container(edit_answer)).install(monkeypatch)

    assert harness.run(controller_budget=budget) == 0

    assert provider.body(0)["max_tokens"] == budget["output_tokens"]
    assert provider.body(1)["max_tokens"] == budget["output_tokens"] - 60


def test_a_truncated_reply_is_a_budget_stop(harness, monkeypatch, capsys):
    Provider(
        completion(
            {"role": "assistant", "content": "half a th"},
            finish_reason="length",
            reported=usage(),
        )
    ).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["budget"]
    assert harness.payload("terminal")["reason"] == "budget"


def test_no_tool_budget_stops_before_a_container_starts(harness, monkeypatch, capsys):
    provider = Provider(calls_tool("rm -rf /"))
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)
    before = harness.snapshot()

    code = harness.run(controller_budget={**BUDGET, "tool_calls": 0})

    assert code == controller.EXIT_CODES["budget"]
    assert docker.calls == []
    assert "tool_call" not in harness.kinds()
    assert harness.snapshot() == before
    assert result_of(capsys)["tool_calls"] == 0


def test_the_tool_budget_stops_a_tool_calling_model(harness, monkeypatch, capsys):
    provider = Provider(
        calls_tool("echo 42 > answer.txt", identifier="call-1"),
        calls_tool("echo 43 > answer.txt", identifier="call-2"),
    )
    provider.install(monkeypatch)
    docker = Docker(container(edit_answer))
    docker.install(monkeypatch)

    code = harness.run(controller_budget={**BUDGET, "tool_calls": 1})

    assert code == controller.EXIT_CODES["budget"]
    assert provider.queued == []
    assert docker.commands() == ["run", "inspect", "stop", "rm"]
    assert harness.payload("terminal")["reason"] == "budget"
    assert result_of(capsys)["tool_calls"] == 1


def test_repeated_refusals_end_at_the_step_limit(harness, monkeypatch, capsys):
    unknown = {
        "id": "call-x",
        "type": "function",
        "function": {"name": "write_file", "arguments": "{}"},
    }
    provider = Provider(
        *[
            completion(
                {"role": "assistant", "content": None, "tool_calls": [unknown]},
                finish_reason="tool_calls",
                reported=usage(cost=0.0),
            )
            for _ in range(3)
        ]
    )
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)

    code = harness.run(controller_budget={**BUDGET, "tool_calls": 2})

    assert code == controller.EXIT_CODES["budget"]
    assert provider.queued == []
    assert docker.calls == []
    refusals = harness.payloads("tool_result")
    assert [payload["status"] for payload in refusals] == ["refused"] * 2
    assert all("write_file" in payload["refusal"] for payload in refusals)
    assert harness.payload("terminal")["reason"] == "budget"
    # Refused invocations consume the same declared tool budget.
    assert result_of(capsys)["tool_calls"] == len(refusals)


# ---------------------------------------------------------------------------
# The container boundary
# ---------------------------------------------------------------------------


def test_the_shell_runs_only_in_a_container_on_a_staging_copy(harness, monkeypatch, capsys):
    seen = {}

    def changes(stage):
        seen["stage"] = stage
        seen["copy"] = (stage / "answer.txt").read_bytes()
        seen["live_during"] = (harness.workspace / "answer.txt").read_bytes()
        (stage / "answer.txt").write_bytes(b"42\n")

    provider = Provider(calls_tool("echo 42 > answer.txt"), replies())
    provider.install(monkeypatch)
    docker = Docker(container(changes))
    docker.install(monkeypatch)

    assert harness.run() == 0

    stage = seen["stage"]
    name = harness.payload("tool_call")["container"]
    assert name.startswith("sibyl-coding-")
    assert docker.calls[0] == [
        str(harness.docker_path),
        "run",
        "--name",
        name,
        "--network",
        "none",
        "--pull",
        "never",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--memory",
        f"{MEMORY_MB}m",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev",  # noqa: S108
        "--mount",
        f"type=bind,src={stage},dst=/workspace",
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp",
        "--env",
        "TMPDIR=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--entrypoint",
        "/bin/sh",
        IMAGE,
        "-c",
        "echo 42 > answer.txt",
    ]
    # One bind, and it is a staging copy under TMPDIR, never the live tree.
    assert docker.calls[0].count("--mount") == 1
    assert not {"-v", "--volume", "--privileged", "--net"} & set(docker.calls[0])
    assert str(harness.workspace) not in " ".join(docker.calls[0])
    assert stage.parent == harness.staging
    assert not stage.is_relative_to(harness.workspace)
    assert seen["copy"] == b"0\n"
    assert seen["live_during"] == b"0\n"
    assert (harness.workspace / "answer.txt").read_bytes() == b"42\n"
    assert docker.keywords[0]["timeout"] == TOOL_TIMEOUT
    assert docker.keywords[0]["check"] is False
    # Each call stages fresh and leaves nothing behind.
    assert list(harness.staging.iterdir()) == []
    assert harness.payload("tool_result")["stage_removed"] is True


def test_copy_back_preserves_edits_deletions_and_modes(harness, monkeypatch, capsys):
    def changes(stage):
        (stage / "answer.txt").write_bytes(b"42\n")
        (stage / "notes.txt").unlink()
        script = stage / "fix.sh"
        script.write_bytes(b"#!/bin/sh\ntrue\n")
        script.chmod(0o755)
        (stage / "tests" / "test_answer.py").chmod(0o600)
        locked = stage / "locked"
        locked.mkdir()
        (locked / "inner.txt").write_bytes(b"inner\n")
        (locked / "inner.txt").chmod(0o444)
        locked.chmod(0o500)

    Provider(calls_tool("./fix.sh"), replies()).install(monkeypatch)
    Docker(container(changes)).install(monkeypatch)

    try:
        assert harness.run() == 0
        live = harness.snapshot()
        assert live["answer.txt"] == (0o644, b"42\n")
        assert "notes.txt" not in live
        assert live["fix.sh"] == (0o755, b"#!/bin/sh\ntrue\n")
        assert live["locked/inner.txt"] == (0o444, b"inner\n")
        # Every mode is carried across exactly, not normalized.
        assert {path: value[0] for path, value in live.items()} == {
            "answer.txt": 0o644,
            "fix.sh": 0o755,
            "locked": 0o500,
            "locked/inner.txt": 0o444,
            "tests": 0o755,
            "tests/test_answer.py": 0o600,
        }
        result = harness.payload("tool_result")
        assert result["carried_back"] is True
        before = {item["path"] for item in result["workspace_before"]}
        after = {item["path"] for item in result["workspace_after"]}
        assert before == WORKSPACE_PATHS
        assert after - before == {"fix.sh", "locked", "locked/inner.txt"}
        assert before - after == {"notes.txt"}
        modes = {item["path"]: item["mode"] for item in result["workspace_after"]}
        assert {path: modes[path] for path in ("fix.sh", "locked")} == {
            "fix.sh": 0o755,
            "locked": 0o500,
        }
    finally:
        locked = harness.workspace / "locked"
        if locked.is_dir():
            locked.chmod(0o700)


def test_a_second_call_stages_the_carried_back_state(harness, monkeypatch, capsys):
    seen = {}

    def first(stage):
        (stage / "answer.txt").write_bytes(b"41\n")

    def second(stage):
        seen["copy"] = (stage / "answer.txt").read_bytes()
        (stage / "answer.txt").write_bytes(b"42\n")

    provider = Provider(
        calls_tool("echo 41 > answer.txt", identifier="call-1"),
        calls_tool("echo 42 > answer.txt", identifier="call-2"),
        replies(reported=usage(cost=0.0)),
    )
    provider.install(monkeypatch)
    Docker(container(first), container(second)).install(monkeypatch)

    assert harness.run() == 0

    assert seen["copy"] == b"41\n"
    assert (harness.workspace / "answer.txt").read_bytes() == b"42\n"
    stages = [payload["stage"] for payload in harness.payloads("tool_call")]
    assert stages[0] != stages[1]
    identifiers = [payload["tool_call_id"] for payload in harness.payloads("tool_result")]
    assert identifiers == ["call-1", "call-2"]
    # A call and its result carry the same index, so the pair is unambiguous.
    for kind in ("tool_call", "tool_result"):
        assert [payload["index"] for payload in harness.payloads(kind)] == [0, 1]
    assert [payload["stage"] for payload in harness.payloads("tool_result")] == stages
    assert result_of(capsys)["tool_calls"] == len(identifiers)


def test_a_failing_test_command_is_a_valid_tool_result(harness, monkeypatch, capsys):
    provider = Provider(calls_tool("pytest -q"), replies())
    provider.install(monkeypatch)
    Docker(
        container(edit_answer, returncode=1, stdout=b"1 failed\n", stderr=b"E  assert 0\n")
    ).install(monkeypatch)

    assert harness.run() == 0

    result = harness.payload("tool_result")
    assert result["status"] == "ok"
    assert result["returncode"] == 1
    assert result["carried_back"] is True
    assert (harness.workspace / "answer.txt").read_bytes() == b"42\n"
    reply = provider.messages(1)[-1]
    assert reply["content"].startswith("exit_code: 1")
    assert "1 failed" in reply["content"]
    assert "E  assert 0" in reply["content"]
    assert harness.payload("terminal")["reason"] == "stop"


@pytest.mark.parametrize("code", [125, 126, 127])
def test_a_docker_operation_failure_is_not_a_task_outcome(harness, monkeypatch, capsys, code):
    provider = Provider(calls_tool("pytest -q"))
    provider.install(monkeypatch)
    docker = Docker(container(edit_answer, returncode=code))
    docker.state_error = "container could not start"
    docker.install(monkeypatch)
    before = harness.snapshot()

    assert harness.run() == controller.EXIT_CODES["operational_error"]

    assert harness.snapshot() == before
    result = harness.payload("tool_result")
    assert result["status"] == "operational"
    assert result["returncode"] == code
    assert result["carried_back"] is False
    assert harness.payload("terminal")["reason"] == "operational_error"
    # The model never gets to read a broken sandbox as a task signal.
    assert len(provider.requests) == 1


def test_a_missing_docker_client_is_an_operational_failure(harness, monkeypatch, capsys):
    provider = Provider(calls_tool("pytest -q"))
    provider.install(monkeypatch)
    Docker(container(edit_answer, fail=FileNotFoundError("docker"))).install(monkeypatch)
    before = harness.snapshot()

    assert harness.run() == controller.EXIT_CODES["operational_error"]

    assert harness.snapshot() == before
    assert harness.payload("tool_result")["detail"] == "FileNotFoundError"


def test_a_timed_out_container_never_carries_its_stage_back(harness, monkeypatch, capsys):
    def changes(stage):
        (stage / "answer.txt").write_bytes(b"half written\n")

    expired = subprocess.TimeoutExpired(
        cmd="docker", timeout=TOOL_TIMEOUT, output=b"partial\n", stderr=b""
    )
    provider = Provider(calls_tool("sleep 600"), replies())
    provider.install(monkeypatch)
    Docker(container(changes, fail=expired)).install(monkeypatch)
    before = harness.snapshot()

    assert harness.run() == 0

    assert harness.snapshot() == before
    result = harness.payload("tool_result")
    assert result["status"] == "timeout"
    assert result["returncode"] is None
    assert result["stdout"] == "partial\n"
    assert result["carried_back"] is False
    assert result["cleanup"]["terminated"] is True
    reply = provider.messages(1)[-1]
    assert reply["content"].startswith(f"timed out after {TOOL_TIMEOUT}s")
    assert "partial" in reply["content"]
    assert list(harness.staging.iterdir()) == []


def test_cleanup_touches_only_the_container_this_call_named(harness, monkeypatch, capsys):
    provider = Provider(calls_tool("echo 42 > answer.txt"), replies())
    provider.install(monkeypatch)
    docker = Docker(container(edit_answer))
    docker.install(monkeypatch)

    assert harness.run() == 0

    name = harness.payload("tool_call")["container"]
    assert docker.commands() == ["run", "inspect", "stop", "rm"]
    for argv in docker.calls[1:]:
        assert argv[-1] == name
        assert not {"prune", "-a", "--all", "--filter", "kill"} & set(argv)
    assert docker.calls[2] == [str(harness.docker_path), "stop", "--time", "0", name]
    assert docker.calls[3] == [str(harness.docker_path), "rm", "--force", name]


def test_unconfirmed_cleanup_denies_the_copy_back(harness, monkeypatch, capsys):
    provider = Provider(calls_tool("echo 42 > answer.txt"))
    provider.install(monkeypatch)
    Docker(
        container(edit_answer), cleanup=(1, b"", b"Error response from daemon: daemon unreachable")
    ).install(monkeypatch)
    before = harness.snapshot()

    assert harness.run() == controller.EXIT_CODES["operational_error"]

    assert harness.snapshot() == before
    result = harness.payload("tool_result")
    assert result["cleanup"]["terminated"] is False
    assert result["status"] == "operational"
    assert result["carried_back"] is False
    assert result["stage_removed"] is False
    assert harness.payload("terminal")["reason"] == "operational_error"


def test_an_auto_removed_container_still_counts_as_stopped(harness, monkeypatch, capsys):
    provider = Provider(calls_tool("echo 42 > answer.txt"), replies())
    provider.install(monkeypatch)
    Docker(
        container(edit_answer),
        cleanup=(1, b"", b"Error response from daemon: No such container: sibyl-coding-x"),
    ).install(monkeypatch)

    assert harness.run() == 0

    assert harness.payload("tool_result")["cleanup"]["terminated"] is True
    assert (harness.workspace / "answer.txt").read_bytes() == b"42\n"


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_an_unsafe_staging_entry_refuses_the_copy_back(harness, monkeypatch, capsys, kind):
    def changes(stage):
        (stage / "answer.txt").write_bytes(b"42\n")
        if kind == "symlink":
            (stage / "sneaky").symlink_to("/etc/passwd")
        else:
            os.mkfifo(stage / "sneaky")

    provider = Provider(calls_tool("ln -s /etc/passwd sneaky"), replies())
    provider.install(monkeypatch)
    Docker(container(changes, stdout=b"linked\n")).install(monkeypatch)
    before = harness.snapshot()

    assert harness.run() == 0

    # Nothing from that container reached the authoritative workspace.
    assert harness.snapshot() == before
    assert not (harness.workspace / "sneaky").exists()
    result = harness.payload("tool_result")
    assert result["status"] == "refused"
    assert result["carried_back"] is False
    assert "sneaky" in result["refusal"]
    assert result["workspace_before"] == result["workspace_after"]
    assert result["stage_removed"] is True
    reply = provider.messages(1)[-1]
    assert reply["content"].startswith("refused:")
    assert "linked" in reply["content"]
    assert reply["tool_call_id"] == "call-1"


def test_a_setuid_bit_in_the_stage_refuses_the_copy_back(harness, monkeypatch, capsys):
    def changes(stage):
        target = stage / "answer.txt"
        target.write_bytes(b"42\n")
        target.chmod(0o644 | stat.S_ISUID)

    provider = Provider(calls_tool("chmod u+s answer.txt"), replies())
    provider.install(monkeypatch)
    Docker(container(changes)).install(monkeypatch)
    before = harness.snapshot()

    assert harness.run() == 0

    assert harness.snapshot() == before
    result = harness.payload("tool_result")
    assert result["status"] == "refused"
    assert "special permission bits" in result["refusal"]


def test_an_unreadable_staging_directory_refuses_rather_than_normalizing(
    harness, monkeypatch, capsys
):
    def changes(stage):
        (stage / "answer.txt").write_bytes(b"42\n")
        hidden = stage / "hidden"
        hidden.mkdir()
        (hidden / "inner.txt").write_bytes(b"inner\n")
        hidden.chmod(0)

    provider = Provider(calls_tool("mkdir hidden && chmod 000 hidden"), replies())
    provider.install(monkeypatch)
    Docker(container(changes)).install(monkeypatch)
    before = harness.snapshot()

    try:
        assert harness.run() == 0
        assert harness.snapshot() == before
        result = harness.payload("tool_result")
        assert result["status"] == "refused"
        assert "cannot read the tree completely" in result["refusal"]
    finally:
        for stage in harness.staging.iterdir():
            if (stage / "hidden").is_dir():
                (stage / "hidden").chmod(0o700)


def test_an_unsafe_workspace_stops_before_any_provider_call(harness, monkeypatch, capsys):
    (harness.workspace / "escape").symlink_to("/etc")
    provider = Provider()
    provider.install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["operational_error"]

    assert provider.requests == []
    assert harness.payload("start")["workspace_initial"] is None
    assert harness.payload("terminal")["reason"] == "operational_error"


def test_the_whole_tool_output_is_retained_and_shown(harness, monkeypatch, capsys):
    noisy = "".join(f"line {index:06d}\n" for index in range(12000))
    provider = Provider(calls_tool("pytest -q"), replies())
    provider.install(monkeypatch)
    Docker(container(edit_answer, stdout=noisy.encode())).install(monkeypatch)

    assert harness.run() == 0

    result = harness.payload("tool_result")
    assert result["stdout"] == noisy
    assert result["stdout_sha256"] == hashlib.sha256(noisy.encode()).hexdigest()
    assert noisy in provider.messages(1)[-1]["content"]


# ---------------------------------------------------------------------------
# Declared inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tools", [["bash"], ["shell", "write_file"], [], "shell", ["Shell"], None])
def test_a_different_declared_tool_set_is_refused(harness, monkeypatch, capsys, tools):
    provider = Provider()
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)

    assert harness.run(controller_tools=tools) == controller.EXIT_CODES["invalid_request"]

    assert provider.requests == []
    assert docker.calls == []
    assert harness.kinds() == ["terminal"]
    assert harness.payload("terminal")["reason"] == "invalid_request"
    result = result_of(capsys)
    assert result["model"] is None
    assert result["synthetic"] is False
    assert result["tool_calls"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        {"seed": True},
        {"seed": -1},
        {"seed": "7"},
        {"prompt": 12},
        {"memory_pack_sha256": "a" * 64},
        {"task_sha256": "not-a-digest"},
        {"pack_id": ""},
        {"controller_model": 5},
        {"controller_budget": {**BUDGET, "cost_usd": "0.5"}},
        {"controller_budget": {**BUDGET, "cost_usd": -1}},
        {"controller_budget": {**BUDGET, "input_tokens": 1.5}},
        {"controller_budget": {**BUDGET, "extra": 1}},
        {"unexpected": True},
        {"endpoint": "https://proxy.example/v1"},
    ],
)
def test_an_invalid_request_stops_before_any_call(harness, monkeypatch, capsys, mutation):
    provider = Provider()
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)

    assert harness.run(**mutation) == controller.EXIT_CODES["invalid_request"]

    assert provider.requests == []
    assert docker.calls == []
    assert harness.payload("terminal")["reason"] == "invalid_request"


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"seed": 7, "seed": 8}',
        json.dumps({key: value for key, value in REQUEST.items() if key != "prompt"}),
        json.dumps({key: value for key, value in REQUEST.items() if key != "controller_budget"}),
    ],
)
def test_a_malformed_request_document_stops_before_any_call(harness, monkeypatch, capsys, text):
    provider = Provider()
    provider.install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run_raw(text) == controller.EXIT_CODES["invalid_request"]

    assert provider.requests == []
    assert harness.payload("terminal")["reason"] == "invalid_request"


@pytest.mark.parametrize(
    "argv",
    [
        ["--image", "alpine:3.20", "--tool-timeout", "45", "--memory-mb", "512"],
        ["--image", "sha256:abc", "--tool-timeout", "45", "--memory-mb", "512"],
        ["--image", "sha256:" + "B" * 64, "--tool-timeout", "45", "--memory-mb", "512"],
        ["--image", IMAGE, "--tool-timeout", "0", "--memory-mb", "512"],
        ["--image", IMAGE, "--tool-timeout", "-1", "--memory-mb", "512"],
        ["--image", IMAGE, "--tool-timeout", "nan", "--memory-mb", "512"],
        ["--image", IMAGE, "--tool-timeout", "soon", "--memory-mb", "512"],
        ["--image", IMAGE, "--tool-timeout", "45", "--memory-mb", "0"],
        ["--image", IMAGE, "--tool-timeout", "45", "--memory-mb", "-8"],
        ["--image", IMAGE, "--tool-timeout", "45"],
        ["--image", IMAGE, "--tool-timeout", "45", "--memory-mb", "512", "--network", "host"],
        [*ARGV, "--endpoint", "https://proxy.example/v1"],
    ],
)
def test_invalid_arguments_stop_before_any_call(harness, monkeypatch, capsys, argv):
    provider = Provider()
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)

    assert harness.run_argv(argv) == controller.EXIT_CODES["invalid_request"]

    assert provider.requests == []
    assert docker.calls == []
    assert harness.payload("terminal")["reason"] == "invalid_request"


def test_a_root_controller_refuses_to_run(harness, monkeypatch, capsys):
    monkeypatch.setattr(controller.os, "getuid", lambda: 0)
    provider = Provider()
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["invalid_request"]

    assert provider.requests == []
    assert docker.calls == []
    assert "uid 0" in harness.payload("terminal")["detail"]


def test_a_missing_docker_executable_stops_before_any_call(harness, monkeypatch, capsys):
    harness.docker_path.unlink()
    monkeypatch.setattr(controller.shutil, "which", lambda *args, **kwargs: None)
    provider = Provider()
    provider.install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["invalid_request"]

    assert provider.requests == []
    assert "docker" in harness.payload("terminal")["detail"]


# ---------------------------------------------------------------------------
# Trace placement, malformed responses and refused tool calls
# ---------------------------------------------------------------------------


def test_the_trace_lives_outside_the_workspace(harness, monkeypatch, capsys):
    Provider(replies()).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == 0

    assert harness.trace_path.exists()
    assert not (harness.workspace / controller.TRACE_NAME).exists()
    initial = harness.payload("start")["workspace_initial"]
    assert {item["path"] for item in initial} == WORKSPACE_PATHS


def test_a_home_inside_the_workspace_is_refused(harness, monkeypatch, capsys):
    inside = harness.workspace / "home"
    inside.mkdir()
    monkeypatch.setenv("HOME", str(inside))
    provider = Provider()
    provider.install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["invalid_request"]

    assert provider.requests == []
    assert not (inside / controller.TRACE_NAME).exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid_request" in captured.err


def test_an_existing_trace_is_never_appended(harness, monkeypatch, capsys):
    harness.trace_path.write_text("an earlier attempt\n")
    provider = Provider()
    provider.install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["invalid_request"]

    assert harness.trace_path.read_text() == "an earlier attempt\n"
    assert provider.requests == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "body", [b"not json", b'{"choices": [{}], "choices": []}', b'{"choices": [], "extra": NaN}']
)
def test_an_unreadable_response_is_not_retained_verbatim(harness, monkeypatch, capsys, body):
    Provider(provider_response(body=body)).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["protocol_error"]

    response = harness.payload("model_response")
    assert response["raw"] is None
    assert response["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert response["body_bytes"] == len(body)
    assert body not in harness.trace_path.read_bytes()
    result = result_of(capsys)
    assert result["input_tokens"] is None
    assert result["cost_usd"] is None


@pytest.mark.parametrize(
    "message",
    [
        {"content": "hi"},
        {"role": "assistant", "tool_calls": {}},
        {"role": "assistant", "tool_calls": [{"type": "function", "function": {"name": "shell"}}]},
        {
            "role": "assistant",
            "tool_calls": [{"id": "x", "type": "custom", "function": {"name": "shell"}}],
        },
        {"role": "assistant", "tool_calls": [{"id": "x", "type": "function", "function": {}}]},
    ],
)
def test_an_unusable_response_shape_ends_the_attempt(harness, monkeypatch, capsys, message):
    Provider(completion(message, reported=usage())).install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["protocol_error"]

    assert docker.calls == []
    assert harness.payload("terminal")["reason"] == "protocol_error"
    # The response was served, so what it did report is still retained.
    assert harness.payload("model_response")["raw"]["usage"] == usage()


def test_a_response_without_choices_ends_the_attempt(harness, monkeypatch, capsys):
    Provider(provider_response({"id": "gen-1", "choices": [], "usage": usage()})).install(
        monkeypatch
    )
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["protocol_error"]
    assert "choices" in harness.payload("terminal")["detail"]


def test_an_unexpected_finish_reason_is_not_a_normal_stop(harness, monkeypatch, capsys):
    Provider(
        completion(
            {"role": "assistant", "content": "blocked"},
            finish_reason="content_filter",
            reported=usage(),
        )
    ).install(monkeypatch)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["protocol_error"]
    assert "content_filter" in harness.payload("terminal")["detail"]


@pytest.mark.parametrize(
    "arguments", ["{}", "not json", '{"command": ""}', '{"command": 7}', '{"cmd": "ls"}']
)
def test_unusable_tool_arguments_are_refused_without_a_container(
    harness, monkeypatch, capsys, arguments
):
    call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "shell", "arguments": arguments},
    }
    provider = Provider(
        completion(
            {"role": "assistant", "content": None, "tool_calls": [call]},
            finish_reason="tool_calls",
            reported=usage(),
        ),
        replies(),
    )
    provider.install(monkeypatch)
    docker = Docker()
    docker.install(monkeypatch)
    before = harness.snapshot()

    assert harness.run() == 0

    assert docker.calls == []
    assert harness.snapshot() == before
    assert harness.payload("tool_result")["status"] == "refused"
    assert provider.messages(1)[-1]["content"].startswith("refused:")
    assert result_of(capsys)["tool_calls"] == 1
    assert harness.payload("tool_call")["container"] is None


def test_an_unexpected_internal_failure_reveals_only_its_class(harness, monkeypatch, capsys):
    def https_open(handler, request):
        raise MemoryError("a message that must not be retained")

    monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
    Docker().install(monkeypatch)

    assert harness.run() == controller.EXIT_CODES["operational_error"]

    terminal = harness.payload("terminal")
    assert terminal["reason"] == "operational_error"
    assert terminal["detail"] == "MemoryError"
    assert b"must not be retained" not in harness.trace_path.read_bytes()
    assert result_of(capsys)["trace_sha256"] is not None


@pytest.mark.parametrize("code", [125, 126, 127])
def test_shell_reserved_exit_codes_remain_model_visible(harness, monkeypatch, capsys, code):
    provider = Provider(calls_tool("missing-command"), replies())
    provider.install(monkeypatch)
    Docker(container(edit_answer, returncode=code)).install(monkeypatch)
    assert harness.run() == 0
    result = harness.payload("tool_result")
    assert result["returncode"] == code
    assert result["carried_back"] is True
    assert provider.messages(1)[-1]["content"].startswith(f"exit_code: {code}")


def test_binary_tool_output_is_retained_exactly(harness, monkeypatch, capsys):
    Provider(calls_tool("binary-output"), replies()).install(monkeypatch)
    raw = b"\xff\x00\xfe\r\n"
    Docker(container(stdout=raw, stderr=raw[::-1])).install(monkeypatch)
    assert harness.run() == 0
    result = harness.payload("tool_result")
    assert base64.b64decode(result["stdout_base64"]) == raw
    assert base64.b64decode(result["stderr_base64"]) == raw[::-1]
    for request in harness.payloads("model_request"):
        body = base64.b64decode(request["body_base64"])
        assert hashlib.sha256(body).hexdigest() == request["body_sha256"]
        assert json.loads(body) == request["body"]
    for response in harness.payloads("model_response"):
        body = base64.b64decode(response["body_base64"])
        assert hashlib.sha256(body).hexdigest() == response["body_sha256"]
        assert json.loads(body) == response["raw"]


def test_docker_discovery_uses_system_path_when_runner_path_is_isolated(harness, monkeypatch):
    calls = []

    def which(name, *, path=None):
        calls.append((name, path))
        return "/usr/bin/docker" if path == os.defpath else None

    monkeypatch.setattr(controller.shutil, "which", which)
    Docker().install(monkeypatch)
    assert controller._parse_options(ARGV).docker == "/usr/bin/docker"
    assert calls == [("docker", None), ("docker", os.defpath)]


@pytest.mark.parametrize("rootless", [False, True])
def test_container_user_matches_daemon_namespace(harness, monkeypatch, capsys, rootless):
    Provider(calls_tool("echo 42 > answer.txt"), replies()).install(monkeypatch)
    docker = Docker(container(edit_answer))
    docker.security_options = ["name=seccomp,profile=builtin", "name=rootless"] if rootless else []
    docker.install(monkeypatch)
    assert harness.run() == 0
    argv = harness.payload("tool_call")["argv"]
    expected = "0:0" if rootless else f"{os.getuid()}:{os.getgid()}"
    assert argv[argv.index("--user") + 1] == expected
    assert harness.payload("start")["options"]["container_user"] == expected


def test_unknown_daemon_mode_stops_before_provider_call(harness, monkeypatch, capsys):
    provider = Provider()
    provider.install(monkeypatch)
    docker = Docker()
    docker.security_options = {"unexpected": True}
    docker.install(monkeypatch)
    assert harness.run() == controller.EXIT_CODES["operational_error"]
    assert not provider.requests
    assert result_of(capsys)["cost_usd"] == 0


def test_docker_endpoint_is_explicit_and_cannot_inherit_credentials(harness, monkeypatch, capsys):
    monkeypatch.setenv("DOCKER_HOST", "tcp://unselected:2376")
    monkeypatch.setenv("DOCKER_CONFIG", "/unselected/secrets")
    provider = Provider(calls_tool("echo 42 > answer.txt"), replies())
    provider.install(monkeypatch)
    docker = Docker(container(edit_answer))
    docker.install(monkeypatch)
    endpoint = "unix:///run/devbox-docker/docker.sock"
    assert (
        harness.run_argv([*ARGV, "--docker", str(harness.docker_path), "--docker-host", endpoint])
        == 0
    )
    assert all(env["DOCKER_HOST"] == endpoint for env in docker.environments)
    assert all("DOCKER_CONFIG" not in env for env in docker.environments)
    assert harness.payload("start")["options"]["docker_host"] == endpoint


@pytest.mark.parametrize("endpoint", ["tcp://host:2375", "unix://relative", "unix:///bad\npath"])
def test_docker_endpoint_rejects_nonlocal_or_invalid_addresses(harness, monkeypatch, endpoint):
    Provider().install(monkeypatch)
    Docker().install(monkeypatch)
    assert (
        harness.run_argv([*ARGV, "--docker-host", endpoint])
        == controller.EXIT_CODES["invalid_request"]
    )


def test_response_trace_failure_keeps_reported_usage(harness, monkeypatch, capsys):
    Provider(replies(reported=usage(prompt=101, output=17, cost=0.125))).install(monkeypatch)
    Docker().install(monkeypatch)
    record = controller.Trace.record

    def fail_response(trace, kind, payload):
        if kind == "model_response":
            raise OSError("trace disk unavailable")
        return record(trace, kind, payload)

    monkeypatch.setattr(controller.Trace, "record", fail_response)
    assert harness.run() == controller.EXIT_CODES["operational_error"]
    result = result_of(capsys)
    assert result["input_tokens"] == usage()["prompt_tokens"]
    assert result["cost_usd"] == usage()["cost"]


def test_interrupted_provider_call_keeps_usage_unknown(harness, monkeypatch, capsys):
    Docker().install(monkeypatch)

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(controller.Controller, "_send", interrupt)
    assert harness.run() == controller.EXIT_CODES["interrupted"]
    result = result_of(capsys)
    assert result["input_tokens"] is None
    assert result["cost_usd"] is None


def test_host_inventory_failure_is_operational(harness, monkeypatch, capsys):
    provider = Provider(calls_tool("echo 42 > answer.txt"))
    provider.install(monkeypatch)
    Docker().install(monkeypatch)
    inventory = controller.inventory
    calls = 0

    def fail_later(root):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise controller.Refusal("cannot read host workspace")
        return inventory(root)

    monkeypatch.setattr(controller, "inventory", fail_later)
    assert harness.run() == controller.EXIT_CODES["operational_error"]
    assert len(provider.requests) == 1
    assert not harness.payloads("tool_result")


@pytest.mark.parametrize("failure", ["recursion", "interrupt"])
def test_failure_while_parsing_a_served_response_invalidates_prior_totals(
    harness, monkeypatch, capsys, failure
):
    parse = controller._strict_json
    responses = 0

    def interrupt_second(data):
        nonlocal responses
        if isinstance(data, bytes) and b'"choices"' in data:
            responses += 1
            if responses > 1:
                raise RecursionError if failure == "recursion" else KeyboardInterrupt
        return parse(data)

    monkeypatch.setattr(controller, "_strict_json", interrupt_second)
    Provider(calls_tool("true"), replies()).install(monkeypatch)
    Docker(container()).install(monkeypatch)
    expected = "operational_error" if failure == "recursion" else "interrupted"
    assert harness.run() == controller.EXIT_CODES[expected]
    result = result_of(capsys)
    assert result["input_tokens"] is None
    assert result["output_tokens"] is None
    assert result["cost_usd"] is None
    assert result["tool_calls"] == 1
    assert harness.payloads("model_response")[-1]["raw"] is None


def test_unexpected_constructor_failure_emits_a_redacted_terminal_result(
    harness, monkeypatch, capsys
):
    provider = Provider()
    provider.install(monkeypatch)
    Docker().install(monkeypatch)

    def fail_constructor(*args, **kwargs):
        raise OSError(f"unavailable workspace: {KEY}")

    monkeypatch.setattr(controller, "Controller", fail_constructor)
    assert harness.run() == controller.EXIT_CODES["operational_error"]
    result = result_of(capsys)
    assert result["model"] is None
    assert result["input_tokens"] == 0
    assert result["cost_usd"] == 0
    assert harness.payload("terminal")["detail"] == "OSError"
    assert KEY not in harness.trace_path.read_text()
    assert KEY not in json.dumps(result)
    assert not provider.requests


def test_copied_controller_remains_standalone_in_isolated_python(tmp_path):
    copied = tmp_path / "coding_controller.py"
    copied.write_bytes(Path(controller.__file__).read_bytes())
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(copied), "--unknown-offline-argument"],
        cwd=workspace,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        input=b"{}",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == controller.EXIT_CODES["invalid_request"]
    result = json.loads(completed.stdout)
    assert result["synthetic"] is False
    assert result["trace_sha256"]
    assert "ModuleNotFoundError" not in completed.stderr.decode()


def test_shell_controller_delegates_to_public_container_runtime(monkeypatch, tmp_path):

    calls = []
    expected = {"status": "ok", "returncode": 0}

    def execute(options, **kwargs):
        calls.append((options, kwargs))
        return expected

    monkeypatch.setattr(controller, "execute_container", execute)
    owner = controller.Controller.__new__(controller.Controller)
    owner.options = controller.Options("sha256:" + "a" * 64, 1.0, 256, "/docker", "0:0", None)
    owner.environment = {"PATH": "/bin"}
    assert controller.Controller._invoke(owner, "owned", ["docker"], tmp_path) is expected
    assert calls == [
        (owner.options, {"name": "owned", "argv": ["docker"], "environment": owner.environment})
    ]


def test_public_container_runtime_streams_only_supplied_json(monkeypatch, tmp_path):
    calls = []
    options = controller.Options("sha256:" + "a" * 64, 2.0, 256, "/docker", "0:0", None)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == "run":
            assert kwargs["input"] == b'{"value":21}\n'
            return subprocess.CompletedProcess(argv, 0, b"42\n", b"")
        if argv[1] == "inspect":
            return subprocess.CompletedProcess(
                argv, 0, b'{"Status":"exited","ExitCode":0,"Error":"","OOMKilled":false}', b""
            )
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr(controller.subprocess, "run", run)
    argv = controller.container_argv(
        options, "owned-json", tmp_path, ["python", "app.py"], read_only=True, stdin=True
    )
    outcome = controller.execute_container(
        options, name="owned-json", argv=argv, environment={}, stdin=b'{"value":21}\n'
    )
    assert outcome["status"] == "ok"
    assert outcome["stdout_base64"] == base64.b64encode(b"42\n").decode()
    assert outcome["cleanup"]["terminated"] is True
    assert outcome["carried_back"] is False
    assert [argv[1] for argv, _ in calls] == ["run", "inspect", "stop", "rm"]
    assert all("input" not in kwargs for _, kwargs in calls[1:])
