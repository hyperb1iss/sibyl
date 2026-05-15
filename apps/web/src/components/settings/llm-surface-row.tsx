'use client';

import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import {
  Check,
  NavArrowDown,
  NavArrowRight,
  RefreshDouble,
  WarningTriangle,
} from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import type {
  AIModelEntry,
  LLMConfigSource,
  LLMProviderName,
  LLMSurface,
  LLMSurfaceSettings,
  LLMTestResult,
  UpdateLLMSurfaceRequest,
} from '@/lib/api';
import { useTestLLMSurface, useUpdateLLMSurface } from '@/lib/hooks';
import { SettingsField, type SettingsFieldSource, StatusPill } from './primitives';

const CUSTOM_MODEL = '__custom__';

const PROVIDERS: Array<{ value: LLMProviderName; label: string }> = [
  { value: 'anthropic', label: 'Anthropic' },
  { value: 'gemini', label: 'Gemini' },
  { value: 'openai', label: 'OpenAI' },
];

interface SurfaceDraft {
  provider: LLMProviderName;
  model: string;
  customModel: string;
  temperature: string;
  maxTokens: string;
  timeoutSeconds: string;
  customConfirmed: boolean;
}

function normalizeProvider(value: unknown): LLMProviderName {
  return value === 'gemini' || value === 'openai' || value === 'anthropic' ? value : 'anthropic';
}

function fieldText(value: string | number | null) {
  return value === null || value === undefined ? '' : String(value);
}

function modelsForProvider(entries: AIModelEntry[], provider: LLMProviderName) {
  return entries.filter(entry => entry.kind === 'llm' && entry.provider === provider);
}

function recommendedModel(
  entries: AIModelEntry[],
  provider: LLMProviderName,
  useCase: string
): AIModelEntry | undefined {
  const providerModels = modelsForProvider(entries, provider);
  return (
    providerModels.find(entry => entry.use_cases.includes(useCase)) ??
    providerModels.find(entry => entry.use_cases.includes('default')) ??
    providerModels[0]
  );
}

function isCuratedModel(entries: AIModelEntry[], provider: LLMProviderName, model: string) {
  return modelsForProvider(entries, provider).some(
    entry => entry.alias === model || entry.snapshot === model || entry.provider_model_id === model
  );
}

function createDraft(surface: LLMSurfaceSettings, entries: AIModelEntry[]): SurfaceDraft {
  const provider = normalizeProvider(surface.provider.value);
  const model = fieldText(surface.model.value);
  const custom = model ? !isCuratedModel(entries, provider, model) : false;
  return {
    provider,
    model,
    customModel: custom ? model : '',
    temperature: fieldText(surface.temperature.value),
    maxTokens: fieldText(surface.max_tokens.value),
    timeoutSeconds: fieldText(surface.timeout_seconds.value),
    customConfirmed: false,
  };
}

function buildRequest(draft: SurfaceDraft): UpdateLLMSurfaceRequest {
  const model =
    draft.model === CUSTOM_MODEL || draft.customModel.trim()
      ? draft.customModel.trim()
      : draft.model.trim();
  const request: UpdateLLMSurfaceRequest = {
    provider: draft.provider,
    model,
    temperature: Number(draft.temperature),
    timeout_seconds: Number(draft.timeoutSeconds),
  };
  const maxTokens = draft.maxTokens.trim();
  if (maxTokens) {
    request.max_tokens = Number(maxTokens);
  }
  return request;
}

function validateDraft(draft: SurfaceDraft) {
  const isCustom = draft.model === CUSTOM_MODEL || draft.customModel.trim().length > 0;
  if (isCustom && !draft.customModel.trim()) return 'Enter a custom model before saving.';
  if (!isCustom && !draft.model.trim()) return 'Choose a model before saving.';
  if (isCustom && !draft.customConfirmed) return 'Confirm the custom model before saving.';
  if (!Number.isFinite(Number(draft.temperature))) return 'Temperature must be a number.';
  if (!Number.isFinite(Number(draft.timeoutSeconds))) return 'Timeout must be a number.';
  if (draft.maxTokens.trim() && !Number.isInteger(Number(draft.maxTokens))) {
    return 'Max tokens must be a whole number.';
  }
  return null;
}

function parseEnvLockedError(error: unknown) {
  const message = error instanceof Error ? error.message : String(error);
  try {
    const payload = JSON.parse(message) as {
      detail?: { code?: string; fields?: Array<{ field?: string; env_var?: string | null }> };
    };
    if (payload.detail?.code === 'LOCKED_BY_ENV') {
      const fields = payload.detail.fields ?? [];
      const names = fields
        .map(field => (field.env_var ? `${field.field} (${field.env_var})` : field.field))
        .filter(Boolean)
        .join(', ');
      return names
        ? `Locked by environment: ${names}.`
        : "This field is set by an environment variable and can't be changed here.";
    }
  } catch {
    return message;
  }
  return message;
}

function formatTokens(result: LLMTestResult) {
  const input = result.input_tokens ?? 0;
  const output = result.output_tokens ?? 0;
  return input || output ? `${input} in / ${output} out` : 'tokens unavailable';
}

function toFieldSource(source: LLMConfigSource): SettingsFieldSource {
  if (source === 'env') return 'env';
  if (source === 'db') return 'db';
  return 'default';
}

interface LLMSurfaceRowProps {
  id: LLMSurface;
  label: string;
  description: string;
  useCase: string;
  surface: LLMSurfaceSettings;
  entries: AIModelEntry[];
}

export function LLMSurfaceRow({
  id,
  label,
  description,
  useCase,
  surface,
  entries,
}: LLMSurfaceRowProps) {
  const updateSurface = useUpdateLLMSurface();
  const testSurface = useTestLLMSurface();

  const [draft, setDraft] = useState<SurfaceDraft>(() => createDraft(surface, entries));
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<LLMTestResult | null>(null);
  const [isSaving, setIsSaving] = useState(false);
  const [isTesting, setIsTesting] = useState(false);

  useEffect(() => {
    setDraft(createDraft(surface, entries));
    setSaveError(null);
  }, [surface, entries]);

  const providerModels = modelsForProvider(entries, draft.provider);
  const recommended = recommendedModel(entries, draft.provider, useCase);
  const modelIsCustom =
    !!draft.model &&
    draft.model !== CUSTOM_MODEL &&
    !isCuratedModel(entries, draft.provider, draft.model);
  const selectedModel = modelIsCustom ? CUSTOM_MODEL : draft.model;

  const lockedFields = useMemo(() => {
    const fields: Array<{ name: string; envVar: string | null }> = [];
    const check = (name: string, f: { locked_by_env: boolean; env_var: string | null }) => {
      if (f.locked_by_env) fields.push({ name, envVar: f.env_var });
    };
    check('Provider', surface.provider);
    check('Model', surface.model);
    check('Temperature', surface.temperature);
    check('Max tokens', surface.max_tokens);
    check('Timeout', surface.timeout_seconds);
    return fields;
  }, [surface]);

  const isDirty = useMemo(() => {
    const original = createDraft(surface, entries);
    return (
      original.provider !== draft.provider ||
      original.model !== draft.model ||
      original.customModel !== draft.customModel ||
      original.temperature !== draft.temperature ||
      original.maxTokens !== draft.maxTokens ||
      original.timeoutSeconds !== draft.timeoutSeconds
    );
  }, [draft, surface, entries]);

  const updateDraft = (patch: Partial<SurfaceDraft>) => {
    setDraft(prev => ({ ...prev, ...patch }));
    setSaveError(null);
  };

  const handleProviderChange = (provider: LLMProviderName) => {
    const rec = recommendedModel(entries, provider, useCase);
    updateDraft({
      provider,
      model: rec?.alias ?? '',
      customModel: '',
      customConfirmed: false,
    });
  };

  const handleSave = async () => {
    const validationError = validateDraft(draft);
    if (validationError) {
      setSaveError(validationError);
      return;
    }
    setIsSaving(true);
    try {
      const response = await updateSurface.mutateAsync({
        surface: id,
        request: buildRequest(draft),
      });
      toast.success(`${label} LLM saved`);
      if (response.warning === 'unverified_model') {
        toast.warning('Saved with an unverified model');
      }
    } catch (error) {
      setSaveError(parseEnvLockedError(error));
    } finally {
      setIsSaving(false);
    }
  };

  const handleTest = async () => {
    setIsTesting(true);
    try {
      const result = await testSurface.mutateAsync(id);
      setTestResult(result);
      if (result.valid) toast.success(`${label} surface is ready`);
      else toast.error(result.error || `${label} surface test failed`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Surface test failed');
    } finally {
      setIsTesting(false);
    }
  };

  const keyReady = surface.api_key.configured;

  return (
    <div className="border-b border-sc-fg-subtle/5 px-6 py-5 last:border-b-0">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-base font-semibold text-sc-fg-primary">{label}</h3>
            {keyReady ? (
              <StatusPill tone="success" icon={Check}>
                Key ready
              </StatusPill>
            ) : (
              <StatusPill tone="danger" icon={WarningTriangle}>
                Missing key
              </StatusPill>
            )}
            {lockedFields.length > 0 && (
              <StatusPill tone="info">{lockedFields.length} env-locked</StatusPill>
            )}
          </div>
          <p className="mt-1 text-sm text-sc-fg-muted">{description}</p>
        </div>
        <button
          type="button"
          onClick={handleTest}
          disabled={isTesting}
          aria-label={`Test ${label}`}
          className="inline-flex shrink-0 items-center gap-2 rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-highlight/40 px-3 py-1.5 text-sm font-medium text-sc-fg-secondary transition-colors hover:bg-sc-bg-highlight disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isTesting ? (
            <Spinner size="sm" color="current" />
          ) : (
            <RefreshDouble width={14} height={14} />
          )}
          Test
        </button>
      </div>

      <div className="mt-4 grid gap-3 sm:grid-cols-[180px_minmax(0,1fr)]">
        <SettingsField
          label="Provider"
          source={toFieldSource(surface.provider.source)}
          locked={surface.provider.locked_by_env}
          envVar={surface.provider.env_var}
        >
          <select
            value={draft.provider}
            onChange={event => handleProviderChange(event.target.value as LLMProviderName)}
            disabled={surface.provider.locked_by_env}
            className="w-full rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-highlight/40 px-3 py-2 text-sm text-sc-fg-primary focus:border-sc-cyan/50 focus:outline-none focus:ring-1 focus:ring-sc-cyan/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {PROVIDERS.map(provider => (
              <option key={provider.value} value={provider.value}>
                {provider.label}
              </option>
            ))}
          </select>
        </SettingsField>

        <SettingsField
          label="Model"
          source={toFieldSource(surface.model.source)}
          locked={surface.model.locked_by_env}
          envVar={surface.model.env_var}
        >
          <select
            value={selectedModel}
            onChange={event => {
              if (event.target.value === CUSTOM_MODEL) {
                updateDraft({
                  model: CUSTOM_MODEL,
                  customModel: modelIsCustom ? draft.customModel || draft.model : '',
                  customConfirmed: false,
                });
                setAdvancedOpen(true);
                return;
              }
              updateDraft({ model: event.target.value, customModel: '', customConfirmed: false });
            }}
            disabled={surface.model.locked_by_env}
            className="w-full rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-highlight/40 px-3 py-2 text-sm text-sc-fg-primary focus:border-sc-cyan/50 focus:outline-none focus:ring-1 focus:ring-sc-cyan/20 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {providerModels.map(model => (
              <option key={model.alias} value={model.alias}>
                {model.alias}
                {model.alias === recommended?.alias ? ' — recommended' : ''}
              </option>
            ))}
            <option value={CUSTOM_MODEL}>Custom model…</option>
          </select>
        </SettingsField>
      </div>

      <div className="mt-4">
        <button
          type="button"
          onClick={() => setAdvancedOpen(open => !open)}
          aria-expanded={advancedOpen}
          className="inline-flex items-center gap-1.5 text-xs font-medium text-sc-cyan transition-colors hover:text-sc-purple"
        >
          {advancedOpen ? (
            <NavArrowDown width={12} height={12} />
          ) : (
            <NavArrowRight width={12} height={12} />
          )}
          Advanced — temperature, tokens, timeout, custom model
        </button>

        {advancedOpen && (
          <div className="mt-3 grid gap-3 rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight/30 p-4 sm:grid-cols-3">
            <SettingsField
              label="Temperature"
              hint="0 = deterministic"
              source={toFieldSource(surface.temperature.source)}
              locked={surface.temperature.locked_by_env}
              envVar={surface.temperature.env_var}
            >
              <input
                type="number"
                min={0}
                max={2}
                step={0.1}
                value={draft.temperature}
                onChange={e => updateDraft({ temperature: e.target.value })}
                disabled={surface.temperature.locked_by_env}
                className="w-full rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-base px-3 py-2 text-sm text-sc-fg-primary focus:border-sc-cyan/50 focus:outline-none focus:ring-1 focus:ring-sc-cyan/20 disabled:cursor-not-allowed disabled:opacity-50"
              />
            </SettingsField>

            <SettingsField
              label="Max tokens"
              hint="Leave blank for provider default"
              source={toFieldSource(surface.max_tokens.source)}
              locked={surface.max_tokens.locked_by_env}
              envVar={surface.max_tokens.env_var}
            >
              <input
                type="number"
                min={1}
                step={1}
                value={draft.maxTokens}
                onChange={e => updateDraft({ maxTokens: e.target.value })}
                disabled={surface.max_tokens.locked_by_env}
                placeholder="default"
                className="w-full rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-base px-3 py-2 text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan/50 focus:outline-none focus:ring-1 focus:ring-sc-cyan/20 disabled:cursor-not-allowed disabled:opacity-50"
              />
            </SettingsField>

            <SettingsField
              label="Timeout"
              hint="Seconds"
              source={toFieldSource(surface.timeout_seconds.source)}
              locked={surface.timeout_seconds.locked_by_env}
              envVar={surface.timeout_seconds.env_var}
            >
              <input
                type="number"
                min={1}
                step={1}
                value={draft.timeoutSeconds}
                onChange={e => updateDraft({ timeoutSeconds: e.target.value })}
                disabled={surface.timeout_seconds.locked_by_env}
                className="w-full rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-base px-3 py-2 text-sm text-sc-fg-primary focus:border-sc-cyan/50 focus:outline-none focus:ring-1 focus:ring-sc-cyan/20 disabled:cursor-not-allowed disabled:opacity-50"
              />
            </SettingsField>

            <div className="sm:col-span-3">
              <SettingsField
                label="Custom model ID"
                hint={`Override with any ${PROVIDERS.find(p => p.value === draft.provider)?.label} model id. Will be marked unverified.`}
              >
                <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-center">
                  <input
                    type="text"
                    value={draft.customModel}
                    onChange={e =>
                      updateDraft({
                        customModel: e.target.value,
                        model: CUSTOM_MODEL,
                        customConfirmed: false,
                      })
                    }
                    placeholder="provider model id"
                    className="w-full rounded-lg border border-sc-fg-subtle/15 bg-sc-bg-base px-3 py-2 text-sm text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-cyan/50 focus:outline-none focus:ring-1 focus:ring-sc-cyan/20"
                  />
                  <label className="inline-flex items-center gap-2 text-xs text-sc-fg-muted">
                    <input
                      type="checkbox"
                      checked={draft.customConfirmed}
                      onChange={e => updateDraft({ customConfirmed: e.target.checked })}
                      className="h-3.5 w-3.5 accent-sc-yellow"
                    />
                    I understand this model is unverified
                  </label>
                </div>
              </SettingsField>
            </div>
          </div>
        )}
      </div>

      {saveError && (
        <div className="mt-3 rounded-lg border border-sc-red/20 bg-sc-red/5 px-3 py-2 text-sm text-sc-red">
          {saveError}
        </div>
      )}

      {testResult && (
        <div
          className={`mt-3 rounded-lg border px-3 py-2 text-sm ${
            testResult.valid
              ? 'border-sc-green/20 bg-sc-green/5 text-sc-fg-secondary'
              : 'border-sc-red/20 bg-sc-red/5 text-sc-red'
          }`}
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="font-medium">
              {testResult.valid ? 'Test passed' : `Test failed: ${testResult.status}`}
            </span>
            <span className="text-xs">{Math.round(testResult.latency_ms)} ms</span>
            <span className="text-xs">{formatTokens(testResult)}</span>
          </div>
          {testResult.error && <p className="mt-1 text-xs">{testResult.error}</p>}
        </div>
      )}

      {isDirty && (
        <div className="mt-4 flex items-center justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setDraft(createDraft(surface, entries))}>
            Reset
          </Button>
          <Button variant="primary" size="sm" onClick={handleSave} loading={isSaving}>
            Save changes
          </Button>
        </div>
      )}
    </div>
  );
}
