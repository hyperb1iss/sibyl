# Coding task attempts

The task runner executes one frozen learning or development task, retains the
controller's actions, and checks a separate copy of the final workspace. The
coding controller uses OpenRouter and one shell tool. Shell commands run in Docker
against a disposable staging copy of the selected workspace.

## Prepare the runtime

Install Docker and provision a local image with the tools the task needs. The
controller uses `--pull never` and accepts an immutable image ID, so image setup
happens before an attempt. For a Python standard-library or POSIX shell task:

```sh
docker pull python:3.13.15-slim
docker image inspect python:3.13.15-slim --format '{{.Id}}'
```

Freeze the returned image ID, the controller script bytes, the dependency lock,
and the task inputs in the experiment manifest. The following fragment belongs
inside a complete `sibyl-agent-task-manifest-v1` manifest; replace both digest
placeholders with hashes of the actual artifacts:

```json
{
  "controller": {
    "script": {"path": "coding_controller.py", "sha256": "<script digest>"},
    "args": [
      "--image", "sha256:<image digest>",
      "--docker", "/usr/bin/docker",
      "--docker-host", "unix:///run/devbox-docker/docker.sock",
      "--tool-timeout", "60",
      "--memory-mb", "2048"
    ]
  },
  "controller_api_key_env": "OPENROUTER_API_KEY",
  "controller_model": "qwen/qwen3-coder-next",
  "controller_tools": ["shell"],
  "controller_budget": {
    "input_tokens": 100000,
    "output_tokens": 8000,
    "tool_calls": 20,
    "cost_usd": 0.10
  },
  "controller_timeout_seconds": 600
}
```

The Docker path and socket above describe the eval devbox. Choose the paths for
your host. Omit `--docker-host` for the default local socket. The runner gives each
process a fresh home and an isolated PATH; it does not inherit Docker contexts,
registry credentials, or arbitrary environment variables. An explicit absolute
`--docker` path also supports installations outside the system PATH.

Run as an unprivileged host user. The controller checks the daemon's namespace
mode: ordinary Docker uses the caller's UID/GID; rootless Docker uses container
UID/GID `0:0` (mapped to the daemon's unprivileged host user). Docker documents
[this namespace mapping](https://docs.docker.com/engine/security/rootless/).
The daemon must see the staging directory at the same absolute path. On the
eval devbox, use the shared home directory for attempt output; client-side `/tmp`
is outside the daemon's filesystem view.

## Run a frozen attempt

Make `OPENROUTER_API_KEY` available in the invoking environment, then run:

```sh
moon run root:agent-task-run -- \
  --manifest /path/to/frozen/manifest.json \
  --task task-id --arm arm-id \
  --output /path/to/new-attempt
```

The output directory must be new and outside the frozen input directory. The
runner passes the key only to the declared controller process. The checker and
tool containers do not receive it. The provider endpoint is fixed; redirects,
ambient HTTP proxies, and request retries are disabled.

Tool containers have no network, a read-only root filesystem, dropped
capabilities, and one writable staging mount. After the container stops, the
controller validates and copies its file changes back. Symlinks and special files
are refused. Ordinary command failures remain visible to the model, including
shell exits 125 through 127. An unsuccessful container launch is an operational
failure. A controller timeout allows a short interrupt/cleanup grace before the
runner kills the process group.

## Read the evidence

The attempt retains the selected inputs, request/result streams, workspace
inventories, checker outcome, and `controller-trace.jsonl`. Trace records include
model requests and responses, tool calls and results, and the terminal outcome.
Successful provider exchanges and tool output retain their original bytes in
base64 alongside the decoded representation. Error response bodies are omitted
because they may echo credentials.

Usage comes from provider reports. Missing counts or cost remain unknown and stop
further paid calls. Output tokens are limited per request; input and cost limits
are checked after a response, so one request can cross either allowance. Tool
invocations consume their declared budget even when refused. Full tool output is
shown to the model and retained; large output can exceed the provider context
limit. Workspace copying and trace inventories also scale with task size.

These are trusted development attempts. Separate processes and staging mounts do
not provide sealed isolation against hostile same-user processes. A forced kill
or an unavailable daemon can prevent container cleanup. Trace hashes establish
byte consistency, not authenticated learning admission or improved task success.
