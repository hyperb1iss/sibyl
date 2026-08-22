#!/usr/bin/env python3
"""Run Sibyl through the official LongMemEval-V2 harness."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

HTTP_STATUS_OK = 200
HTTP_STATUS_SERVER_ERROR = 500
MIN_COMBINED_SOURCE_METRICS = 2
POSITIVE_DECIMAL_PATTERN = re.compile(r"[1-9][0-9]*")
WORKFLOW_FILENAME_PATTERN = re.compile(r"[^/@\x00-\x20\x7f]+\.ya?ml")
ROOT = Path(__file__).resolve().parents[1]
CORE_SRC = ROOT / "packages" / "python" / "sibyl-core" / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from benchmarks.git_provenance import git_provenance  # noqa: E402
from benchmarks.local_execution_identity import (  # noqa: E402
    GIT_SHA_LENGTH,
    is_canonical_repository as _is_canonical_repository,
    is_valid_branch_ref as _is_valid_branch_ref,
    require_local_checkout,
)
from benchmarks.longmemeval_v2_official_source import (  # noqa: E402
    official_source_record,
    require_pinned_source,
)
from benchmarks.longmemeval_v2_memory.sibyl_memory import (  # noqa: E402
    MEMORY_MANIFEST_SCHEMA_VERSION,
    SibylLiveApiMemory,
)
from benchmarks.longmemeval_v2_diagnostics import (  # noqa: E402
    DEFAULT_MAX_RANK,
    build_question_trace,
    build_state_text_index,
    load_jsonl_by_id,
)
from benchmarks.provider_usage import (  # noqa: E402
    AsyncUsageTrackingClient,
    ProviderUsageRecorder,
    SyncUsageTrackingClient,
    usage_from_response,
)
from sibyl_core.evals.longmemeval_v2 import (  # noqa: E402
    load_longmemeval_v2_haystack,
    load_longmemeval_v2_questions,
    summarize_longmemeval_v2_inputs,
)
from sibyl_core.retrieval.refinement import MAX_REFINEMENT_QUERIES  # noqa: E402

PLAN_SCHEMA_VERSION = "sibyl-longmemeval-v2-official-plan-v2"
RECEIPT_SCHEMA_VERSION = "sibyl-longmemeval-v2-official-receipt-v1"
ACCOUNTING_SCHEMA_VERSION = "sibyl-eval-accounting-v1"
EXPERIMENT_IDENTITY_SCHEMA_VERSION = "sibyl-longmemeval-v2-experiment-identity-v2"
EXECUTION_IDENTITY_SCHEMA_VERSION = "sibyl-longmemeval-v2-execution-identity-v1"
EXPERIMENT_ARM_ROLES = frozenset({"machine", "naive", "render_control", "render_treatment"})
EXPERIMENT_PHASES = frozenset({"aa", "anchor", "race", "render"})
EXPERIMENT_SUBSTRATES = frozenset({"machine", "naive"})
MILLION_TOKENS = 1_000_000
EVAL_PRICE_SNAPSHOT = {
    "reader": {"model": "qwen/qwen3.5-9b", "input_per_million": 0.17, "output_per_million": 0.25},
    "judge": {"model": "gpt-5.2", "input_per_million": 1.75, "output_per_million": 14.0},
    "operations": {
        "model": "gpt-5.4-nano",
        "input_per_million": 0.20,
        "output_per_million": 1.25,
    },
}
LOADED_MEMORY_RUNTIME_KEYS = frozenset(
    {
        "api_token",
        "email",
        "password",
        "allow_localhost",
        "allow_signup",
        "api_timeout_seconds",
        "api_retry_attempts",
        "api_retry_base_delay_seconds",
        "api_retry_max_delay_seconds",
        "search_limit",
        "max_context_items",
        "max_context_chars_per_item",
        "max_context_total_chars",
        "render_char_total_treatment",
        "render_group_lanes",
        "render_action_spines",
        "operational_note_dedupe_mode",
        "operational_note_lane_mode",
        "max_chunks_per_trajectory",
        "neighbor_stitch_items",
        "neighbor_stitch_span",
        "state_part_completion_items",
        "state_part_refinement",
        "neighbor_support_exempt",
        "neighbor_trajectory_preserving",
        "neighbor_support_overflow_items",
        "neighbor_stitch_spread",
        "context_expansion_max_ratio",
        "evidence_types",
        "evidence_char_budget",
        "evidence_char_budget_raw_reserve",
        "evidence_composition_mode",
        "source_evidence_bundling",
        "typed_stream_retrieval",
        "typed_stream_limit",
        "note_distillation",
        "note_distillation_model",
        "typed_reservation_items",
        "retrieval_mode",
        "retrieval_max_planned_queries",
        "knn_type_overfetch",
        "checkpoint_dir",
        "semantic_prior_rescue_weight",
        "typed_pool",
        "agentic_traversal",
        "traversal_widening_rounds",
        "traversal_model",
        "traversal_max_actions",
        "traversal_followup_searches",
        "traversal_deadline_seconds",
        "traversal_overflow_items",
        "traversal_search_limit",
        "runner_provenance",
        "official_source",
    }
)
QWEN_READER_MODEL_FRAGMENT = "qwen3.5-9b"
GPT_EVALUATOR_MODEL_FRAGMENT = "gpt-5.2"
DEFAULT_METHOD = "sibyl_live_api"
TRANSIENT_READER_EXCEPTION_NAMES = frozenset(
    {
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "RateLimitError",
    }
)
_SENSITIVE_COMMAND_FLAGS = frozenset(
    {
        "--api-token",
        "--api-credentials-file",
        "--password",
        "--reader-api-key",
        "--evaluator-api-key",
        "--token",
    }
)
_SENSITIVE_COMMAND_SUBSTRINGS = ("secret", "token", "password", "api-key", "apikey")
_SENSITIVE_CONFIG_KEYS = frozenset({"api_token", "email", "password"})


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.command_args = list(argv if argv is not None else sys.argv[1:])
    install_memory_credentials(args)
    data_root = Path(args.data_root).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    if args.receipt_only:
        output_dir.mkdir(parents=True, exist_ok=True)
        receipt = build_receipt_from_artifacts(
            args=args, data_root=data_root, output_dir=output_dir
        )
        write_json(resolve_receipt_output(args, output_dir), receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))  # noqa: T201
        return 0

    ensure_fresh_provider_usage_output(output_dir)
    runtime_dir = output_dir / "runtime_inputs"
    runtime_dir.mkdir(parents=True, exist_ok=True)

    selected_questions = materialize_runtime_questions(
        data_root=data_root,
        domain=args.domain,
        question_ids=parse_question_ids(args.question_ids),
        limit=args.limit,
        output_path=runtime_dir / "questions.json",
    )
    selected_haystack = materialize_runtime_haystack(
        data_root=data_root,
        tier=args.tier,
        selected_questions=selected_questions,
        output_path=runtime_dir / "haystack.json",
    )
    memory_config = build_memory_config(args)
    memory_config_path = runtime_dir / "memory_config.json"
    write_json(memory_config_path, memory_config)

    plan = build_run_plan(
        args=args,
        data_root=data_root,
        output_dir=output_dir,
        runtime_dir=runtime_dir,
        memory_config_path=memory_config_path,
        selected_questions=selected_questions,
        selected_haystack=selected_haystack,
    )
    write_json(output_dir / "longmemeval_v2_official_plan.json", plan)
    print(json.dumps(plan, indent=2, sort_keys=True))  # noqa: T201
    if args.plan_only:
        attest_loaded_memory_project(
            args=args,
            data_root=data_root,
            selected_haystack=selected_haystack,
            memory_config=memory_config,
        )
        return 0
    if args.experiment_id:
        enforce_spend_reservation(plan)

    official_repo = resolve_official_repo(args.official_repo)
    require_pinned_source(official_repo)
    sys.path.insert(0, str(official_repo))
    import benchmarks.longmemeval_v2_memory.sibyl_memory  # noqa: F401, PLC0415
    import evaluation.harness as official_harness  # noqa: PLC0415
    import evaluation.qa_eval_metrics as official_metrics  # noqa: PLC0415

    install_provider_usage_tracking(
        official_harness,
        official_metrics,
        args=args,
        output_dir=output_dir,
    )
    install_reader_retry(official_harness, args=args)
    install_memory_finalize(official_harness)
    install_evaluator_retry(official_metrics, args=args)

    old_argv = sys.argv
    try:
        sys.argv = build_harness_argv(
            args=args,
            data_root=data_root,
            output_dir=output_dir,
            runtime_dir=runtime_dir,
            memory_config_path=memory_config_path,
        )
        official_harness.main()
    finally:
        sys.argv = old_argv
    if args.experiment_id:
        write_rig_rows(data_root=data_root, output_dir=output_dir, domain=args.domain)
    receipt = build_receipt_from_artifacts(args=args, data_root=data_root, output_dir=output_dir)
    write_json(resolve_receipt_output(args, output_dir), receipt)
    if args.experiment_id:
        enforce_actual_spend_cap(receipt, max_spend_usd=args.max_spend_usd)
    return 0


def _redacted_command_args(command_args: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for arg in command_args:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        if "=" in arg:
            flag, _value = arg.split("=", 1)
            lowered_flag = flag.lower()
            if flag in _SENSITIVE_COMMAND_FLAGS or any(
                token in lowered_flag for token in _SENSITIVE_COMMAND_SUBSTRINGS
            ):
                redacted.append(f"{flag}=<redacted>")
                continue
        lowered_arg = arg.lower()
        if arg in _SENSITIVE_COMMAND_FLAGS or any(
            token in lowered_arg for token in _SENSITIVE_COMMAND_SUBSTRINGS
        ):
            redacted.append(arg)
            redact_next = True
            continue
        redacted.append(arg)
    return redacted


def ensure_fresh_provider_usage_output(output_dir: Path) -> None:
    usage_dir = output_dir / "provider_usage"
    existing_logs = sorted(path.name for path in usage_dir.glob("*.jsonl") if path.is_file())
    if existing_logs:
        logs = ", ".join(existing_logs)
        msg = (
            f"Output directory already contains provider usage logs ({logs}): {output_dir}. "
            "Use a fresh --output-dir; checkpoint state may be reused separately."
        )
        raise RuntimeError(msg)


def install_reader_retry(official_harness: Any, *, args: argparse.Namespace) -> None:
    attempts = int(args.reader_retry_attempts)
    if attempts <= 1:
        return
    original = official_harness.call_reader_model_async

    async def call_reader_model_async_with_retry(
        client: Any,
        harness_args: argparse.Namespace,
        messages: list[dict[str, Any]],
    ) -> tuple[str, dict[str, int]]:
        for attempt in range(1, attempts + 1):
            try:
                return await original(client, harness_args, messages)
            except Exception as exc:
                if attempt >= attempts or not is_transient_reader_exception(exc):
                    raise
                delay = reader_retry_delay_seconds(args, failed_attempt=attempt)
                print(
                    f"Reader request failed with {exc.__class__.__name__}; "
                    f"retrying attempt {attempt + 1}/{attempts} after {delay:.1f}s.",
                    file=sys.stderr,
                    flush=True,
                )
                await asyncio.sleep(delay)
        msg = "Reader retry loop exhausted unexpectedly"
        raise RuntimeError(msg)

    official_harness.call_reader_model_async = call_reader_model_async_with_retry


def install_provider_usage_tracking(
    official_harness: Any,
    official_metrics: Any,
    *,
    args: argparse.Namespace,
    output_dir: Path,
) -> None:
    usage_dir = output_dir / "provider_usage"
    reader_recorder = ProviderUsageRecorder(
        usage_dir / "reader.jsonl",
        run_id=args.provider_usage_run_id,
        role="reader",
    )
    judge_recorder = ProviderUsageRecorder(
        usage_dir / "judge.jsonl",
        run_id=args.provider_usage_run_id,
        role="judge",
    )

    original_create_async_client = official_harness.create_async_client

    def create_tracked_async_client(*call_args: Any, **call_kwargs: Any) -> Any:
        client = original_create_async_client(*call_args, **call_kwargs)
        return AsyncUsageTrackingClient(client, reader_recorder)

    original_extract_usage = official_harness.extract_usage_dict

    def extract_tracked_usage(response: Any) -> dict[str, Any]:
        usage = dict(original_extract_usage(response))
        usage.update(usage_from_response(response))
        if isinstance(model := getattr(response, "model", None), str):
            usage["provider_model"] = model
        if isinstance(response_id := getattr(response, "id", None), str):
            usage["response_id"] = response_id
        return usage

    original_create_evaluator_client = official_metrics._create_openai_client

    def create_tracked_evaluator_client(*call_args: Any, **call_kwargs: Any) -> Any:
        client = original_create_evaluator_client(*call_args, **call_kwargs)
        return SyncUsageTrackingClient(client, judge_recorder)

    official_harness.create_async_client = create_tracked_async_client
    official_harness.extract_usage_dict = extract_tracked_usage
    official_metrics._create_openai_client = create_tracked_evaluator_client


def install_memory_finalize(official_harness: Any) -> None:
    original = official_harness.build_prompt_row

    def build_prompt_row_with_finalized_memory(*call_args: Any, **call_kwargs: Any) -> Any:
        memory = call_kwargs.get("memory")
        if memory is None and len(call_args) >= 3:
            memory = call_args[2]
        finalize_ingest = getattr(memory, "finalize_ingest", None)
        if callable(finalize_ingest):
            finalize_ingest()
        return original(*call_args, **call_kwargs)

    official_harness.build_prompt_row = build_prompt_row_with_finalized_memory


def is_transient_reader_exception(exc: Exception) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True
    if isinstance(exc, TypeError) and "not subscriptable" in str(exc):
        return True
    if exc.__class__.__name__ in TRANSIENT_READER_EXCEPTION_NAMES:
        return True
    status_code = getattr(exc, "status_code", None)
    return isinstance(status_code, int) and (status_code == 429 or status_code >= 500)


def reader_retry_delay_seconds(args: argparse.Namespace, *, failed_attempt: int) -> float:
    base = float(args.reader_retry_base_delay_seconds)
    max_delay = float(args.reader_retry_max_delay_seconds)
    return min(max_delay, base * (2 ** max(0, failed_attempt - 1)))


def install_evaluator_retry(official_metrics: Any, *, args: argparse.Namespace) -> None:
    attempts = int(args.evaluator_retry_attempts)
    if attempts <= 1:
        return
    for function_name in ("llm_abstention_checker", "llm_gotchas_checker"):
        original = getattr(official_metrics, function_name)
        setattr(
            official_metrics,
            function_name,
            _evaluator_retry_wrapper(original, function_name=function_name, attempts=attempts),
        )


def _evaluator_retry_wrapper(original: Any, *, function_name: str, attempts: int) -> Any:
    def evaluator_with_retry(*call_args: Any, **call_kwargs: Any) -> Any:
        for attempt in range(1, attempts + 1):
            try:
                return original(*call_args, **call_kwargs)
            except ValueError as exc:
                if attempt >= attempts or not is_malformed_evaluator_judgement(exc):
                    raise
                print(
                    f"Official {function_name} returned a malformed judgement; "
                    f"retrying evaluator attempt {attempt + 1}/{attempts}.",
                    file=sys.stderr,
                    flush=True,
                )
        msg = "Evaluator retry loop exhausted unexpectedly"
        raise RuntimeError(msg)

    return evaluator_with_retry


def is_malformed_evaluator_judgement(exc: ValueError) -> bool:
    message = str(exc)
    return message.startswith(
        (
            "Could not parse evaluator binary judgement:",
            "Empty judgement response from evaluator model.",
            "Evaluator model returned empty response content.",
        )
    )


def _validate_github_run_id(run_id: str) -> None:
    if not POSITIVE_DECIMAL_PATTERN.fullmatch(run_id):
        raise ValueError("GitHub execution run ID must be a canonical positive decimal string")


def _validate_workflow_ref(*, repository: str, ref: str, workflow_ref: str) -> None:
    prefix = f"{repository}/.github/workflows/"
    suffix = f"@{ref}"
    if not workflow_ref.startswith(prefix) or not workflow_ref.endswith(suffix):
        raise ValueError("GitHub workflow ref is not canonical for its repository and ref")
    filename = workflow_ref[len(prefix) : -len(suffix)]
    if not WORKFLOW_FILENAME_PATTERN.fullmatch(filename):
        raise ValueError("GitHub workflow ref must name one canonical YAML workflow file")
    if workflow_ref != f"{prefix}{filename}{suffix}":
        raise ValueError("GitHub workflow ref is not canonical for its repository and ref")


def _validate_execution_common(
    *,
    repository: str,
    ref: str,
    sha: str,
    run_id: str,
    run_attempt: int,
) -> None:
    if not _is_canonical_repository(repository):
        raise ValueError("execution repository must be a canonical owner/repository slug")
    if not _is_valid_branch_ref(ref):
        raise ValueError("execution ref must be a valid full refs/heads/* branch ref")
    if len(sha) != GIT_SHA_LENGTH or any(character not in "0123456789abcdef" for character in sha):
        raise ValueError("execution SHA must be a full lowercase Git SHA")
    if not run_id or run_id != run_id.strip():
        raise ValueError("execution run ID must be a non-empty canonical value")
    if isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt < 1:
        raise ValueError("execution run attempt must be a positive integer")


def resolve_execution_identity(  # noqa: PLR0912
    args: argparse.Namespace,
    *,
    root: Path,
) -> dict[str, object]:
    """Return an exact public execution identity for a paid experiment."""

    if os.getenv("GITHUB_ACTIONS") == "true":
        if args.execution_kind not in {"", "github"}:
            raise ValueError("GitHub Actions experiment runs require GitHub execution identity")
        local_fields = {
            "--local-repository": args.local_repository,
            "--local-ref": args.local_ref,
            "--local-sha": args.local_sha,
            "--local-run-id": args.local_run_id,
            "--local-run-attempt": args.local_run_attempt,
        }
        if any(value for value in local_fields.values()):
            raise ValueError("GitHub execution identity cannot include local execution fields")
        github_environment = {
            "repository": os.getenv("GITHUB_REPOSITORY", ""),
            "ref": os.getenv("GITHUB_REF", ""),
            "sha": os.getenv("GITHUB_SHA", ""),
            "run_id": os.getenv("GITHUB_RUN_ID", ""),
            "workflow_ref": os.getenv("GITHUB_WORKFLOW_REF", ""),
        }
        github_arguments = {
            "repository": args.github_repository,
            "ref": args.github_ref,
            "sha": args.github_workflow_sha,
            "run_id": args.github_run_id,
            "workflow_ref": args.github_workflow_ref,
        }
        github_run_attempt = os.getenv("GITHUB_RUN_ATTEMPT", "")
        missing = sorted(key for key, value in github_environment.items() if not value)
        if missing or not POSITIVE_DECIMAL_PATTERN.fullmatch(github_run_attempt):
            raise ValueError(f"GitHub execution environment is incomplete: {missing}")
        if github_arguments != github_environment or args.github_run_attempt != int(
            github_run_attempt
        ):
            raise ValueError("GitHub execution arguments differ from the Actions environment")
        _validate_execution_common(
            repository=github_environment["repository"],
            ref=github_environment["ref"],
            sha=github_environment["sha"],
            run_id=github_environment["run_id"],
            run_attempt=args.github_run_attempt,
        )
        _validate_github_run_id(github_environment["run_id"])
        workflow_ref = github_environment["workflow_ref"]
        _validate_workflow_ref(
            repository=github_environment["repository"],
            ref=github_environment["ref"],
            workflow_ref=workflow_ref,
        )
        return {
            "schema_version": EXECUTION_IDENTITY_SCHEMA_VERSION,
            "kind": "github",
            **github_environment,
            "run_attempt": args.github_run_attempt,
        }

    if args.execution_kind != "local":
        raise ValueError("local experiment runs require --execution-kind local")
    if any(
        (
            args.github_repository,
            args.github_ref,
            args.github_workflow_ref,
            args.github_workflow_sha,
            args.github_run_id,
            args.github_run_attempt,
        )
    ):
        raise ValueError("local execution identity cannot include GitHub execution fields")
    local_fields = {
        "--local-repository": args.local_repository,
        "--local-ref": args.local_ref,
        "--local-sha": args.local_sha,
        "--local-run-id": args.local_run_id,
    }
    missing = sorted(flag for flag, value in local_fields.items() if not value)
    if missing or args.local_run_attempt < 1:
        raise ValueError(
            f"local execution identity is incomplete: {missing or ['--local-run-attempt']}"
        )
    _validate_execution_common(
        repository=args.local_repository,
        ref=args.local_ref,
        sha=args.local_sha,
        run_id=args.local_run_id,
        run_attempt=args.local_run_attempt,
    )
    try:
        normalized_run_id = str(UUID(args.local_run_id))
    except ValueError as exc:
        raise ValueError("local execution run ID must be a canonical UUID") from exc
    if normalized_run_id != args.local_run_id:
        raise ValueError("local execution run ID must be a canonical UUID")
    checkout = require_local_checkout(root)
    source = checkout["source_identity"]
    actual_repository = str(source["repository"])
    actual_ref = str(source["ref"])
    actual_sha = str(source["sha"])
    if (args.local_repository, args.local_ref, args.local_sha) != (
        actual_repository,
        actual_ref,
        actual_sha,
    ):
        raise ValueError("local execution repository, ref, or SHA differs from the checkout")
    return {
        "schema_version": EXECUTION_IDENTITY_SCHEMA_VERSION,
        "kind": "local",
        "repository": actual_repository,
        "ref": actual_ref,
        "sha": actual_sha,
        "run_id": args.local_run_id,
        "run_attempt": args.local_run_attempt,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:  # noqa: PLR0912, PLR0915
    parser = argparse.ArgumentParser(description="Run LongMemEval-V2 with Sibyl memory.")
    parser.add_argument("--official-repo", default=os.getenv("LME_V2_OFFICIAL_REPO"))
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--domain", choices=["web", "enterprise", "combined"], required=True)
    parser.add_argument("--tier", choices=["small", "medium"], default="small")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--receipt-output", default=None)
    parser.add_argument("--receipt-only", action="store_true")
    parser.add_argument("--metric-overview", default=None)
    parser.add_argument("--combined-metrics", default=None)
    parser.add_argument("--submission-overview", default=None)
    parser.add_argument("--submission-archive", default=None)
    parser.add_argument("--web-output-dir", default=None)
    parser.add_argument("--enterprise-output-dir", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--question-ids", nargs="*", default=None)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--save-memory", action="store_true")
    parser.add_argument("--skip-evaluation", action="store_true")
    parser.add_argument("--load-memory-dir", default=None)
    parser.add_argument("--checkpoint-dir", default=None)
    parser.add_argument("--experiment-id", default="")
    parser.add_argument("--experiment-phase", choices=sorted(EXPERIMENT_PHASES), default="")
    parser.add_argument("--pass-id", default="")
    parser.add_argument("--pass-seed", type=int, default=None)
    parser.add_argument("--arm-role", choices=sorted(EXPERIMENT_ARM_ROLES), default="")
    parser.add_argument("--substrate", choices=sorted(EXPERIMENT_SUBSTRATES), default="")
    parser.add_argument("--preregistration-sha256", default="")
    parser.add_argument("--max-spend-usd", type=float, default=None)
    parser.add_argument("--spend-contingency-multiplier", type=float, default=1.25)
    parser.add_argument("--unmetered-provider-reserve-usd", type=float, default=2.0)
    parser.add_argument("--reader-output-reserve-tokens", type=int, default=1024)
    parser.add_argument("--evaluator-input-reserve-tokens", type=int, default=2048)
    parser.add_argument("--evaluator-output-reserve-tokens", type=int, default=512)
    parser.add_argument("--operations-input-reserve-tokens", type=int, default=10_000)
    parser.add_argument("--operations-output-reserve-tokens", type=int, default=1024)
    parser.add_argument("--github-repository", default=os.getenv("GITHUB_REPOSITORY", ""))
    parser.add_argument("--github-ref", default=os.getenv("GITHUB_REF", ""))
    parser.add_argument("--github-workflow-ref", default=os.getenv("GITHUB_WORKFLOW_REF", ""))
    parser.add_argument("--github-workflow-sha", default=os.getenv("GITHUB_SHA", ""))
    parser.add_argument("--github-run-id", default=os.getenv("GITHUB_RUN_ID", ""))
    parser.add_argument(
        "--github-run-attempt",
        type=int,
        default=int(os.getenv("GITHUB_RUN_ATTEMPT", "0")),
    )
    parser.add_argument("--execution-kind", choices=["github", "local"], default="")
    parser.add_argument("--local-repository", default="")
    parser.add_argument("--local-ref", default="")
    parser.add_argument("--local-sha", default="")
    parser.add_argument("--local-run-id", default="")
    parser.add_argument("--local-run-attempt", type=int, default=0)

    parser.add_argument(
        "--api-url", default=os.getenv("SIBYL_API_URL", "http://127.0.0.1:3334/api")
    )
    parser.add_argument("--api-token", default=os.getenv("SIBYL_API_TOKEN", ""))
    parser.add_argument(
        "--api-credentials-file",
        default=os.getenv("SIBYL_API_CREDENTIALS_FILE", ""),
    )
    parser.add_argument("--email", default=os.getenv("LME_SIBYL_EMAIL", ""))
    parser.add_argument("--password", default=os.getenv("LME_SIBYL_PASSWORD", ""))
    parser.add_argument("--project-id", default="")
    parser.add_argument("--reuse-existing-project", action="store_true")
    parser.add_argument(
        "--run-id", default=os.getenv("LME_V2_RUN_ID", f"lme-v2-{uuid4().hex[:12]}")
    )
    parser.add_argument("--allow-localhost", action="store_true")
    parser.add_argument("--no-signup", action="store_true")
    parser.add_argument("--content-max-chars", type=int, default=18_000)
    parser.add_argument("--chunking-mode", choices=["trajectory", "state"], default="state")
    parser.add_argument("--search-limit", type=int, default=12)
    parser.add_argument("--max-context-items", type=int, default=8)
    parser.add_argument("--max-context-chars-per-item", type=int, default=18_000)
    parser.add_argument("--max-context-total-chars", type=int, default=60_000)
    parser.add_argument("--max-chunks-per-trajectory", type=int, default=2)
    parser.add_argument("--neighbor-stitch-items", type=int, default=2)
    parser.add_argument("--neighbor-stitch-span", type=int, default=1)
    parser.add_argument("--state-part-completion-items", type=int, default=0)
    parser.add_argument(
        "--state-part-refinement",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--neighbor-support-exempt",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--neighbor-trajectory-preserving",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--neighbor-support-overflow-items", type=int, default=0)
    parser.add_argument("--semantic-prior-rescue-weight", type=float, default=0.0)
    parser.add_argument("--typed-pool", default="typed", choices=["typed", "typed_entity_overlap"])
    parser.add_argument(
        "--neighbor-stitch-spread",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument("--context-expansion-max-ratio", type=float, default=0.0)
    parser.add_argument(
        "--evidence-types",
        nargs="+",
        choices=["passage", "session"],
        default=["session"],
        help=(
            "Entity types the raw evidence lane may retrieve. The list reaches the search "
            "as a hard node-type filter, so a type left out never enters the candidate pool. "
            "Defaults to the shipped whole-state substrate."
        ),
    )
    parser.add_argument(
        "--evidence-char-budget-raw-reserve",
        type=int,
        default=None,
        help=(
            "Reserve this many characters of --evidence-char-budget for the raw "
            "evidence lane, admitted before typed items spend anything. Requires "
            "--evidence-char-budget; unset reproduces the shared-budget admission."
        ),
    )
    parser.add_argument(
        "--evidence-char-budget",
        type=int,
        default=None,
        help=(
            "Bound the composed pack by characters of returned content instead of item "
            "count. Item count only bounds reader payload while item size is stable, "
            "which stops holding once passages and whole states share a pack. Leave "
            "unset to reproduce the shipped item-bounded geometry."
        ),
    )
    parser.add_argument(
        "--evidence-composition-mode",
        choices=["reserved_support", "shared_relevance"],
        default="shared_relevance",
    )
    parser.add_argument("--source-evidence-bundling", action="store_true")
    parser.add_argument("--typed-stream-retrieval", action="store_true")
    parser.add_argument("--typed-stream-limit", type=int, default=8)
    parser.add_argument("--note-distillation", action="store_true")
    parser.add_argument("--note-distillation-model", default="gpt-5.4-nano")
    parser.add_argument(
        "--operational-note-dedupe-mode",
        choices=["source", "source_kind"],
        default="source",
    )
    parser.add_argument(
        "--operational-note-lane-mode",
        choices=["reserved", "additive"],
        default="reserved",
    )
    parser.add_argument(
        "--operational-note-distillation-profile",
        choices=["baseline", "render_v1"],
        default="baseline",
    )
    parser.add_argument("--render-char-total-treatment", action="store_true")
    parser.add_argument("--render-group-lanes", action="store_true")
    parser.add_argument("--render-action-spines", action="store_true")
    parser.add_argument("--typed-reservation-items", type=int, default=None)
    parser.add_argument("--knn-type-overfetch", type=int, default=0)
    parser.add_argument(
        "--retrieval-mode",
        choices=["fast", "accurate", "naive"],
        default="fast",
        help=(
            "Server-side retrieval pipeline. naive selects the 1.3 Phase 0 "
            "control arm (BM25 + dense KNN + plain RRF + a tight pack); it "
            "refuses to run beside --typed-stream-retrieval or "
            "--agentic-traversal, both of which retrieve outside the arm."
        ),
    )
    parser.add_argument("--retrieval-max-planned-queries", type=int, default=3)
    parser.add_argument(
        "--agentic-traversal",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Enable bounded model-in-the-loop widening between the one-shot pass and "
            "deterministic composition: the traversal model inspects pool previews and "
            "gathers via /search/expand, /search/slice, and one follow-up search. "
            "Score-blind and question-text-only; off reproduces the arm-off geometry."
        ),
    )
    parser.add_argument("--traversal-widening-rounds", type=int, default=1)
    parser.add_argument("--traversal-model", default="gpt-5.4-nano")
    parser.add_argument("--traversal-max-actions", type=int, default=4)
    parser.add_argument("--traversal-followup-searches", type=int, default=1)
    parser.add_argument("--traversal-deadline-seconds", type=float, default=8.0)
    parser.add_argument("--traversal-overflow-items", type=int, default=4)
    parser.add_argument("--traversal-search-limit", type=int, default=8)
    parser.add_argument("--include-screenshot-refs", action="store_true")
    parser.add_argument("--inline-embeddings", action="store_true")
    parser.add_argument("--api-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--api-retry-attempts", type=int, default=3)
    parser.add_argument("--api-retry-base-delay-seconds", type=float, default=2.0)
    parser.add_argument("--api-retry-max-delay-seconds", type=float, default=30.0)
    parser.add_argument("--embedding-job-wait-timeout-seconds", type=float, default=1_800.0)
    parser.add_argument("--bulk-max-entities", type=int, default=32)
    parser.add_argument("--bulk-max-content-chars", type=int, default=512_000)
    parser.add_argument("--embedding-backfill-max-pending-jobs", type=int, default=8)

    parser.add_argument("--reader-model", default=os.getenv("READER_MODEL", "Qwen/Qwen3.5-9B"))
    parser.add_argument(
        "--reader-base-url", default=os.getenv("READER_BASE_URL", "http://localhost:8023/v1")
    )
    parser.add_argument("--reader-api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--reader-disable-thinking", action="store_true")
    parser.add_argument("--reader-temperature", type=float, default=0.6)
    parser.add_argument("--reader-top-p", type=float, default=0.95)
    parser.add_argument("--reader-top-k", type=int, default=20)
    parser.add_argument("--reader-max-concurrent-requests", type=int, default=16)
    parser.add_argument("--reader-retry-attempts", type=int, default=4)
    parser.add_argument("--reader-retry-base-delay-seconds", type=float, default=2.0)
    parser.add_argument("--reader-retry-max-delay-seconds", type=float, default=30.0)
    parser.add_argument("--max-completion-tokens", type=int, default=20_000)
    parser.add_argument("--memory-context-max-tokens", type=int, default=200_000)
    parser.add_argument("--timeout-seconds", type=float, default=43_200.0)

    parser.add_argument("--evaluator-model", default=os.getenv("EVALUATOR_MODEL", "gpt-5.2"))
    parser.add_argument("--evaluator-base-url", default=os.getenv("EVALUATOR_BASE_URL", ""))
    parser.add_argument(
        "--evaluator-api-key-env", default=os.getenv("EVALUATOR_API_KEY_ENV", "OPENAI_API_KEY")
    )
    parser.add_argument(
        "--evaluator-reasoning-effort", choices=["low", "medium", "high"], default="medium"
    )
    parser.add_argument("--evaluator-max-completion-tokens", type=int, default=4096)
    parser.add_argument("--evaluator-timeout-seconds", type=float, default=43_200.0)
    parser.add_argument("--evaluator-retry-attempts", type=int, default=3)
    parser.add_argument("--prompt-build-max-workers", type=int, default=1)
    parser.add_argument("--shuffle-questions-seed", type=int, default=None)
    args = parser.parse_args(argv)
    if not args.receipt_only and args.domain == "combined":
        parser.error("--domain combined is only valid with --receipt-only")
    if args.load_memory_dir and args.checkpoint_dir:
        parser.error("--load-memory-dir cannot be combined with --checkpoint-dir")
    if args.reuse_existing_project and not args.project_id:
        parser.error("--reuse-existing-project requires --project-id")
    if args.reuse_existing_project and args.checkpoint_dir:
        parser.error("--reuse-existing-project cannot be combined with --checkpoint-dir")
    experiment_fields = {
        "--experiment-id": args.experiment_id,
        "--experiment-phase": args.experiment_phase,
        "--pass-id": args.pass_id,
        "--pass-seed": args.pass_seed,
        "--arm-role": args.arm_role,
        "--substrate": args.substrate,
    }
    populated_experiment_fields = {
        flag
        for flag, value in experiment_fields.items()
        if value is not None and (not isinstance(value, str) or value.strip())
    }
    if populated_experiment_fields and len(populated_experiment_fields) != len(experiment_fields):
        missing = sorted(set(experiment_fields) - populated_experiment_fields)
        parser.error(f"experiment runs require all identity fields; missing {missing}")
    if args.pass_seed is not None and args.pass_seed < 0:
        parser.error("--pass-seed must be non-negative")
    preregistration = args.preregistration_sha256.strip()
    if preregistration and (
        len(preregistration) != 64 or any(char not in "0123456789abcdef" for char in preregistration)
    ):
        parser.error("--preregistration-sha256 must be a lowercase SHA-256 digest")
    if populated_experiment_fields:
        if args.tier != "small":
            parser.error("paid experiment runs require the complete Small corpus")
        if args.limit is not None or args.question_ids:
            parser.error(
                "paid experiment runs require every official domain question; "
                "--limit and --question-ids are forbidden"
            )
        if args.experiment_phase in {"race", "render"} and not preregistration:
            parser.error("race and render experiment phases require --preregistration-sha256")
        if args.experiment_phase in {"aa", "anchor"} and preregistration:
            parser.error("A/A and anchor experiment phases must precede preregistration")
    if args.max_spend_usd is not None and (
        not math.isfinite(args.max_spend_usd) or args.max_spend_usd <= 0
    ):
        parser.error("--max-spend-usd must be finite and positive")
    if (
        not math.isfinite(args.spend_contingency_multiplier)
        or args.spend_contingency_multiplier < 1
    ):
        parser.error("--spend-contingency-multiplier must be finite and at least 1")
    if (
        not math.isfinite(args.unmetered_provider_reserve_usd)
        or args.unmetered_provider_reserve_usd < 0
    ):
        parser.error("--unmetered-provider-reserve-usd must be finite and non-negative")
    reserve_token_fields = {
        "--reader-output-reserve-tokens": args.reader_output_reserve_tokens,
        "--evaluator-input-reserve-tokens": args.evaluator_input_reserve_tokens,
        "--evaluator-output-reserve-tokens": args.evaluator_output_reserve_tokens,
        "--operations-input-reserve-tokens": args.operations_input_reserve_tokens,
        "--operations-output-reserve-tokens": args.operations_output_reserve_tokens,
    }
    invalid_reserve_tokens = sorted(
        flag for flag, value in reserve_token_fields.items() if value < 1
    )
    if invalid_reserve_tokens:
        parser.error(f"spend reservation token ceilings must be positive: {invalid_reserve_tokens}")
    if populated_experiment_fields and not (args.plan_only or args.receipt_only):
        if args.max_spend_usd is None:
            parser.error("paid experiment runs require --max-spend-usd")
    args.execution = None
    if populated_experiment_fields:
        try:
            args.execution = resolve_execution_identity(args, root=ROOT)
        except ValueError as exc:
            parser.error(str(exc))
    if args.reader_retry_attempts < 1:
        parser.error("--reader-retry-attempts must be positive")
    if args.reader_retry_base_delay_seconds < 0:
        parser.error("--reader-retry-base-delay-seconds must be non-negative")
    if args.reader_retry_max_delay_seconds < 0:
        parser.error("--reader-retry-max-delay-seconds must be non-negative")
    if args.evaluator_retry_attempts < 1:
        parser.error("--evaluator-retry-attempts must be positive")
    if args.api_timeout_seconds <= 0:
        parser.error("--api-timeout-seconds must be positive")
    if args.api_retry_attempts < 1:
        parser.error("--api-retry-attempts must be positive")
    invalid_expansion_ratio = (
        not math.isfinite(args.context_expansion_max_ratio)
        or args.context_expansion_max_ratio < 0.0
        or 0.0 < args.context_expansion_max_ratio < 1.0
    )
    if invalid_expansion_ratio:
        parser.error("--context-expansion-max-ratio must be zero or at least 1.0")
    if args.api_retry_base_delay_seconds < 0:
        parser.error("--api-retry-base-delay-seconds must be non-negative")
    if args.api_retry_max_delay_seconds < 0:
        parser.error("--api-retry-max-delay-seconds must be non-negative")
    if args.max_chunks_per_trajectory < 1:
        parser.error("--max-chunks-per-trajectory must be positive")
    if args.neighbor_stitch_items < 0:
        parser.error("--neighbor-stitch-items must be non-negative")
    if args.neighbor_stitch_span < 0:
        parser.error("--neighbor-stitch-span must be non-negative")
    if args.state_part_completion_items < 0:
        parser.error("--state-part-completion-items must be non-negative")
    if args.max_context_total_chars < 1:
        parser.error("--max-context-total-chars must be positive")
    if args.render_char_total_treatment and args.max_context_total_chars <= 60_000:
        parser.error("--render-char-total-treatment requires more than 60000 context characters")
    if not 1 <= args.retrieval_max_planned_queries <= MAX_REFINEMENT_QUERIES:
        parser.error(
            f"--retrieval-max-planned-queries must be between 1 and {MAX_REFINEMENT_QUERIES}"
        )
    args.provider_usage_run_id = f"lme-v2-usage-{uuid4().hex[:12]}"
    return args


def parse_question_ids(raw_values: list[str] | None) -> list[str] | None:
    if not raw_values:
        return None
    ids = []
    for raw_value in raw_values:
        ids.extend(item.strip() for item in raw_value.split(",") if item.strip())
    return ids or None


def materialize_runtime_questions(
    *,
    data_root: Path,
    domain: str,
    question_ids: list[str] | None,
    limit: int | None,
    output_path: Path,
) -> list[dict[str, Any]]:
    questions = [
        question
        for question in load_longmemeval_v2_questions(data_root / "questions.jsonl")
        if question.domain == domain
    ]
    if question_ids:
        requested = set(question_ids)
        questions = [question for question in questions if question.id in requested]
        found = {question.id for question in questions}
        missing = requested - found
        if missing:
            msg = f"Unknown question ids for {domain}: {sorted(missing)}"
            raise RuntimeError(msg)
    if limit is not None:
        if limit <= 0:
            msg = "--limit must be positive"
            raise RuntimeError(msg)
        questions = questions[:limit]
    if not questions:
        msg = "No questions selected"
        raise RuntimeError(msg)

    rows: list[dict[str, Any]] = []
    for question in questions:
        row: dict[str, Any] = {
            "id": question.id,
            "domain": question.domain,
            "environment": question.environment,
            "question_type": question.question_type,
            "question": question.question,
            "answer": question.answer,
            "eval_function": question.eval_function,
        }
        if question.image is not None:
            image_path = data_root / question.image
            if not image_path.exists():
                msg = f"Missing question image: {image_path}"
                raise RuntimeError(msg)
            row["question"] = {"text": question.question, "image": str(image_path.resolve())}
        rows.append(row)
    write_json(output_path, rows)
    return rows


def materialize_runtime_haystack(
    *,
    data_root: Path,
    tier: str,
    selected_questions: list[dict[str, Any]],
    output_path: Path,
) -> dict[str, list[str]]:
    haystack = load_longmemeval_v2_haystack(haystack_path(data_root, tier))
    selected_haystack = {}
    for question in selected_questions:
        question_id = str(question["id"])
        if question_id not in haystack:
            msg = f"Missing haystack entry for question {question_id}"
            raise RuntimeError(msg)
        selected_haystack[question_id] = list(haystack[question_id])
    write_json(output_path, selected_haystack)
    return selected_haystack


def build_memory_config(args: argparse.Namespace) -> dict[str, object]:
    params: dict[str, object] = {
        "api_url": args.api_url,
        "longmemeval_v2_domain": args.domain,
        "project_id": args.project_id,
        "run_id": args.run_id,
        "reuse_existing_project": args.reuse_existing_project,
        "allow_localhost": args.allow_localhost,
        "allow_signup": not args.no_signup,
        "content_max_chars": args.content_max_chars,
        "chunking_mode": args.chunking_mode,
        "checkpoint_dir": args.checkpoint_dir,
        "search_limit": args.search_limit,
        "max_context_items": args.max_context_items,
        "max_context_chars_per_item": args.max_context_chars_per_item,
        "max_context_total_chars": args.max_context_total_chars,
        "max_chunks_per_trajectory": args.max_chunks_per_trajectory,
        "neighbor_stitch_items": args.neighbor_stitch_items,
        "neighbor_stitch_span": args.neighbor_stitch_span,
        "state_part_completion_items": args.state_part_completion_items,
        "state_part_refinement": args.state_part_refinement,
        "neighbor_support_exempt": args.neighbor_support_exempt,
        "neighbor_trajectory_preserving": args.neighbor_trajectory_preserving,
        "neighbor_support_overflow_items": args.neighbor_support_overflow_items,
        "neighbor_stitch_spread": args.neighbor_stitch_spread,
        "semantic_prior_rescue_weight": args.semantic_prior_rescue_weight,
        "typed_pool": args.typed_pool,
        "context_expansion_max_ratio": args.context_expansion_max_ratio,
        "evidence_types": list(args.evidence_types),
        "evidence_char_budget": args.evidence_char_budget,
        "evidence_char_budget_raw_reserve": args.evidence_char_budget_raw_reserve,
        "evidence_composition_mode": args.evidence_composition_mode,
        "source_evidence_bundling": args.source_evidence_bundling,
        "typed_stream_retrieval": args.typed_stream_retrieval,
        "typed_stream_limit": args.typed_stream_limit,
        "note_distillation": args.note_distillation,
        "note_distillation_model": args.note_distillation_model,
        "typed_reservation_items": args.typed_reservation_items,
        "retrieval_mode": args.retrieval_mode,
        "knn_type_overfetch": args.knn_type_overfetch,
        "retrieval_max_planned_queries": args.retrieval_max_planned_queries,
        "agentic_traversal": args.agentic_traversal,
        "traversal_widening_rounds": args.traversal_widening_rounds,
        "traversal_model": args.traversal_model,
        "traversal_max_actions": args.traversal_max_actions,
        "traversal_followup_searches": args.traversal_followup_searches,
        "traversal_deadline_seconds": args.traversal_deadline_seconds,
        "traversal_overflow_items": args.traversal_overflow_items,
        "traversal_search_limit": args.traversal_search_limit,
        "include_screenshot_refs": args.include_screenshot_refs,
        "defer_embeddings": not args.inline_embeddings,
        "api_timeout_seconds": args.api_timeout_seconds,
        "api_retry_attempts": args.api_retry_attempts,
        "api_retry_base_delay_seconds": args.api_retry_base_delay_seconds,
        "api_retry_max_delay_seconds": args.api_retry_max_delay_seconds,
        "embedding_job_wait_timeout_seconds": args.embedding_job_wait_timeout_seconds,
        "bulk_max_entities": args.bulk_max_entities,
        "bulk_max_content_chars": args.bulk_max_content_chars,
        "embedding_backfill_max_pending_jobs": args.embedding_backfill_max_pending_jobs,
        "runner_provenance": git_provenance(ROOT),
        "official_source": official_source_record(
            Path(args.official_repo).expanduser().resolve() if args.official_repo else None
        ),
    }
    if args.operational_note_dedupe_mode != "source":
        params["operational_note_dedupe_mode"] = args.operational_note_dedupe_mode
    if args.operational_note_lane_mode != "reserved":
        params["operational_note_lane_mode"] = args.operational_note_lane_mode
    if args.operational_note_distillation_profile != "baseline":
        params["operational_note_distillation_profile"] = (
            args.operational_note_distillation_profile
        )
    if args.render_char_total_treatment:
        params["render_char_total_treatment"] = True
    if args.render_group_lanes:
        params["render_group_lanes"] = True
    if args.render_action_spines:
        params["render_action_spines"] = True
    config = {"memory_type": "sibyl_live_api", "memory_params": params}
    if args.load_memory_dir:
        return build_loaded_memory_config(
            Path(args.load_memory_dir).expanduser().resolve(),
            requested_config=config,
        )
    if args.checkpoint_dir:
        checkpoint_dir = Path(args.checkpoint_dir).expanduser().resolve()
        if (checkpoint_dir / "memory_config.json").is_file():
            return build_loaded_memory_config(
                checkpoint_dir,
                requested_config=config,
            )
    return config


def _loaded_memory_attestation_dir(args: argparse.Namespace) -> Path | None:
    if args.load_memory_dir:
        return Path(args.load_memory_dir).expanduser().resolve()
    if not args.checkpoint_dir:
        return None
    checkpoint = Path(args.checkpoint_dir).expanduser().resolve()
    if not (checkpoint / "memory_config.json").is_file():
        return None
    manifest_path = checkpoint / "memory_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Loaded checkpoint has no completed memory manifest: {checkpoint}")
    return checkpoint


def attest_loaded_memory_project(
    *,
    args: argparse.Namespace,
    data_root: Path,
    selected_haystack: dict[str, list[str]],
    memory_config: dict[str, object],
) -> dict[str, object] | None:
    """Read-audit a completed saved project during plan-only execution."""

    memory_dir = _loaded_memory_attestation_dir(args)
    if memory_dir is None:
        return None
    manifest_path = memory_dir / "memory_manifest.json"
    manifest = _load_json_if_exists(manifest_path)
    params = memory_config.get("memory_params")
    if not isinstance(params, dict):
        raise RuntimeError("Loaded memory config has no memory_params")
    project_id = params.get("project_id")
    run_id = params.get("run_id")
    if (
        manifest.get("schema_version") != MEMORY_MANIFEST_SCHEMA_VERSION
        or manifest.get("ingest_finalized") is not True
        or manifest.get("longmemeval_v2_domain") != args.domain
        or manifest.get("project_id") != project_id
        or manifest.get("run_id") != run_id
        or params.get("longmemeval_v2_domain") != args.domain
        or not isinstance(project_id, str)
        or not project_id
        or not isinstance(run_id, str)
        or not run_id
    ):
        raise RuntimeError("Loaded memory manifest identity is incomplete or inconsistent")
    expected_ids = {
        str(trajectory_id)
        for trajectory_ids in selected_haystack.values()
        for trajectory_id in trajectory_ids
    }
    if not expected_ids:
        raise RuntimeError("Loaded memory attestation has no required trajectories")
    trajectories_by_id = load_jsonl_by_id(
        data_root / "trajectories.jsonl",
        required_ids=expected_ids,
    )
    if set(trajectories_by_id) != expected_ids:
        raise RuntimeError("Loaded memory attestation cannot resolve every trajectory")
    receipt = SibylLiveApiMemory.attest_existing(
        params,
        expected_trajectory_ids=expected_ids,
        trajectories=[trajectories_by_id[item] for item in sorted(expected_ids)],
    )
    if (
        receipt.get("project_id") != project_id
        or receipt.get("run_id") != run_id
        or receipt.get("longmemeval_v2_domain") != args.domain
        or receipt.get("content_audit", {}).get("status") != "verified"
    ):
        raise RuntimeError("Loaded memory remote attestation is incomplete or inconsistent")
    return receipt


def install_memory_credentials(args: argparse.Namespace) -> None:
    for environment_key, value in (
        ("SIBYL_API_TOKEN", args.api_token),
        ("SIBYL_API_CREDENTIALS_FILE", args.api_credentials_file),
        ("LME_SIBYL_EMAIL", args.email),
        ("LME_SIBYL_PASSWORD", args.password),
    ):
        if value:
            os.environ[environment_key] = value


# Requested keys deliberately NOT merged into a loaded memory config: ingest
# identity is pinned by the saved corpus, and transport/provenance describe
# this run, not retrieval behavior. Every other requested key MUST be in
# LOADED_MEMORY_RUNTIME_KEYS or the merge below would silently drop it (the
# arm-off-under-the-arm's-name failure mode).
LOADED_MEMORY_NON_MERGED_KEYS = frozenset(
    {
        "api_url",
        "longmemeval_v2_domain",
        "project_id",
        "run_id",
        "reuse_existing_project",
        "content_max_chars",
        "chunking_mode",
        "include_screenshot_refs",
        "defer_embeddings",
        "bulk_max_entities",
        "bulk_max_content_chars",
        "embedding_backfill_max_pending_jobs",
        "embedding_job_wait_timeout_seconds",
        "operational_note_distillation_profile",
    }
)


def build_loaded_memory_config(
    memory_dir: Path,
    *,
    requested_config: dict[str, object],
) -> dict[str, object]:
    saved_path = memory_dir / "memory_config.json"
    saved_config = _load_json_if_exists(saved_path)
    if saved_config.get("memory_type") != "sibyl_live_api":
        msg = f"Saved memory config is not sibyl_live_api: {saved_path}"
        raise RuntimeError(msg)
    saved_params = saved_config.get("memory_params")
    requested_params = requested_config.get("memory_params")
    if not isinstance(saved_params, dict) or not isinstance(requested_params, dict):
        msg = f"Saved or requested memory config is missing memory_params: {saved_path}"
        raise RuntimeError(msg)
    dropped = set(requested_params) - LOADED_MEMORY_RUNTIME_KEYS - LOADED_MEMORY_NON_MERGED_KEYS
    if dropped:
        msg = (
            "loaded-memory merge would silently drop requested keys "
            f"{sorted(dropped)}; classify them in LOADED_MEMORY_RUNTIME_KEYS "
            "or LOADED_MEMORY_NON_MERGED_KEYS"
        )
        raise RuntimeError(msg)
    effective_params = dict(saved_params)
    effective_params.update(
        {
            key: requested_params[key]
            for key in LOADED_MEMORY_RUNTIME_KEYS
            if key in requested_params
        }
    )
    return {"memory_type": "sibyl_live_api", "memory_params": effective_params}


def haystack_path(data_root: Path, tier: str) -> Path:
    nested = data_root / "haystacks" / f"lme_v2_{tier}.json"
    if nested.exists():
        return nested
    return data_root / f"lme_v2_{tier}.json"


def evaluator_function_name(raw: object) -> str:
    """Return the harness evaluator name without its encoded parameters."""

    value = str(raw).strip()
    return value.split("|", 1)[0].split("(", 1)[0].strip()


def experiment_identity(args: argparse.Namespace) -> dict[str, object]:
    return {
        "experiment_identity_schema_version": EXPERIMENT_IDENTITY_SCHEMA_VERSION,
        "experiment_id": args.experiment_id or None,
        "experiment_phase": args.experiment_phase or None,
        "pass_id": args.pass_id or None,
        "pass_seed": args.pass_seed,
        "arm_role": args.arm_role or None,
        "substrate": args.substrate or None,
        "preregistration_sha256": args.preregistration_sha256 or None,
        "max_spend_usd": args.max_spend_usd,
        "execution": args.execution,
    }


def _token_cost(tokens: int, price_per_million: float) -> float:
    return tokens * price_per_million / MILLION_TOKENS


def build_spend_reservation(
    *,
    args: argparse.Namespace,
    question_count: int,
    llm_eval_count: int,
    required_trajectory_count: int,
) -> dict[str, object]:
    """Reserve a bounded paid run before the first provider call."""

    reader_input_tokens = question_count * (
        math.ceil(args.max_context_total_chars / 4) + 2048
    )
    reader_output_tokens = question_count * args.reader_output_reserve_tokens
    judge_input_tokens = llm_eval_count * args.evaluator_input_reserve_tokens
    judge_output_tokens = llm_eval_count * args.evaluator_output_reserve_tokens
    planner_request_count = (
        question_count * args.retrieval_max_planned_queries
        if args.retrieval_mode == "accurate"
        else 0
    )
    distillation_request_count = required_trajectory_count if args.note_distillation else 0
    operations_request_count = planner_request_count + distillation_request_count
    operations_input_tokens = operations_request_count * args.operations_input_reserve_tokens
    operations_output_tokens = operations_request_count * args.operations_output_reserve_tokens

    sections = {
        "reader": {
            "requests": question_count,
            "input_tokens": reader_input_tokens,
            "output_tokens": reader_output_tokens,
            "estimated_usd": _token_cost(
                reader_input_tokens,
                float(EVAL_PRICE_SNAPSHOT["reader"]["input_per_million"]),
            )
            + _token_cost(
                reader_output_tokens,
                float(EVAL_PRICE_SNAPSHOT["reader"]["output_per_million"]),
            ),
        },
        "judge": {
            "requests": llm_eval_count,
            "input_tokens": judge_input_tokens,
            "output_tokens": judge_output_tokens,
            "estimated_usd": _token_cost(
                judge_input_tokens,
                float(EVAL_PRICE_SNAPSHOT["judge"]["input_per_million"]),
            )
            + _token_cost(
                judge_output_tokens,
                float(EVAL_PRICE_SNAPSHOT["judge"]["output_per_million"]),
            ),
        },
        "operations": {
            "requests": operations_request_count,
            "planner_requests": planner_request_count,
            "distillation_requests": distillation_request_count,
            "input_tokens": operations_input_tokens,
            "output_tokens": operations_output_tokens,
            "estimated_usd": _token_cost(
                operations_input_tokens,
                float(EVAL_PRICE_SNAPSHOT["operations"]["input_per_million"]),
            )
            + _token_cost(
                operations_output_tokens,
                float(EVAL_PRICE_SNAPSHOT["operations"]["output_per_million"]),
            ),
        },
    }
    metered_estimate = sum(float(section["estimated_usd"]) for section in sections.values())
    reserved_total = (
        metered_estimate * args.spend_contingency_multiplier
        + args.unmetered_provider_reserve_usd
    )
    max_spend = args.max_spend_usd
    return {
        "schema_version": "sibyl-longmemeval-v2-spend-reservation-v1",
        "status": (
            "PASS" if max_spend is None or reserved_total <= max_spend else "BLOCKED"
        ),
        "currency": "USD",
        "price_snapshot": EVAL_PRICE_SNAPSHOT,
        "price_snapshot_sources": {
            "reader": "OpenRouter Qwen3.5-9B provider maximum, verified 2026-08-20",
            "judge": "OpenAI GPT-5.2 standard API price, verified 2026-08-20",
            "operations": "OpenAI GPT-5.4 nano standard API price, verified 2026-08-20",
        },
        "sections": sections,
        "metered_estimate_usd": metered_estimate,
        "contingency_multiplier": args.spend_contingency_multiplier,
        "unmetered_provider_reserve_usd": args.unmetered_provider_reserve_usd,
        "reserved_total_usd": reserved_total,
        "max_spend_usd": max_spend,
        "within_cap": max_spend is None or reserved_total <= max_spend,
        "enforcement": (
            "reserve by explicit token estimate before providers; reject incomplete "
            "or over-cap provider accounting after the run"
        ),
    }


def enforce_spend_reservation(plan: dict[str, Any]) -> None:
    reservation = plan.get("spend_reservation")
    if not isinstance(reservation, dict) or reservation.get("within_cap") is not True:
        raise RuntimeError("paid experiment spend reservation exceeds its fixed cap")


def enforce_actual_spend_cap(
    receipt: dict[str, Any],
    *,
    max_spend_usd: float | None,
) -> None:
    accounting = receipt.get("accounting")
    cost = accounting.get("cost") if isinstance(accounting, dict) else None
    if not isinstance(cost, dict) or cost.get("coverage_complete") is not True:
        raise RuntimeError("paid experiment provider accounting is incomplete")
    actual = cost.get("provider_reported_total_usd")
    if not isinstance(actual, int | float) or isinstance(actual, bool) or not math.isfinite(actual):
        raise RuntimeError("paid experiment provider spend is missing")
    if max_spend_usd is None or actual > max_spend_usd:
        raise RuntimeError(
            f"paid experiment spend {actual:.6f} exceeds fixed cap {max_spend_usd!r}"
        )


def write_rig_rows(*, data_root: Path, output_dir: Path, domain: str) -> Path:
    """Derive score-blind exposure and explicit activity from official bytes."""

    questions = load_jsonl_by_id(data_root / "questions.jsonl")
    haystack = _load_json_if_exists(output_dir / "runtime_inputs" / "haystack.json")
    required_trajectory_ids = {
        str(trajectory_id)
        for trajectory_ids in haystack.values()
        if isinstance(trajectory_ids, list)
        for trajectory_id in trajectory_ids
    }
    trajectories = load_jsonl_by_id(
        data_root / "trajectories.jsonl",
        required_ids=required_trajectory_ids,
    )
    state_text_index = build_state_text_index(trajectories)
    result_rows = _load_jsonl_if_exists(output_dir / "per_question.jsonl")
    rig_rows: list[dict[str, object]] = []
    for index, result in enumerate(result_rows):
        question_id = str(result.get("question_id") or "").strip()
        question = questions.get(question_id)
        trajectory_ids = haystack.get(question_id)
        if question is None or not isinstance(trajectory_ids, list):
            raise RuntimeError(f"rig row {index} cannot resolve question {question_id!r}")
        trace = build_question_trace(
            domain=domain,
            result=result,
            question=question,
            haystack_ids=[str(item) for item in trajectory_ids],
            state_text_index=state_text_index,
            max_rank=DEFAULT_MAX_RANK,
        )
        context_status = _nested_value(result, "memory_post_query_metadata", "context_status")
        activity = _nested_value(result, "memory_post_query_metadata", "rig_activity")
        if context_status not in {"complete", "empty"}:
            raise RuntimeError(f"rig row {question_id!r} has invalid context status")
        if not isinstance(activity, dict):
            raise RuntimeError(f"rig row {question_id!r} has no explicit activity receipt")
        activity_events = activity.get("activity_events")
        if (
            not isinstance(activity_events, int)
            or isinstance(activity_events, bool)
            or activity_events < 1
        ):
            raise RuntimeError(f"rig row {question_id!r} has no positive activity events")
        if not isinstance(activity.get("mode"), str) or not activity["mode"].strip():
            raise RuntimeError(f"rig row {question_id!r} has no activity mode")
        if not isinstance(result.get("score_bool"), bool):
            raise RuntimeError(f"rig row {question_id!r} has no valid official score")
        exposure_eligible = bool(
            trace["exact_evidence_eligible"] and trace["exact_evidence_source_complete"]
        )
        rig_rows.append(
            {
                "question_id": question_id,
                "status": "valid",
                "context_status": context_status,
                "evidence_exposure_eligible": exposure_eligible,
                "evidence_exposed": (
                    bool(trace["metrics"]["exact_context_recall_at_k"])
                    if exposure_eligible
                    else None
                ),
                "activity": activity,
            }
        )
    if not rig_rows:
        raise RuntimeError("official experiment produced no rig rows")
    output_path = output_dir / "rig_rows.jsonl"
    output_path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rig_rows),
        encoding="utf-8",
    )
    return output_path


def build_run_plan(
    *,
    args: argparse.Namespace,
    data_root: Path,
    output_dir: Path,
    runtime_dir: Path,
    memory_config_path: Path,
    selected_questions: list[dict[str, Any]],
    selected_haystack: dict[str, list[str]],
) -> dict[str, Any]:
    all_questions = load_longmemeval_v2_questions(data_root / "questions.jsonl")
    question_by_id = {question.id: question for question in all_questions}
    official_question_ids = [
        question.id for question in all_questions if question.domain == args.domain
    ]
    selected_question_ids = [str(row["id"]) for row in selected_questions]
    selected_question_models = [question_by_id[str(row["id"])] for row in selected_questions]
    required_trajectories = sorted({tid for ids in selected_haystack.values() for tid in ids})
    llm_eval_count = sum(
        1
        for row in selected_questions
        if evaluator_function_name(row["eval_function"])
        in {"llm_abstention_checker", "llm_gotchas_checker"}
    )
    spend_reservation = build_spend_reservation(
        args=args,
        question_count=len(selected_questions),
        llm_eval_count=llm_eval_count,
        required_trajectory_count=len(required_trajectories),
    )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "run_id": args.run_id,
        "provider_usage_run_id": args.provider_usage_run_id,
        **experiment_identity(args),
        "runner_provenance": git_provenance(ROOT),
        "domain": args.domain,
        "tier": args.tier,
        "method": DEFAULT_METHOD,
        "data_root": str(data_root),
        "output_dir": str(output_dir),
        "runtime_dir": str(runtime_dir),
        "memory_config_path": str(memory_config_path),
        "official_repo": args.official_repo,
        "official_source": official_source_record(
            Path(args.official_repo).expanduser().resolve() if args.official_repo else None
        ),
        "plan_only": args.plan_only,
        "save_memory": args.save_memory,
        "skip_evaluation": args.skip_evaluation,
        "load_memory_dir": args.load_memory_dir,
        "checkpoint_dir": args.checkpoint_dir,
        "reuse_existing_project": args.reuse_existing_project,
        "trajectory_path": str(data_root / "trajectories.jsonl"),
        "trajectory_path_exists": (data_root / "trajectories.jsonl").exists(),
        "question_count": len(selected_questions),
        "selected_question_ids_sha256": sha256_question_ids(selected_question_ids),
        "official_question_count": len(official_question_ids),
        "official_question_ids_sha256": sha256_question_ids(official_question_ids),
        "selection_complete": selected_question_ids == official_question_ids,
        "required_trajectory_count": len(required_trajectories),
        "llm_eval_count": llm_eval_count,
        "spend_reservation": spend_reservation,
        "reader_model": args.reader_model,
        "reader_base_url": args.reader_base_url,
        "reader_max_concurrent_requests": args.reader_max_concurrent_requests,
        "reader_retry_attempts": args.reader_retry_attempts,
        "reader_retry_base_delay_seconds": args.reader_retry_base_delay_seconds,
        "reader_retry_max_delay_seconds": args.reader_retry_max_delay_seconds,
        "memory_api_timeout_seconds": args.api_timeout_seconds,
        "memory_api_retry_attempts": args.api_retry_attempts,
        "memory_api_retry_base_delay_seconds": args.api_retry_base_delay_seconds,
        "memory_api_retry_max_delay_seconds": args.api_retry_max_delay_seconds,
        "chunking_mode": args.chunking_mode,
        "max_chunks_per_trajectory": args.max_chunks_per_trajectory,
        "neighbor_stitch_items": args.neighbor_stitch_items,
        "neighbor_stitch_span": args.neighbor_stitch_span,
        "neighbor_support_exempt": args.neighbor_support_exempt,
        "neighbor_trajectory_preserving": args.neighbor_trajectory_preserving,
        "neighbor_support_overflow_items": args.neighbor_support_overflow_items,
        "neighbor_stitch_spread": args.neighbor_stitch_spread,
        "semantic_prior_rescue_weight": args.semantic_prior_rescue_weight,
        "typed_pool": args.typed_pool,
        "context_expansion_max_ratio": args.context_expansion_max_ratio,
        "max_context_total_chars": args.max_context_total_chars,
        "evidence_types": list(args.evidence_types),
        "evidence_char_budget": args.evidence_char_budget,
        "evidence_char_budget_raw_reserve": args.evidence_char_budget_raw_reserve,
        "evidence_composition_mode": args.evidence_composition_mode,
        "source_evidence_bundling": args.source_evidence_bundling,
        "typed_stream_retrieval": args.typed_stream_retrieval,
        "typed_stream_limit": args.typed_stream_limit,
        "note_distillation": args.note_distillation,
        "note_distillation_model": args.note_distillation_model,
        "operational_note_dedupe_mode": args.operational_note_dedupe_mode,
        "operational_note_lane_mode": args.operational_note_lane_mode,
        "operational_note_distillation_profile": args.operational_note_distillation_profile,
        "render_char_total_treatment": args.render_char_total_treatment,
        "render_group_lanes": args.render_group_lanes,
        "render_action_spines": args.render_action_spines,
        "typed_reservation_items": args.typed_reservation_items,
        "retrieval_mode": args.retrieval_mode,
        "knn_type_overfetch": args.knn_type_overfetch,
        "retrieval_max_planned_queries": args.retrieval_max_planned_queries,
        "agentic_traversal": args.agentic_traversal,
        "traversal_widening_rounds": args.traversal_widening_rounds,
        "traversal_model": args.traversal_model,
        "traversal_max_actions": args.traversal_max_actions,
        "traversal_followup_searches": args.traversal_followup_searches,
        "traversal_deadline_seconds": args.traversal_deadline_seconds,
        "traversal_overflow_items": args.traversal_overflow_items,
        "traversal_search_limit": args.traversal_search_limit,
        "include_screenshot_refs": args.include_screenshot_refs,
        "evaluator_model": args.evaluator_model,
        "evaluator_retry_attempts": args.evaluator_retry_attempts,
        "provider_usage": {
            "reader": str(output_dir / "provider_usage" / "reader.jsonl"),
            "judge": str(output_dir / "provider_usage" / "judge.jsonl"),
        },
        "requirements": build_requirement_status(args=args, data_root=data_root),
        "summary": summarize_longmemeval_v2_inputs(
            selected_question_models,
            selected_haystack,
        ),
        "honesty_contract": {
            "answer_gold_visible_to_memory": False,
            "question_gold_ids_visible_to_memory": False,
            "memory_surface": "Sibyl live API /entities and /search",
            "reader_surface": "official harness reader model",
            "scoring_surface": "official deterministic and LLM scoring functions",
        },
    }


def build_requirement_status(*, args: argparse.Namespace, data_root: Path) -> dict[str, bool]:
    official_repo = Path(args.official_repo).expanduser().resolve() if args.official_repo else None
    official_source = official_source_record(official_repo)
    return {
        "official_repo_configured": official_repo is not None,
        "official_harness_exists": bool(official_source["harness_exists"]),
        "official_commit_matches_pin": bool(official_source["pin_matches"]),
        "official_checkout_clean": official_source["git_status"] == "clean",
        "trajectories_jsonl_exists": (data_root / "trajectories.jsonl").exists(),
        "reader_api_key_env_set": bool(os.getenv(args.reader_api_key_env)),
        "reader_endpoint_reachable": reader_endpoint_reachable(
            args.reader_base_url,
            api_key_env=args.reader_api_key_env,
        ),
        "evaluator_api_key_env_set": bool(os.getenv(args.evaluator_api_key_env)),
        "transformers_available": importlib.util.find_spec("transformers") is not None,
        "torch_available": importlib.util.find_spec("torch") is not None,
    }


def reader_endpoint_reachable(base_url: str, *, api_key_env: str = "") -> bool:
    if not base_url:
        return True
    models_url = f"{base_url.rstrip('/')}/models"
    if urlparse(models_url).scheme not in {"http", "https"}:
        return False
    headers = {}
    if api_key_env and (api_key := os.getenv(api_key_env)):
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        request = Request(models_url, headers=headers)
        with urlopen(request, timeout=2) as response:  # noqa: S310
            return HTTP_STATUS_OK <= int(response.status) < HTTP_STATUS_SERVER_ERROR
    except Exception:
        return False


def build_harness_argv(
    *,
    args: argparse.Namespace,
    data_root: Path,
    output_dir: Path,
    runtime_dir: Path,
    memory_config_path: Path,
) -> list[str]:
    argv = [
        "evaluation.harness",
        "--domain",
        args.domain,
        "--questions-path",
        str(runtime_dir / "questions.json"),
        "--haystack-path",
        str(runtime_dir / "haystack.json"),
        "--trajectories-path",
        str(data_root / "trajectories.jsonl"),
        "--memory-config-path",
        str(memory_config_path),
        "--output-dir",
        str(output_dir),
        "--model",
        args.reader_model,
        "--api-key-env",
        args.reader_api_key_env,
        "--temperature",
        str(args.reader_temperature),
        "--top-p",
        str(args.reader_top_p),
        "--top-k",
        str(args.reader_top_k),
        "--max-completion-tokens",
        str(args.max_completion_tokens),
        "--memory-context-max-tokens",
        str(args.memory_context_max_tokens),
        "--reader-max-concurrent-requests",
        str(args.reader_max_concurrent_requests),
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--prompt-build-max-workers",
        str(args.prompt_build_max_workers),
        "--evaluator-model",
        args.evaluator_model,
        "--evaluator-api-key-env",
        args.evaluator_api_key_env,
        "--evaluator-reasoning-effort",
        args.evaluator_reasoning_effort,
        "--evaluator-max-completion-tokens",
        str(args.evaluator_max_completion_tokens),
        "--evaluator-timeout-seconds",
        str(args.evaluator_timeout_seconds),
    ]
    if args.save_memory:
        argv.append("--save-memory")
    if args.skip_evaluation:
        argv.append("--skip-evaluation")
    if args.load_memory_dir:
        argv.extend(["--load-memory-dir", args.load_memory_dir])
    if args.reader_base_url:
        argv.extend(["--base-url", args.reader_base_url])
    if args.reader_disable_thinking:
        argv.append("--reader-disable-thinking")
    if args.evaluator_base_url:
        argv.extend(["--evaluator-base-url", args.evaluator_base_url])
    if args.shuffle_questions_seed is not None:
        argv.extend(["--shuffle-questions-seed", str(args.shuffle_questions_seed)])
    return argv


def resolve_official_repo(raw_path: str | None) -> Path:
    if not raw_path:
        msg = "Set --official-repo or LME_V2_OFFICIAL_REPO to the LongMemEval-V2 checkout"
        raise RuntimeError(msg)
    return Path(raw_path).expanduser().resolve()


def ensure_official_harness(path: Path) -> None:
    if not (path / "evaluation" / "harness.py").exists():
        msg = f"Missing official evaluation/harness.py under {path}"
        raise RuntimeError(msg)


def resolve_receipt_output(args: argparse.Namespace, output_dir: Path) -> Path:
    if args.receipt_output:
        return Path(args.receipt_output).expanduser().resolve()
    return output_dir / "longmemeval_v2_official_receipt.json"


def build_receipt_from_artifacts(
    *,
    args: argparse.Namespace,
    data_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    official_repo = Path(args.official_repo).expanduser().resolve() if args.official_repo else None
    plan_path = output_dir / "longmemeval_v2_official_plan.json"
    aggregated_path = output_dir / "aggregated_metrics.json"
    per_question_path = output_dir / "per_question.jsonl"
    run_args_path = output_dir / "run_args.json"
    metric_overview_path = (
        _optional_path(args.metric_overview) or output_dir / "metric_overview.json"
    )
    combined_metrics_path = _optional_path(args.combined_metrics)
    submission_overview_path = _optional_path(args.submission_overview)
    submission_archive_path = _optional_path(args.submission_archive)

    plan = _load_json_if_exists(plan_path)
    run_args = _load_json_if_exists(run_args_path)
    aggregated_metrics = _load_json_if_exists(aggregated_path)
    metric_overview = _load_json_if_exists(metric_overview_path)
    combined_metrics = _load_json_if_exists(combined_metrics_path)
    submission_overview = _load_json_if_exists(submission_overview_path)
    per_question_rows = _load_jsonl_if_exists(per_question_path)
    source_runs = load_receipt_source_runs(args=args, output_dir=output_dir)
    source_runs_receipt = build_source_runs_receipt(args=args, source_runs=source_runs)

    metrics = extract_receipt_metrics(
        metric_overview=metric_overview,
        combined_metrics=combined_metrics,
        aggregated_metrics=aggregated_metrics,
        submission_overview=submission_overview,
    )
    accounting = build_receipt_accounting(
        metrics=metrics,
        aggregated_metrics=accounting_aggregated_metrics(
            source_runs=source_runs,
            combined_metrics=combined_metrics,
            aggregated_metrics=aggregated_metrics,
        ),
        per_question_rows=accounting_per_question_rows(
            source_runs=source_runs,
            fallback_rows=per_question_rows,
        ),
        source_runs=source_runs,
    )
    metrics.update(accounting_metric_aliases(accounting))
    official_repo_record = official_source_record(official_repo)
    dataset = build_dataset_receipt(
        data_root=data_root,
        domain=args.domain,
        tier=args.tier,
        plan=plan,
        aggregated_metrics=combined_metrics or aggregated_metrics,
    )
    artifacts = build_artifact_receipt(
        output_dir=output_dir,
        plan_path=plan_path,
        aggregated_path=aggregated_path,
        per_question_path=per_question_path,
        run_args_path=run_args_path,
        metric_overview_path=metric_overview_path,
        combined_metrics_path=combined_metrics_path,
        submission_overview_path=submission_overview_path,
        submission_archive_path=submission_archive_path,
    )
    runner_provenance = git_provenance(ROOT)
    source_reader_model = _consistent_source_run_arg(source_runs, "model")
    source_reader_base_url = _consistent_source_run_arg(source_runs, "base_url")
    source_evaluator_model = _consistent_source_run_arg(source_runs, "evaluator_model")
    source_evaluator_reasoning_effort = _consistent_source_run_arg(
        source_runs,
        "evaluator_reasoning_effort",
    )
    cli_model_fallback = (
        "" if args.domain == "combined" and args.receipt_only else args.reader_model
    )
    cli_evaluator_fallback = (
        "" if args.domain == "combined" and args.receipt_only else args.evaluator_model
    )
    models = {
        "reader_model": _first_string(
            source_reader_model, run_args.get("model"), cli_model_fallback
        ),
        "reader_base_url": _first_string(
            source_reader_base_url,
            run_args.get("base_url"),
            args.reader_base_url,
        ),
        "reader_expected_fragment": QWEN_READER_MODEL_FRAGMENT,
        "evaluator_model": _first_string(
            source_evaluator_model,
            run_args.get("evaluator_model"),
            cli_evaluator_fallback,
        ),
        "evaluator_expected_fragment": GPT_EVALUATOR_MODEL_FRAGMENT,
        "evaluator_reasoning_effort": _first_string(
            source_evaluator_reasoning_effort,
            run_args.get("evaluator_reasoning_effort"),
            args.evaluator_reasoning_effort,
        ),
    }
    method = _first_string(
        _consistent_source_run_arg(source_runs, "method"),
        run_args.get("method"),
        DEFAULT_METHOD,
    )

    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "suite": "LongMemEval-V2 official",
        "suite_version": "official-harness-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "sibyl_commit": git_commit(ROOT),
        "runner_provenance": runner_provenance,
        "command": [
            "benchmarks/longmemeval_v2_official.py",
            *_redacted_command_args(args.command_args),
        ],
        "domain": args.domain,
        "tier": args.tier,
        "method": method,
        "claim_boundary": (
            "Official LongMemEval-V2 harness receipt for Sibyl live API memory. "
            "It is citable only when both domains are complete and LAFS gain is finite."
        ),
        "official_repo": official_repo_record,
        "dataset": dataset,
        "source_runs": source_runs_receipt,
        "models": models,
        "artifacts": artifacts,
        "metrics": metrics,
        "accounting": accounting,
        "approval_boundary": {
            "official_full_run_requires_approval": True,
            "paid_model_calls": "reader/evaluator endpoints are external to the PR-safe probe",
            "safe_without_approval": args.plan_only or args.receipt_only,
        },
    }
    receipt["checks"] = build_receipt_checks(receipt)
    return receipt


def load_receipt_source_runs(
    *,
    args: argparse.Namespace,
    output_dir: Path,
) -> list[dict[str, Any]]:
    configured = [
        ("web", _optional_path(args.web_output_dir)),
        ("enterprise", _optional_path(args.enterprise_output_dir)),
    ]
    source_dirs = [(domain, path) for domain, path in configured if path is not None]
    if not source_dirs:
        source_dirs = [(args.domain, output_dir)]

    source_runs: list[dict[str, Any]] = []
    for domain, source_dir in source_dirs:
        plan_path = source_dir / "longmemeval_v2_official_plan.json"
        run_args_path = source_dir / "run_args.json"
        aggregated_path = source_dir / "aggregated_metrics.json"
        per_question_path = source_dir / "per_question.jsonl"
        rig_rows_path = source_dir / "rig_rows.jsonl"
        official_receipt_path = source_dir / "longmemeval_v2_official_receipt.json"
        runtime_dir = source_dir / "runtime_inputs"
        runtime_questions_path = runtime_dir / "questions.json"
        runtime_haystack_path = runtime_dir / "haystack.json"
        memory_config_path = runtime_dir / "memory_config.json"
        reader_usage_path = source_dir / "provider_usage" / "reader.jsonl"
        judge_usage_path = source_dir / "provider_usage" / "judge.jsonl"
        plan = _load_json_if_exists(plan_path)
        expected_run_id = plan.get("provider_usage_run_id") or plan.get("run_id")
        if not isinstance(expected_run_id, str) or not expected_run_id:
            expected_run_id = None
        memory_config = _load_json_if_exists(memory_config_path)
        reader_usage_log = _load_usage_log(
            reader_usage_path,
            role="reader",
            expected_run_id=expected_run_id,
            filter_to_expected_run=True,
        )
        judge_usage_log = _load_usage_log(
            judge_usage_path,
            role="judge",
            expected_run_id=expected_run_id,
            filter_to_expected_run=True,
        )
        source_runs.append(
            {
                "domain": domain,
                "output_dir": source_dir,
                "plan_path": plan_path,
                "run_args_path": run_args_path,
                "aggregated_path": aggregated_path,
                "per_question_path": per_question_path,
                "rig_rows_path": rig_rows_path,
                "official_receipt_path": official_receipt_path,
                "runtime_questions_path": runtime_questions_path,
                "runtime_haystack_path": runtime_haystack_path,
                "memory_config_path": memory_config_path,
                "reader_usage_path": reader_usage_path,
                "judge_usage_path": judge_usage_path,
                "plan": plan,
                "run_args": _load_json_if_exists(run_args_path),
                "aggregated_metrics": _load_json_if_exists(aggregated_path),
                "per_question_rows": _load_jsonl_if_exists(per_question_path),
                "memory_config": memory_config,
                "reader_usage_events": reader_usage_log["events"],
                "reader_usage_invalid_lines": reader_usage_log["invalid_lines"],
                "reader_usage_run_ids": reader_usage_log["run_ids"],
                "reader_usage_foreign_event_count": reader_usage_log["foreign_event_count"],
                "judge_usage_events": judge_usage_log["events"],
                "judge_usage_invalid_lines": judge_usage_log["invalid_lines"],
                "judge_usage_run_ids": judge_usage_log["run_ids"],
                "judge_usage_foreign_event_count": judge_usage_log["foreign_event_count"],
                "expected_usage_run_id": expected_run_id,
            }
        )
    return source_runs


def build_source_runs_receipt(
    *,
    args: argparse.Namespace,
    source_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    domains: dict[str, dict[str, Any]] = {}
    for source_run in source_runs:
        domain = str(source_run["domain"])
        run_args = source_run["run_args"]
        output_dir = source_run["output_dir"]
        api_runtime, api_runtime_consistent = _source_api_runtime(source_run["per_question_rows"])
        domains[domain] = {
            "output_dir": str(output_dir),
            "plan": artifact_path_record(source_run["plan_path"]),
            "run_args": artifact_path_record(source_run["run_args_path"]),
            "aggregated_metrics": artifact_path_record(source_run["aggregated_path"]),
            "per_question": artifact_path_record(source_run["per_question_path"]),
            "rig_rows": artifact_path_record(source_run["rig_rows_path"]),
            "official_receipt": artifact_path_record(source_run["official_receipt_path"]),
            "runtime_inputs": {
                "questions": artifact_path_record(source_run["runtime_questions_path"]),
                "haystack": artifact_path_record(source_run["runtime_haystack_path"]),
                "memory_config": artifact_path_record(source_run["memory_config_path"]),
            },
            "provider_usage": {
                "reader": {
                    **artifact_path_record(source_run["reader_usage_path"]),
                    "event_count": len(source_run["reader_usage_events"]),
                    "invalid_line_count": source_run["reader_usage_invalid_lines"],
                    "run_ids": source_run["reader_usage_run_ids"],
                    "expected_run_id": source_run["expected_usage_run_id"],
                    "foreign_event_count": source_run["reader_usage_foreign_event_count"],
                    "attempt_count": len(source_run["reader_usage_run_ids"]),
                },
                "judge": {
                    **artifact_path_record(source_run["judge_usage_path"]),
                    "event_count": len(source_run["judge_usage_events"]),
                    "invalid_line_count": source_run["judge_usage_invalid_lines"],
                    "run_ids": source_run["judge_usage_run_ids"],
                    "expected_run_id": source_run["expected_usage_run_id"],
                    "foreign_event_count": source_run["judge_usage_foreign_event_count"],
                    "attempt_count": len(source_run["judge_usage_run_ids"]),
                },
            },
            "effective_memory_config": _sanitize_config(source_run["memory_config"]),
            "api_runtime": api_runtime,
            "api_runtime_consistent": api_runtime_consistent,
            "reader_model": run_args.get("model"),
            "reader_base_url": run_args.get("base_url"),
            "evaluator_model": run_args.get("evaluator_model"),
            "method": run_args.get("method"),
            "tier": run_args.get("tier"),
        }

    expected_domains = ("web", "enterprise") if args.domain == "combined" else (args.domain,)
    complete = all(
        domain in domains
        and domains[domain]["run_args"]["exists"]
        and domains[domain]["aggregated_metrics"]["exists"]
        and domains[domain]["per_question"]["exists"]
        for domain in expected_domains
    )
    integrity_complete = all(
        domain in domains
        and domains[domain]["plan"]["exists"]
        and (args.domain != "combined" or domains[domain]["official_receipt"]["exists"])
        and all(record["exists"] for record in domains[domain]["runtime_inputs"].values())
        and all(
            record["exists"]
            and record["event_count"] > 0
            and record["invalid_line_count"] == 0
            and record["foreign_event_count"] == 0
            and record["run_ids"] == [record["expected_run_id"]]
            for record in domains[domain]["provider_usage"].values()
        )
        for domain in expected_domains
    )
    return {
        "expected_domains": list(expected_domains),
        "domains": domains,
        "complete": complete,
        "integrity_complete": integrity_complete,
        "api_runtime_consistent": all(
            domain in domains
            and domains[domain]["api_runtime_consistent"]
            and _runtime_provenance_complete(domains[domain]["api_runtime"])
            for domain in expected_domains
        ),
        "model_consistent": _source_run_values_consistent(source_runs, "model")
        and _source_run_values_consistent(source_runs, "evaluator_model"),
        "method_consistent": _source_run_values_consistent(source_runs, "method"),
    }


def accounting_aggregated_metrics(
    *,
    source_runs: list[dict[str, Any]],
    combined_metrics: dict[str, Any],
    aggregated_metrics: dict[str, Any],
) -> dict[str, Any]:
    if combined_metrics:
        return combined_metrics
    source_metrics = [
        source_run["aggregated_metrics"]
        for source_run in source_runs
        if source_run["aggregated_metrics"]
    ]
    if len(source_metrics) == 1:
        return source_metrics[0]
    if len(source_metrics) >= MIN_COMBINED_SOURCE_METRICS:
        return combine_accounting_metrics(source_metrics)
    return aggregated_metrics


def accounting_per_question_rows(
    *,
    source_runs: list[dict[str, Any]],
    fallback_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows = [
        row
        for source_run in source_runs
        for row in source_run["per_question_rows"]
        if isinstance(row, dict)
    ]
    return rows or fallback_rows


def combine_accounting_metrics(metrics: list[dict[str, Any]]) -> dict[str, Any]:
    question_counts = [_question_count_from_metrics(metric) for metric in metrics]
    total_questions = sum(question_counts)
    return {
        "tokens": _combine_token_metrics(metrics, question_counts),
        "memory_query": _combine_timing_metrics(metrics, question_counts, "memory_query"),
        "overall": {"count_all_questions": total_questions},
    }


def _question_count_from_metrics(metrics: dict[str, Any]) -> int:
    count = _number_from(metrics, ("overall", "count_all_questions"), ("question_count",))
    return int(count or 0)


def _combine_token_metrics(
    metrics: list[dict[str, Any]],
    question_counts: list[int],
) -> dict[str, float]:
    prompt_tokens = _sum_metric(metrics, ("tokens", "prompt_tokens"))
    completion_tokens = _sum_metric(metrics, ("tokens", "completion_tokens"))
    total_tokens = _sum_metric(metrics, ("tokens", "total_tokens"))
    if total_tokens == 0.0 and (prompt_tokens or completion_tokens):
        total_tokens = prompt_tokens + completion_tokens
    total_questions = sum(question_counts)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "avg_prompt_tokens": prompt_tokens / total_questions if total_questions else 0.0,
        "avg_completion_tokens": (completion_tokens / total_questions if total_questions else 0.0),
        "avg_total_tokens": total_tokens / total_questions if total_questions else 0.0,
    }


def _combine_timing_metrics(
    metrics: list[dict[str, Any]],
    question_counts: list[int],
    section: str,
) -> dict[str, float | None]:
    total_seconds = 0.0
    saw_total = False
    max_seconds: float | None = None
    total_questions = sum(question_counts)
    for metric, question_count in zip(metrics, question_counts, strict=True):
        timing = metric.get(section) if isinstance(metric.get(section), dict) else {}
        section_total = _number_from(timing, ("total_seconds",))
        if section_total is None:
            avg_seconds = _number_from(timing, ("avg_seconds",))
            section_total = avg_seconds * question_count if avg_seconds is not None else None
        if section_total is not None:
            total_seconds += section_total
            saw_total = True
        section_max = _number_from(timing, ("max_seconds",))
        if section_max is not None:
            max_seconds = section_max if max_seconds is None else max(max_seconds, section_max)
    return {
        "avg_seconds": total_seconds / total_questions if saw_total and total_questions else None,
        "max_seconds": max_seconds,
        "total_seconds": total_seconds if saw_total else None,
    }


def _sum_metric(metrics: list[dict[str, Any]], path: tuple[str, ...]) -> float:
    total = 0.0
    for metric in metrics:
        total += _number_from(metric, path) or 0.0
    return total


def _source_run_values_consistent(source_runs: list[dict[str, Any]], key: str) -> bool:
    values = [
        value.strip()
        for source_run in source_runs
        if isinstance(value := source_run["run_args"].get(key), str) and value.strip()
    ]
    return len(set(values)) <= 1


def _consistent_source_run_arg(source_runs: list[dict[str, Any]], key: str) -> str:
    values = [
        value.strip()
        for source_run in source_runs
        if isinstance(value := source_run["run_args"].get(key), str) and value.strip()
    ]
    if not values:
        return ""
    return values[0] if len(set(values)) == 1 else ""


def extract_receipt_metrics(
    *,
    metric_overview: dict[str, Any],
    combined_metrics: dict[str, Any],
    aggregated_metrics: dict[str, Any],
    submission_overview: dict[str, Any],
) -> dict[str, float | None]:
    metric_sources = (metric_overview, combined_metrics, aggregated_metrics)
    return {
        "lafs_gain": _extract_lafs_gain(submission_overview),
        "overall_full_set": _number_from_sources(
            metric_sources,
            ("overall_full_set",),
            ("overall", "overall_full_set"),
        ),
        "gotchas_accuracy": _number_from_sources(
            metric_sources,
            ("gotchas_accuracy",),
            ("non_abstention_by_category", "gotchas", "pct_correct"),
        ),
        "static_accuracy": _number_from_sources(
            metric_sources,
            ("static_accuracy",),
            ("combined_abstention_by_category", "static", "pct_correct"),
        ),
        "dynamic_accuracy": _number_from_sources(
            metric_sources,
            ("dynamic_accuracy",),
            ("combined_abstention_by_category", "dynamic", "pct_correct"),
        ),
        "procedure_accuracy": _number_from_sources(
            metric_sources,
            ("procedure_accuracy",),
            ("combined_abstention_by_category", "procedure", "pct_correct"),
        ),
        "memory_query_avg_seconds": _number_from_sources(
            metric_sources,
            ("memory_query_avg_seconds",),
            ("memory_query", "avg_seconds"),
        ),
    }


def _number_from_sources(
    sources: tuple[dict[str, Any], ...],
    *paths: tuple[str, ...],
) -> float | None:
    for source in sources:
        number = _number_from(source, *paths)
        if number is not None:
            return number
    return None


def _extract_lafs_gain(submission_overview: dict[str, Any]) -> float | None:
    return _number_from(
        submission_overview,
        ("lafs_gain",),
        ("lafs", "lafs_gain"),
        ("lafs_summary", "lafs_gain"),
    )


def build_receipt_accounting(
    *,
    metrics: dict[str, float | None],
    aggregated_metrics: dict[str, Any],
    per_question_rows: list[dict[str, Any]],
    source_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    latencies = [
        latency
        for row in per_question_rows
        if (latency := _extract_case_latency_seconds(row)) is not None
    ]
    p50_seconds = percentile(latencies, 50)
    p95_seconds = percentile(latencies, 95)
    max_seconds = (
        max(latencies)
        if latencies
        else _number_from(aggregated_metrics, ("memory_query", "max_seconds"))
    )
    avg_seconds = metrics.get("memory_query_avg_seconds")
    if p50_seconds is None:
        p50_seconds = avg_seconds
    if p95_seconds is None:
        p95_seconds = max_seconds if max_seconds is not None else avg_seconds
    if max_seconds is None:
        max_seconds = p95_seconds
    p50_ms = seconds_to_ms(p50_seconds)
    p95_ms = seconds_to_ms(p95_seconds)
    max_ms = seconds_to_ms(max_seconds)

    tokens = (
        aggregated_metrics.get("tokens")
        if isinstance(aggregated_metrics.get("tokens"), dict)
        else {}
    )
    prompt_tokens = (
        _as_number(tokens.get("prompt_tokens")) or _as_number(tokens.get("total_tokens")) or 0.0
    )
    completion_tokens = _as_number(tokens.get("completion_tokens")) or 0.0
    total_tokens = _as_number(tokens.get("total_tokens")) or (prompt_tokens + completion_tokens)
    embedding = _embedding_accounting(source_runs)
    planner = _planner_accounting(source_runs)
    distillation = _distillation_accounting(source_runs)
    reader = _provider_accounting(
        source_runs,
        role="reader",
        fallback_input_tokens=prompt_tokens,
        fallback_output_tokens=completion_tokens,
    )
    judge = _provider_accounting(
        source_runs,
        role="judge",
        fallback_input_tokens=0.0,
        fallback_output_tokens=0.0,
    )
    provider_reported_total_usd = sum(
        float(section["provider_reported_cost_usd"])
        for section in (embedding, planner, distillation, reader, judge)
    )
    cost_coverage_complete = all(
        bool(section["cost_coverage_complete"])
        for section in (embedding, planner, distillation, reader, judge)
    )

    return {
        "schema_version": ACCOUNTING_SCHEMA_VERSION,
        "gate_status": "required-for-citable-longmemeval-v2-receipts",
        "latency": {
            "p50_ms": p50_ms,
            "p95_ms": p95_ms,
            "max_ms": max_ms,
            "source": "official per_question memory query timings",
        },
        "tokens": {
            "estimated_input_tokens": prompt_tokens,
            "estimated_output_tokens": completion_tokens,
            "full_context_baseline_estimated_tokens": total_tokens,
            "estimator": "official-harness-token-counters",
            "source": "official aggregated token counters",
        },
        "embedding": embedding,
        "planner": planner,
        "distillation": distillation,
        "reader": reader,
        "judge": judge,
        "cost": {
            "estimated_total_usd": provider_reported_total_usd,
            "provider_reported_total_usd": provider_reported_total_usd,
            "currency": "USD",
            "coverage_complete": cost_coverage_complete,
            "is_lower_bound": not cost_coverage_complete,
            "enforcement": (
                "provider-reported cost only; unpriced requests remain explicit and are not "
                "silently estimated"
            ),
        },
    }


def _embedding_accounting(source_runs: list[dict[str, Any]]) -> dict[str, Any]:
    ingest_records: list[dict[str, Any]] = []
    query_records: list[dict[str, Any]] = []
    tracking_complete = bool(source_runs)
    for source_run in source_runs:
        rows = source_run["per_question_rows"]
        ingest_candidates = [
            usage
            for row in rows
            if isinstance(
                usage := _nested_value(
                    row,
                    "memory_post_query_metadata",
                    "ingest_embedding_usage",
                ),
                dict,
            )
            and usage
        ]
        if ingest_candidates:
            ingest_records.append(ingest_candidates[0])
        else:
            tracking_complete = False
        for row in rows:
            usage = _nested_value(
                row,
                "memory_post_query_metadata",
                "search_metadata",
                "embedding_usage",
            )
            if isinstance(usage, dict) and usage:
                query_records.append(usage)
            else:
                tracking_complete = False
    ingest = _summarize_embedding_usage(ingest_records)
    query = _summarize_embedding_usage(query_records)
    requests = int(ingest["requests"] + query["requests"])
    priced_requests = int(ingest["cost_reported_requests"] + query["cost_reported_requests"])
    provider_reported_cost_usd = float(
        ingest["provider_reported_cost_usd"] + query["provider_reported_cost_usd"]
    )
    providers = sorted(set(ingest["providers"]) | set(query["providers"]))
    models = sorted(set(ingest["models"]) | set(query["models"]))
    return {
        "calls": requests,
        "requests": requests,
        "priced_requests": priced_requests,
        "provider": ",".join(providers) or "unknown",
        "model": ",".join(models) or "unknown",
        "providers": providers,
        "models": models,
        "estimated_input_tokens": float(ingest["input_tokens"] + query["input_tokens"]),
        "estimated_output_tokens": 0.0,
        "provider_reported_cost_usd": provider_reported_cost_usd,
        "estimated_cost_usd": provider_reported_cost_usd,
        "cost_coverage_complete": tracking_complete and priced_requests == requests,
        "tracking_complete": tracking_complete,
        "ingest": ingest,
        "query": query,
        "cost_basis": "Sibyl embedding provider response usage",
    }


def _planner_accounting(source_runs: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    expected_rows = 0
    tracking_complete = bool(source_runs)
    for source_run in source_runs:
        source_mode = _nested_value(
            source_run.get("memory_config", {}),
            "memory_params",
            "retrieval_mode",
        )
        rows = source_run["per_question_rows"]
        for row in rows:
            row_mode = _nested_value(
                row,
                "memory_post_query_metadata",
                "retrieval_mode",
            )
            status = _nested_value(
                row,
                "memory_post_query_metadata",
                "search_metadata",
                "planner_status",
            )
            usage = _nested_value(
                row,
                "memory_post_query_metadata",
                "search_metadata",
                "planner_usage",
            )
            if row_mode is None:
                if status in {"success", "fallback"} or isinstance(usage, dict):
                    row_mode = "accurate"
                elif status == "not_requested":
                    row_mode = "fast"
                else:
                    row_mode = source_mode
            if row_mode != "accurate":
                continue
            expected_rows += 1
            if status in {"success", "partial"} and isinstance(usage, dict) and usage:
                records.append(usage)
            else:
                tracking_complete = False

    requests = int(sum(_number_from(record, ("requests",)) or 0 for record in records))
    input_tokens = sum(_number_from(record, ("input_tokens",)) or 0.0 for record in records)
    output_tokens = sum(_number_from(record, ("output_tokens",)) or 0.0 for record in records)
    priced_requests = int(
        sum(
            (_number_from(record, ("requests",)) or 0)
            if record.get("cost_complete") is True
            and _number_from(record, ("cost_usd",)) is not None
            else 0
            for record in records
        )
    )
    provider_reported_cost_usd = sum(
        _number_from(record, ("cost_usd",)) or 0.0 for record in records
    )
    providers = sorted(
        {
            provider
            for record in records
            if isinstance(provider := record.get("provider"), str) and provider
        }
    )
    models = sorted(
        {model for record in records if isinstance(model := record.get("model"), str) and model}
    )
    coverage_complete = tracking_complete and len(records) == expected_rows
    return {
        "calls": requests,
        "requests": requests,
        "priced_requests": priced_requests,
        "provider": ",".join(providers) or "not_requested",
        "model": ",".join(models) or "not_requested",
        "providers": providers,
        "models": models,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "provider_reported_cost_usd": provider_reported_cost_usd,
        "estimated_cost_usd": provider_reported_cost_usd,
        "cost_coverage_complete": coverage_complete and priced_requests == requests,
        "tracking_complete": coverage_complete,
        "expected_question_count": expected_rows,
        "recorded_question_count": len(records),
        "cost_basis": "Sibyl structured query planner usage receipt",
    }


def _distillation_accounting(source_runs: list[dict[str, Any]]) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    tracking_complete = bool(source_runs)
    expected_source_runs = 0
    for source_run in source_runs:
        if source_run.get("plan", {}).get("note_distillation") is not True:
            continue
        expected_source_runs += 1
        rows = source_run["per_question_rows"]
        candidates = [
            usage
            for row in rows
            if isinstance(
                usage := _nested_value(
                    row,
                    "memory_post_query_metadata",
                    "ingest_note_distillation_usage",
                ),
                dict,
            )
            and usage
        ]
        serialized = {json.dumps(candidate, sort_keys=True) for candidate in candidates}
        if len(candidates) != len(rows) or len(serialized) != 1:
            tracking_complete = False
            continue
        records.append(candidates[0])

    requests = int(sum(_number_from(record, ("requests",)) or 0 for record in records))
    input_tokens = sum(_number_from(record, ("input_tokens",)) or 0.0 for record in records)
    output_tokens = sum(_number_from(record, ("output_tokens",)) or 0.0 for record in records)
    total_tokens = sum(_number_from(record, ("total_tokens",)) or 0.0 for record in records)
    priced_requests = int(
        sum(
            (_number_from(record, ("requests",)) or 0)
            if record.get("cost_complete") is True
            and _number_from(record, ("cost_usd",)) is not None
            else 0
            for record in records
        )
    )
    provider_reported_cost_usd = sum(
        _number_from(record, ("cost_usd",)) or 0.0 for record in records
    )
    providers = sorted(
        {
            provider
            for record in records
            if isinstance(provider := record.get("provider"), str) and provider
        }
    )
    models = sorted(
        {model for record in records if isinstance(model := record.get("model"), str) and model}
    )
    tracking_complete = tracking_complete and len(records) == expected_source_runs
    return {
        "calls": requests,
        "requests": requests,
        "priced_requests": priced_requests,
        "provider": ",".join(providers) or "not_requested",
        "model": ",".join(models) or "not_requested",
        "providers": providers,
        "models": models,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "provider_reported_cost_usd": provider_reported_cost_usd,
        "estimated_cost_usd": provider_reported_cost_usd,
        "cost_coverage_complete": tracking_complete and priced_requests == requests,
        "tracking_complete": tracking_complete,
        "expected_source_run_count": expected_source_runs,
        "recorded_source_run_count": len(records),
        "cost_basis": "Sibyl operational note distillation usage receipt",
    }


def _summarize_embedding_usage(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "requests": int(sum(_number_from(record, ("requests",)) or 0 for record in records)),
        "inputs": int(sum(_number_from(record, ("inputs",)) or 0 for record in records)),
        "input_tokens": sum(
            _number_from(record, ("prompt_tokens",), ("total_tokens",)) or 0.0 for record in records
        ),
        "cost_reported_requests": int(
            sum(_number_from(record, ("cost_reported_requests",)) or 0 for record in records)
        ),
        "provider_reported_cost_usd": sum(
            _number_from(record, ("cost_usd",)) or 0.0 for record in records
        ),
        "providers": sorted(
            {
                provider
                for record in records
                if isinstance(provider := record.get("provider"), str) and provider
            }
        ),
        "models": sorted(
            {model for record in records if isinstance(model := record.get("model"), str) and model}
        ),
    }


def _provider_accounting(
    source_runs: list[dict[str, Any]],
    *,
    role: str,
    fallback_input_tokens: float,
    fallback_output_tokens: float,
) -> dict[str, Any]:
    event_key = f"{role}_usage_events"
    path_key = f"{role}_usage_path"
    events = [event for source_run in source_runs for event in source_run[event_key]]
    tracking_complete = bool(source_runs) and all(
        source_run[path_key].is_file()
        and source_run[f"{role}_usage_invalid_lines"] == 0
        and source_run["expected_usage_run_id"] is not None
        and source_run[f"{role}_usage_foreign_event_count"] == 0
        and bool(source_run[event_key])
        for source_run in source_runs
    )
    input_tokens = sum(_number_from(event, ("usage", "prompt_tokens")) or 0.0 for event in events)
    output_tokens = sum(
        _number_from(event, ("usage", "completion_tokens")) or 0.0 for event in events
    )
    costs = [
        cost for event in events if (cost := _number_from(event, ("usage", "cost_usd"))) is not None
    ]
    request_count = len(events)
    priced_requests = len(costs)
    provider_reported_cost_usd = sum(costs)
    return {
        "requests": request_count,
        "priced_requests": priced_requests,
        "requested_models": sorted(
            {
                model
                for event in events
                if isinstance(model := event.get("requested_model"), str) and model
            }
        ),
        "provider_models": sorted(
            {
                model
                for event in events
                if isinstance(model := event.get("provider_model"), str) and model
            }
        ),
        "estimated_input_tokens": input_tokens if events else fallback_input_tokens,
        "estimated_output_tokens": output_tokens if events else fallback_output_tokens,
        "provider_reported_cost_usd": provider_reported_cost_usd,
        "estimated_cost_usd": provider_reported_cost_usd,
        "cost_coverage_complete": tracking_complete and priced_requests == request_count,
        "tracking_complete": tracking_complete,
        "cost_basis": "provider response usage sidecar",
    }


def accounting_metric_aliases(accounting: dict[str, Any]) -> dict[str, float]:
    latency = accounting.get("latency") if isinstance(accounting.get("latency"), dict) else {}
    tokens = accounting.get("tokens") if isinstance(accounting.get("tokens"), dict) else {}
    embedding = accounting.get("embedding") if isinstance(accounting.get("embedding"), dict) else {}
    aliases: dict[str, float] = {}
    for metric, value in (
        ("latency_p50_ms", latency.get("p50_ms")),
        ("latency_p95_ms", latency.get("p95_ms")),
        ("max_latency_ms", latency.get("max_ms")),
        ("estimated_input_tokens", tokens.get("estimated_input_tokens")),
        ("estimated_output_tokens", tokens.get("estimated_output_tokens")),
        (
            "full_context_baseline_estimated_tokens",
            tokens.get("full_context_baseline_estimated_tokens"),
        ),
        ("embedding_call_count", embedding.get("calls")),
        ("embedding_estimated_input_tokens", embedding.get("estimated_input_tokens")),
    ):
        number = _as_number(value)
        if number is not None:
            aliases[metric] = number
    return aliases


def build_dataset_receipt(
    *,
    data_root: Path,
    domain: str,
    tier: str,
    plan: dict[str, Any],
    aggregated_metrics: dict[str, Any],
) -> dict[str, Any]:
    questions_path = data_root / "questions.jsonl"
    trajectories_path = data_root / "trajectories.jsonl"
    haystack = haystack_path(data_root, tier)
    question_count, required_trajectory_count = summarize_dataset_counts(
        data_root=data_root,
        domain=domain,
        tier=tier,
    )
    official_question_ids = [
        question.id
        for question in load_longmemeval_v2_questions(questions_path)
        if domain in ("combined", question.domain)
    ]
    recorded_question_count = plan.get("question_count")
    if recorded_question_count is None:
        recorded_question_count = _number_from(
            aggregated_metrics,
            ("overall", "count_all_questions"),
        )
    recorded_required_trajectory_count = plan.get("required_trajectory_count")
    dataset_record = {
        "name": "longmemeval-v2",
        "data_root": str(data_root),
        "tier": tier,
        "questions_sha256": sha256_file(questions_path),
        "trajectories_sha256": sha256_file(trajectories_path),
        "haystack_sha256": sha256_file(haystack),
        "question_count": _coerce_integral_number(recorded_question_count),
        "selected_question_ids_sha256": plan.get("selected_question_ids_sha256"),
        "official_question_count": len(official_question_ids),
        "official_question_ids_sha256": sha256_question_ids(official_question_ids),
        "selection_complete": (
            plan.get("question_count") == len(official_question_ids)
            and plan.get("selected_question_ids_sha256")
            == sha256_question_ids(official_question_ids)
        ),
        "required_trajectory_count": _coerce_integral_number(recorded_required_trajectory_count),
    }
    if dataset_record["question_count"] is None:
        dataset_record["question_count"] = question_count
    if dataset_record["required_trajectory_count"] is None:
        dataset_record["required_trajectory_count"] = required_trajectory_count
    return dataset_record


def summarize_dataset_counts(
    *, data_root: Path, domain: str, tier: str
) -> tuple[int | None, int | None]:
    try:
        questions = load_longmemeval_v2_questions(data_root / "questions.jsonl")
        haystack = load_longmemeval_v2_haystack(haystack_path(data_root, tier))
    except (OSError, ValueError, RuntimeError):
        return None, None
    selected_ids = {
        question.id for question in questions if domain in ("combined", question.domain)
    }
    required_trajectories = {
        trajectory_id
        for question_id in selected_ids
        for trajectory_id in haystack.get(question_id, [])
    }
    return len(selected_ids), len(required_trajectories)


def build_artifact_receipt(
    *,
    output_dir: Path,
    plan_path: Path,
    aggregated_path: Path,
    per_question_path: Path,
    run_args_path: Path,
    metric_overview_path: Path,
    combined_metrics_path: Path | None,
    submission_overview_path: Path | None,
    submission_archive_path: Path | None,
) -> dict[str, Any]:
    paths = {
        "output_dir": output_dir,
        "plan": plan_path,
        "runtime_questions": output_dir / "runtime_inputs" / "questions.json",
        "runtime_haystack": output_dir / "runtime_inputs" / "haystack.json",
        "memory_config": output_dir / "runtime_inputs" / "memory_config.json",
        "reader_provider_usage": output_dir / "provider_usage" / "reader.jsonl",
        "judge_provider_usage": output_dir / "provider_usage" / "judge.jsonl",
        "aggregated_metrics": aggregated_path,
        "per_question": per_question_path,
        "rig_rows": output_dir / "rig_rows.jsonl",
        "run_args": run_args_path,
        "metric_overview": metric_overview_path,
        "combined_metrics": combined_metrics_path,
        "submission_overview": submission_overview_path,
        "submission_archive": submission_archive_path,
    }
    return {key: artifact_path_record(path) for key, path in paths.items() if path is not None}


def build_receipt_checks(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        check(
            "official harness",
            _truthy_path(receipt, ("official_repo", "commit"))
            and _truthy_path(receipt, ("official_repo", "harness_exists"))
            and receipt.get("official_repo", {}).get("pin_matches") is True
            and receipt.get("official_repo", {}).get("git_status") == "clean",
            "official repo is the clean reviewed pin with evaluation/harness.py present",
            ["official harness"],
        ),
        check(
            "dataset hashes",
            all(
                _truthy_path(receipt, ("dataset", field))
                for field in ("questions_sha256", "trajectories_sha256", "haystack_sha256")
            )
            and (
                receipt.get("domain") == "combined"
                or receipt.get("dataset", {}).get("selection_complete") is True
            ),
            "dataset digests are recorded and domain selection is complete",
            ["dataset hashes"],
        ),
        check(
            "model pins",
            _contains_fragment(receipt["models"].get("reader_model"), QWEN_READER_MODEL_FRAGMENT)
            and _contains_fragment(
                receipt["models"].get("evaluator_model"), GPT_EVALUATOR_MODEL_FRAGMENT
            ),
            "reader is Qwen3.5-9B and evaluator is gpt-5.2",
            ["model pins"],
        ),
        check(
            "source runs",
            receipt.get("source_runs", {}).get("complete") is True
            and receipt.get("source_runs", {}).get("integrity_complete") is True
            and receipt.get("source_runs", {}).get("model_consistent") is True,
            "source artifacts, runtime inputs, and provider logs are present",
            ["source runs"],
        ),
        check(
            "runtime provenance",
            _runtime_provenance_complete(receipt.get("runner_provenance"))
            and receipt.get("source_runs", {}).get("api_runtime_consistent") is True,
            "runner and serving API commits are recorded consistently",
            ["runtime provenance"],
        ),
        check(
            "leaderboard metrics",
            all(
                _is_finite_number(receipt["metrics"].get(field))
                for field in ("lafs_gain", "overall_full_set", "memory_query_avg_seconds")
            ),
            "LAFS gain, accuracy, and memory-query latency are finite",
            ["leaderboard metrics"],
        ),
        check(
            "accounting",
            _receipt_accounting_complete(receipt.get("accounting")),
            "latency, token, embedding, planner, reader, judge, and cost sections are recorded",
            ["accounting"],
        ),
        check(
            "approval boundary",
            receipt.get("approval_boundary", {}).get("official_full_run_requires_approval") is True,
            "official full runs remain approval-bound",
            ["approval boundary"],
        ),
    ]


def check(name: str, passed: bool, detail: str, surfaces: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "status": "PASS" if passed else "FAIL",
        "detail": detail,
        "surfaces": surfaces,
    }


def _receipt_accounting_complete(accounting: Any) -> bool:
    if not isinstance(accounting, dict):
        return False
    required_sections = (
        "latency",
        "tokens",
        "embedding",
        "planner",
        "reader",
        "judge",
        "cost",
    )
    if not all(isinstance(accounting.get(section), dict) for section in required_sections):
        return False
    latency = accounting["latency"]
    tokens = accounting["tokens"]
    cost = accounting["cost"]
    return (
        _is_finite_number(latency.get("p50_ms"))
        and _is_finite_number(latency.get("p95_ms"))
        and _is_finite_number(latency.get("max_ms"))
        and _is_finite_number(tokens.get("estimated_input_tokens"))
        and _is_finite_number(tokens.get("estimated_output_tokens"))
        and _is_finite_number(tokens.get("full_context_baseline_estimated_tokens"))
        and _is_finite_number(cost.get("estimated_total_usd"))
    )


def artifact_path_record(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "sha256": sha256_file(path) if path.is_file() else None,
        "size_bytes": path.stat().st_size if path.is_file() else None,
    }


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def sha256_question_ids(question_ids: Sequence[str]) -> str:
    encoded = json.dumps(sorted(question_ids), separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def git_commit(path: Path | None) -> str | None:
    if path is None:
        return None
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603
            [git, "-C", str(path), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _optional_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(value).expanduser().resolve()


def _load_json_if_exists(path: Path | None) -> dict[str, Any]:
    if path is None or not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _load_jsonl_if_exists(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded = json.loads(line)
        if isinstance(loaded, dict):
            rows.append(loaded)
    return rows


def _load_usage_log(
    path: Path,
    *,
    role: str,
    expected_run_id: str | None = None,
    filter_to_expected_run: bool = False,
) -> dict[str, Any]:
    if not path.is_file():
        return {
            "events": [],
            "invalid_lines": 0,
            "run_ids": [],
            "foreign_event_count": 0,
        }
    valid_events: list[dict[str, Any]] = []
    invalid_lines = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if not isinstance(loaded, dict):
            invalid_lines += 1
            continue
        if loaded.get("role") != role or not isinstance(loaded.get("usage"), dict):
            invalid_lines += 1
            continue
        valid_events.append(loaded)
    events = [
        event
        for event in valid_events
        if not filter_to_expected_run
        or (expected_run_id is not None and event.get("run_id") == expected_run_id)
    ]
    return {
        "events": events,
        "invalid_lines": invalid_lines,
        "run_ids": sorted(
            {
                run_id
                for event in valid_events
                if isinstance(run_id := event.get("run_id"), str) and run_id
            }
        ),
        "foreign_event_count": len(valid_events) - len(events),
    }


def _sanitize_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_config(item)
            for key, item in value.items()
            if str(key).lower() not in _SENSITIVE_CONFIG_KEYS
        }
    if isinstance(value, list):
        return [_sanitize_config(item) for item in value]
    return value


def _source_api_runtime(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    runtimes = [
        runtime
        for row in rows
        if isinstance(
            runtime := _nested_value(
                row,
                "memory_post_query_metadata",
                "api_runtime",
            ),
            dict,
        )
        and runtime
    ]
    if not runtimes:
        return {}, False
    serialized = {json.dumps(runtime, sort_keys=True) for runtime in runtimes}
    return dict(runtimes[0]), len(serialized) == 1 and len(runtimes) == len(rows)


def _runtime_provenance_complete(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    direct_commit = value.get("sibyl_commit")
    if isinstance(direct_commit, str):
        return bool(direct_commit.strip()) and direct_commit != "unknown"
    version = value.get("version")
    runtime = value.get("runtime")
    commit = runtime.get("commit") if isinstance(runtime, dict) else None
    return (
        isinstance(version, str)
        and bool(version.strip())
        and isinstance(commit, str)
        and bool(commit.strip())
        and commit != "unknown"
    )


def _nested_value(mapping: Any, *path: str) -> Any:
    current = mapping
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _first_string(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _coerce_integral_number(value: Any) -> Any:
    number = _as_number(value)
    if number is not None and number.is_integer():
        return int(number)
    return value


def _is_finite_number(value: Any) -> bool:
    return _as_number(value) is not None


def _number_from(mapping: dict[str, Any], *paths: tuple[str, ...]) -> float | None:
    for path in paths:
        current: Any = mapping
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        number = _as_number(current)
        if number is not None:
            return number
    return None


def _truthy_path(mapping: dict[str, Any], path: tuple[str, ...]) -> bool:
    current: Any = mapping
    for key in path:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
    return current not in (None, "", False)


def _contains_fragment(value: Any, fragment: str) -> bool:
    return fragment in str(value or "").lower()


def _extract_case_latency_seconds(row: dict[str, Any]) -> float | None:
    seconds_paths = (
        ("memory_query_duration_seconds",),
        ("memory_query_seconds",),
        ("memory_query_avg_seconds",),
        ("memory_query_latency_seconds",),
        ("timing", "memory_query_seconds"),
        ("timings", "memory_query_seconds"),
        ("memory_query", "seconds"),
        ("memory_query", "avg_seconds"),
        ("memory_query", "total_seconds"),
        ("memory_query", "elapsed_seconds"),
        ("memory_query", "duration_seconds"),
    )
    milliseconds_paths = (
        ("memory_query_ms",),
        ("memory_query_latency_ms",),
        ("timing", "memory_query_ms"),
        ("timings", "memory_query_ms"),
        ("memory_query", "ms"),
        ("memory_query", "latency_ms"),
    )
    seconds = _number_from(row, *seconds_paths)
    if seconds is not None:
        return seconds
    milliseconds = _number_from(row, *milliseconds_paths)
    if milliseconds is not None:
        return milliseconds / 1000.0
    return None


def percentile(values: list[float], percentile_value: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * (percentile_value / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def seconds_to_ms(seconds: float | None) -> float:
    return float(seconds or 0.0) * 1000.0


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
