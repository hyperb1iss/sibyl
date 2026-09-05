"""LongMemEval-S reader/judge QA helpers for live API artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

import tiktoken
from pydantic import BaseModel, Field, SecretStr
from pydantic_ai import Agent

from sibyl_core.ai.llm.config import LLMConfig, LLMProviderName
from sibyl_core.ai.providers import build_model, resolve_provider_model_id
from sibyl_core.config import settings
from sibyl_core.evals.longmemeval import LongMemEvalCorpusDocument, build_longmemeval_corpus

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
DEFAULT_QA_CONTEXT_ARM = "historical-prefix-v1"
DEFAULT_QA_CONTEXT_TOKENS = 6000
QA_TOKENIZER = "o200k_base"
LongMemEvalContextArm = Literal[
    "historical-prefix-v1",
    "dated-prefix-v1",
    "query-passages-v1",
    "full-sessions-v1",
    "native-context-v1",
]
LONGMEMEVAL_CONTEXT_ARMS = (
    "historical-prefix-v1",
    "dated-prefix-v1",
    "query-passages-v1",
    "full-sessions-v1",
    "native-context-v1",
)
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
    context_arm: LongMemEvalContextArm = DEFAULT_QA_CONTEXT_ARM
    max_context_tokens: int = DEFAULT_QA_CONTEXT_TOKENS

    def __post_init__(self) -> None:
        if self.context_arm not in LONGMEMEVAL_CONTEXT_ARMS:
            raise ValueError(f"Unsupported QA context arm: {self.context_arm}")
        if min(self.max_context_sessions, self.max_session_chars, self.max_context_tokens) <= 0:
            raise ValueError("QA context limits must be positive")


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
        "context_arm": config.context_arm,
        "context_tokenizer": QA_TOKENIZER,
        "max_context_tokens": (
            None if config.context_arm == "historical-prefix-v1" else config.max_context_tokens
        ),
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
    native_markdown: str | None = None,
) -> dict[str, Any]:
    if config.mode == "disabled":
        return _disabled_result(config)

    started = time.perf_counter()
    if config.context_arm == "historical-prefix-v1":
        context_sessions = _context_sessions(
            entry,
            ranked_session_ids=ranked_session_ids,
            corpus_text_policy=corpus_text_policy,
            max_sessions=config.max_context_sessions,
            max_session_chars=config.max_session_chars,
        )
        reader_prompt = _reader_prompt(entry, context_sessions)
        context_text = _session_context(context_sessions)
        spans: list[dict[str, Any]] = []
    else:
        # The selector receives only source documents and the query, never answer labels.
        context_sessions, context_text, spans = render_qa_context(
            question=str(entry.get("question") or ""),
            documents=(
                []
                if config.context_arm == "native-context-v1"
                else build_longmemeval_corpus(entry, text_policy=corpus_text_policy)
            ),
            ranked_session_ids=ranked_session_ids,
            config=config,
            native_markdown=native_markdown,
        )
        reader_prompt = _question_prompt(entry, context_text)
    reference_answer = _reference_answer(entry, corpus_text_policy=corpus_text_policy)
    answer_session_ids = [str(value) for value in entry.get("answer_session_ids", [])]

    if config.mode == "fixture":
        result = _fixture_result(
            entry,
            config=config,
            context_sessions=context_sessions,
            reference_answer=reference_answer,
            answer_session_ids=answer_session_ids,
            reader_prompt=reader_prompt,
        )
    else:
        result = await _model_result(
            entry,
            config=config,
            context_sessions=context_sessions,
            reference_answer=reference_answer,
            answer_session_ids=answer_session_ids,
            reader_prompt=reader_prompt,
        )

    result["context_receipt"] = {
        "arm": config.context_arm,
        "tokenizer": QA_TOKENIZER,
        "context_tokens": count_context_tokens(context_text),
        "reader_prompt_tokens": count_context_tokens(reader_prompt),
        "reader_system_tokens": count_context_tokens(READER_SYSTEM_PROMPT),
        "context_sha256": hashlib.sha256(context_text.encode()).hexdigest(),
        "reader_prompt_sha256": hashlib.sha256(reader_prompt.encode()).hexdigest(),
        "rendered_context": context_text,
        "reader_prompt": reader_prompt,
        "spans": spans,
    }
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
    reader_prompt: str,
) -> dict[str, Any]:
    context_ids = {session["session_id"] for session in context_sessions}
    expected_ids = set(answer_session_ids)
    answerable = bool(expected_ids) and expected_ids.issubset(context_ids)
    generated_answer = reference_answer if answerable else "Insufficient retrieved evidence."
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
    reader_prompt: str,
) -> dict[str, Any]:
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
    judgment = judge_response.output
    reader_model = resolve_provider_model_id(reader_config)
    judge_model = resolve_provider_model_id(judge_config)

    return {
        **qa_report_metadata(replace(config, reader_model=reader_model, judge_model=judge_model)),
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


def count_context_tokens(text: str) -> int:
    """Count text with the frozen comparison tokenizer, excluding provider framing."""
    return len(tiktoken.get_encoding(QA_TOKENIZER).encode(text, disallowed_special=()))


def _passage_spans(text: str) -> list[tuple[int, int]]:
    """Keep source sentences and lines whole, with offsets into unmodified text."""
    return [
        (match.start(), match.end())
        for match in re.finditer(r".+?(?:\n|(?<=[.!?])\s+|$)", text, re.DOTALL)
    ]


def _passage_speaker(text: str, start: int) -> str:
    roles = re.finditer(r"^(User|Assistant):", text, re.MULTILINE)
    preceding = [match for match in roles if match.start() <= start]
    return preceding[-1].group(1) if preceding else "not provided"


def render_qa_context(
    *,
    question: str,
    documents: list[LongMemEvalCorpusDocument],
    ranked_session_ids: list[str],
    config: LongMemEvalQAConfig,
    native_markdown: str | None = None,
) -> tuple[list[dict[str, str]], str, list[dict[str, Any]]]:
    """Render source-only controls under one explicit text-token ceiling.

    Full sessions and native output fail on overflow; changing their content would
    change the named control. Passage controls admit complete source spans only.
    """
    if config.context_arm == "native-context-v1":
        if not isinstance(native_markdown, str) or not native_markdown.strip():
            raise ValueError("Native QA requires nonempty compiled context markdown")
        if count_context_tokens(native_markdown) > config.max_context_tokens:
            raise ValueError("Native compiled context exceeds QA token ceiling")
        return [], native_markdown, []
    corpus = {document.session_id: document for document in documents}
    candidates: list[tuple[int, LongMemEvalCorpusDocument, int, int]] = []
    for rank, session_id in enumerate(ranked_session_ids[: config.max_context_sessions], 1):
        document = corpus.get(session_id)
        if document is None:
            raise ValueError(f"Selected session missing from corpus: {session_id}")
        boundaries = (
            [(0, len(document.text))]
            if config.context_arm == "full-sessions-v1"
            else _passage_spans(document.text)
        )
        candidates.extend((rank, document, start, end) for start, end in boundaries)
    if config.context_arm == "query-passages-v1":
        terms = set(re.findall(r"\w{3,}", question.casefold()))
        candidates.sort(
            key=lambda item: (
                -len(
                    terms & set(re.findall(r"\w{3,}", item[1].text[item[2] : item[3]].casefold()))
                ),
                item[0],
                item[2],
            )
        )
    sessions: list[dict[str, str]] = []
    chunks: list[str] = []
    spans: list[dict[str, Any]] = []
    for rank, document, start, end in candidates:
        speaker = (
            "see turn labels"
            if config.context_arm == "full-sessions-v1"
            else _passage_speaker(document.text, start)
        )
        chunk = (
            f"Rank {rank} session {document.session_id} | Date: {document.timestamp or 'not provided'}"
            f" | Speaker: {speaker} | Characters: {start}:{end}\n{document.text[start:end]}"
        )
        candidate = "\n\n".join([*chunks, chunk])
        if count_context_tokens(candidate) > config.max_context_tokens:
            if config.context_arm == "full-sessions-v1":
                raise ValueError(
                    "Full selected sessions exceed QA token ceiling; price a larger control"
                )
            if config.context_arm == "dated-prefix-v1":
                break
            continue
        chunks.append(chunk)
        sessions.append(
            {"rank": str(rank), "session_id": document.session_id, "text": document.text[start:end]}
        )
        spans.append(
            {
                "session_id": document.session_id,
                "start": start,
                "end": end,
                "source_sha256": hashlib.sha256(document.text.encode()).hexdigest(),
                "timestamp": document.timestamp,
                "speaker": speaker,
            }
        )
    return sessions, "\n\n".join(chunks), spans


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
    return _question_prompt(entry, _session_context(context_sessions))


def _session_context(context_sessions: list[dict[str, str]]) -> str:
    return "\n\n".join(
        f"Rank {session['rank']} session {session['session_id']}:\n{session['text']}"
        for session in context_sessions
    )


def _question_prompt(entry: Mapping[str, Any], context: str) -> str:
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
