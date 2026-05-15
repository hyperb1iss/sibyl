import { beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  AIModelEntry,
  LLMConfigSource,
  LLMProviderName,
  LLMSurface,
  LLMSurfaceSettings,
} from '@/lib/api';
import { render, screen } from '@/test/utils';
import { LLMSurfaceRow } from './llm-surface-row';

const hooks = vi.hoisted(() => ({
  useTestLLMSurface: vi.fn(),
  useUpdateLLMSurface: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);
vi.mock('sonner', () => ({ toast }));

function valueField(value: string | number | null, source: LLMConfigSource = 'default') {
  return {
    value,
    source,
    locked_by_env: source === 'env',
    env_var: source === 'env' ? 'SIBYL_LLM_CRAWLER_MODEL' : null,
  };
}

function secretField(configured = true, source: LLMConfigSource = 'db') {
  return {
    configured,
    source,
    locked_by_env: source === 'env',
    env_var: source === 'env' ? 'SIBYL_ANTHROPIC_API_KEY' : null,
    masked: configured ? 'sk-...test' : null,
  };
}

function surface(
  id: LLMSurface,
  provider: LLMProviderName,
  model: string,
  modelSource: LLMConfigSource = 'default'
): LLMSurfaceSettings {
  return {
    surface: id,
    provider: valueField(provider),
    model: valueField(model, modelSource),
    temperature: valueField(0),
    max_tokens: valueField(null),
    timeout_seconds: valueField(60),
    api_key: secretField(true),
    cached_at: null,
  };
}

function model(alias: string, provider: LLMProviderName, useCases: string[]): AIModelEntry {
  return {
    alias,
    snapshot: `${alias}-snapshot`,
    kind: 'llm',
    provider,
    provider_model_id: `${alias}-provider`,
    pydantic_ai_model_class: 'TestModel',
    use_cases: useCases,
    capabilities: ['structured_output'],
    max_output_tokens: 8192,
    embedding_dimensions: null,
    default_temperature: 0,
    input_cost_per_mtok_usd: 1,
    output_cost_per_mtok_usd: 5,
    cost_source_url: 'https://example.test',
    last_verified_at: '2026-05-15T00:00:00Z',
    deprecated_after: null,
    warning: null,
  };
}

const entries = [
  model('claude-haiku-4-5', 'anthropic', ['default', 'extraction']),
  model('claude-sonnet-4-6', 'anthropic', ['synthesis']),
  model('gemini-3-flash', 'gemini', ['extraction']),
];

function renderCrawler(testSurface = surface('crawler', 'anthropic', 'claude-haiku-4-5')) {
  return render(
    <LLMSurfaceRow
      id="crawler"
      label="Crawler"
      description="Structured entity extraction for crawled documents."
      useCase="extraction"
      surface={testSurface}
      entries={entries}
    />
  );
}

describe('LLMSurfaceRow', () => {
  let updateMutateAsync: ReturnType<typeof vi.fn>;
  let testMutateAsync: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    updateMutateAsync = vi.fn().mockResolvedValue({
      warning: null,
      surface: surface('crawler', 'gemini', 'gemini-3-flash'),
    });
    testMutateAsync = vi.fn().mockResolvedValue({
      surface: 'crawler',
      provider: 'anthropic',
      model: 'claude-haiku-4-5',
      status: 'valid',
      valid: true,
      latency_ms: 42,
      parsed_output: { ok: true, summary: 'ready' },
      input_tokens: 3,
      output_tokens: 4,
      error: null,
    });

    hooks.useUpdateLLMSurface.mockReturnValue({
      mutateAsync: updateMutateAsync,
      isPending: false,
    });
    hooks.useTestLLMSurface.mockReturnValue({
      mutateAsync: testMutateAsync,
      isPending: false,
    });
    toast.error.mockReset();
    toast.success.mockReset();
    toast.warning.mockReset();
  });

  it('renders a configured language model surface', () => {
    renderCrawler();

    expect(screen.getByText('Crawler')).toBeInTheDocument();
    expect(screen.getByText('Key ready')).toBeInTheDocument();
    expect(
      screen.getByText('Structured entity extraction for crawled documents.')
    ).toBeInTheDocument();
  });

  it('saves changed routing through the LLM settings mutation', async () => {
    const { user } = renderCrawler();

    await user.selectOptions(screen.getAllByRole('combobox')[0], 'gemini');
    await user.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(updateMutateAsync).toHaveBeenCalledWith({
      surface: 'crawler',
      request: {
        provider: 'gemini',
        model: 'gemini-3-flash',
        temperature: 0,
        timeout_seconds: 60,
      },
    });
  });

  it('runs a surface test and renders latency plus token counts', async () => {
    const { user } = renderCrawler();

    await user.click(screen.getByRole('button', { name: 'Test Crawler' }));

    expect(testMutateAsync).toHaveBeenCalledWith('crawler');
    expect(await screen.findByText('Test passed')).toBeInTheDocument();
    expect(screen.getByText('42 ms')).toBeInTheDocument();
    expect(screen.getByText('3 in / 4 out')).toBeInTheDocument();
  });

  it('disables environment-locked fields', () => {
    renderCrawler(surface('crawler', 'anthropic', 'claude-haiku-4-5', 'env'));

    expect(screen.getAllByRole('combobox')[1]).toBeDisabled();
  });
});
