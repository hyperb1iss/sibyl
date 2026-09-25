import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { SetupStatus } from '@/lib/api/admin';
import { render, screen, userEvent } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useMe: vi.fn(),
  useSetupStatus: vi.fn(),
  useConnectInfo: vi.fn(),
}));

vi.mock('@/lib/hooks/auth', () => ({
  useMe: hooks.useMe,
  useUpdatePreferences: () => ({ mutateAsync: vi.fn() }),
}));
vi.mock('@/lib/hooks/admin', () => ({
  useSetupStatus: hooks.useSetupStatus,
  useConnectInfo: hooks.useConnectInfo,
  useSettings: () => ({ data: undefined }),
  useUpdateSettings: () => ({ mutateAsync: vi.fn(), isPending: false, isError: false }),
  useValidateApiKeys: () => ({ data: undefined }),
}));
vi.mock('@/lib/hooks/graph', () => ({
  useCreateEntity: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

import { OnboardingWizard, onboardingSteps } from './onboarding-wizard';

function setupStatus(providersConfigured: boolean, providers: string[] = []): SetupStatus {
  return {
    needs_setup: false,
    has_users: true,
    has_orgs: true,
    setup_complete: true,
    public_signups_enabled: false,
    openai_configured: providers.includes('openai'),
    anthropic_configured: providers.includes('anthropic'),
    gemini_configured: false,
    openai_valid: null,
    anthropic_valid: null,
    gemini_valid: null,
    providers_configured: providersConfigured,
    configured_providers: providers,
  };
}

function connectInfo(serverUrl: string, sso: boolean) {
  const setup = `sibyl setup ${serverUrl}`;
  return {
    server_url: serverUrl,
    server_version: '1.4.1',
    minimum_client_version: null,
    sso_enabled: sso,
    local_auth_enabled: !sso,
    setup_command: setup,
    install: {
      macos: `brew install hyperb1iss/tap/sibyl && ${setup}`,
      linux: `uv tool install --upgrade sibyl-dev && ${setup}`,
      windows: `uv tool install --upgrade sibyl-dev; ${setup}`,
    },
  };
}

function signInAs(isAdmin: boolean) {
  hooks.useMe.mockReturnValue({
    data: {
      user: {
        id: 'u1',
        github_id: null,
        email: 'ada@example.com',
        name: 'Ada',
        avatar_url: null,
        is_admin: isAdmin,
      },
      organization: null,
      org_role: isAdmin ? 'owner' : 'member',
    },
  });
}

async function startOnboarding() {
  const user = userEvent.setup();
  render(<OnboardingWizard onComplete={vi.fn()} />);
  await user.click(screen.getByRole('button', { name: /get started/i }));
  return user;
}

describe('OnboardingWizard', () => {
  beforeEach(() => {
    vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(
      'Mozilla/5.0 (X11; Linux x86_64)'
    );
  });

  it('connects a local-auth single-user install without asking for keys', async () => {
    signInAs(true);
    hooks.useSetupStatus.mockReturnValue({ data: setupStatus(true, ['anthropic', 'openai']) });
    hooks.useConnectInfo.mockReturnValue({
      data: connectInfo('http://localhost:3334', false),
      isLoading: false,
      isError: false,
    });

    await startOnboarding();

    expect(await screen.findByText('Connect your tools')).toBeInTheDocument();
    expect(
      screen.getByText('uv tool install --upgrade sibyl-dev && sibyl setup http://localhost:3334')
    ).toBeInTheDocument();
    expect(screen.queryByText('Configure API Keys')).not.toBeInTheDocument();
    expect(screen.getByText('Step 2 of 4')).toBeInTheDocument();
  });

  it('never shows a team SSO member the keys step, even with nothing configured', async () => {
    signInAs(false);
    hooks.useSetupStatus.mockReturnValue({ data: setupStatus(false) });
    hooks.useConnectInfo.mockReturnValue({
      data: connectInfo('https://sibyl.example.com', true),
      isLoading: false,
      isError: false,
    });

    await startOnboarding();

    expect(await screen.findByText('Connect your tools')).toBeInTheDocument();
    expect(
      screen.getByText(
        'uv tool install --upgrade sibyl-dev && sibyl setup https://sibyl.example.com'
      )
    ).toBeInTheDocument();
    expect(screen.queryByText('Configure API Keys')).not.toBeInTheDocument();
    expect(screen.queryByText(/api key/i)).not.toBeInTheDocument();
  });

  it('asks an owner with no providers for keys first, then connects', async () => {
    signInAs(true);
    hooks.useSetupStatus.mockReturnValue({ data: setupStatus(false) });
    hooks.useConnectInfo.mockReturnValue({
      data: connectInfo('https://sibyl.example.com', true),
      isLoading: false,
      isError: false,
    });

    const user = await startOnboarding();

    expect(await screen.findByText('Configure API Keys')).toBeInTheDocument();
    expect(screen.getByText('Step 2 of 5')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Set up models later' }));

    expect(await screen.findByText('Connect your tools')).toBeInTheDocument();
  });

  it('keeps an owner moving forward after saving keys refreshes the status', async () => {
    signInAs(true);
    hooks.useSetupStatus.mockReturnValue({ data: setupStatus(false) });
    hooks.useConnectInfo.mockReturnValue({
      data: connectInfo('https://sibyl.example.com', true),
      isLoading: false,
      isError: false,
    });
    const user = userEvent.setup();
    const { rerender } = render(<OnboardingWizard onComplete={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: /get started/i }));
    expect(await screen.findByText('Configure API Keys')).toBeInTheDocument();

    // Saving keys refetches setup status, which now reports providers ready.
    hooks.useSetupStatus.mockReturnValue({ data: setupStatus(true, ['anthropic', 'openai']) });
    rerender(<OnboardingWizard onComplete={vi.fn()} />);

    expect(screen.getByText('Step 2 of 5')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    expect(await screen.findByText('Connect your tools')).toBeInTheDocument();
    expect(screen.getByText('Step 3 of 5')).toBeInTheDocument();
  });
});

describe('OnboardingWizard with late data', () => {
  it('still offers the keys step when the status loads after Get Started', async () => {
    signInAs(true);
    hooks.useSetupStatus.mockReturnValue({ data: undefined });
    hooks.useConnectInfo.mockReturnValue({
      data: connectInfo('https://sibyl.example.com', true),
      isLoading: false,
      isError: false,
    });
    const user = userEvent.setup();
    const { rerender } = render(<OnboardingWizard onComplete={vi.fn()} />);
    await user.click(screen.getByRole('button', { name: /get started/i }));
    expect(await screen.findByText('Connect your tools')).toBeInTheDocument();

    hooks.useSetupStatus.mockReturnValue({ data: setupStatus(false) });
    rerender(<OnboardingWizard onComplete={vi.fn()} />);

    expect(screen.getByText('Step 3 of 5')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Back' }));
    expect(await screen.findByText('Configure API Keys')).toBeInTheDocument();
  });
});

describe('onboardingSteps', () => {
  it('adds the models step only for an admin on an unconfigured server', () => {
    expect(onboardingSteps(setupStatus(false), true)).toEqual([
      'welcome',
      'models',
      'connect',
      'project',
      'task',
    ]);
    expect(onboardingSteps(setupStatus(false), false)).not.toContain('models');
    expect(onboardingSteps(setupStatus(true, ['bedrock']), true)).not.toContain('models');
    expect(onboardingSteps(undefined, true)).not.toContain('models');
  });
});
