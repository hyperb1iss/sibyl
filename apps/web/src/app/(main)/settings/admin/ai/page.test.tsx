import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SettingInfo, SettingsResponse } from '@/lib/api';
import settingsRefusal from '@/test/fixtures/api/settings-locked-by-env-409.json';
import { render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useDeleteSetting: vi.fn(),
  useLLMRegistry: vi.fn(),
  useLLMSettings: vi.fn(),
  useSettings: vi.fn(),
  useUpdateSettings: vi.fn(),
  useValidateApiKeys: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);
vi.mock('sonner', () => ({ toast }));

import AIServicesPage from './page';

function plain(value: string): SettingInfo {
  return { configured: true, source: 'database', is_secret: false, masked: null, value };
}

function ownedBy(envVar: string, value: string): SettingInfo {
  return {
    configured: true,
    source: 'environment',
    is_secret: false,
    masked: null,
    value,
    locked_by_env: true,
    env_var: envVar,
  };
}

/** What a default Helm install reports: the document model and size are pinned. */
function helmDefaults(): SettingsResponse {
  return {
    settings: {
      openai_api_key: {
        configured: true,
        source: 'environment',
        is_secret: true,
        masked: 'sk-...7890',
        value: null,
        locked_by_env: true,
        env_var: 'SIBYL_OPENAI_API_KEY',
      },
      embedding_provider: plain('openai'),
      embedding_model: ownedBy('SIBYL_EMBEDDING_MODEL', 'text-embedding-3-small'),
      embedding_dimensions: ownedBy('SIBYL_EMBEDDING_DIMENSIONS', '1536'),
      graph_embedding_provider: plain('openai'),
      graph_embedding_model: plain('text-embedding-3-small'),
      graph_embedding_dimensions: plain('1024'),
    },
  };
}

describe('AIServicesPage embeddings', () => {
  let mutateAsync: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    toast.success.mockReset();
    toast.error.mockReset();
    toast.info.mockReset();
    mutateAsync = vi.fn().mockResolvedValue({ updated: [], validation: {} });
    hooks.useSettings.mockReturnValue({ data: helmDefaults(), isLoading: false });
    hooks.useLLMSettings.mockReturnValue({ data: undefined, isLoading: true });
    hooks.useLLMRegistry.mockReturnValue({ data: { entries: [] } });
    hooks.useValidateApiKeys.mockReturnValue({
      data: undefined,
      refetch: vi.fn(),
      isLoading: false,
    });
    hooks.useUpdateSettings.mockReturnValue({ mutateAsync, isPending: false, data: undefined });
    hooks.useDeleteSetting.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  });

  it('shows settings the deployment owns as read-only and names their variables', () => {
    render(<AIServicesPage />);

    const model = screen.getByLabelText('Document embeddings model');
    const dimensions = screen.getByLabelText('Document embeddings dimensions');
    expect(model).toBeDisabled();
    expect(model).toHaveValue('text-embedding-3-small');
    expect(dimensions).toBeDisabled();
    expect(dimensions).toHaveValue(1536);
    expect(screen.getByText('SIBYL_EMBEDDING_MODEL')).toBeInTheDocument();
    expect(screen.getByText('SIBYL_EMBEDDING_DIMENSIONS')).toBeInTheDocument();
    expect(screen.getByLabelText('Graph embeddings model')).toBeEnabled();
    expect(screen.getByLabelText('Graph embeddings dimensions')).toBeEnabled();
  });

  it('saves only the fields that changed, never a deployment-owned one', async () => {
    const { user } = render(<AIServicesPage />);

    const graphModel = screen.getByLabelText('Graph embeddings model');
    await user.clear(graphModel);
    await user.type(graphModel, 'text-embedding-3-large');
    await user.click(screen.getByRole('button', { name: /save embeddings/i }));

    expect(mutateAsync).toHaveBeenCalledTimes(1);
    expect(mutateAsync).toHaveBeenCalledWith({ graph_embedding_model: 'text-embedding-3-large' });
    expect(toast.success).toHaveBeenCalledWith('Embedding configuration saved');
  });

  it('does not send a request when nothing changed', async () => {
    const { user } = render(<AIServicesPage />);

    await user.click(screen.getByRole('button', { name: /save embeddings/i }));

    expect(mutateAsync).not.toHaveBeenCalled();
    expect(toast.info).toHaveBeenCalledWith('No embedding changes to save');
  });

  it('names the deployment variable when the server refuses a change', async () => {
    // The exact body the API sends, pinned by apps/api/tests/test_locked_by_env_contract.py.
    mutateAsync.mockRejectedValue(new Error(JSON.stringify(settingsRefusal)));
    const { user } = render(<AIServicesPage />);

    const graphModel = screen.getByLabelText('Graph embeddings model');
    await user.clear(graphModel);
    await user.type(graphModel, 'gemini-embedding-2');
    await user.click(screen.getByRole('button', { name: /save embeddings/i }));

    expect(toast.error).toHaveBeenCalledWith(
      'Set by the deployment (SIBYL_EMBEDDING_MODEL); change it there'
    );
  });

  it('offers no edit for a provider key the deployment owns', () => {
    render(<AIServicesPage />);

    expect(screen.getByText('SIBYL_OPENAI_API_KEY')).toBeInTheDocument();
    // Anthropic and Gemini are not configured, so they still offer Configure; OpenAI does not.
    expect(screen.getAllByRole('button', { name: 'Configure' })).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Update' })).not.toBeInTheDocument();
  });
});
