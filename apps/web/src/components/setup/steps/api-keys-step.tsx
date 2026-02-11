'use client';

import { useCallback, useState } from 'react';
import { Check, HelpCircle, Key, WarningTriangle, Xmark } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';
import type { SetupStatus } from '@/lib/api';
import { useSettings, useUpdateSettings, useValidateApiKeys } from '@/lib/hooks';

interface ApiKeysStepProps {
  initialStatus: SetupStatus | undefined;
  onBack: () => void;
  onValidated: (valid: boolean) => void;
}

export function ApiKeysStep({ initialStatus, onBack, onValidated }: ApiKeysStepProps) {
  // Form state
  const [openaiKey, setOpenaiKey] = useState('');
  const [anthropicKey, setAnthropicKey] = useState('');
  const [showOpenaiKey, setShowOpenaiKey] = useState(false);
  const [showAnthropicKey, setShowAnthropicKey] = useState(false);

  // API hooks
  const { data: settings } = useSettings();
  const updateSettings = useUpdateSettings();
  const {
    data: validation,
    refetch: revalidate,
    isLoading: isValidating,
  } = useValidateApiKeys({ enabled: false });

  // Determine configuration status from settings or initial status
  const openaiConfigured =
    settings?.settings?.openai_api_key?.configured ?? initialStatus?.openai_configured ?? false;
  const anthropicConfigured =
    settings?.settings?.anthropic_api_key?.configured ??
    initialStatus?.anthropic_configured ??
    false;
  const bothConfigured = openaiConfigured && anthropicConfigured;

  // Validation status
  const openaiValid =
    updateSettings.data?.validation?.openai_api_key?.valid ??
    validation?.openai_valid ??
    initialStatus?.openai_valid;
  const anthropicValid =
    updateSettings.data?.validation?.anthropic_api_key?.valid ??
    validation?.anthropic_valid ??
    initialStatus?.anthropic_valid;

  // Validation errors
  const openaiError =
    updateSettings.data?.validation?.openai_api_key?.error ?? validation?.openai_error;
  const anthropicError =
    updateSettings.data?.validation?.anthropic_api_key?.error ?? validation?.anthropic_error;

  const isSaving = updateSettings.isPending;
  const hasKeyInput = openaiKey.trim().length > 0 || anthropicKey.trim().length > 0;

  const handleSaveKeys = useCallback(async () => {
    const request: { openai_api_key?: string; anthropic_api_key?: string } = {};
    if (openaiKey.trim()) {
      request.openai_api_key = openaiKey.trim();
    }
    if (anthropicKey.trim()) {
      request.anthropic_api_key = anthropicKey.trim();
    }

    if (Object.keys(request).length === 0) {
      return;
    }

    const result = await updateSettings.mutateAsync(request);

    // Check if both keys are now valid
    const bothNowValid =
      (result.validation.openai_api_key?.valid ?? openaiValid === true) &&
      (result.validation.anthropic_api_key?.valid ?? anthropicValid === true);

    if (bothNowValid) {
      // Clear input fields on success
      setOpenaiKey('');
      setAnthropicKey('');
    }
  }, [openaiKey, anthropicKey, updateSettings, openaiValid, anthropicValid]);

  const handleValidateExisting = useCallback(async () => {
    const result = await revalidate();
    if (result.data?.openai_valid && result.data?.anthropic_valid) {
      onValidated(true);
    }
  }, [revalidate, onValidated]);

  const handleContinue = useCallback(() => {
    // Both keys are configured (and were validated before being saved)
    if (bothConfigured) {
      onValidated(true);
    }
  }, [bothConfigured, onValidated]);

  return (
    <div className="p-8">
      {/* Header */}
      <div className="text-center mb-8">
        <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-gradient-to-br from-sc-cyan/20 to-sc-purple/20 flex items-center justify-center">
          <Key width={28} height={28} className="text-sc-cyan" />
        </div>
        <h2 className="text-xl font-semibold text-sc-fg-primary mb-2">Configure API Keys</h2>
        <p className="text-sc-fg-muted text-sm max-w-md mx-auto">
          Sibyl needs API keys for semantic search (OpenAI) and entity extraction (Anthropic). Enter
          your keys below to save them securely.
        </p>
      </div>

      {/* API Key Inputs */}
      <div className="space-y-4 mb-6">
        <ApiKeyInput
          name="OpenAI"
          description="Used for embeddings and semantic search"
          placeholder="sk-..."
          value={openaiKey}
          onChange={setOpenaiKey}
          showValue={showOpenaiKey}
          onToggleShow={() => setShowOpenaiKey(!showOpenaiKey)}
          configured={openaiConfigured}
          valid={openaiValid}
          error={openaiError}
          masked={settings?.settings?.openai_api_key?.masked}
          isValidating={isSaving}
        />
        <ApiKeyInput
          name="Anthropic"
          description="Used for entity extraction and agents"
          placeholder="sk-ant-..."
          value={anthropicKey}
          onChange={setAnthropicKey}
          showValue={showAnthropicKey}
          onToggleShow={() => setShowAnthropicKey(!showAnthropicKey)}
          configured={anthropicConfigured}
          valid={anthropicValid}
          error={anthropicError}
          masked={settings?.settings?.anthropic_api_key?.masked}
          isValidating={isSaving}
        />
      </div>

      {/* Status message */}
      {updateSettings.isError && (
        <div className="mb-6 p-4 rounded-xl bg-sc-red/10 border border-sc-red/20">
          <div className="flex gap-3">
            <Xmark width={20} height={20} className="text-sc-red flex-shrink-0 mt-0.5" />
            <p className="text-sm text-sc-red">
              Failed to save settings. Please check your keys and try again.
            </p>
          </div>
        </div>
      )}

      {/* Progress message - show when one key is saved but not both */}
      {!bothConfigured && (openaiConfigured || anthropicConfigured) && (
        <div className="mb-6 p-4 rounded-xl bg-sc-green/10 border border-sc-green/20">
          <div className="flex gap-3">
            <Check width={20} height={20} className="text-sc-green flex-shrink-0 mt-0.5" />
            <p className="text-sm text-sc-green">
              {openaiConfigured && !anthropicConfigured && 'OpenAI key saved! Now add your Anthropic key below.'}
              {anthropicConfigured && !openaiConfigured && 'Anthropic key saved! Now add your OpenAI key below.'}
            </p>
          </div>
        </div>
      )}

      {/* Help text */}
      {!bothConfigured && !hasKeyInput && (
        <div className="mb-6 p-4 rounded-xl bg-sc-cyan/10 border border-sc-cyan/20">
          <div className="flex gap-3">
            <HelpCircle width={20} height={20} className="text-sc-cyan flex-shrink-0 mt-0.5" />
            <div>
              <p className="text-sm font-medium text-sc-cyan mb-1">Need API Keys?</p>
              <p className="text-sm text-sc-fg-muted">
                Get your API keys from{' '}
                <a
                  href="https://platform.openai.com/api-keys"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-sc-cyan hover:underline"
                >
                  OpenAI
                </a>{' '}
                and{' '}
                <a
                  href="https://console.anthropic.com/settings/keys"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-sc-cyan hover:underline"
                >
                  Anthropic
                </a>
                .
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Buttons */}
      <div className="flex gap-3">
        <button
          type="button"
          onClick={onBack}
          className="flex-1 py-2.5 px-4 rounded-lg border border-sc-fg-subtle/20 text-sc-fg-secondary font-medium text-sm transition-colors hover:bg-sc-bg-base"
        >
          Back
        </button>

        {bothConfigured ? (
          // Both keys saved - show Continue
          <button
            type="button"
            onClick={handleContinue}
            className="flex-1 py-2.5 px-4 rounded-lg bg-sc-purple text-white font-medium text-sm transition-all hover:bg-sc-purple/90 hover:shadow-lg hover:shadow-sc-purple/25"
          >
            Continue
          </button>
        ) : hasKeyInput ? (
          // User has typed a key - show Save button
          <button
            type="button"
            onClick={handleSaveKeys}
            disabled={isSaving}
            className="flex-1 py-2.5 px-4 rounded-lg bg-sc-cyan text-sc-bg-dark font-medium text-sm transition-all hover:bg-sc-cyan/90 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {isSaving ? (
              <>
                <Spinner size="sm" color="current" />
                Validating & Saving...
              </>
            ) : (
              `Save ${openaiKey.trim() && anthropicKey.trim() ? 'Keys' : 'Key'}`
            )}
          </button>
        ) : (
          // Waiting for user to enter key(s)
          <button
            type="button"
            disabled
            className="flex-1 py-2.5 px-4 rounded-lg bg-sc-fg-subtle/20 text-sc-fg-muted font-medium text-sm cursor-not-allowed"
          >
            {openaiConfigured && !anthropicConfigured
              ? 'Enter Anthropic Key'
              : anthropicConfigured && !openaiConfigured
                ? 'Enter OpenAI Key'
                : 'Enter API Keys'}
          </button>
        )}
      </div>
    </div>
  );
}

function ApiKeyInput({
  name,
  description,
  placeholder,
  value,
  onChange,
  showValue,
  onToggleShow,
  configured,
  valid,
  error,
  masked,
  isValidating,
}: {
  name: string;
  description: string;
  placeholder: string;
  value: string;
  onChange: (value: string) => void;
  showValue: boolean;
  onToggleShow: () => void;
  configured: boolean;
  valid: boolean | null | undefined;
  error: string | null | undefined;
  masked: string | null | undefined;
  isValidating: boolean;
}) {
  let statusIcon: React.ReactNode;
  let statusColor: string;
  let statusText: string;

  if (isValidating) {
    statusIcon = <Spinner size="sm" color="cyan" />;
    statusColor = 'text-sc-cyan';
    statusText = 'Validating...';
  } else if (valid === false) {
    // Explicit validation failure - show error
    statusIcon = <WarningTriangle aria-hidden="true" width={16} height={16} />;
    statusColor = 'text-sc-red';
    statusText = error || 'Invalid';
  } else if (configured) {
    // Key is configured in DB - it was validated before being saved
    statusIcon = <Check aria-hidden="true" width={16} height={16} />;
    statusColor = 'text-sc-green';
    statusText = 'Saved';
  } else {
    // Not configured yet
    statusIcon = null;
    statusColor = 'text-sc-fg-subtle';
    statusText = 'Required';
  }

  return (
    <div className="p-4 rounded-xl bg-sc-bg-base/50 border border-sc-fg-subtle/10">
      <div className="flex items-center justify-between mb-2">
        <div>
          <h3 className="font-medium text-sc-fg-primary">{name}</h3>
          <p className="text-xs text-sc-fg-muted">{description}</p>
        </div>
        {statusIcon && (
          <div className={`flex items-center gap-1.5 ${statusColor}`}>
            {statusIcon}
            <span className="text-xs font-medium">{statusText}</span>
          </div>
        )}
      </div>

      <div className="relative">
        <input
          type={showValue ? 'text' : 'password'}
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={configured && masked ? masked : placeholder}
          className="w-full px-3 py-2 rounded-lg bg-sc-bg-dark border border-sc-fg-subtle/20 text-sc-fg-primary text-sm placeholder:text-sc-fg-subtle focus:outline-none focus:border-sc-cyan/50 focus:ring-1 focus:ring-sc-cyan/20"
        />
        <button
          type="button"
          onClick={onToggleShow}
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
    </div>
  );
}
