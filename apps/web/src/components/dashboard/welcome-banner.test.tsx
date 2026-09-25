import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const hooks = vi.hoisted(() => ({
  useSetupStatus: vi.fn(),
  useOnboardingProgress: vi.fn(),
}));
const storage = vi.hoisted(() => ({
  getItem: vi.fn(),
  setItem: vi.fn(),
}));

vi.mock('@/lib/hooks/admin', () => ({ useSetupStatus: hooks.useSetupStatus }));
vi.mock('@/lib/hooks/auth', () => ({ useOnboardingProgress: hooks.useOnboardingProgress }));
vi.mock('@/components/dashboard/connect-agent-modal', () => ({
  ConnectAgentModal: () => <div data-testid="connect-agent-modal" />,
}));
vi.stubGlobal('localStorage', storage);

import { WelcomeBanner } from './welcome-banner';

describe('WelcomeBanner', () => {
  beforeEach(() => {
    storage.getItem.mockReset();
    storage.setItem.mockReset();
    storage.getItem.mockReturnValue(null);
    hooks.useSetupStatus.mockReturnValue({
      data: {
        openai_valid: false,
        anthropic_valid: false,
      },
    });
    hooks.useOnboardingProgress.mockReturnValue({
      checklist: {
        connected_agent: false,
        added_source: false,
        tried_search: false,
      },
      markConnectedAgent: vi.fn(),
      markAddedSource: vi.fn(),
      markTriedSearch: vi.fn(),
    });
  });

  it('points new users at the connect flow instead of MCP setup', () => {
    render(<WelcomeBanner totalEntities={0} />);

    expect(screen.getByText('Connect your tools')).toBeInTheDocument();
    expect(
      screen.getByText(/one line in your terminal, or hand it to your agent/i)
    ).toBeInTheDocument();
    expect(screen.queryByText(/mcp/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/api keys/i)).not.toBeInTheDocument();
  });

  it('reports models ready when the server configured a keyless provider', () => {
    hooks.useSetupStatus.mockReturnValue({
      data: {
        openai_configured: false,
        anthropic_configured: false,
        gemini_configured: false,
        providers_configured: true,
        configured_providers: ['bedrock'],
      },
    });

    render(<WelcomeBanner totalEntities={0} />);

    expect(screen.getByText('Models ready')).toBeInTheDocument();
    expect(screen.queryByText('Models need setup')).not.toBeInTheDocument();
  });

  it('reports models need setup when no provider is ready', () => {
    render(<WelcomeBanner totalEntities={0} />);

    expect(screen.getByText('Models need setup')).toBeInTheDocument();
  });

  it('shows no model status until the server has answered', () => {
    hooks.useSetupStatus.mockReturnValue({ data: undefined });

    render(<WelcomeBanner totalEntities={0} />);

    expect(screen.queryByText('Models need setup')).not.toBeInTheDocument();
    expect(screen.queryByText('Models ready')).not.toBeInTheDocument();
  });
});
