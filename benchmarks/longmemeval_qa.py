"""LongMemEval-S reader/judge QA helpers for live API artifacts."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import BaseModel, Field, SecretStr
from pydantic_ai import Agent

from sibyl_core.ai.llm.config import LLMConfig, LLMProviderName
from sibyl_core.ai.providers import build_model, resolve_provider_model_id
from sibyl_core.config import settings
from sibyl_core.evals.longmemeval import build_longmemeval_corpus

QA_SCHEMA_VERSION = "sibyl-longmemeval-s-qa-v1"
QA_READER_PROMPT_ID = "sibyl-longmemeval-reader-v1"
QA_JUDGE_PROMPT_ID = "sibyl-longmemeval-judge-v1"
QA_RUBRIC_ID = "longmemeval-s-answer-correctness-v1"
DEFAULT_QA_MODE = "disabled"
DEFAULT_QA_READER_PROVIDER: LLMProviderName = "openai"
DEFAULT_QA_JUDGE_PROVIDER: LLMProviderName = "openai"
DEFAULT_QA_READER_MODEL = "gpt-4o"
DEFAULT_QA_JUDGE_MODEL = "gpt-5.2"
DEFAULT_QA_MAX_CONTEXT_SESSIONS = 5
DEFAULT_QA_MAX_SESSION_CHARS = 4000
DEFAULT_QA_TIMEOUT_SECONDS = 120.0
APPROX_CHARS_PER_TOKEN = 4.0
APPROX_TOKEN_SAFETY_MARGIN = 1.2

LongMemEvalQAMode = Literal["disabled", "fixture", "model"]
LONGMEMEVAL_QA_MODES = ("disabled", "fixture", "model")

_PROVIDER_ENV_KEYS: dict[LLMProviderName, tuple[str, ...]] = {
    "anthropic": ("SIBYL_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    "gemini": ("SIBYL_GEMINI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("SIBYL_OPENAI_API_KEY", "OPENAI_API_KEY"),
}

READER_SYSTEM_PROMPT = """
Answer the LongMemEval question using only the retrieved session excerpts.
If the excerpts do not contain enough evidence, say that the retrieved evidence
is insufficient. Keep the answer concise and do not invent facts.
""".strip()

JUDGE_SYSTEM_PROMPT = """
Judge whether the candidate answer correctly answers the LongMemEval question.
Use the reference answer and reference session evidence as ground truth. Mark
correct only when the answer is semantically equivalent and does not add
unsupported facts.
""".strip()


class LongMemEvalQAJudgment(BaseModel):
    correct: bool = Field(description="Whether the candidate answer is correct.")
    score: float = Field(ge=0.0, le=1.0, description="Answer correctness score from 0 to 1.")
    rationale: str = Field(description="Short evidence-grounded rationale.")


@dataclass(frozen=True)
class LongMemEvalQAConfig:
    mode: LongMemEvalQAMode = DEFAULT_QA_MODE
    reader_provider: LLMProviderName = DEFAULT_QA_READER_PROVIDER
    reader_model: str = DEFAULT_QA_READER_MODEL
    judge_provider: LLMProviderName = DEFAULT_QA_JUDGE_PROVIDER
    judge_model: str = DEFAULT_QA_JUDGE_MODEL
    max_context_sessions: int = DEFAULT_QA_MAX_CONTEXT_SESSIONS
    max_session_chars: int = DEFAULT_QA_MAX_SESSION_CHARS
    timeout_seconds: float = DEFAULT_QA_TIMEOUT_SECONDS


def qa_report_metadata(config: LongMemEvalQAConfig) -> dict[str, Any]:
    return {
        "schema_version": QA_SCHEMA_VERSION,
        "mode": config.mode,
        "enabled": config.mode != "disabled",
        "reader_provider": config.reader_provider,
        "reader_model": config.reader_model if config.mode != "disabled" else "not-applicable",
        "reader_prompt_id": QA_READER_PROMPT_ID if config.mode != "disabled" else "not-applicable",
        "judge_provider": config.judge_provider,
        "judge_model": config.judge_model if config.mode != "disabled" else "not-applicable",
        "judge_prompt_id": QA_JUDGE_PROMPT_ID if config.mode != "disabled" else "not-applicable",
        "rubric_id": QA_RUBRIC_ID if config.mode != "disabled" else "not-applicable",
        "max_context_sessions": config.max_context_sessions,
        "max_session_chars": config.max_session_chars,
        "timeout_seconds": config.timeout_seconds,
        "claim_boundary": _claim_boundary(config),
    }


async def evaluate_longmemeval_case_qa(
    entry: Mapping[str, Any],
    *,
    ranked_session_ids: list[str],
    corpus_text_policy: str,
    config: LongMemEvalQAConfig,
) -> dict[str, Any]:
    if config.mode == "disabled":
        return _disabled_result(config)

    started = time.perf_counter()
    context_sessions = _context_sessions(
        entry,
        ranked_session_ids=ranked_session_ids,
        corpus_text_policy=corpus_text_policy,
        max_sessions=config.max_context_sessions,
        max_session_chars=config.max_session_chars,
    )
    reference_answer = _reference_answer(entry, corpus_text_policy=corpus_text_policy)
    answer_session_ids = [str(value) for value in entry.get("answer_session_ids", [])]

    if config.mode == "fixture":
        result = _fixture_result(
            entry,
            config=config,
            context_sessions=context_sessions,
            reference_answer=reference_answer,
            answer_session_ids=answer_session_ids,
        )
    else:
        result = await _model_result(
            entry,
            config=config,
            context_sessions=context_sessions,
            reference_answer=reference_answer,
            answer_session_ids=answer_session_ids,
        )

    result["latency_ms"] = (time.perf_counter() - started) * 1000
    return result


def _disabled_result(config: LongMemEvalQAConfig) -> dict[str, Any]:
    return {
        **qa_report_metadata(config),
        "evaluated": False,
        "correct": None,
        "score": None,
        "generated_answer": "",
        "reference_answer": "",
        "context_session_ids": [],
        "answer_session_ids": [],
        "judge_rationale": "",
        "latency_ms": 0.0,
        "reader_estimated_input_tokens": 0.0,
        "reader_estimated_output_tokens": 0.0,
        "judge_estimated_input_tokens": 0.0,
        "judge_estimated_output_tokens": 0.0,
    }


def _fixture_result(
    entry: Mapping[str, Any],
    *,
    config: LongMemEvalQAConfig,
    context_sessions: list[dict[str, str]],
    reference_answer: str,
    answer_session_ids: list[str],
) -> dict[str, Any]:
    context_ids = {session["session_id"] for session in context_sessions}
    expected_ids = set(answer_session_ids)
    answerable = bool(expected_ids) and expected_ids.issubset(context_ids)
    generated_answer = reference_answer if answerable else "Insufficient retrieved evidence."
    reader_prompt = _reader_prompt(entry, context_sessions)
    judge_prompt = _judge_prompt(
        entry,
        generated_answer=generated_answer,
        reference_answer=reference_answer,
        answer_session_ids=answer_session_ids,
    )
    return {
        **qa_report_metadata(config),
        "evaluated": True,
        "correct": answerable,
        "score": 1.0 if answerable else 0.0,
        "generated_answer": generated_answer,
        "reference_answer": reference_answer,
        "context_session_ids": [session["session_id"] for session in context_sessions],
        "answer_session_ids": answer_session_ids,
        "judge_rationale": (
            "All reference answer sessions are present in the retrieved QA context."
            if answerable
            else "At least one reference answer session is missing from the retrieved QA context."
        ),
        "reader_estimated_input_tokens": _estimate_tokens(reader_prompt),
        "reader_estimated_output_tokens": _estimate_tokens(generated_answer),
        "judge_estimated_input_tokens": _estimate_tokens(judge_prompt),
        "judge_estimated_output_tokens": _estimate_tokens("correct" if answerable else "incorrect"),
    }


async def _model_result(
    entry: Mapping[str, Any],
    *,
    config: LongMemEvalQAConfig,
    context_sessions: list[dict[str, str]],
    reference_answer: str,
    answer_session_ids: list[str],
) -> dict[str, Any]:
    reader_prompt = _reader_prompt(entry, context_sessions)
    reader_config = _llm_config(
        provider=config.reader_provider,
        model=config.reader_model,
        config=config,
    )
    reader_agent = Agent(
        build_model(reader_config),
        output_type=str,
        instructions=READER_SYSTEM_PROMPT,
    )
    reader_response = await reader_agent.run(reader_prompt)
    generated_answer = str(reader_response.output).strip()

    judge_prompt = _judge_prompt(
        entry,
        generated_answer=generated_answer,
        reference_answer=reference_answer,
        answer_session_ids=answer_session_ids,
    )
    judge_config = _llm_config(
        provider=config.judge_provider,
        model=config.judge_model,
        config=config,
    )
    judge_agent = Agent(
        build_model(judge_config),
        output_type=LongMemEvalQAJudgment,
        instructions=JUDGE_SYSTEM_PROMPT,
    )
    judge_response = await judge_agent.run(judge_prompt)
    judgment = cast(LongMemEvalQAJudgment, judge_response.output)
    reader_model = resolve_provider_model_id(reader_config)
    judge_model = resolve_provider_model_id(judge_config)

    return {
        **qa_report_metadata(
            LongMemEvalQAConfig(
                mode=config.mode,
                reader_provider=config.reader_provider,
                reader_model=reader_model,
                judge_provider=config.judge_provider,
                judge_model=judge_model,
                max_context_sessions=config.max_context_sessions,
                max_session_chars=config.max_session_chars,
                timeout_seconds=config.timeout_seconds,
            )
        ),
        "evaluated": True,
        "correct": judgment.correct,
        "score": float(judgment.score),
        "generated_answer": generated_answer,
        "reference_answer": reference_answer,
        "context_session_ids": [session["session_id"] for session in context_sessions],
        "answer_session_ids": answer_session_ids,
        "judge_rationale": judgment.rationale,
        "reader_estimated_input_tokens": _estimate_tokens(reader_prompt),
        "reader_estimated_output_tokens": _estimate_tokens(generated_answer),
        "judge_estimated_input_tokens": _estimate_tokens(judge_prompt),
        "judge_estimated_output_tokens": _estimate_tokens(judgment.model_dump_json()),
    }


def _llm_config(
    *,
    provider: LLMProviderName,
    model: str,
    config: LongMemEvalQAConfig,
) -> LLMConfig:
    api_key = _api_key(provider)
    if not api_key:
        msg = f"Missing API key for LongMemEval QA provider {provider!r}"
        raise RuntimeError(msg)
    return LLMConfig(
        provider=provider,
        model=model,
        temperature=0.0,
        timeout_seconds=config.timeout_seconds,
        api_key=SecretStr(api_key),
    )


def _api_key(provider: LLMProviderName) -> str:
    for name in _PROVIDER_ENV_KEYS[provider]:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    if provider == "openai":
        return settings.openai_api_key.get_secret_value()
    if provider == "anthropic":
        return settings.anthropic_api_key.get_secret_value()
    if provider == "gemini":
        return settings.gemini_api_key.get_secret_value()
    return ""


def _context_sessions(
    entry: Mapping[str, Any],
    *,
    ranked_session_ids: list[str],
    corpus_text_policy: str,
    max_sessions: int,
    max_session_chars: int,
) -> list[dict[str, str]]:
    corpus = {
        document.session_id: document.text
        for document in build_longmemeval_corpus(entry, text_policy=corpus_text_policy)
    }
    sessions: list[dict[str, str]] = []
    for rank, session_id in enumerate(ranked_session_ids[: max(1, max_sessions)], start=1):
        text = corpus.get(session_id)
        if not text:
            continue
        sessions.append(
            {
                "rank": str(rank),
                "session_id": session_id,
                "text": _truncate(text, max_session_chars),
            }
        )
    return sessions


def _reference_answer(entry: Mapping[str, Any], *, corpus_text_policy: str) -> str:
    for key in ("answer", "expected_answer", "gold_answer", "reference_answer"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            parts = [str(item).strip() for item in value if str(item).strip()]
            if parts:
                return "\n".join(parts)

    answer_ids = {str(value) for value in entry.get("answer_session_ids", [])}
    documents = build_longmemeval_corpus(entry, text_policy=corpus_text_policy)
    reference_parts = [
        f"[{document.session_id}] {document.text}"
        for document in documents
        if document.session_id in answer_ids
    ]
    return "\n\n".join(reference_parts)


def _reader_prompt(entry: Mapping[str, Any], context_sessions: list[dict[str, str]]) -> str:
    context = "\n\n".join(
        f"Rank {session['rank']} session {session['session_id']}:\n{session['text']}"
        for session in context_sessions
    )
    return (
        f"Question date: {entry.get('question_date') or 'not provided'}\n"
        f"Question: {entry.get('question')}\n\n"
        f"Retrieved sessions:\n{context or '[none]'}\n\n"
        "Answer:"
    )


def _judge_prompt(
    entry: Mapping[str, Any],
    *,
    generated_answer: str,
    reference_answer: str,
    answer_session_ids: list[str],
) -> str:
    return (
        f"Question: {entry.get('question')}\n"
        f"Question type: {entry.get('question_type')}\n"
        f"Reference answer session IDs: {', '.join(answer_session_ids)}\n\n"
        f"Reference answer/evidence:\n{reference_answer or '[missing]'}\n\n"
        f"Candidate answer:\n{generated_answer}\n\n"
        "Return whether the candidate answer is correct under the rubric."
    )


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[: max(0, max_chars - 15)].rstrip()} [truncated]"


def _estimate_tokens(text: str) -> float:
    if not text:
        return 0.0
    return float(int((len(text) / APPROX_CHARS_PER_TOKEN) * APPROX_TOKEN_SAFETY_MARGIN + 0.9999))


def _claim_boundary(config: LongMemEvalQAConfig) -> str:
    if config.mode == "fixture":
        return (
            "Deterministic fixture QA validates artifact and gate wiring only; "
            "it is not a publishable LongMemEval-S QA-accuracy score."
        )
    if config.mode == "model":
        return (
            "Reader/judge QA over retrieved LongMemEval-S sessions. Publishable "
            "only when generated with pinned dataset, prompts, reader model, "
            "judge model, and committed accounting receipt."
        )
    return "QA disabled; artifact measures retrieval only."
