'use client';

import type { FormEvent, ReactNode } from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { LLMSurfaceRow } from '@/components/settings/llm-surface-row';
import {
  HelpNote,
  SettingsField,
  SettingsPageHeader,
  SettingsSection,
  SettingsSectionSkeleton,
  StatusPill,
} from '@/components/settings/primitives';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  Check,
  Database,
  EditPencil,
  Eye,
  Flash,
  Globe,
  InfoCircle,
  RefreshDouble,
  Trash,
  WarningTriangle,
  Xmark,
} from '@/components/ui/icons';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Spinner } from '@/components/ui/spinner';
import { Tooltip } from '@/components/ui/tooltip';
import type {
  AIModelEntry,
  LLMSurface,
  SettingInfo,
  SettingsResponse,
  UpdateSettingsRequest,
} from '@/lib/api';
import {
  useDeleteSetting,
  useLLMRegistry,
  useLLMSettings,
  useSettings,
  useUpdateSettings,
  useValidateApiKeys,
} from '@/lib/hooks';

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

const SURFACES: Array<{
  id: LLMSurface;
  label: string;
  description: string;
  useCase: string;
}> = [
  {
    id: 'default',
    label: 'Default',
    description: 'Fallback surface for shared LLM consumers.',
    useCase: 'default',
  },
  {
    id: 'crawler',
    label: 'Crawler',
    description: 'Structured entity extraction for crawled documents.',
    useCase: 'extraction',
  },
  {
    id: 'memory',
    label: 'Memory',
    description: 'Entity extraction from live memory and session prose.',
    useCase: 'extraction',
  },
  {
    id: 'synthesis',
    label: 'Synthesis',
    description: 'Long-form generation and memory synthesis.',
    useCase: 'synthesis',
  },
];

const API_KEYS: Array<{
  key: ApiKeySettingKey;
  name: string;
  description: string;
  placeholder: string;
}> = [
  {
    key: 'openai_api_key',
    name: 'OpenAI',
    description: 'Used when OpenAI is the selected embedding or LLM provider.',
    placeholder: 'sk-...',
  },
  {
    key: 'gemini_api_key',
    name: 'Gemini',
    description: 'Used when Gemini is the selected embedding or LLM provider.',
    placeholder: 'AIza...',
  },
  {
    key: 'anthropic_api_key',
    name: 'Anthropic',
    description: 'Used for Claude-powered extraction and synthesis.',
    placeholder: 'sk-ant-...',
  },
];

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
  const config = API_KEYS.find(k => k.key === key);
  return config?.placeholder ?? '';
}

interface ApiKeyRowProps {
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

function ApiKeyRow({
  name,
  description,
  setting,
  valid,
  error,
  isValidating,
  onEdit,
  onDelete,
  isDeleting,
}: ApiKeyRowProps) {
  const configured = setting?.configured ?? false;
  const source = setting?.source ?? 'none';

  let pill: ReactNode;
  if (!configured) {
    pill = (
      <StatusPill tone="neutral" icon={Xmark}>
        Not configured
      </StatusPill>
    );
  } else if (isValidating) {
    pill = (
      <StatusPill tone="info" icon={RefreshDouble}>
        Validating
      </StatusPill>
    );
  } else if (valid === true) {
    pill = (
      <StatusPill tone="success" icon={Check}>
        Active
      </StatusPill>
    );
  } else if (valid === false) {
    pill = (
      <StatusPill tone="danger" icon={WarningTriangle}>
        Invalid
      </StatusPill>
    );
  } else {
    pill = (
      <StatusPill tone="neutral" icon={Check}>
        Ready
      </StatusPill>
    );
  }

  return (
    <div className="flex flex-col gap-3 border-b border-sc-fg-subtle/5 px-6 py-4 last:border-b-0 sm:flex-row sm:items-center sm:justify-between">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-sc-fg-primary">{name}</h3>
          {pill}
          {configured && source === 'environment' && (
            <Tooltip content="Value comes from a deployment environment variable. Update here only overrides if the env var is unset.">
              <span className="inline-flex items-center gap-1 rounded-full bg-sc-purple/10 px-2 py-0.5 text-[11px] text-sc-purple">
                <Globe width={11} height={11} />
                env
              </span>
            </Tooltip>
          )}
          {configured && source === 'database' && (
            <Tooltip content="Value entered here and stored in the database for this deployment.">
              <span className="inline-flex items-center gap-1 rounded-full bg-sc-cyan/10 px-2 py-0.5 text-[11px] text-sc-cyan">
                <Database width={11} height={11} />
                db
              </span>
            </Tooltip>
          )}
        </div>
        <p className="mt-0.5 text-xs text-sc-fg-muted">{description}</p>
        {configured && setting?.masked && (
          <code className="mt-1.5 inline-block rounded bg-sc-bg-highlight/60 px-2 py-0.5 text-[11px] font-mono text-sc-fg-secondary">
            {setting.masked}
          </code>
        )}
        {valid === false && error && <p className="mt-1 text-xs text-sc-red">{error}</p>}
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          icon={<EditPencil width={14} height={14} />}
          onClick={onEdit}
        >
          {configured ? 'Update' : 'Configure'}
        </Button>
        {configured && source === 'database' && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onDelete}
            disabled={isDeleting}
            icon={
              isDeleting ? <Spinner size="sm" color="current" /> : <Trash width={14} height={14} />
            }
          >
            Remove
          </Button>
        )}
      </div>
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

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!value.trim()) return;
    await onSave(value.trim());
  };

  return (
    <Dialog open onOpenChange={open => !open && onClose()}>
      <DialogContent size="md">
        <DialogHeader>
          <DialogTitle>{name} API key</DialogTitle>
          <DialogDescription>
            {currentMasked
              ? `Replacing ${currentMasked}.`
              : 'Configure this provider to enable its routes.'}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={handleSubmit}>
          <div className="relative">
            <input
              autoFocus
              type={showValue ? 'text' : 'password'}
              value={value}
              onChange={e => setValue(e.target.value)}
              placeholder={placeholder}
              aria-label={`${name} API key`}
              className="w-full rounded-lg border border-sc-fg-subtle/20 bg-sc-bg-highlight px-3 py-2.5 pr-10 text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            />
            <button
              type="button"
              onClick={() => setShowValue(v => !v)}
              aria-label={showValue ? 'Hide value' : 'Show value'}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-sc-fg-subtle transition-colors duration-200 hover:text-sc-fg-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            >
              {showValue ? <Xmark width={16} height={16} /> : <Eye width={16} height={16} />}
            </button>
          </div>
          <DialogFooter>
            <Button variant="secondary" size="md" onClick={onClose} className="flex-1">
              Cancel
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="md"
              disabled={!value.trim()}
              loading={isSaving}
              className="flex-1"
            >
              Save key
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

interface EmbeddingPanelProps {
  title: string;
  description: string;
  provider: EmbeddingProvider;
  model: string;
  dimensions: string;
  onProviderChange: (provider: EmbeddingProvider) => void;
  onModelChange: (model: string) => void;
  onDimensionsChange: (dimensions: string) => void;
}

function EmbeddingPanel({
  title,
  description,
  provider,
  model,
  dimensions,
  onProviderChange,
  onModelChange,
  onDimensionsChange,
}: EmbeddingPanelProps) {
  return (
    <div className="border-b border-sc-fg-subtle/5 px-6 py-5 last:border-b-0">
      <div className="mb-3">
        <h3 className="text-sm font-semibold text-sc-fg-primary">{title}</h3>
        <p className="mt-0.5 text-xs text-sc-fg-muted">{description}</p>
      </div>
      <div className="grid gap-3 sm:grid-cols-[160px_minmax(0,1fr)_140px]">
        <SettingsField label="Provider">
          <Select
            value={provider}
            onValueChange={value => onProviderChange(value as EmbeddingProvider)}
          >
            <SelectTrigger className="w-full" aria-label={`${title} provider`}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="openai">OpenAI</SelectItem>
              <SelectItem value="gemini">Gemini</SelectItem>
            </SelectContent>
          </Select>
        </SettingsField>
        <SettingsField label="Model">
          <input
            type="text"
            value={model}
            onChange={e => onModelChange(e.target.value)}
            aria-label={`${title} model`}
            className="w-full rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-highlight/40 px-3 py-2 text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
          />
        </SettingsField>
        <SettingsField label="Dimensions">
          <input
            type="number"
            min={128}
            max={3072}
            step={1}
            value={dimensions}
            onChange={e => onDimensionsChange(e.target.value)}
            aria-label={`${title} dimensions`}
            className="w-full rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-highlight/40 px-3 py-2 text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
          />
        </SettingsField>
      </div>
    </div>
  );
}

export default function AIServicesPage() {
  const { data: settings, isLoading } = useSettings();
  const { data: llmSettings, isLoading: isLLMLoading } = useLLMSettings();
  const { data: registry } = useLLMRegistry('llm');
  const {
    data: validation,
    refetch: revalidate,
    isLoading: isValidateLoading,
  } = useValidateApiKeys({ enabled: false });
  const updateSettings = useUpdateSettings();
  const deleteSetting = useDeleteSetting();

  const entries: AIModelEntry[] = useMemo(() => registry?.entries ?? [], [registry]);

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
        <SettingsPageHeader
          icon={Flash}
          iconColor="text-sc-yellow"
          title="AI Services"
          description="API keys, model routing, and embeddings."
        />
        <SettingsSectionSkeleton rows={3} rowHeight={72} />
        <SettingsSectionSkeleton rows={3} rowHeight={120} />
        <SettingsSectionSkeleton rows={2} rowHeight={96} />
      </div>
    );
  }

  const keyValidByKey: Record<ApiKeySettingKey, boolean | null | undefined> = {
    openai_api_key: openaiValid,
    gemini_api_key: geminiValid,
    anthropic_api_key: anthropicValid,
  };

  const keyErrorByKey: Record<ApiKeySettingKey, string | null | undefined> = {
    openai_api_key:
      validation?.openai_error ?? updateSettings.data?.validation?.openai_api_key?.error,
    gemini_api_key:
      validation?.gemini_error ?? updateSettings.data?.validation?.gemini_api_key?.error,
    anthropic_api_key:
      validation?.anthropic_error ?? updateSettings.data?.validation?.anthropic_api_key?.error,
  };

  return (
    <div className="space-y-6">
      <SettingsPageHeader
        icon={Flash}
        iconColor="text-sc-yellow"
        title="AI Services"
        description="API keys, model routing, and embeddings. Apply across every organization in this deployment."
        actions={
          <Button
            variant="secondary"
            size="sm"
            onClick={handleValidate}
            loading={isValidating || isValidateLoading}
            icon={<RefreshDouble width={14} height={14} />}
          >
            Validate all
          </Button>
        }
      />

      <SettingsSection
        title="Provider keys"
        description="Configure here, or set environment variables for deployment-wide overrides."
        flush
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 border-b border-sc-fg-subtle/5 px-6 py-3 text-[11px] text-sc-fg-muted">
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-flex items-center gap-1 rounded-full bg-sc-purple/10 px-2 py-0.5 text-sc-purple">
              <Globe width={11} height={11} />
              env
            </span>
            from a deployment environment variable
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="inline-flex items-center gap-1 rounded-full bg-sc-cyan/10 px-2 py-0.5 text-sc-cyan">
              <Database width={11} height={11} />
              db
            </span>
            entered here and stored in the database
          </span>
        </div>
        {API_KEYS.map(config => (
          <ApiKeyRow
            key={config.key}
            name={config.name}
            description={config.description}
            setting={settings?.settings?.[config.key]}
            valid={keyValidByKey[config.key]}
            error={keyErrorByKey[config.key]}
            isValidating={isValidating}
            onEdit={() => setEditingKey(config.key)}
            onDelete={() => handleDeleteKey(config.key)}
            isDeleting={deletingKey === config.key}
          />
        ))}
      </SettingsSection>

      <SettingsSection
        title="Model routing"
        description="Each surface picks a provider and model. Test before saving."
        flush
      >
        {isLLMLoading || !llmSettings ? (
          <SettingsSectionSkeleton rows={4} rowHeight={120} showHeader={false} />
        ) : (
          SURFACES.map(surface => (
            <LLMSurfaceRow
              key={surface.id}
              id={surface.id}
              label={surface.label}
              description={surface.description}
              useCase={surface.useCase}
              surface={llmSettings.surfaces[surface.id]}
              entries={entries}
            />
          ))
        )}
      </SettingsSection>

      <SettingsSection
        title="Embeddings"
        description="Vector spaces for document chunks and graph memory."
        flush
        actions={
          <Button
            variant="primary"
            size="sm"
            onClick={handleSaveEmbeddingConfig}
            loading={updateSettings.isPending}
          >
            Save embeddings
          </Button>
        }
      >
        <div className="px-6 pt-4">
          <HelpNote tone="warning" icon={WarningTriangle}>
            Changing provider, model, or dimensions changes the vector space. Re-crawl document
            sources and rebuild graph indexes before trusting mixed search results.
          </HelpNote>
        </div>
        <EmbeddingPanel
          title="Document embeddings"
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
        <EmbeddingPanel
          title="Graph embeddings"
          description="Used by graph entities, relationships, and similarity search."
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
      </SettingsSection>

      <HelpNote tone="muted" icon={InfoCircle}>
        Environment variables take precedence over database settings for the same field. Gemini also
        checks <code className="font-mono text-sc-fg-secondary">GEMINI_API_KEY</code> and{' '}
        <code className="font-mono text-sc-fg-secondary">GOOGLE_API_KEY</code>; embedding providers
        can be set with{' '}
        <code className="font-mono text-sc-fg-secondary">SIBYL_EMBEDDING_PROVIDER</code> and{' '}
        <code className="font-mono text-sc-fg-secondary">SIBYL_GRAPH_EMBEDDING_PROVIDER</code>.
      </HelpNote>

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
