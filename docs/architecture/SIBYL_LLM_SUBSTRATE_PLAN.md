# Sibyl Native LLM Substrate Plan

Status: complete v4, verified for Wave 1 handoff. Author: Nova (Claude Opus 4.7) Date: 2026-05-15
Target milestone: v0.10 Roadmap: [`SIBYL_1_0_ROADMAP.md`](SIBYL_1_0_ROADMAP.md)

1.0 planning note: this substrate is required for automatic reflection, synthesis, and memory
review. It should also replace remaining Graphiti-era extraction/model-provider seams as Graphiti is
deleted from the supported runtime.

## 1. Goal

Stand up a native PydanticAI-based LLM substrate that owns every non-Graphiti structured-extraction
and text-generation call site in Sibyl, offers three first-class providers (Anthropic, Google
Gemini, OpenAI), and exposes operator-grade configuration through Settings API + web UI. The
substrate keeps a strict package boundary so `sibyl_core.ai` stays pure (env + defaults), while
`apps/api/src/sibyl/ai/llm` plugs in the DB-backed config source.

This is the surface that will outlive the Graphiti exit and host the v1.0 automatic reflection,
synthesis, and memory-review features named in the roadmap.

## 2. Success criteria

- `sibyl_core.ai` defines `ModelRegistry` and shared AI primitives, while `sibyl_core.ai.llm`
  defines `Extractor[T]`, `Generator`, `LLMConfig`, and an `LLMConfigSource` protocol. Core has no
  dependency on `apps/api` or any DB.
- `apps/api/src/sibyl/ai/llm` provides `DBSettingsConfigSource` that implements `LLMConfigSource`
  via `SettingsService` and is the resolver used by API + worker processes.
- First-class providers: Anthropic Haiku 4.5, Gemini 3 Flash, GPT-5.4 mini. Versioned model registry
  with capability flags, `last_verified_at`, deprecation date, and cost source.
- Crawler entity extraction (`apps/api/src/sibyl/crawler/graph_integration.py`) and the synthesis
  generator (`apps/api/src/sibyl/generator/llm.py`) run through the substrate. No direct
  `anthropic.AsyncAnthropic`, `openai.AsyncOpenAI`, or `google.genai` instantiation in those call
  sites.
- Settings API exposes provider, model, and key configuration **per surface** as instance-wide
  settings guarded by the existing `require_settings_admin` owner-or-admin policy. Env overrides DB
  and is surfaced as `locked_by_env` in API responses.
- `apps/web/src/app/(main)/settings/admin/ai/page.tsx` renders an LLM section with per-surface model
  selection, env-lock indicators, an explicit instance-wide banner, and a per-row "Test" affordance.
- Provider key validation hits each provider with a current, non-retired model via the substrate's
  `check_provider_key`. Setup and Settings routes drop `claude-3-haiku-20240307` references.
- `moon run :check` is green across core, api, cli, and web.
- Crawler smoke harness gates on schema success rate, required-field coverage, and type-distribution
  drift versus the current Haiku 4.5 baseline (not just entity count).

## 3. Non-goals

- Replacing or refactoring code under `packages/python/sibyl-core/src/sibyl_core/graph/`. The
  Graphiti `llm_provider`/`llm_model` config in `core/config.py` and `apps/api/src/sibyl/config.py`
  remains untouched. It dies on its own schedule per the Graphiti exit inventory.
- Replacing the embedding pipeline. Embedding provider config and `gemini_embedder.py` stay as-is.
- **Migrating `tools/reflect.py`.** Reflect is heuristic today (regex-based, no LLM SDK import).
  Adding LLM-driven reflection is a separate scope item that _consumes_ this substrate; it is not
  part of this plan. See [§9.1](#91-future-consumers-out-of-scope-for-this-plan).
- **Migrating the user-prompt-submit hook.** Its stdlib-HTTP design exists to avoid SDK and package
  coupling so the hook stays fast on cold starts. Migration would require the hook to call an API
  endpoint that uses the substrate server-side; defer to v0.11
  ([§9.1](#91-future-consumers-out-of-scope-for-this-plan)).
- Per-organization LLM configuration. v0.10 ships instance-wide config matching the existing
  `system_settings` model and settings-admin policy. Per-org overrides are tracked as a v0.11+
  follow-up.
- Building a generic agent system. Tool use lands when a real consumer asks for it; `Generator`
  exposes streamed text only.
- Adding LiteLLM, OpenRouter, or other proxy layers.

## 4. Current state inventory

Non-Graphiti LLM-adjacent surfaces:

| Surface         | File                                                         | Behavior                                                | Model                        | In scope?            |
| --------------- | ------------------------------------------------------------ | ------------------------------------------------------- | ---------------------------- | -------------------- |
| Crawler extract | `apps/api/src/sibyl/crawler/graph_integration.py:189`        | Anthropic SDK, JSON-from-prose parsing, retry-by-string | hardcoded `claude-haiku-4-5` | **Yes (v0.10)**      |
| Synthesis       | `apps/api/src/sibyl/generator/llm.py:45`                     | Anthropic SDK direct, blocking client                   | hardcoded                    | **Yes (v0.10)**      |
| Reflect         | `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py` | Regex/heuristic; no LLM SDK import today                | n/a                          | No (future consumer) |
| Prompt hook     | `apps/cli/src/sibyl_cli/data/hooks/user-prompt-submit.py:34` | Stdlib HTTPS POST to Anthropic; intentional no-SDK      | `claude-haiku-4-5-20251001`  | No (defer)           |

Settings/setup validation:

- `apps/api/src/sibyl/api/routes/setup.py:121` and `apps/api/src/sibyl/api/routes/settings.py:135`
  both probe `claude-3-haiku-20240307` for Anthropic key validation. Retired model; replace.

Web UI:

- `apps/web/src/app/(main)/settings/admin/ai/page.tsx` exists with embedding provider/model
  selection and key entry for OpenAI, Anthropic, and Gemini. We extend it with LLM config sections.

Process surfaces that need to resolve LLM config:

- FastAPI app (`apps/api/src/sibyl/main.py`, `apps/api/src/sibyl/api/app.py`) — multiple workers,
  multiple event loops over the lifetime of a process.
- arq worker (`apps/api/src/sibyl/jobs/worker.py`) — separate process, separate event loop.
- CLI/MCP — typically env-only; should not import `SettingsService`.

## 5. Target architecture

### 5.1 Package layout

The boundary fix: `sibyl_core.ai` is pure (no `apps/api` import, no DB import). It exposes a
`LLMConfigSource` protocol and an env-only default implementation. `apps/api` ships the DB-backed
implementation and wires it at process startup.

The umbrella package is `sibyl_core.ai/` — v0.10 ships the `ai.llm/` submodule and reserves
`ai.embeddings/` for v0.11+ when embedding providers join the substrate. Shared primitives (provider
key resolution, validation, client cache, registry, errors, observability) live directly under `ai/`
so the embedding submodule can reuse them without a future rename.

```
packages/python/sibyl-core/src/sibyl_core/ai/
├── __init__.py             # Public surface re-exports from llm/
├── registry.py             # ModelRegistry, ModelEntry (with kind: "llm" | "embedding")
├── providers.py            # provider-keyed factories shared by llm and (future) embeddings
├── clients.py              # scoped-cache of (loop_id, fingerprint) → Agent / Embedder
├── validation.py           # check_provider_key (kind-agnostic), check_model_availability
├── observability.py        # Token tracker, structlog spans (no Logfire dep)
├── errors.py               # AIError + classified subclasses (LLM and embedding share these)
├── _testing.py             # TestModel-based mocks for both kinds
└── llm/
    ├── __init__.py
    ├── config.py           # LLMConfig, LLMSurface, LLMConfigSource protocol, EnvConfigSource
    ├── extractor.py        # Extractor[T: BaseModel] — single-shot structured extraction
    └── generator.py        # Generator — streamed text generation
# ai/embeddings/ reserved for v0.11; not created in v0.10.

apps/api/src/sibyl/ai/
├── __init__.py
├── llm/
│   ├── __init__.py
│   ├── config_source.py    # DBSettingsConfigSource: implements LLMConfigSource via SettingsService
│   ├── routes.py           # LLM settings endpoints (mounted under /api/settings/ai/llm)
│   └── service.py          # cache invalidation, worker startup reload, settings-admin guard
# apps/api/src/sibyl/ai/embeddings/ reserved for v0.11.
```

Reasoning: core stays usable in any process that has env vars. API processes call
`set_config_source(DBSettingsConfigSource(...))` once at startup. Worker processes do the same after
`load_runtime_settings_from_db()`. The `ai/` umbrella is structural-only in v0.10 — there is no
abstract `AIConfigSource` base trying to unify LLM and embedding configs at the protocol level
(their fields differ — temperature/max_tokens vs dimensions). What we share is the surrounding
plumbing: keys, registry, cache, validation, error taxonomy, settings page layout.

### 5.2 Key types

```python
# sibyl_core/ai/llm/config.py
class LLMSurface(StrEnum):
    DEFAULT = "default"
    CRAWLER = "crawler"
    SYNTHESIS = "synthesis"
    # Future consumers reserved here; added when their feature lands:
    # REFLECT = "reflect"           # v0.11 LLM-assisted reflection
    # PROMPT_HOOK = "prompt_hook"   # v0.11 hook-via-api migration

class LLMConfig(BaseModel):
    """Raw shape passed to provider factories. Flat values, no metadata."""
    provider: Literal["anthropic", "gemini", "openai"]
    model: str                       # alias or snapshot from the registry; custom allowed with warning
    temperature: float = 0.0
    max_tokens: int | None = None
    timeout_seconds: float = 60.0

class ConfigField[T](BaseModel):
    """Per-field resolution metadata. Different fields can have different sources."""
    value: T
    source: Literal["env", "db", "default"]
    locked_by_env: bool = False
    env_var: str | None = None       # populated when source = "env"

class ResolvedLLMConfig(BaseModel):
    """Output of LLMConfigSource.resolve. Per-field metadata so the API + UI
    can render source badges and reject env-locked writes correctly."""
    surface: LLMSurface
    provider: ConfigField[Literal["anthropic", "gemini", "openai"]]
    model: ConfigField[str]
    temperature: ConfigField[float]
    max_tokens: ConfigField[int | None]
    timeout_seconds: ConfigField[float]
    api_key: ConfigField[SecretStr]  # never serialized in API responses; UI sees only `source` + presence
    cached_at: datetime | None = None # populated by DB-backed sources; used for worker TTL refresh

    def to_llm_config(self) -> LLMConfig: ...

class LLMConfigSource(Protocol):
    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig: ...
    async def invalidate(self, surface: LLMSurface | None = None) -> None: ...

class EnvConfigSource:
    """Pure-env resolver; the default when no DB-backed source is installed.
    Reads from os.environ at resolution time; never mutates it."""
    async def resolve(self, surface: LLMSurface) -> ResolvedLLMConfig: ...

# sibyl_core/ai/llm/extractor.py
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
    async def extract_many(
        self,
        chunks: Sequence[str],
        *,
        max_concurrency: int = 4,
    ) -> list[T | LLMError]: ...

# sibyl_core/ai/llm/generator.py
class Generator:
    async def generate(self, prompt: str) -> str: ...
    async def stream(self, prompt: str) -> AsyncIterator[str]: ...

# apps/api/src/sibyl/ai/llm/config_source.py
class DBSettingsConfigSource:
    """LLMConfigSource backed by SettingsService. Env wins over DB (see §5.3)."""
    def __init__(self, get_settings_service: Callable[[], SettingsService]) -> None: ...
```

### 5.3 Configuration hierarchy

**Contract: env overrides DB.** Deployment-time env beats UI-set values so operators can hot-fix
without web access. The UI exposes an `env_lock` indicator and the API rejects writes to env-locked
surfaces with `409 LOCKED_BY_ENV` (the lock state is per-key, e.g. `model` may be env-locked while
`temperature` is not).

Resolution order (most-specific wins):

1. Explicit `model_override` on the call site (rarely used; tests/scripts).
2. Env override: `SIBYL_LLM_<SURFACE>_MODEL`, `SIBYL_LLM_<SURFACE>_PROVIDER`,
   `SIBYL_LLM_<SURFACE>_TEMPERATURE`, `SIBYL_LLM_<SURFACE>_MAX_TOKENS`.
3. Database setting: `llm.<surface>.<field>` written via Settings API.
4. Global env: `SIBYL_LLM_MODEL`, `SIBYL_LLM_PROVIDER`, …
5. Global database setting: `llm.default.<field>`.
6. Compile-time default from `registry.py`: `("anthropic", "claude-haiku-4-5")`.

API keys follow the same precedence using existing key vars (`SIBYL_ANTHROPIC_API_KEY`,
`ANTHROPIC_API_KEY`, …) and are resolved **through `LLMConfigSource.resolve()`**, not through
`os.environ`. The legacy `load_api_keys_from_db()` function copies DB-stored keys into `os.environ`
for non-substrate callers (e.g. `anthropic.AsyncAnthropic()` direct construction) and remains in
place for those code paths during the Graphiti exit window. **The substrate does not depend on it**
and reads the key from the resolver. This is what makes env-over-DB internally consistent: the
substrate sees env-first because the resolver checks env first, not because `load_api_keys_from_db`
ran. The UI shows `env`/`db`/`default` source per field and disables fields where env wins.

### 5.4 Model registry

`registry.py` ships a versioned registry under `sibyl_core.ai.registry`. Each entry is a record (not
a dict literal). The registry is the source of truth for what shows up in dropdowns and what
validation accepts without an explicit "custom model" opt-in. v0.10 ships only `kind="llm"` entries;
embedding entries land in v0.11 using the same shape.

```python
class ModelKind(StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"

class ModelCapability(StrEnum):
    STRUCTURED_OUTPUT = "structured_output"
    STREAMING = "streaming"
    TOOL_USE = "tool_use"
    THINKING = "thinking"

class ModelEntry(BaseModel):
    alias: str                          # e.g. "claude-haiku-4-5"
    snapshot: str                       # e.g. "claude-haiku-4-5-20251001"
    kind: ModelKind                     # "llm" or "embedding" — v0.10 only ships llm entries
    provider: Literal["anthropic", "gemini", "openai", "cohere", "voyageai", "bedrock"]
    provider_model_id: str              # exact string passed to PydanticAI
    pydantic_ai_model_class: str        # e.g. "AnthropicModel" or "OpenAIResponsesModel"
    use_cases: list[str]                # ["extraction", "synthesis", "bulk", "semantic-search", ...]
    capabilities: set[ModelCapability]  # empty for embedding entries
    max_output_tokens: int | None       # None for embedding entries
    embedding_dimensions: int | None    # set for embedding entries; None for llm
    default_temperature: float | None   # None for embedding entries
    input_cost_per_mtok_usd: float
    output_cost_per_mtok_usd: float | None  # None for embedding entries
    cost_source_url: str
    last_verified_at: datetime
    deprecated_after: datetime | None
```

Lookup helpers accept a `kind` filter so the LLM UI only sees LLM entries and the future embedding
UI only sees embedding entries:

```python
def llm_entries() -> list[ModelEntry]: ...                  # filter kind=LLM
def embedding_entries() -> list[ModelEntry]: ...            # filter kind=EMBEDDING (empty in v0.10)
def recommended_for(use_case: str, kind: ModelKind) -> ModelEntry: ...
```

Initial entries (all `last_verified_at = 2026-05-15`):

| Provider  | Alias                   | Provider model ID           | Use cases                 | $/M in | $/M out |
| --------- | ----------------------- | --------------------------- | ------------------------- | ------ | ------- |
| Anthropic | `claude-haiku-4-5`      | `claude-haiku-4-5-20251001` | extraction, default       | 1.00   | 5.00    |
| Anthropic | `claude-sonnet-4-6`     | `claude-sonnet-4-6`         | synthesis (quality tier)  | 3.00   | 15.00   |
| Google    | `gemini-3-flash`        | `gemini-3-flash-preview`    | cost-optimized extraction | 0.50   | 3.00    |
| Google    | `gemini-3-1-flash-lite` | `gemini-3.1-flash-lite`     | bulk crawling             | 0.25   | 1.50    |
| OpenAI    | `gpt-5.4-mini`          | `gpt-5.4-mini`              | OpenAI parity             | 0.75   | 4.50    |
| OpenAI    | `gpt-5.4-nano`          | `gpt-5.4-nano`              | budget extraction         | 0.20   | 1.25    |

**Custom-model override:** if an operator picks a model not in the registry, the API allows it but
returns `{"warning": "unverified_model"}` and the UI shows a yellow badge. We do not silently trust
an arbitrary string.

**Pinning:** `SIBYL_LLM_PIN_SNAPSHOTS=true` resolves aliases to snapshots at startup. CI uses this.

**Registry refresh:** §6 Wave 6 adds `scripts/llm/verify_registry.py`, which probes each entry's
provider for availability and refreshes `last_verified_at`. It runs nightly (out of scope for v0.10
to schedule; tracked as v0.11 follow-up).

### 5.5 Settings API surface

LLM settings are **deployment-global** and use the existing `require_settings_admin` policy (owner
or admin role), matching how the rest of the AI settings page works today. They are not org-scoped
in v0.10. The API returns this scope explicitly so the UI cannot misrepresent it.

Note on policy tightening: `require_settings_admin` allows any admin in any org to mutate
instance-wide config. That is the existing settings-admin contract; the substrate inherits it rather
than introducing a new policy break. If multi-tenant risk warrants tightening to owner-only, that is
a separate policy decision tracked as a v0.11+ follow-up
([§9.1](#91-future-consumers-out-of-scope-for-this-plan)).

New endpoints live under the AI umbrella at `/api/settings/ai/*`. v0.10 mounts
`/api/settings/ai/llm/*` and reserves `/api/settings/ai/embeddings/*` for v0.11. All guarded by
`require_settings_admin`:

- `GET /api/settings/ai/llm` — returns the resolved config for every surface, including `source` and
  `locked_by_env` per field. Response shape includes a top-level `scope: "instance_wide"` marker.
- `PUT /api/settings/ai/llm/{surface}` — updates provider/model/temperature/max_tokens for a
  surface. Validates the model against the registry (filtered to `kind=llm`); allows custom models
  with a warning. Returns `409 LOCKED_BY_ENV` if any updated key is env-locked.
- `POST /api/settings/ai/llm/{surface}/test` — runs a representative extraction against the resolved
  config. Returns latency, token counts, parsed output, and `LLMError` classification on failure.
- `POST /api/settings/ai/keys/{provider}/test` — validates the configured API key for the provider
  using `check_provider_key` (text probe against the provider's cheapest current model). Path is
  kind-agnostic because the same key serves both LLM and embedding calls on most providers.
- `POST /api/settings/ai/models/{model_alias}/test` — probes a specific registry entry's
  availability using `check_model_availability(provider, provider_model_id, key)`. Used by both the
  UI (when an operator picks an unfamiliar model) and `verify_registry.py`. Kind is derived from the
  registry entry, so this endpoint stays under `/ai/` rather than `/ai/llm/`.
- `GET /api/settings/ai/registry?kind=llm` — exposes the registry to the web UI, with optional
  `kind` filter (defaults to all). Embedding UI in v0.11 will use `?kind=embedding`.

Endpoints reject non-admin requests with `403`. UI banner explicitly labels the section
"Instance-wide settings (affects all organizations)."

Existing `_validate_anthropic_key`, `_validate_openai_key`, `_validate_gemini_key` in
`apps/api/src/sibyl/api/routes/setup.py` and `apps/api/src/sibyl/api/routes/settings.py` are
replaced by calls to `sibyl_core.ai.validation.check_provider_key`. Setup-mode behavior is
preserved. These legacy endpoints stay at their current paths (no `/ai/` prefix) because they're
referenced by the existing first-run UI.

### 5.6 Web UI

`apps/web/src/app/(main)/settings/admin/ai/page.tsx` already lives under the "AI Settings" parent.
v0.10 reorganizes the page into two sibling sections: **Language Models** (new) and **Embeddings**
(existing), with the existing embedding controls moved into the Embeddings section unchanged. The
Language Models card has the following contract:

- **Instance-wide banner** at top of the card: "These settings apply to all organizations in this
  deployment. Per-organization overrides are planned for a future release."
- **Per-row source badge** (`env` / `db` / `default`). Env-locked rows are visually disabled with a
  tooltip naming the env var and explaining that env wins by design.
- **Save** returns `409 LOCKED_BY_ENV` on env-locked fields; the UI surfaces this inline ("This
  field is set by an environment variable and can't be changed here"), not as a generic toast error.
- **Test** button per row calls `POST /api/settings/ai/llm/{surface}/test` and renders latency,
  token count, and parsed sample output. Failure renders the classified error, not just the message.
- **Custom-model field** is hidden behind an "Advanced" disclosure with a confirmation toggle: "I
  know this model isn't in the registry. Sibyl can't verify capabilities or cost." Yellow badge next
  to saved custom values.
- **Recommended pill** next to the curated default for each surface, sourced from `use_cases` in the
  registry.

Per-surface rows for v0.10: Default, Crawler, Synthesis. Reflect and Prompt-hook rows ship when
those consumers migrate (v0.11+).

### 5.7 Provider client lifetime

Global async-client caches break under multi-loop, multi-process, settings-mutation conditions.
Contract:

- `clients.py` keys cached agents by `(asyncio.get_running_loop().__hash__(), config_fingerprint)`.
  `config_fingerprint = sha256(json(provider, model, temperature, max_tokens, timeout_seconds, api_key_hash))`.
- `LLMConfigSource.invalidate(surface)` clears every cache entry for surfaces resolving from the
  changed setting. `PUT /api/settings/ai/llm/{surface}` calls `invalidate` in-process after writing,
  before returning the response. This is the strong guarantee for the API process.
- **Cross-process propagation** to the arq worker uses a TTL-based refresh, not pubsub. The
  `SettingsService` already caches reads per `_CacheEntry` with a TTL; the substrate plumbs a
  `cached_at` timestamp into each `ResolvedLLMConfig` and the worker re-resolves when the cache TTL
  expires (default 60s) or when the worker explicitly calls `invalidate()` on shutdown of a long-
  running task. We do not invent a new settings-change pubsub for v0.10. The trade-off is named:
  worker-side changes can lag by up to one TTL.
- Worker startup (`apps/api/src/sibyl/jobs/worker.py`) calls
  `install_db_config_source(SettingsService)` after the existing `load_api_keys_from_db()` runs. The
  substrate does **not** rely on `load_api_keys_from_db()` having mutated `os.environ`; it reads
  keys directly from `SettingsService` via the resolver.
- No mutation of `os.environ` from substrate code. Env reads happen at resolution time only.
  Existing `load_api_keys_from_db()` continues to mutate `os.environ` for non-substrate callers
  during the Graphiti exit; that path is orthogonal.
- Verification (Task 4): a multi-loop test that creates two `asyncio.new_event_loop()` instances in
  the same process, builds clients on each, asserts isolated cache entries, then invalidates and
  asserts both clear.

### 5.8 Observability

`observability.py` wraps every call with structured logging: `surface`, `provider`, `model`,
`prompt_name`, `input_tokens`, `output_tokens`, `latency_ms`, `retries`, `error_class`. Counters
land on `sibyl debug status` so operators see usage per surface.

We do **not** install Logfire. The dependency strategy is
`pydantic-ai-slim[anthropic,google,openai]` (see Task 1) and our existing `structlog` setup covers
structured logging.

## 6. Decomposition

### Wave 1: Foundation (parallel)

#### Task 1: Add pydantic-ai-slim and create ai/ package skeleton

- **Files:** `packages/python/sibyl-core/pyproject.toml`,
  `packages/python/sibyl-core/src/sibyl_core/ai/__init__.py`,
  `packages/python/sibyl-core/src/sibyl_core/ai/errors.py`,
  `packages/python/sibyl-core/src/sibyl_core/ai/llm/__init__.py`
- **Parallel:** Yes (with Tasks 2, 3)
- **Implementation:**
  - `uv add 'pydantic-ai-slim[anthropic,google,openai]>=1.96.1,<1.97'` at the sibyl-core package.
  - Avoid the full `pydantic-ai` distribution (it pulls Logfire and all model deps we don't use).
  - Create the `ai/` umbrella with `errors.py` defining `AIError` (base), `LLMError`,
    `LLMConfigError`, `LLMValidationError`, `LLMRateLimitError`, `LLMProviderError`,
    `LLMTimeoutError`. Embedding-specific errors are reserved for v0.11 alongside their submodule.
  - Empty `ai/llm/` submodule stub for Tasks 2 and 4-7 to populate.
  - Document the exact tested version (1.96.1 at time of writing) in `pyproject.toml` comments.
- **Verify:**
  - `moon run core:typecheck` passes.
  - `uv tree -p sibyl-core | rg 'pydantic-ai-slim'` shows the slim package, no `logfire` extra.

#### Task 2: LLM config types and EnvConfigSource

- **Files:** `packages/python/sibyl-core/src/sibyl_core/ai/llm/config.py`,
  `tests/ai/llm/test_config.py`
- **Parallel:** Yes
- **Implementation:**
  - Define `LLMSurface`, `LLMConfig`, `ConfigField[T]`, `ResolvedLLMConfig`, `LLMConfigSource`
    protocol, and `EnvConfigSource`.
  - `EnvConfigSource` honors the env-only precedence (steps 1, 2, 4, 6 from §5.3).
  - Tests: precedence order; surface scoping; missing-key path; per-field `locked_by_env` marking.
- **Verify:**
  - `moon run core:test -- tests/ai/llm/test_config.py` passes.
  - `moon run core:lint`, `moon run core:typecheck` green.

#### Task 3: Shared registry

- **Files:** `packages/python/sibyl-core/src/sibyl_core/ai/registry.py`, `tests/ai/test_registry.py`
- **Parallel:** Yes
- **Implementation:**
  - `ModelKind`, `ModelEntry` Pydantic model, `ModelRegistry` with the six LLM entries from §5.4
    (all `kind=ModelKind.LLM` in v0.10).
  - Lookup by alias **and** snapshot. Capability checks. Kind-filtered helpers: `llm_entries()`,
    `embedding_entries()`, `recommended_for(use_case, kind)`.
  - Custom-model handling returns a `ModelEntry`-shaped record with `capabilities = set()` and a
    warning flag.
- **Verify:**
  - `moon run core:test -- tests/ai/test_registry.py` passes.

### Wave 2: Provider clients and call surface (sequential after Wave 1)

#### Task 4: Provider factory and scoped client cache

- **Files:** `packages/python/sibyl-core/src/sibyl_core/ai/providers.py`,
  `packages/python/sibyl-core/src/sibyl_core/ai/clients.py`, `tests/ai/test_clients.py`
- **Depends on:** Tasks 1, 2, 3
- **Implementation:**
  - `build_model(config)` maps `LLMConfig` →
    `pydantic_ai.models.AnthropicModel | GoogleModel | OpenAIResponsesModel`.
  - `clients.get_agent(surface, schema=None, system_prompt=...)` returns a `pydantic_ai.Agent` keyed
    by `(loop_id, fingerprint)`.
  - Per §5.7: no `os.environ` mutation; API keys are passed via `pydantic_ai.providers.*Provider`.
  - Tests: cache hit/miss; invalidate after config change; behavior across two event loops in the
    same process (use `asyncio.new_event_loop` to simulate).
- **Verify:**
  - `moon run core:test -- tests/ai/test_clients.py` passes.

#### Task 5: Extractor and structured-output retry

- **Files:** `packages/python/sibyl-core/src/sibyl_core/ai/llm/extractor.py`,
  `tests/ai/llm/test_extractor.py`
- **Depends on:** Task 4
- **Implementation:**
  - `Extractor[T]` wraps `pydantic_ai.Agent` with `output_type=T`.
  - `extract` returns the parsed Pydantic model. PydanticAI's `ModelRetry` is mapped to
    `LLMValidationError` after exhaustion. `ModelHTTPError` maps to provider/rate-limit/timeout
    error classes based on status code.
  - `extract_many` runs with bounded concurrency (`asyncio.Semaphore`) and returns
    `list[T | LLMError]` so callers can decide how to surface partial failures.
  - Tests via PydanticAI's `TestModel`; cover success, validation retry, rate-limit mapping,
    timeout.
- **Verify:**
  - `moon run core:test -- tests/ai/llm/test_extractor.py` passes.
  - Local live smoke against one Anthropic + one Gemini + one OpenAI key (skip in CI when keys are
    absent).

#### Task 6: Generator and streamed text

- **Files:** `packages/python/sibyl-core/src/sibyl_core/ai/llm/generator.py`,
  `tests/ai/llm/test_generator.py`
- **Depends on:** Task 4
- **Implementation:**
  - `Generator.generate` (sync result) and `Generator.stream` (`AsyncIterator[str]`).
  - System prompt and `model_override` plumbing identical to `Extractor`.
- **Verify:**
  - `moon run core:test -- tests/ai/llm/test_generator.py` passes.
  - Manual stream against Anthropic produces incremental text.

#### Task 7: Validation helpers — split key vs surface

- **Files:** `packages/python/sibyl-core/src/sibyl_core/ai/validation.py`,
  `tests/ai/test_validation.py`
- **Depends on:** Task 5
- **Implementation:**
  - `check_provider_key(provider, key) -> KeyValidationResult` issues a tiny **non-structured** text
    request to the provider's cheapest current model. Returns a tagged result distinguishing
    `valid`, `invalid_key`, `network`, `rate_limited`, `model_not_found`, `permission_denied`.
  - `check_model_availability(provider, provider_model_id, key) -> ModelValidationResult` issues a
    1-token text request against a **specific** model. Used by `verify_registry.py` and by the UI
    when an operator picks a model the registry hasn't verified recently. Returns the same tagged
    classification plus the actual provider model string echoed back when available (catches silent
    provider aliasing).
  - `test_surface_config(surface, source) -> SurfaceTestResult` runs a representative extraction
    using the resolved config and returns parsed output, latency, token counts.
  - Classify `pydantic_ai.exceptions.ModelHTTPError` status codes (401/403/404/429/5xx).
- **Verify:**
  - `moon run core:test -- tests/ai/test_validation.py` passes with mocked transport.
  - Manual run against a known-good key and a junk key returns the right tag for each.

### Wave 3: DB-backed config source and Settings API (sequential after Wave 2)

#### Task 8: DBSettingsConfigSource

- **Files:** `apps/api/src/sibyl/ai/llm/__init__.py`, `apps/api/src/sibyl/ai/llm/config_source.py`,
  `apps/api/tests/ai/llm/test_config_source.py`
- **Depends on:** Task 2
- **Implementation:**
  - `DBSettingsConfigSource` implements `LLMConfigSource`, reading from `SettingsService` using keys
    `llm.<surface>.provider`, `llm.<surface>.model`, etc.
  - Resolution order from §5.3 — env wins over DB, this resolver tracks both and returns
    `locked_by_env` per field.
  - API key resolution uses the existing setting keys and encrypted-secret storage but resolves
    env-first for substrate callers. Do not call `get_anthropic_key`, `get_openai_key`, or
    `get_gemini_key` directly for the final value because those helpers are DB-first for legacy
    callers.
- **Verify:**
  - `moon run api:test -- tests/ai/llm/test_config_source.py` passes.
  - Round-trip set→get with env unset returns DB values; env-set returns env value with lock flag.

#### Task 9: Settings service additions and cache invalidation

- **Files:** `apps/api/src/sibyl/services/settings.py`, `apps/api/src/sibyl/ai/llm/service.py`,
  `apps/api/tests/test_settings_service.py`
- **Depends on:** Task 8
- **Implementation:**
  - Add `get_llm_setting(surface, field)`, `set_llm_setting(surface, field, value)` to
    `SettingsService`. Reuse the existing encrypted-secret path for keys; no new key storage.
  - `apps/api/src/sibyl/ai/llm/service.py` exposes `install_db_config_source()` for app startup and
    calls `LLMConfigSource.invalidate(surface)` in-process after settings writes. Worker processes
    rely on TTL-based refresh per §5.7; no settings-change pubsub is introduced in v0.10.
  - Worker startup (`apps/api/src/sibyl/jobs/worker.py`) calls `install_db_config_source()` after
    `load_api_keys_from_db()`.
- **Verify:**
  - `moon run api:test -- tests/test_settings_service.py` passes.
  - Spin up the API + worker locally, set a value via API, observe API-side invalidation immediately
    and worker-side refresh after the configured TTL.

#### Task 10: Settings API routes

- **Files:** `apps/api/src/sibyl/ai/llm/routes.py`, `apps/api/src/sibyl/api/routes/setup.py`,
  `apps/api/src/sibyl/api/routes/settings.py`, `apps/api/tests/ai/test_llm_settings_api.py`
- **Depends on:** Tasks 7, 9
- **Implementation:**
  - Endpoints from §5.5 with `require_settings_admin`. Response schemas explicit about
    `scope: "instance_wide"` and per-field `source` + `locked_by_env`.
  - Replace the inline `claude-3-haiku-20240307` probes in `setup.py:121` and `settings.py:135` with
    `check_provider_key`.
  - OpenAPI schemas + tests for: GET returns expected shape; PUT writes; PUT returns 409 when
    env-locked; POST `/test` runs end-to-end; non-admin returns 403.
- **Verify:**
  - `moon run api:test -- tests/test_llm_settings_api.py` passes.
  - `curl -X GET http://localhost:3334/api/settings/ai/llm` returns expected shape with sources.

### Wave 4: Call-site migration (parallel after Wave 3)

#### Task 11: Migrate crawler entity extraction

- **Files:** `apps/api/src/sibyl/crawler/graph_integration.py`,
  `apps/api/tests/crawler/test_graph_integration.py`,
  `apps/api/tests/crawler/fixtures/extraction_baseline.json` (new)
- **Depends on:** Tasks 5, 8
- **Parallel:** Yes (with Task 12)
- **Implementation:**
  - Define a **container** schema:

    ```python
    class ExtractedEntityPayload(BaseModel):
        name: str = Field(min_length=1, max_length=200)
        type: Literal["concept", "tool", "pattern", "person", "organization", "project"]
        description: str = Field(min_length=1, max_length=500)
        confidence: float = Field(ge=0.0, le=1.0)

    class ExtractedEntitiesPayload(BaseModel):
        entities: list[ExtractedEntityPayload] = Field(max_length=50)
    ```

  - Replace `EntityExtractor.extract_from_chunk` with `Extractor(ExtractedEntitiesPayload, ...)` and
    convert results back to the existing `ExtractedEntity` dataclass with `source_chunk_id` and
    `source_url` re-attached.
  - Delete the markdown JSON-stripping fallback; PydanticAI handles structured output.
  - `extract_batch` uses `Extractor.extract_many`.

- **Verify:**
  - `moon run api:test -- tests/crawler/test_graph_integration.py` passes.
  - Schema-success gate: for the 20-chunk baseline fixture, ≥95% chunks return valid payloads.
  - Field-coverage gate: ≥98% of returned entities have non-empty `description`, valid `type`,
    `confidence ∈ [0,1]`.
  - Type-distribution drift gate: per-type entity ratio drifts ≤ 25% vs. fixture baseline.
  - Entity-count gate: ±15% (loose, distribution drift catches the real regressions).

#### Task 12: Migrate synthesis generator

- **Files:** `apps/api/src/sibyl/generator/llm.py`, `apps/api/tests/generator/test_llm.py`
- **Depends on:** Task 6, 8
- **Parallel:** Yes
- **Implementation:**
  - Replace `anthropic.Anthropic()` direct client with `Generator(surface=LLMSurface.SYNTHESIS)`.
  - Drop the sync client path; synthesis is async throughout the chain anyway.
  - Carry over the system prompt verbatim.
- **Verify:**
  - `moon run api:test -- tests/generator/test_llm.py` passes.
  - Manual: run an existing synthesis preset, diff output structure.

### Wave 5: Web UI (sequential after Wave 4)

#### Task 13: Web API client and hooks

- **Files:** `apps/web/src/lib/api.ts`, `apps/web/src/lib/hooks.ts`
- **Depends on:** Task 10
- **Implementation:**
  - `getLLMSettings`, `updateLLMSurface`, `testLLMSurface`, `testProviderKey`, `getLLMRegistry`.
  - React Query hooks mirroring the existing settings hooks.
  - Generated types from OpenAPI shapes added in Task 10.
- **Verify:** `moon run web:typecheck` and `moon run web:lint` pass.

#### Task 14: Language Models settings card

- **Files:** `apps/web/src/app/(main)/settings/admin/ai/page.tsx`,
  `apps/web/src/components/settings/llm-config-card.tsx` (new),
  `apps/web/src/components/settings/llm-config-card.test.tsx`
- **Depends on:** Task 13
- **Implementation:**
  - Card per §5.6: instance-wide banner, per-row provider+model selects, source badge, env-lock
    disabled state with tooltip, recommended pill, Test button with structured failure rendering.
  - "Advanced — custom model" disclosure with confirmation toggle and yellow badge for saved custom
    values.
  - Save renders inline 409 message on env-locked fields; toast only for unexpected errors.
- **Verify:**
  - `moon run web:test -- llm-config-card` passes (unit tests against mocked hooks).
  - Manual via `agent-browser`: change crawler to Gemini Flash, save, Test, see latency + tokens.

### Wave 6: Verification, smoke, docs (sequential after Wave 5)

#### Task 15: Smoke harness and registry probe script

- **Files:** `scripts/llm/smoke.py` (new), `scripts/llm/verify_registry.py` (new),
  `docs/architecture/SIBYL_LLM_SUBSTRATE_PLAN.md` (append benchmark appendix)
- **Depends on:** Tasks 11, 12
- **Implementation:**
  - `smoke.py` runs the **same 20-chunk fixture** from Task 11 through `Extractor` for each
    (provider, model) pair. Reuses Task 11's gate functions: schema success ≥95%, field coverage
    ≥98%, type-distribution drift ≤25%, entity-count ±15%. Adds a name-quality gate (entity name
    length ≥2, non-whitespace, not in `{"UNKNOWN", "null", "None", ""}`). Records p50/p95 latency
    and per-chunk cost.
  - `verify_registry.py` probes each registry entry via
    `check_model_availability(entry.provider, entry.provider_model_id, key)` (not
    `check_provider_key` — that only validates the key against the cheapest model). Per-entry
    failures update `last_verified_at` accordingly and log deprecation candidates. Exit code
    reflects whether any first-class entry failed.
- **Verify:**
  - `python scripts/llm/smoke.py` end-to-end with each provider's key set.
  - `python scripts/llm/verify_registry.py` exits 0 with the registry intact.

#### Task 16: Documentation

- **Files:** `packages/python/sibyl-core/README.md`, `apps/api/README.md`, `apps/web/README.md`,
  this doc.
- **Depends on:** Tasks 11, 12, 14
- **Implementation:**
  - Document substrate, env contract, settings UI surface, custom-model behavior, how to add a
    provider. Cross-link from Northstar and Roadmap.
  - Add the multi-tenant boundary explicitly: this is instance-wide config. Per-org override is a
    v0.11+ follow-up tracked separately.
- **Verify:** manual.

#### Task 17: Final gates

- **Depends on:** all prior.
- **Implementation:**
  - `moon run :check` green.
  - `agent-browser` smoke of the settings UI in dark mode.
  - `rg "claude-3-haiku-20240307"` empty.
  - `rg "AsyncAnthropic|anthropic\.Anthropic|openai\.AsyncOpenAI" apps/api/src/sibyl/crawler/graph_integration.py apps/api/src/sibyl/generator/llm.py`
    empty after Tasks 11-12.
  - `rg "google\.genai" apps/api/src packages/python/sibyl-core/src/sibyl_core` shows only embedding
    adapters or Graphiti-compat paths until the v0.11 embedding migration.
- **Verify:** all of the above succeed.

## 7. Verification gate summary

| Wave | Gate                                                                        |
| ---- | --------------------------------------------------------------------------- |
| 1    | `moon run core:check` green; slim install only                              |
| 2    | `moon run core:check` green; multi-loop client cache test green             |
| 3    | API integration tests green; env-lock 409 verified; worker invalidates      |
| 4    | Schema-success ≥95%, field coverage ≥98%, type-distribution drift ≤25%      |
| 5    | Web typecheck + lint + unit + visual smoke green; 409 inline error verified |
| 6    | `moon run :check` green; smoke + registry-probe results recorded            |

## 8. Risks and mitigations

- **Package boundary regressions.** Mitigation: add a `tests/test_core_no_apps_imports.py` that
  fails if `sibyl_core.ai` imports anything from `apps.*` or `sibyl.services.*`.
- **PydanticAI minor-release churn.** Mitigation: `>=1.96.1,<1.97` constraint; explicit upgrade task
  per minor; `scripts/llm/verify_registry.py` catches model deprecations.
- **Multi-tenant footgun if anyone bolts org-scoping on later.** Mitigation: the response carries
  `scope: "instance_wide"`. UI banner is explicit. Per-org work has a separate plan and migration
  story; do not pretend the v0.10 settings are tenant-safe.
- **Stale clients after settings change.** Mitigation: `LLMConfigSource.invalidate` is called by
  `set_llm_setting`; worker re-resolves on the existing settings-cache TTL; cache key includes
  `loop_id`.
- **Custom-model rot.** Mitigation: UI explicitly warns; API returns `unverified_model` warning;
  custom values still flow through the same validators.
- **Validation conflation.** Mitigation: `check_provider_key` and `test_surface_config` are separate
  endpoints with classified error types.
- **Crawler extraction quality regression.** Mitigation: schema, field coverage, and type-drift
  gates with a frozen 20-chunk fixture, not just entity count.
- **Hook and reflect drift while out of scope.** Mitigation: explicit non-goal note in §3; ADR-style
  comment in each file pointing to this plan and the v0.11 follow-up.

## 9. Out of scope (v0.11+ follow-ups)

### 9.1 Future consumers (out of scope for this plan)

- **LLM-assisted reflection.** Today's `reflect.py` is heuristic. A future feature replaces or
  augments it with `Extractor[ReflectionCandidatesPayload]` on the substrate. Requires: golden
  fixtures, latency/cost gates, fallback to heuristic when LLM unavailable, policy-metadata
  preservation. Tracked separately.
- **Prompt-submit hook migration.** Hook stays on stdlib HTTP. Migration path is to add a small
  local API endpoint (`POST /api/llm/hook-query`) that uses the substrate server-side; the hook
  calls that instead of Anthropic directly. Loses the standalone fast path; needs benchmark.
- **Per-organization LLM configuration.** Requires extending `system_settings` keys with
  `organization_id`, migration of resolved-config callers, and UI per-org override panes.
- **Streaming synthesis to the web UI.** Backend support lands in `Generator.stream` (Task 6); the
  web surface is a separate UX project.
- **Tool use in `Generator`.** When a consumer needs retrieval-aware synthesis.
- **Scheduled registry refresh.** `verify_registry.py` exists; scheduling it (nightly job) is a
  v0.11 ops task.

### 9.2 Out of plan, not out of mind

- **Token budgeting and cross-surface rate-limit coordination.**
- **Telemetry export to Logfire** (kept off-path; we don't depend on it).
- **Per-call-site latency SLOs.**

### 9.3 Embedding migration to the AI substrate (v0.11+)

PydanticAI ships a real `Embedder` class with OpenAI, Google, Cohere, Bedrock, VoyageAI, and
SentenceTransformers support. The v0.10 substrate is shaped to host embeddings without restructure:
the `ai/` umbrella, the kind-agnostic `check_provider_key`, the `kind`-tagged `ModelEntry`, the
shared client cache, the `/api/settings/ai/` route prefix, and the parent "AI Settings" UI page.

Migration (deferred): replace `NativeEmbeddingProvider` and the crawler's `Embedder` client with
PydanticAI's `Embedder` under `sibyl_core.ai.embeddings`. The Graphiti-shaped
`sibyl_core/graph/gemini_embedder.py` stays untouched until the Graphiti exit removes the path.
Existing `SIBYL_GRAPH_EMBEDDING_*` env vars continue to work alongside new `SIBYL_AI_EMBEDDING_*`
vars during a deprecation window.

Why we hold for v0.10: current embedding code works, has its own cache layer, and migrating it adds
3-4 call sites plus a Graphiti-compat coordination beat that does not earn the slip.

## 10. Recommendation

Ship v4. The plan is internally aligned around the `sibyl_core.ai` umbrella, DB-backed
`apps/api/src/sibyl/ai/llm` config source, existing `require_settings_admin` policy, env-over-DB
precedence, and TTL-based worker refresh. The v4 AI-umbrella reshape keeps v0.10 focused on LLMs
while leaving an obvious v0.11 path for PydanticAI embeddings without renaming the substrate.

Wave 1 (Tasks 1-3) is parallel and unblocked. Stop iterating with cross-model review after this
pass: the remaining surface is implementation choices, not architecture.

## 11. Verification receipts

### 11.1 Repo facts checked on 2026-05-15

- `apps/api/src/sibyl/crawler/graph_integration.py` directly imports `AsyncAnthropic` and hardcodes
  `claude-haiku-4-5`.
- `apps/api/src/sibyl/generator/llm.py` directly constructs `anthropic.Anthropic()`.
- `apps/cli/src/sibyl_cli/data/hooks/user-prompt-submit.py` uses stdlib HTTPS with
  `claude-haiku-4-5-20251001`, not an SDK.
- `packages/python/sibyl-core/src/sibyl_core/tools/reflect.py` is heuristic/regex-based and imports
  no LLM SDK.
- `apps/api/src/sibyl/persistence/surreal/setup.py` defines `require_settings_admin` as owner or
  admin, with setup-mode bootstrap access.
- `apps/api/src/sibyl/services/settings.py` has `_CACHE_TTL = 60`, DB-first legacy key helpers, and
  `load_api_keys_from_db()` for non-substrate callers.

### 11.2 Primary-source checks

- PydanticAI: PyPI lists `pydantic-ai-slim` 1.96.1 released May 15, 2026, with `anthropic`,
  `google`, and `openai` extras. PydanticAI install docs confirm slim extras omit Logfire by
  default: <https://pypi.org/project/pydantic-ai-slim/> and
  <https://pydantic.dev/docs/ai/overview/install/>.
- PydanticAI models: docs confirm `AnthropicModel`, `GoogleModel`, `OpenAIResponsesModel`, and
  `Embedder` are current public surfaces: <https://pydantic.dev/docs/ai/models/anthropic/>,
  <https://pydantic.dev/docs/ai/models/google/>, <https://pydantic.dev/docs/ai/models/openai/>, and
  <https://pydantic.dev/docs/ai/guides/embeddings/>.
- Anthropic: Claude model overview lists `claude-haiku-4-5-20251001`, `claude-haiku-4-5`,
  `claude-sonnet-4-6`, and the $1/$5 and $3/$15 per-MTok pricing used in §5.4:
  <https://platform.claude.com/docs/en/about-claude/models/overview>.
- Google: Gemini API model and pricing docs list `gemini-3-flash-preview`, Gemini 3.1 Flash-Lite,
  structured output support, and the $0.50/$3.00 plus $0.25/$1.50 per-MTok rates used in §5.4:
  <https://ai.google.dev/gemini-api/docs/models> and
  <https://ai.google.dev/gemini-api/docs/pricing>.
- OpenAI: API pricing docs list `gpt-5.4-mini` and `gpt-5.4-nano` standard rates at $0.75/$4.50 and
  $0.20/$1.25 per MTok: <https://developers.openai.com/api/docs/pricing>.

## 12. Changelog

Driven by Codex cross-model review at xhigh effort over two passes, then a Bliss-prompted
AI-umbrella reshape.

### v3 → v4 (AI umbrella for future embedding integration)

Triggered by the observation that PydanticAI ships a real `Embedder` class. The substrate is
restructured to host embeddings as a future sibling without a v0.11 rename or re-layout. No
implementation work added to v0.10; structural-only changes.

- **§5.1 package layout:** `sibyl_core.llm/` → `sibyl_core.ai/` with `ai.llm/` submodule. Shared
  primitives (registry, providers, clients, validation, observability, errors, \_testing) live
  directly under `ai/`. `ai.embeddings/` reserved for v0.11. Same restructure on
  `apps/api/src/sibyl/ai/`.
- **§5.4 registry:** `ModelEntry` gains `kind: ModelKind` (LLM | EMBEDDING) plus
  `embedding_dimensions` and nullable LLM-only fields. v0.10 ships only `kind=LLM` entries. Lookup
  helpers accept a kind filter.
- **§5.5 settings API:** routes moved from `/api/settings/llm/*` to `/api/settings/ai/llm/*`.
  `/api/settings/ai/keys/{provider}/test` and `/api/settings/ai/models/{alias}/test` live under
  `/ai/` (kind-agnostic) because embeddings will reuse them. `GET /api/settings/ai/registry` takes
  an optional `kind` filter.
- **§5.6 web UI:** the existing `admin/ai/page.tsx` is reorganized into "Language Models" and
  "Embeddings" sibling sections under the existing "AI Settings" parent. Embedding controls move
  unchanged into the Embeddings section.
- **§6 Wave 1 tasks:** Task 1 creates `ai/` umbrella and `ai/llm/` submodule. Task 2 lives at
  `ai/llm/config.py`. Task 3 lives at `ai/registry.py` (renamed from `llm/models.py`).
- **§9.3:** new section reserving the embedding migration path with deprecation/rollout notes.

### v2 → v3 (post second-pass Codex review)

Codex confirmed 7 of 10 prior findings fully resolved by v2; the v3 patches close the remaining 3
partial items and the 2 new HIGHs.

- **§5.2 config shape:** `LLMConfig` is the raw shape; new `ResolvedLLMConfig` carries per-field
  `ConfigField[T]` metadata (`value`, `source`, `locked_by_env`, `env_var`). API responses use the
  resolved DTO so different fields can be env-locked independently. (Addresses new HIGH 1.)
- **§5.3 + §5.7 API key precedence:** keys are resolved via `LLMConfigSource.resolve()` rather than
  through `os.environ` mutation. `load_api_keys_from_db()` stays for non-substrate callers but the
  substrate does not depend on it. Removes the inconsistency between "env wins" and the existing
  DB-into-env copy. (Tightens BLOCKER 2 + HIGH 8 partial resolutions.)
- **§5.5 auth role:** aligned with the existing `require_settings_admin` policy (owner or admin)
  instead of inventing an owner-only break. Tightening is named as a v0.11 policy decision.
  (Addresses HIGH 3 partial resolution.)
- **§5.7 cross-process invalidation:** named the actual mechanism — in-process invalidation on the
  API side; TTL-based refresh on the worker side (existing `SettingsService._CacheEntry` TTL). No
  invented pubsub. Trade-off (up to 60s worker lag) is stated. (Addresses HIGH 8 partial.)
- **§5.5 + §6 Task 7 model availability:** new `check_model_availability(provider, model_id, key)`
  helper and `POST /api/settings/ai/models/{model_alias}/test` endpoint (kind-agnostic; mounted
  under `/ai/` not `/ai/llm/` because embeddings will reuse it). `verify_registry.py` probes
  per-entry rather than per-provider. (Addresses new HIGH 2.)
- **§6 Task 15 smoke gates:** reuses Task 11 gate functions instead of redefining looser ones; adds
  a name-quality gate. (Addresses MEDIUM 10 partial.)

### v1 → v2 (first-pass Codex review)

- **§5.1 Package layout:** split into `sibyl_core.llm` (pure) and `apps/api/src/sibyl/llm`
  (DB-backed). Added `LLMConfigSource` protocol + `EnvConfigSource` default. (Addresses BLOCKER 1.)
- **§5.3 Precedence:** changed to **env wins over DB**, with `locked_by_env` semantics and
  `409 LOCKED_BY_ENV` on write. UI lock state now matches the contract. (Addresses BLOCKER 2.)
- **§5.5 Multi-tenancy:** LLM settings are explicitly instance-wide and owner-only. UI banner names
  the scope. Per-org config moved to v0.11. (Addresses HIGH 3.)
- **§3, §4, §6:** dropped reflect from migration scope (it's heuristic, not an SDK call site).
  Dropped prompt hook from migration scope (stdlib-HTTP design is intentional). Both tracked as
  v0.11 follow-ups. (Addresses HIGH 4 + HIGH 5.)
- **§6 Task 1:** `pydantic-ai-slim[anthropic,google,openai]>=1.96.1,<1.97`. Avoids Logfire and full
  model deps. (Addresses HIGH 6.)
- **§5.4 Registry:** versioned `ModelEntry` records with `provider_model_id`,
  `pydantic_ai_model_class`, `last_verified_at`, `deprecated_after`, capability flags, cost source.
  Custom-model override allowed with explicit warning. (Addresses HIGH 7.)
- **§5.7 Provider lifetime:** scoped cache by `(loop_id, config_fingerprint)`; invalidation hook on
  settings writes; worker startup reload; no `os.environ` mutation. (Addresses HIGH 8.)
- **§6 Task 7:** split validation into `check_provider_key` (cheap text probe with classified
  results) and `test_surface_config` (full pipeline). (Addresses MEDIUM 9.)
- **§6 Task 11:** crawler schema is a container `ExtractedEntitiesPayload(entities: list[...])` with
  field validators. Smoke gates by schema success, field coverage, and type-distribution drift, not
  just entity count. (Addresses MEDIUM 10.)
- Added §9.1 explicit future-consumer reservations so the substrate is shaped for
  reflection/hook/per-org without preempting their migration plans.
