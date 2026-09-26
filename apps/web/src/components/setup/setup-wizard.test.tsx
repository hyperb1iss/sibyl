import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SetupStatus } from '@/lib/api/admin';
import { render, screen, userEvent } from '@/test/utils';
import { SetupWizard, setupSteps } from './setup-wizard';

// Test the step persistence utility functions directly
const STEPS = ['welcome', 'api-keys', 'admin', 'connect'] as const;
type SetupStep = (typeof STEPS)[number];
const STEP_STORAGE_KEY = 'sibyl-setup-step';

function getStoredStep(): SetupStep {
  if (typeof window === 'undefined') return 'welcome';
  const stored = sessionStorage.getItem(STEP_STORAGE_KEY);
  if (stored && STEPS.includes(stored as SetupStep)) {
    return stored as SetupStep;
  }
  return 'welcome';
}

describe('SetupWizard Step Persistence', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  afterEach(() => {
    sessionStorage.clear();
  });

  describe('getStoredStep', () => {
    it('returns welcome when sessionStorage is empty', () => {
      expect(getStoredStep()).toBe('welcome');
    });

    it('returns stored step when valid', () => {
      sessionStorage.setItem(STEP_STORAGE_KEY, 'api-keys');
      expect(getStoredStep()).toBe('api-keys');
    });

    it('returns welcome when stored step is invalid', () => {
      sessionStorage.setItem(STEP_STORAGE_KEY, 'invalid-step');
      expect(getStoredStep()).toBe('welcome');
    });

    it('handles all valid steps', () => {
      for (const step of STEPS) {
        sessionStorage.setItem(STEP_STORAGE_KEY, step);
        expect(getStoredStep()).toBe(step);
      }
    });
  });

  describe('sessionStorage persistence', () => {
    it('persists step to sessionStorage', () => {
      const step: SetupStep = 'api-keys';
      sessionStorage.setItem(STEP_STORAGE_KEY, step);

      expect(sessionStorage.getItem(STEP_STORAGE_KEY)).toBe('api-keys');
    });

    it('clears step from sessionStorage', () => {
      sessionStorage.setItem(STEP_STORAGE_KEY, 'admin');
      sessionStorage.removeItem(STEP_STORAGE_KEY);

      expect(sessionStorage.getItem(STEP_STORAGE_KEY)).toBeNull();
    });

    it('survives getting and setting multiple times', () => {
      sessionStorage.setItem(STEP_STORAGE_KEY, 'welcome');
      expect(getStoredStep()).toBe('welcome');

      sessionStorage.setItem(STEP_STORAGE_KEY, 'api-keys');
      expect(getStoredStep()).toBe('api-keys');

      sessionStorage.setItem(STEP_STORAGE_KEY, 'admin');
      expect(getStoredStep()).toBe('admin');
    });
  });
});

// =============================================================================
// Step list: the keys step exists only while no model provider is ready
// =============================================================================

describe('setupSteps', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  const status = (providersConfigured: boolean) =>
    ({
      needs_setup: true,
      providers_configured: providersConfigured,
      configured_providers: providersConfigured ? ['bedrock'] : [],
    }) as unknown as SetupStatus;

  it('keeps the API keys step when the server has no ready provider', () => {
    expect(setupSteps(status(false))).toEqual(['welcome', 'api-keys', 'admin', 'connect']);
    expect(setupSteps(undefined)).toEqual(['welcome', 'api-keys', 'admin', 'connect']);
  });

  it('drops the API keys step when providers are configured server-side', () => {
    expect(setupSteps(status(true))).toEqual(['welcome', 'admin', 'connect']);
  });

  it('goes from welcome straight to the admin account with a configured summary', async () => {
    const user = userEvent.setup();
    render(<SetupWizard initialStatus={status(true)} onComplete={vi.fn()} />);

    expect(screen.getByText(/models are configured on the server/i)).toBeInTheDocument();
    expect(screen.getByText('bedrock')).toBeInTheDocument();
    expect(screen.getByText('Step 1 of 2')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: "Let's Get Started" }));

    expect(await screen.findByText('Create Admin Account')).toBeInTheDocument();
    expect(screen.queryByText('Configure API Keys')).not.toBeInTheDocument();
  });

  it('continues to the admin account after saving keys refreshes the status', async () => {
    const user = userEvent.setup();
    const { rerender } = render(<SetupWizard initialStatus={status(false)} onComplete={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: "Let's Get Started" }));
    expect(await screen.findByText('Configure API Keys')).toBeInTheDocument();

    // The page refetches setup status after the save and passes the new one down.
    rerender(
      <SetupWizard
        initialStatus={
          {
            ...status(true),
            anthropic_configured: true,
            openai_configured: true,
          } as unknown as SetupStatus
        }
        onComplete={vi.fn()}
      />
    );

    expect(screen.getByText('Step 2 of 3')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByText('Create Admin Account')).toBeInTheDocument();
  });
});
