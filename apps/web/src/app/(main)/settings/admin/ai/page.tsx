'use client';

import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import {
  Check,
  Database,
  EditPencil,
  Flash,
  Globe,
  InfoCircle,
  RefreshDouble,
  Settings as SettingsIcon,
  Trash,
  WarningTriangle,
  Xmark,
} from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import type { SettingInfo, SettingsResponse, UpdateSettingsRequest } from '@/lib/api';
import { useDeleteSetting, useSettings, useUpdateSettings, useValidateApiKeys } from '@/lib/hooks';

type ApiKeySettingKey = 'openai_api_key' | 'anthropic_api_key' | 'gemini_api_key';
type EmbeddingProvider = 'openai' | 'gemini';

interface EmbeddingConfigState {
  embedding_provider: EmbeddingProvider;
  embedding_model: string;
  embedding_dimensions: string;
  graph_embedding_provider: EmbeddingProvider;
  graph_embedding_model: string;
  graph_embedding_dimensions: string;
}

const OPENAI_EMBEDDING_MODEL = 'text-embedding-3-small';
const GEMINI_EMBEDDING_MODEL = 'gemini-embedding-2';

const DEFAULT_EMBEDDING_CONFIG: EmbeddingConfigState = {
  embedding_provider: 'openai',
  embedding_model: OPENAI_EMBEDDING_MODEL,
  embedding_dimensions: '1536',
  graph_embedding_provider: 'openai',
  graph_embedding_model: OPENAI_EMBEDDING_MODEL,
  graph_embedding_dimensions: '1024',
};

function defaultModelForProvider(provider: EmbeddingProvider): string {
  return provider === 'gemini' ? GEMINI_EMBEDDING_MODEL : OPENAI_EMBEDDING_MODEL;
}

function parseProvider(value: string | null | undefined, fallback: EmbeddingProvider) {
  return value === 'gemini' || value === 'openai' ? value : fallback;
}

function readSetting(
  settings: SettingsResponse | undefined,
  key: keyof EmbeddingConfigState,
  fallback: string
) {
  return settings?.settings?.[key]?.value ?? fallback;
}

function keyDisplayName(key: ApiKeySettingKey) {
  if (key === 'openai_api_key') return 'OpenAI';
  if (key === 'anthropic_api_key') return 'Anthropic';
  return 'Gemini';
}

function keyPlaceholder(key: ApiKeySettingKey) {
  if (key === 'openai_api_key') return 'sk-...';
  if (key === 'anthropic_api_key') return 'sk-ant-...';
  return 'AIza...';
}

interface ApiKeyCardProps {
  name: string;
  description: string;
  setting: SettingInfo | undefined;
  valid: boolean | null | undefined;
  error?: string | null;
  isValidating: boolean;
  onEdit: () => void;
  onDelete: () => void;
  isDeleting: boolean;
}

function ApiKeyCard({
  name,
  description,
  setting,
  valid,
  error,
  isValidating,
  onEdit,
  onDelete,
  isDeleting,
}: ApiKeyCardProps) {
  const configured = setting?.configured ?? false;
  const source = setting?.source ?? 'none';

  let statusIcon: React.ReactNode;
  let statusColor: string;
  let statusBg: string;
  let statusText: string;

  if (!configured) {
    statusIcon = <Xmark width={16} height={16} />;
    statusColor = 'text-sc-red';
    statusBg = 'bg-sc-red/10 border-sc-red/20';
    statusText = 'Not configured';
  } else if (isValidating) {
    statusIcon = <Spinner size="sm" color="cyan" />;
    statusColor = 'text-sc-cyan';
    statusBg = 'bg-sc-cyan/10 border-sc-cyan/20';
    statusText = 'Validating...';
  } else if (valid === true) {
    statusIcon = <Check width={16} height={16} />;
    statusColor = 'text-sc-green';
    statusBg = 'bg-sc-green/10 border-sc-green/20';
    statusText = 'Active';
  } else if (valid === false) {
    statusIcon = <WarningTriangle width={16} height={16} />;
    statusColor = 'text-sc-red';
    statusBg = 'bg-sc-red/10 border-sc-red/20';
    statusText = error || 'Invalid';
  } else {
    statusIcon = <RefreshDouble width={16} height={16} />;
    statusColor = 'text-sc-fg-muted';
    statusBg = 'bg-sc-bg-highlight border-sc-fg-subtle/10';
    statusText = 'Not validated';
  }

  // Source indicator
  const SourceIcon = source === 'database' ? Database : Globe;
  const sourceLabel =
    source === 'database' ? 'Database' : source === 'environment' ? 'Environment' : '';

  return (
    <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <h3 className="font-semibold text-sc-fg-primary mb-1">{name}</h3>
          <p className="text-sm text-sc-fg-muted">{description}</p>
        </div>
        <div
          className={`flex items-center gap-2 px-3 py-1.5 rounded-full border text-xs font-medium ${statusBg} ${statusColor}`}
        >
          {statusIcon}
          <span>{statusText}</span>
        </div>
      </div>

      {/* Source and masked value */}
      {configured && (
        <div className="mt-4 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-1.5 text-xs text-sc-fg-muted">
              <SourceIcon width={14} height={14} />
              <span>{sourceLabel}</span>
            </div>
            {setting?.masked && (
              <code className="text-xs font-mono text-sc-fg-secondary bg-sc-bg-highlight px-2 py-1 rounded">
                {setting.masked}
              </code>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onEdit}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium text-sc-fg-secondary hover:bg-sc-bg-highlight transition-colors"
            >
              <EditPencil width={14} height={14} />
              Update
            </button>
            {source === 'database' && (
              <button
                type="button"
                onClick={onDelete}
                disabled={isDeleting}
                className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium text-sc-red hover:bg-sc-red/10 transition-colors disabled:opacity-50"
              >
                {isDeleting ? (
                  <Spinner size="sm" color="current" />
                ) : (
                  <Trash width={14} height={14} />
                )}
                Remove
              </button>
            )}
          </div>
        </div>
      )}

      {/* Not configured - show edit button */}
      {!configured && (
        <div className="mt-4">
          <button
            type="button"
            onClick={onEdit}
            className="flex items-center gap-2 px-4 py-2 rounded-lg bg-sc-cyan text-sc-bg-dark text-sm font-medium hover:bg-sc-cyan/90 transition-colors"
          >
            <EditPencil width={14} height={14} />
            Configure API Key
          </button>
        </div>
      )}

      {/* Error details */}
      {valid === false && error && (
        <div className="mt-4 p-3 rounded-lg bg-sc-red/5 border border-sc-red/10">
          <p className="text-xs text-sc-red">{error}</p>
        </div>
      )}
    </div>
  );
}

interface EditModalProps {
  name: string;
  placeholder: string;
  currentMasked: string | null | undefined;
  onClose: () => void;
  onSave: (value: string) => Promise<void>;
  isSaving: boolean;
}

function EditModal({
  name,
  placeholder,
  currentMasked,
  onClose,
  onSave,
  isSaving,
}: EditModalProps) {
  const [value, setValue] = useState('');
  const [showValue, setShowValue] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!value.trim()) return;
    await onSave(value.trim());
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

      {/* Modal */}
      <div className="relative w-full max-w-md bg-sc-bg-dark rounded-xl border border-sc-fg-subtle/20 shadow-2xl">
        <div className="p-6">
          <h3 className="text-lg font-semibold text-sc-fg-primary mb-2">Update {name} API Key</h3>
          <p className="text-sm text-sc-fg-muted mb-4">
            {currentMasked
              ? `Current key: ${currentMasked}. Enter a new key to replace it.`
              : 'Enter your API key to configure this service.'}
          </p>

          <form onSubmit={handleSubmit}>
            <div className="relative mb-4">
              <input
                type={showValue ? 'text' : 'password'}
                value={value}
                onChange={e => setValue(e.target.value)}
                placeholder={placeholder}
                className="w-full px-3 py-2.5 pr-10 rounded-lg bg-sc-bg-base border border-sc-fg-subtle/20 text-sc-fg-primary text-sm placeholder:text-sc-fg-subtle focus:outline-none focus:border-sc-cyan/50 focus:ring-1 focus:ring-sc-cyan/20"
              />
              <button
                type="button"
                onClick={() => setShowValue(!showValue)}
                className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-sc-fg-subtle hover:text-sc-fg-secondary transition-colors"
              >
                {showValue ? (
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    role="img"
                  >
                    <title>Hide</title>
                    <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                    <line x1="1" y1="1" x2="23" y2="23" />
                  </svg>
                ) : (
                  <svg
                    xmlns="http://www.w3.org/2000/svg"
                    width="16"
                    height="16"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    role="img"
                  >
                    <title>Show</title>
                    <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" />
                    <circle cx="12" cy="12" r="3" />
                  </svg>
                )}
              </button>
            </div>

            <div className="flex gap-3">
              <button
                type="button"
                onClick={onClose}
                className="flex-1 py-2.5 px-4 rounded-lg border border-sc-fg-subtle/20 text-sc-fg-secondary font-medium text-sm transition-colors hover:bg-sc-bg-base"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={!value.trim() || isSaving}
                className="flex-1 py-2.5 px-4 rounded-lg bg-sc-cyan text-sc-bg-dark font-medium text-sm transition-all hover:bg-sc-cyan/90 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
              >
                {isSaving ? (
                  <>
                    <Spinner size="sm" color="current" />
                    Saving...
                  </>
                ) : (
                  'Save'
                )}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}

interface EmbeddingConfigPanelProps {
  title: string;
  description: string;
  provider: EmbeddingProvider;
  model: string;
  dimensions: string;
  onProviderChange: (provider: EmbeddingProvider) => void;
  onModelChange: (model: string) => void;
  onDimensionsChange: (dimensions: string) => void;
}

function EmbeddingConfigPanel({
  title,
  description,
  provider,
  model,
  dimensions,
  onProviderChange,
  onModelChange,
  onDimensionsChange,
}: EmbeddingConfigPanelProps) {
  return (
    <div className="rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-dark/40 p-4">
      <div className="mb-4">
        <h4 className="font-medium text-sc-fg-primary">{title}</h4>
        <p className="text-sm text-sc-fg-muted">{description}</p>
      </div>

      <div className="grid gap-4 md:grid-cols-[160px_1fr_140px]">
        <label className="block">
          <span className="mb-1.5 block text-xs font-medium uppercase tracking-[0.08em] text-sc-fg-subtle">
            Provider
          </span>
          <select
            value={provider}
            onChange={e => onProviderChange(e.target.value as EmbeddingProvider)}
            className="w-full rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base px-3 py-2.5 text-sm text-sc-fg-primary focus:border-sc-cyan/50 focus:outline-none focus:ring-1 focus:ring-sc-cyan/20"
          >
            <option value="openai">OpenAI</option>
            <option value="gemini">Gemini</option>
          </select>
        </label>

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium uppercase tracking-[0.08em] text-sc-fg-subtle">
            Model
          </span>
          <input
            type="text"
            value={model}
            onChange={e => onModelChange(e.target.value)}
            className="w-full rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base px-3 py-2.5 text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan/50 focus:outline-none focus:ring-1 focus:ring-sc-cyan/20"
          />
        </label>

        <label className="block">
          <span className="mb-1.5 block text-xs font-medium uppercase tracking-[0.08em] text-sc-fg-subtle">
            Dimensions
          </span>
          <input
            type="number"
            min={128}
            max={3072}
            step={1}
            value={dimensions}
            onChange={e => onDimensionsChange(e.target.value)}
            className="w-full rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-base px-3 py-2.5 text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan/50 focus:outline-none focus:ring-1 focus:ring-sc-cyan/20"
          />
        </label>
      </div>
    </div>
  );
}

export default function AIServicesPage() {
  const { data: settings, isLoading } = useSettings();
  const {
    data: validation,
    refetch: revalidate,
    isLoading: isValidateLoading,
  } = useValidateApiKeys({ enabled: false });
  const updateSettings = useUpdateSettings();
  const deleteSetting = useDeleteSetting();

  const [isValidating, setIsValidating] = useState(false);
  const [editingKey, setEditingKey] = useState<ApiKeySettingKey | null>(null);
  const [deletingKey, setDeletingKey] = useState<string | null>(null);
  const [embeddingConfig, setEmbeddingConfig] = useState(DEFAULT_EMBEDDING_CONFIG);

  useEffect(() => {
    if (!settings) return;

    const embeddingProvider = parseProvider(
      readSetting(settings, 'embedding_provider', DEFAULT_EMBEDDING_CONFIG.embedding_provider),
      DEFAULT_EMBEDDING_CONFIG.embedding_provider
    );
    const graphProvider = parseProvider(
      readSetting(
        settings,
        'graph_embedding_provider',
        DEFAULT_EMBEDDING_CONFIG.graph_embedding_provider
      ),
      DEFAULT_EMBEDDING_CONFIG.graph_embedding_provider
    );

    setEmbeddingConfig({
      embedding_provider: embeddingProvider,
      embedding_model: readSetting(
        settings,
        'embedding_model',
        defaultModelForProvider(embeddingProvider)
      ),
      embedding_dimensions: readSetting(
        settings,
        'embedding_dimensions',
        DEFAULT_EMBEDDING_CONFIG.embedding_dimensions
      ),
      graph_embedding_provider: graphProvider,
      graph_embedding_model: readSetting(
        settings,
        'graph_embedding_model',
        defaultModelForProvider(graphProvider)
      ),
      graph_embedding_dimensions: readSetting(
        settings,
        'graph_embedding_dimensions',
        DEFAULT_EMBEDDING_CONFIG.graph_embedding_dimensions
      ),
    });
  }, [settings]);

  const handleValidate = async () => {
    setIsValidating(true);
    try {
      const result = await revalidate();
      const embeddingValid = result.data?.openai_valid || result.data?.gemini_valid;
      if (embeddingValid && result.data?.anthropic_valid) {
        toast.success('All API keys validated successfully');
      } else {
        toast.error('Some API keys failed validation');
      }
    } catch {
      toast.error('Failed to validate API keys');
    } finally {
      setIsValidating(false);
    }
  };

  const handleSaveKey = useCallback(
    async (key: ApiKeySettingKey, value: string) => {
      try {
        const result = await updateSettings.mutateAsync({ [key]: value });
        const keyResult = result.validation[key];
        if (keyResult?.valid) {
          toast.success(`${keyDisplayName(key)} API key saved and validated`);
          setEditingKey(null);
        } else {
          toast.error(keyResult?.error || 'API key validation failed');
        }
      } catch {
        toast.error('Failed to save API key');
      }
    },
    [updateSettings]
  );

  const handleDeleteKey = useCallback(
    async (key: string) => {
      setDeletingKey(key);
      try {
        await deleteSetting.mutateAsync(key);
        toast.success('API key removed from database');
      } catch {
        toast.error('Failed to remove API key');
      } finally {
        setDeletingKey(null);
      }
    },
    [deleteSetting]
  );

  const handleSaveEmbeddingConfig = useCallback(async () => {
    const embeddingDimensions = Number.parseInt(embeddingConfig.embedding_dimensions, 10);
    const graphDimensions = Number.parseInt(embeddingConfig.graph_embedding_dimensions, 10);

    if (
      !Number.isInteger(embeddingDimensions) ||
      embeddingDimensions < 128 ||
      embeddingDimensions > 3072 ||
      !Number.isInteger(graphDimensions) ||
      graphDimensions < 128 ||
      graphDimensions > 3072
    ) {
      toast.error('Embedding dimensions must be whole numbers from 128 to 3072');
      return;
    }

    const request: UpdateSettingsRequest = {
      embedding_provider: embeddingConfig.embedding_provider,
      embedding_model: embeddingConfig.embedding_model.trim(),
      embedding_dimensions: embeddingDimensions,
      graph_embedding_provider: embeddingConfig.graph_embedding_provider,
      graph_embedding_model: embeddingConfig.graph_embedding_model.trim(),
      graph_embedding_dimensions: graphDimensions,
    };

    if (!request.embedding_model || !request.graph_embedding_model) {
      toast.error('Embedding models cannot be blank');
      return;
    }

    try {
      await updateSettings.mutateAsync(request);
      toast.success('Embedding configuration saved');
    } catch {
      toast.error('Failed to save embedding configuration');
    }
  }, [embeddingConfig, updateSettings]);

  // Use validation results if available, otherwise fall back to update results, then settings
  const openaiValid =
    validation?.openai_valid ?? updateSettings.data?.validation?.openai_api_key?.valid ?? null;
  const anthropicValid =
    validation?.anthropic_valid ??
    updateSettings.data?.validation?.anthropic_api_key?.valid ??
    null;
  const geminiValid =
    validation?.gemini_valid ?? updateSettings.data?.validation?.gemini_api_key?.valid ?? null;

  const updateEmbeddingConfig = <Key extends keyof EmbeddingConfigState>(
    key: Key,
    value: EmbeddingConfigState[Key]
  ) => {
    setEmbeddingConfig(current => ({ ...current, [key]: value }));
  };

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
          <div className="flex items-center gap-3 mb-4">
            <Flash width={20} height={20} className="text-sc-yellow" />
            <h2 className="text-lg font-semibold text-sc-fg-primary">AI Services</h2>
          </div>
          <div className="flex items-center justify-center py-8">
            <Spinner size="md" color="purple" />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-3">
            <Flash width={20} height={20} className="text-sc-yellow" />
            <h2 className="text-lg font-semibold text-sc-fg-primary">AI Services</h2>
          </div>
          <button
            type="button"
            onClick={handleValidate}
            disabled={isValidating || isValidateLoading}
            className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-sc-bg-highlight border border-sc-fg-subtle/20 text-sm font-medium text-sc-fg-secondary hover:bg-sc-bg-base transition-colors disabled:opacity-50"
          >
            {isValidating || isValidateLoading ? (
              <>
                <Spinner size="sm" color="current" />
                Validating...
              </>
            ) : (
              <>
                <RefreshDouble width={14} height={14} />
                Validate All
              </>
            )}
          </button>
        </div>
        <p className="text-sc-fg-muted">
          Sibyl uses Anthropic for entity extraction and either OpenAI or Gemini for embeddings. API
          keys can be configured here or via environment variables.
        </p>
      </div>

      {/* API Key Cards */}
      <div className="grid gap-4">
        <ApiKeyCard
          name="OpenAI"
          description="Powers semantic search when OpenAI is selected as an embedding provider."
          setting={settings?.settings?.openai_api_key}
          valid={openaiValid}
          error={validation?.openai_error ?? updateSettings.data?.validation?.openai_api_key?.error}
          isValidating={isValidating}
          onEdit={() => setEditingKey('openai_api_key')}
          onDelete={() => handleDeleteKey('openai_api_key')}
          isDeleting={deletingKey === 'openai_api_key'}
        />
        <ApiKeyCard
          name="Gemini"
          description="Powers semantic search when Gemini is selected as an embedding provider."
          setting={settings?.settings?.gemini_api_key}
          valid={geminiValid}
          error={validation?.gemini_error ?? updateSettings.data?.validation?.gemini_api_key?.error}
          isValidating={isValidating}
          onEdit={() => setEditingKey('gemini_api_key')}
          onDelete={() => handleDeleteKey('gemini_api_key')}
          isDeleting={deletingKey === 'gemini_api_key'}
        />
        <ApiKeyCard
          name="Anthropic"
          description="Powers entity extraction workflows. Uses Claude Haiku for extraction."
          setting={settings?.settings?.anthropic_api_key}
          valid={anthropicValid}
          error={
            validation?.anthropic_error ?? updateSettings.data?.validation?.anthropic_api_key?.error
          }
          isValidating={isValidating}
          onEdit={() => setEditingKey('anthropic_api_key')}
          onDelete={() => handleDeleteKey('anthropic_api_key')}
          isDeleting={deletingKey === 'anthropic_api_key'}
        />
      </div>

      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="mb-5 flex items-start justify-between gap-4">
          <div className="flex items-start gap-3">
            <SettingsIcon width={20} height={20} className="mt-0.5 text-sc-cyan" />
            <div>
              <h3 className="font-semibold text-sc-fg-primary">Embedding Configuration</h3>
              <p className="mt-1 text-sm text-sc-fg-muted">
                Choose the provider, model, and vector dimensions used for document chunks and graph
                memory.
              </p>
            </div>
          </div>
          <button
            type="button"
            onClick={handleSaveEmbeddingConfig}
            disabled={updateSettings.isPending}
            className="flex items-center gap-2 rounded-lg bg-sc-cyan px-3 py-2 text-sm font-medium text-sc-bg-dark transition-colors hover:bg-sc-cyan/90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {updateSettings.isPending ? (
              <>
                <Spinner size="sm" color="current" />
                Saving...
              </>
            ) : (
              'Save Config'
            )}
          </button>
        </div>

        <div className="mb-5 rounded-lg border border-sc-yellow/20 bg-sc-yellow/10 p-4">
          <div className="flex gap-3">
            <InfoCircle width={18} height={18} className="mt-0.5 flex-shrink-0 text-sc-yellow" />
            <p className="text-sm text-sc-fg-secondary">
              Changing provider, model, or dimensions changes the vector space. Re-crawl document
              sources and rebuild graph indexes before trusting mixed search results.
            </p>
          </div>
        </div>

        <div className="grid gap-4">
          <EmbeddingConfigPanel
            title="Document Embeddings"
            description="Used by crawled sources, chunks, and semantic document search."
            provider={embeddingConfig.embedding_provider}
            model={embeddingConfig.embedding_model}
            dimensions={embeddingConfig.embedding_dimensions}
            onProviderChange={provider =>
              setEmbeddingConfig(current => ({
                ...current,
                embedding_provider: provider,
                embedding_model: defaultModelForProvider(provider),
              }))
            }
            onModelChange={model => updateEmbeddingConfig('embedding_model', model)}
            onDimensionsChange={dimensions =>
              updateEmbeddingConfig('embedding_dimensions', dimensions)
            }
          />
          <EmbeddingConfigPanel
            title="Graph Embeddings"
            description="Used by Graphiti entities, relationships, and graph similarity search."
            provider={embeddingConfig.graph_embedding_provider}
            model={embeddingConfig.graph_embedding_model}
            dimensions={embeddingConfig.graph_embedding_dimensions}
            onProviderChange={provider =>
              setEmbeddingConfig(current => ({
                ...current,
                graph_embedding_provider: provider,
                graph_embedding_model: defaultModelForProvider(provider),
              }))
            }
            onModelChange={model => updateEmbeddingConfig('graph_embedding_model', model)}
            onDimensionsChange={dimensions =>
              updateEmbeddingConfig('graph_embedding_dimensions', dimensions)
            }
          />
        </div>
      </div>

      {/* Configuration Info */}
      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <h3 className="font-semibold text-sc-fg-primary mb-3">Configuration Priority</h3>
        <div className="space-y-3 text-sm text-sc-fg-muted">
          <p>API keys are loaded in this order of priority:</p>
          <ol className="list-decimal list-inside space-y-2 pl-2">
            <li>
              <span className="inline-flex items-center gap-1.5">
                <Database width={14} height={14} className="text-sc-cyan" />
                <strong className="text-sc-fg-secondary">Database</strong> - Keys saved via this UI
                (encrypted)
              </span>
            </li>
            <li>
              <span className="inline-flex items-center gap-1.5">
                <Globe width={14} height={14} className="text-sc-purple" />
                <strong className="text-sc-fg-secondary">Environment</strong> -
                SIBYL_OPENAI_API_KEY, SIBYL_ANTHROPIC_API_KEY, SIBYL_GEMINI_API_KEY
              </span>
            </li>
          </ol>
          <p className="mt-4 text-xs">
            Database values take precedence. Gemini also checks GEMINI_API_KEY and GOOGLE_API_KEY.
            Provider settings can be supplied with SIBYL_EMBEDDING_PROVIDER and
            SIBYL_GRAPH_EMBEDDING_PROVIDER.
          </p>
        </div>
      </div>

      {/* Edit Modal */}
      {editingKey && (
        <EditModal
          name={keyDisplayName(editingKey)}
          placeholder={keyPlaceholder(editingKey)}
          currentMasked={settings?.settings?.[editingKey]?.masked}
          onClose={() => setEditingKey(null)}
          onSave={value => handleSaveKey(editingKey, value)}
          isSaving={updateSettings.isPending}
        />
      )}
    </div>
  );
}
