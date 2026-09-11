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

## Grade JSON CLI tasks

A task can use a versioned JSON oracle instead of the development Python checker.
The candidate reads one JSON value from stdin and emits one JSON value on stdout.
The host compares the response with its private expected value. Candidate Python
is never imported into the host checker process on this path.

The checker declaration replaces the task's existing `checker` object:

```json
{
  "schema_version": "sibyl-json-cli-oracle-v1",
  "oracle": {"path": "private/cases.json", "sha256": "<cases digest>"},
  "runtime": {"path": "runtime.py", "sha256": "<coding_controller.py digest>"},
  "evaluator": {"path": "oracle.py", "sha256": "<json_oracle.py digest>"},
  "argv": ["python", "app.py"],
  "image": "sha256:<image digest>",
  "docker": "/usr/bin/docker",
  "docker_host": "unix:///run/devbox-docker/docker.sock",
  "timeout_seconds": 10.0,
  "memory_mb": 256
}
```

Freeze copies of the installed `coding_controller.py` and `json_oracle.py` under
those artifact paths. The runner checks their bytes against its installed
implementation; supplied Python artifacts are retained but never dynamically
imported. Existing script checkers and v1 controller execution remain supported.

The private case artifact has this shape:

```json
{
  "schema_version": "sibyl-json-cli-cases-v1",
  "cases": [
    {"id": "double-positive", "input": {"value": 21}, "expected": 42}
  ]
}
```

Each case runs in a fresh container against the final submission, mounted
read-only. The container receives only the case input, the candidate snapshot,
and temporary writable storage. Expected values and evaluator artifacts remain
on the host. Oracle artifacts cannot also be declared as candidate workspace,
prompt, experience, or memory-pack bytes. This check detects exact overlap; it
cannot establish that task authors kept their cases private.

Responses must contain one valid JSON value with no duplicate object keys,
non-finite numbers, or trailing text. Comparisons ignore whitespace and object
key order but preserve JSON types and numeric representation (`42` differs from
`42.0`). A nonzero candidate exit, timeout, malformed response, wrong answer, and
runtime failure have separate outcome statuses. The per-case timeout applies to
candidate execution; the manifest's checker timeout limits the case loop.
Container inspection and cleanup can take additional time.

The retained `oracle-outcome.json` binds the attempt, final snapshot, private
cases, evaluator, runtime, image, and argv. Per-case execution evidence includes
stdout and stderr bytes. The same container lifecycle implementation serves the
coding controller and oracle, while the controller remains a standalone script
that works under isolated Python mode.

JSON-oracle attempts still report `sealed_isolation: false`, and sealed tasks
remain refused. Separate oracle ownership, admitted controller/runtime policy,
authenticated outcomes, and split-aware learning admission remain required for
sealed evaluation. The private artifact and result files share the trusted host
user's storage. Their hashes do not authenticate the worker or prove learning.

## Collect authenticated learning episodes

The trusted learning harness registers an immutable assignment before running the
installed coding controller. The JSON oracle checks the final candidate, then the
harness signs the retained outcome, transcript, and deterministic episode. Sibyl
admits the episode into one private raw capture and returns its ID and current
revision. A failed HTTP response can be retried without another model call.

The host operator and controller implementation are trusted. The controller keeps
its existing Docker access; the harness provides no hostile-controller isolation
or sealed grading. All collection receipts retain `sealed_isolation: false`.
A signed episode establishes issuer authenticity and evidence binding. Collection
alone establishes no consolidation or held-out learning improvement.

Use a frozen manifest with a `learning` task and a `sibyl-json-cli-oracle-v1`
checker. The controller artifact must exactly match the installed
`coding_controller.py`, with `shell` as its only tool and
`OPENROUTER_API_KEY` as its explicit provider credential. Declare the controller's
image, Docker executable, Docker socket, tool timeout, and memory allowance in its
arguments. Its image and Docker endpoint must match the checker; the controller
and checker may have different time and memory allowances.

First inspect the policy:

```bash
moon run :agent-learning-run -- policy --manifest /path/to/frozen/manifest.json
```

Authorize the resulting policy digest and experiment revision in the server's
`eval_issuers` configuration, together with the issuer's Ed25519 public key and
organization. The API credential must belong to the destination owner, have an
owner or admin organization role, and permit private memory writes. Keep the API
credential in `SIBYL_EVAL_API_TOKEN`; the provider credential remains in
`OPENROUTER_API_KEY`. Neither credential is printed or copied into the candidate
container. The signing key is a private regular file containing 32 raw Ed25519
private-key bytes (mode `0600`), loaded only after controller execution completes.

```bash
moon run :agent-learning-run -- run \
  --manifest /path/to/frozen/manifest.json \
  --task learning-task --arm raw \
  --organization ORGANIZATION_ID --owner OWNER_PRINCIPAL_ID \
  --issuer learning-oracle --checkpoint 0 \
  --policy-sha256 APPROVED_POLICY_DIGEST \
  --signing-key /private/path/learning-oracle.key \
  --api-url https://sibyl.example/api/ \
  --output /path/to/attempts/learning-task-001
```

The output directory must be new and outside the input directory. The harness
freezes only the selected task, arm, and referenced experiences, preserving the
original experiment revision in its assignment. Operational failures, incomplete
traces, mismatched artifacts, and budget violations cannot reach signing.
Task failures remain learning outcomes with their original status.

The durable `admission-bundle.json` contains the exact signed request and is
written before the admission HTTP call. If that request fails or its response is
lost, resend it:

```bash
moon run :agent-learning-run -- admit \
  --api-url https://sibyl.example/api/ \
  --output /path/to/attempts/learning-task-001
```

Admission retries use no signing key and never rerun the controller. Sibyl checks
the signature and permanent assignment on every request; deleted or changed
captures cannot be recreated through retry. Keep the complete output directory
for replay. A directory without an admission bundle cannot resume execution;
retain its partial evidence and use a fresh output directory for a new attempt.
Consolidation must load the admitted source and its current revision from Sibyl,
then recheck source eligibility before storing or promoting a proposal.
