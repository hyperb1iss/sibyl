# Sibyl Native LLM Substrate Plan

Status: draft, awaiting cross-model review. Author: Nova (Claude Opus 4.7) Date: 2026-05-15 Target
milestone: v0.10 Roadmap: [`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md)

1.0 planning note: this substrate is required for automatic reflection, synthesis, and memory
review. It should also replace remaining Graphiti-era extraction/model-provider seams as Graphiti is
deleted from the supported runtime.

## 1. Goal

Stand up a single native PydanticAI-based LLM substrate inside `sibyl-core` that owns every
non-Graphiti LLM call site in Sibyl. The substrate offers structured extraction and streamed
generation across Anthropic, Google Gemini, and OpenAI, with per-call-site model overrides, runtime
key validation, and a settings UI that lets an operator pick the right model per workload.

This is the surface that will outlive the Graphiti exit. When `graph/client.py` and the
`compat/ops/*` adapters finally come out (v0.7/v0.8 burndown owners), this substrate is what handles
all remaining extraction.

## 2. Success criteria

- A `sibyl_core.llm` package exposing `Extractor[T]` and `Generator` with a single config knob per
  surface (`SIBYL_<SURFACE>_LLM_*`) plus a global default.
- First-class support for Anthropic Haiku 4.5, Gemini 3 Flash, and GPT-5.4 mini, with a model
  registry that ships recommended pins per use case.
- Crawler entity extraction, reflection candidate extraction, the synthesis text generator, and the
  prompt-submit hook all run through the substrate. No direct `anthropic.AsyncAnthropic`,
  `openai.AsyncOpenAI`, or `google.genai` instantiation outside the substrate package.
- Settings API exposes provider, model, and key configuration per surface; key validation hits each
  provider with a current, non-retired model.
- `apps/web/src/app/(main)/settings/admin/ai/page.tsx` renders an LLM section alongside the existing
  embeddings UI, with per-surface model selection, provider-key status indicators, and a "test this
  configuration" affordance.
- `moon run :check` is green across core, api, cli, and web.
- A small smoke harness (≤20 chunks per call site) shows no regression in extracted entity count or
  field coverage versus the current Haiku 4.5 baseline.

## 3. Non-goals

- Replacing or refactoring any code under `packages/python/sibyl-core/src/sibyl_core/graph/`. The
  Graphiti `llm_provider`/`llm_model` config in `core/config.py` and `apps/api/src/sibyl/config.py`
  remains untouched. It dies on its own schedule per the Graphiti exit inventory.
- Replacing the embedding pipeline. Embedding provider config and the `gemini_embedder.py` adapter
  stay as-is.
- Building a generic agent system. Tool use lands when a real consumer asks for it; until then the
  `Generator` exposes streamed text only.
- Adding LiteLLM, OpenRouter, or other proxy layers. PydanticAI supports them natively if we ever
  want them.

## 4. Current state inventory

LLM call sites that bypass the Graphiti config and use SDKs directly:

| Surface         | File                                                         | Behavior                                                | Model                         |
| --------------- | ------------------------------------------------------------ | ------------------------------------------------------- | ----------------------------- |
| Crawler extract | `apps/api/src/sibyl/crawler/graph_integration.py:189`        | Anthropic SDK, JSON-from-prose parsing, retry-by-string | hardcoded `claude-haiku-4-5`  |
| Reflect         | `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py` | Reflection candidate extraction from raw notes          | reads `core_config.llm_model` |
| Synthesis       | `apps/api/src/sibyl/generator/llm.py:45`                     | Anthropic SDK direct, blocking client                   | hardcoded                     |
| Prompt hook     | `apps/cli/src/sibyl_cli/data/hooks/user-prompt-submit.py:34` | Generates search query from prompt + context            | `claude-haiku-4-5-20251001`   |

Settings/setup validation:

- `apps/api/src/sibyl/api/routes/setup.py:121` and `apps/api/src/sibyl/api/routes/settings.py:135`
  both probe `claude-3-haiku-20240307` for Anthropic key validation. That model has been retired.

Web UI:

- `apps/web/src/app/(main)/settings/admin/ai/page.tsx` already exists with embedding provider/model
  selection and key entry for OpenAI, Anthropic, and Gemini. We extend it with LLM config sections.

## 5. Target architecture

### 5.1 Package layout

```
packages/python/sibyl-core/src/sibyl_core/llm/
├── __init__.py             # Public surface: Extractor, Generator, LLMConfig, ModelRegistry
├── config.py               # Pydantic settings for global + per-surface model selection
├── models.py               # ModelRegistry: provider × model → metadata, recommendations, aliases
├── providers.py            # Provider factory: builds pydantic_ai.models.Model from config
├── extractor.py            # Extractor[T: BaseModel] — single-shot structured extraction
├── generator.py            # Generator — streamed text generation, optional system prompt
├── validation.py           # Key/model validation helpers (used by settings + setup routes)
├── observability.py        # Token tracker, structured logging, optional OTel span hooks
├── errors.py               # LLMError, LLMConfigError, LLMValidationError, LLMRateLimitError
└── _testing.py             # MockLLM for tests, parity with current mock_llm.py shape
```

### 5.2 Key types

```python
# config.py
class LLMSurface(StrEnum):
    DEFAULT = "default"
    CRAWLER = "crawler"
    REFLECT = "reflect"
    SYNTHESIS = "synthesis"
    PROMPT_HOOK = "prompt_hook"

class LLMConfig(BaseModel):
    provider: Literal["anthropic", "gemini", "openai"]
    model: str
    temperature: float = 0.0
    max_tokens: int | None = None
    api_key: SecretStr  # resolved at call site; never persisted in this model

# extractor.py
class Extractor[T: BaseModel]:
    def __init__(
        self,
        schema: type[T],
        *,
        surface: LLMSurface = LLMSurface.DEFAULT,
        system_prompt: str,
        model_override: str | None = None,
    ) -> None: ...

    async def extract(self, content: str, *, retries: int = 2) -> T: ...
    async def extract_batch(self, chunks: Sequence[str]) -> list[T | LLMError]: ...

# generator.py
class Generator:
    def __init__(
        self,
        *,
        surface: LLMSurface = LLMSurface.SYNTHESIS,
        system_prompt: str | None = None,
        model_override: str | None = None,
    ) -> None: ...

    async def generate(self, prompt: str) -> str: ...
    async def stream(self, prompt: str) -> AsyncIterator[str]: ...
```

### 5.3 Configuration hierarchy

Resolution order, most-specific wins:

1. Explicit `model_override` on the call site (rare; used for tests and one-off scripts).
2. Database setting: `llm.<surface>.model` and `llm.<surface>.provider` written via Settings API.
3. Env override: `SIBYL_LLM_<SURFACE>_MODEL`, `SIBYL_LLM_<SURFACE>_PROVIDER`.
4. Global database setting: `llm.default.model`, `llm.default.provider`.
5. Global env: `SIBYL_LLM_MODEL`, `SIBYL_LLM_PROVIDER`.
6. Compile-time default in `models.py`: `("anthropic", "claude-haiku-4-5")`.

Temperature, max tokens, and timeouts follow the same hierarchy with their own keys.

### 5.4 Model registry

`models.py` ships a curated registry rather than free-text. The registry is the source of truth for
what shows up in the web UI's model dropdowns and for what passes validation.

| Provider  | Alias                    | Snapshot                       | Use case           | $/M in | $/M out |
| --------- | ------------------------ | ------------------------------ | ------------------ | ------ | ------- |
| Anthropic | `claude-haiku-4-5`       | `claude-haiku-4-5-20251001`    | default extraction | 1.00   | 5.00    |
| Anthropic | `claude-sonnet-4-6`      | `claude-sonnet-4-6-2026-04-XX` | hard synthesis     | 3.00   | 15.00   |
| Google    | `gemini-3-flash-preview` | (date-pinned alias)            | cost-optimized     | 0.50   | 3.00    |
| Google    | `gemini-3-1-flash-lite`  | (date-pinned alias)            | bulk crawling      | 0.25   | 1.50    |
| OpenAI    | `gpt-5.4-mini`           | `gpt-5.4-mini-2026-03-17`      | OpenAI parity      | 0.75   | 4.50    |
| OpenAI    | `gpt-5.4-nano`           | `gpt-5.4-nano-2026-03-17`      | budget extraction  | 0.20   | 1.25    |

Registry entries carry: alias, snapshot, provider, structured-output support flag, max output
tokens, default temperature, cost metadata, recommended-for tags (e.g. `extraction`, `synthesis`,
`bulk`), and a stable string ID for the settings layer.

A `SIBYL_LLM_PIN_SNAPSHOTS=true` env flag makes the substrate resolve aliases to the registry
snapshot at startup, giving us reproducibility for benchmarking and CI.

### 5.5 Settings API surface

New endpoints under `/api/settings/llm/*`:

- `GET /api/settings/llm` → returns global + per-surface configuration, with sources (`env`, `db`,
  `default`) and effective resolved values. Mirrors the existing `get_with_source` shape.
- `PUT /api/settings/llm/{surface}` → updates provider/model/temperature for a surface. Validates
  the model is in the registry and the corresponding API key is configured.
- `POST /api/settings/llm/{surface}/test` → runs a minimal extraction against the chosen
  provider+model. Returns latency, token counts, and the parsed output.
- `GET /api/settings/llm/registry` → exposes the model registry to the web UI. Server-rendered so
  the UI never hardcodes model strings.

Key validation endpoints (`/api/settings/validate-anthropic`, `-openai`, `-gemini`) move from their
ad-hoc HTTP probes to calling `sibyl_core.llm.validation.check_provider_key(provider, key)`. The
validation helper uses a current registry model with `max_tokens=1` for cheap probes.

### 5.6 Web UI

Extend `apps/web/src/app/(main)/settings/admin/ai/page.tsx` with a new "Language Models" card above
the existing embeddings card. Layout sketch:

```
┌─ Language Models ───────────────────────────────────────────────────┐
│  Default                                                            │
│  ┌────────────┐  ┌──────────────────────────┐  ┌────────────────┐   │
│  │ Anthropic ▾│  │ claude-haiku-4-5       ▾ │  │ Test ▸         │   │
│  └────────────┘  └──────────────────────────┘  └────────────────┘   │
│  ✓ API key configured · ~80-120 tok/s · $1/$5 per M tokens          │
│                                                                     │
│  Per-surface overrides                            [+ Add override]  │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ Crawler        │ Gemini ▾   │ gemini-3-1-flash-lite      ▾  │    │
│  │ Reflect        │ (default)                                  │    │
│  │ Synthesis      │ Anthropic ▾│ claude-sonnet-4-6          ▾  │    │
│  │ Prompt hook    │ (default)                                  │    │
│  └─────────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────────┘
```

UX details:

- Provider dropdown is keyed off the registry; selecting a provider populates the model dropdown
  with that provider's curated entries.
- Per-row "Test" button calls `POST /api/settings/llm/{surface}/test` and surfaces latency + token
  count inline. Failure shows the error inline, not a toast.
- Each row shows a small badge: `env` (overridden by environment), `db` (set here), or `default`.
  Rows overridden by env are disabled with a tooltip explaining why and how to unset.
- A "recommended" pill next to the curated default for each use case nudges users toward sensible
  picks.
- Inline cost summary: rough cost per 1M tokens, calculated from the registry, surfaces a tooltip
  with the comparison to the global default.
- Save uses optimistic updates via the existing `useUpdateSettings` mutation pattern.

### 5.7 Observability

`observability.py` wraps every call with structured logging (`surface`, `provider`, `model`,
`prompt_name`, `input_tokens`, `output_tokens`, `latency_ms`, `retries`). Counters land on the
existing `sibyl debug status` surface so operators can see model usage per surface.

PydanticAI's instrumentation API (added in v1.95.0) provides span hooks; we wire it to our existing
`structlog` rather than introducing OTel as a hard dep.

## 6. Decomposition

### Wave 1: Foundation (parallel)

#### Task 1: Add pydantic-ai dependency and base package

- **Files:** `packages/python/sibyl-core/pyproject.toml`,
  `packages/python/sibyl-core/src/sibyl_core/llm/__init__.py`,
  `packages/python/sibyl-core/src/sibyl_core/llm/errors.py`
- **Parallel:** Yes (with Task 2)
- **Implementation:**
  - `uv add pydantic-ai` at the workspace root, pinned to `^1.95`.
  - Create empty `llm/` package with `errors.py` defining the error hierarchy.
  - Confirm `ty` typechecks the empty package.
- **Verify:**
  - `moon run core:typecheck` passes.
  - `uv tree | rg pydantic-ai` shows the dep installed.

#### Task 2: Model registry and configuration

- **Files:** `packages/python/sibyl-core/src/sibyl_core/llm/models.py`,
  `packages/python/sibyl-core/src/sibyl_core/llm/config.py`,
  `packages/python/sibyl-core/tests/llm/test_models.py`,
  `packages/python/sibyl-core/tests/llm/test_config.py`
- **Parallel:** Yes (with Task 1)
- **Implementation:**
  - Define `ModelRegistry` with entries for the six models in §5.4.
  - Define `LLMSurface` enum and `LLMConfig` Pydantic model.
  - Implement `resolve_config(surface)` honoring the hierarchy in §5.3.
  - Tests cover: registry lookup by alias and snapshot, surface resolution precedence, env-override
    behavior, missing-key error path.
- **Verify:**
  - `moon run core:test -- tests/llm/` passes.
  - `moon run core:typecheck` passes.
  - `moon run core:lint` passes.

### Wave 2: Provider implementations (sequential after Wave 1)

#### Task 3: Provider factory and Extractor

- **Files:** `packages/python/sibyl-core/src/sibyl_core/llm/providers.py`,
  `packages/python/sibyl-core/src/sibyl_core/llm/extractor.py`,
  `packages/python/sibyl-core/tests/llm/test_extractor.py`
- **Depends on:** Tasks 1, 2
- **Implementation:**
  - `build_model(config)` maps `LLMConfig` → `pydantic_ai.models.Model` for the three providers.
  - `Extractor[T]` wraps `pydantic_ai.Agent` with `output_type=T` and a fixed system prompt.
  - `extract` returns the parsed Pydantic model. `extract_batch` runs with bounded concurrency.
  - Retry policy: PydanticAI's built-in validation retry (2 attempts), then surface
    `LLMValidationError`. Rate-limit errors raise `LLMRateLimitError`.
  - Tests use PydanticAI's `TestModel` to assert behavior without hitting real providers.
- **Verify:**
  - `moon run core:test -- tests/llm/test_extractor.py` passes.
  - Local smoke against Anthropic + Gemini + OpenAI with a 5-chunk fixture (skipped in CI when keys
    are missing).

#### Task 4: Generator and streamed text

- **Files:** `packages/python/sibyl-core/src/sibyl_core/llm/generator.py`,
  `packages/python/sibyl-core/tests/llm/test_generator.py`
- **Depends on:** Task 3
- **Implementation:**
  - `Generator.generate` and `Generator.stream` over the same provider factory.
  - System prompt is optional and threaded through.
  - Streaming surfaces `AsyncIterator[str]` of incremental text tokens.
- **Verify:**
  - `moon run core:test -- tests/llm/test_generator.py` passes.
  - Manual streaming smoke against Anthropic Haiku produces incremental output.

#### Task 5: Validation helper

- **Files:** `packages/python/sibyl-core/src/sibyl_core/llm/validation.py`,
  `packages/python/sibyl-core/tests/llm/test_validation.py`
- **Depends on:** Task 3
- **Implementation:**
  - `check_provider_key(provider, key) -> tuple[bool, str | None]` runs a minimal extraction against
    the provider's cheapest current model with `max_tokens=1`.
  - Returns structured failure reasons (`invalid_key`, `network`, `model_unavailable`).
- **Verify:**
  - `moon run core:test -- tests/llm/test_validation.py` passes with mocked HTTP.
  - Manual run against a known-good key and a junk key returns the expected verdicts.

### Wave 3: Call-site migration (mostly parallel)

#### Task 6: Migrate crawler entity extraction

- **Files:** `apps/api/src/sibyl/crawler/graph_integration.py`,
  `apps/api/tests/crawler/test_graph_integration.py`
- **Depends on:** Task 3
- **Parallel:** Yes (with Tasks 7, 8, 9)
- **Implementation:**
  - Define `ExtractedEntityPayload` Pydantic schema with `name`, `type`, `description`, `confidence`
    fields.
  - Replace the `AsyncAnthropic`-based `EntityExtractor.extract_from_chunk` with
    `Extractor(ExtractedEntityPayload, surface=LLMSurface.CRAWLER, system_prompt=EXTRACTION_PROMPT)`.
  - Delete the `json` markdown-stripping fallback; PydanticAI handles structured output.
  - Preserve the existing per-chunk concurrency.
- **Verify:**
  - `moon run api:test -- tests/crawler/test_graph_integration.py` passes.
  - Local smoke: ingest the same 10 documents through old and new paths, diff extracted entity
    counts (expect ±5%).

#### Task 7: Migrate reflection extraction

- **Files:** `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py`,
  `packages/python/sibyl-core/tests/test_reflect.py`
- **Depends on:** Task 3
- **Parallel:** Yes
- **Implementation:**
  - Replace the current extraction prompt path with `Extractor` typed on
    `ReflectionCandidatesPayload`.
  - Drop the manual JSON-cleanup branches.
  - Mock LLM tests use PydanticAI's `TestModel`.
- **Verify:**
  - `moon run core:test -- tests/test_reflect.py` passes.
  - `sibyl reflect "<sample notes>"` produces the same candidate shape locally.

#### Task 8: Migrate synthesis generator

- **Files:** `apps/api/src/sibyl/generator/llm.py`, `apps/api/tests/generator/test_llm.py`
- **Depends on:** Task 4
- **Parallel:** Yes
- **Implementation:**
  - Replace the `anthropic.Anthropic()` direct client with
    `Generator(surface=LLMSurface.SYNTHESIS)`.
  - Synthesis stays async-aware; remove the sync client path.
  - Carry over the current system prompt verbatim into the substrate.
- **Verify:**
  - `moon run api:test -- tests/generator/test_llm.py` passes.
  - Manual: run an existing synthesis preset and diff output structure.

#### Task 9: Migrate prompt-submit hook

- **Files:** `apps/cli/src/sibyl_cli/data/hooks/user-prompt-submit.py`,
  `hooks/user-prompt-submit.py` (mirror), `apps/cli/tests/test_user_prompt_hook.py`
- **Depends on:** Task 4
- **Parallel:** Yes
- **Implementation:**
  - Wrap the substrate import in a try/except so the hook degrades cleanly when `sibyl-core` is
    missing.
  - Use `Generator` with a `surface=LLMSurface.PROMPT_HOOK` to keep the model independently
    configurable.
- **Verify:**
  - `moon run cli:test -- tests/test_user_prompt_hook.py` passes.
  - Manual: send a prompt, confirm the generated search query lands in the context block.

### Wave 4: Settings backend (sequential after Wave 3 starts)

#### Task 10: Settings service: LLM keys

- **Files:** `apps/api/src/sibyl/services/settings.py`, `apps/api/tests/test_settings_service.py`
- **Depends on:** Task 5
- **Implementation:**
  - Add `get_llm_config(surface)` and `set_llm_config(surface, config)` to `SettingsService`.
  - Persist to the `settings` table with keys `llm.<surface>.provider`, `llm.<surface>.model`,
    `llm.<surface>.temperature`.
  - Reuse the existing encrypted-secret path for API keys (no new key storage).
- **Verify:**
  - `moon run api:test -- tests/test_settings_service.py` passes.
  - Round-trip set→get returns the persisted values.

#### Task 11: Settings API routes

- **Files:** `apps/api/src/sibyl/api/routes/settings.py`, `apps/api/tests/test_settings_api.py`
- **Depends on:** Task 10
- **Implementation:**
  - Add the four endpoints from §5.5.
  - Replace the inline `claude-3-haiku-20240307` probes in `_validate_anthropic_key` with the
    substrate's validation helper.
  - Update `setup.py:121` the same way.
  - Add OpenAPI schemas for the new request/response bodies.
- **Verify:**
  - `moon run api:test -- tests/test_settings_api.py` passes.
  - `curl -X GET http://localhost:3334/api/settings/llm` returns expected shape.
  - `curl -X POST http://localhost:3334/api/settings/llm/crawler/test` round-trips against a real
    key.

### Wave 5: Web UI (sequential after Wave 4)

#### Task 12: Web API client and hooks

- **Files:** `apps/web/src/lib/api.ts`, `apps/web/src/lib/hooks.ts`
- **Depends on:** Task 11
- **Implementation:**
  - Add `getLLMSettings`, `updateLLMSurface`, `testLLMSurface`, `getLLMRegistry` to the client.
  - Add corresponding React Query hooks: `useLLMSettings`, `useUpdateLLMSurface`,
    `useTestLLMSurface`, `useLLMRegistry`.
  - Type definitions mirror the OpenAPI shapes generated in Task 11.
- **Verify:**
  - `moon run web:typecheck` passes.
  - `moon run web:lint` passes.

#### Task 13: Language Models settings card

- **Files:** `apps/web/src/app/(main)/settings/admin/ai/page.tsx`,
  `apps/web/src/components/settings/llm-config-card.tsx` (new),
  `apps/web/src/components/settings/llm-config-card.test.tsx`
- **Depends on:** Task 12
- **Implementation:**
  - Build `LLMConfigCard` per the sketch in §5.6.
  - Per-surface row component with provider/model selects, source badge, test button, inline error.
  - Recommended-pill highlights the curated default for the surface.
  - Honor `env` source by disabling the row and rendering a tooltip pointing to the env var name.
- **Verify:**
  - `moon run web:test -- llm-config-card` passes (unit tests with mocked hooks).
  - Manual: open the settings page in the browser, change crawler to Gemini Flash, hit Save, confirm
    the API persists and a Test run succeeds.
  - Visual check via `agent-browser` or manual screenshot in dark mode and light mode.

#### Task 14: First-run setup wizard touchups

- **Files:** `apps/web/src/app/(main)/setup/page.tsx` (or wherever first-run lives — confirm), the
  setup API responses
- **Depends on:** Task 13
- **Implementation:**
  - If the first-run wizard touches API keys, also surface a default LLM picker. If not, skip this
    task.
- **Verify:**
  - `moon run web:test` passes.
  - Manual: walk through first-run on a clean install and confirm LLM choice persists.

### Wave 6: Cleanup and verification (sequential after Wave 5)

#### Task 15: Smoke and benchmark

- **Files:** `scripts/llm/smoke.py` (new), `docs/architecture/SIBYL_LLM_SUBSTRATE_PLAN.md` (this
  doc; updated with benchmark numbers)
- **Depends on:** Tasks 6, 7, 8, 9
- **Implementation:**
  - Script that runs 20 crawled chunks through extractor for each (provider, model) pair, records
    entity count, schema-validation success rate, p50/p95 latency, and cost.
  - Land results in this doc's appendix.
- **Verify:**
  - Script runs end-to-end with each provider's key set.

#### Task 16: Documentation

- **Files:** `packages/python/sibyl-core/README.md`, `apps/api/README.md`, `apps/web/README.md`,
  `docs/architecture/SIBYL_LLM_SUBSTRATE_PLAN.md`
- **Depends on:** Tasks 6, 7, 8, 9
- **Implementation:**
  - Document the substrate, env var contract, settings UI surface, and how to add a new provider.
  - Cross-link from the Northstar spec and from `CLAUDE.md` if anything operator-facing changes.
- **Verify:**
  - Manual review.

#### Task 17: Final gates

- **Files:** none directly
- **Depends on:** all prior
- **Implementation:**
  - `moon run :check` across the workspace.
  - `agent-browser` smoke of the settings UI in dark mode.
  - Confirm `rg "claude-3-haiku-20240307"` returns no hits.
- **Verify:**
  - `moon run :check` green.
  - Manual UI walkthrough green.

## 7. Verification gate summary

| Wave | Gate                                                      |
| ---- | --------------------------------------------------------- |
| 1    | `moon run core:check` green; `pydantic-ai` resolved       |
| 2    | `moon run core:check` green; live smoke per provider      |
| 3    | `moon run :check` green; entity-count diff within ±5%     |
| 4    | API contract verified via curl + integration tests        |
| 5    | Web typecheck + lint + unit + visual smoke green          |
| 6    | Workspace `moon run :check` green; smoke results recorded |

## 8. Risks and mitigations

- **PydanticAI API churn.** Mitigation: pin to `^1.95` and pin the registry snapshots. Cross-model
  review (this doc) catches the obvious API misuses before we wire the substrate to four call sites.
- **Provider structured-output behavior differs.** Mitigation: integration smoke per provider in
  Task 15 catches schema-drop regressions before we cut over defaults. PydanticAI's TestModel keeps
  unit tests deterministic.
- **Per-surface config explosion.** Mitigation: env var names are predictable
  (`SIBYL_LLM_<SURFACE>_*`), the registry constrains valid models, and the UI surfaces effective
  sources so operators can debug overrides.
- **Hook regressions on stale installs.** Mitigation: the hook keeps a try/except fallback to the
  current direct-SDK path while v0.10 is in flight; we remove the fallback in v0.11 after rollout
  bakes.
- **Cost surprises from operators picking expensive models.** Mitigation: registry surfaces cost per
  row, default stays on Haiku 4.5, Test button shows a real token count before save.

## 9. Out of scope (follow-ups for v0.11+)

- Tool use in `Generator` (lookups, retrieval-aware synthesis).
- Token budgeting and rate-limit-aware backoff across surfaces.
- Per-organization (not just per-deployment) model selection.
- Streaming token output to the web UI for synthesis runs.
- Removing the hook's direct-SDK fallback once telemetry confirms substrate stability.

## 10. Recommendation

Approve, hand to Codex for cross-model review on §5 (architecture) and §6 (decomposition), then
spawn the Wave 1 tasks in parallel.
