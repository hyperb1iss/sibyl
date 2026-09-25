import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, userEvent } from '@/test/utils';

const hooks = vi.hoisted(() => ({ useConnectInfo: vi.fn() }));
vi.mock('@/lib/hooks/admin', () => ({ useConnectInfo: hooks.useConnectInfo }));

import { agentSentence, ConnectPanel, detectOs } from './connect-panel';

const SERVER = 'https://sibyl.example.com';
const CONNECT_INFO = {
  server_url: SERVER,
  server_version: '1.4.1',
  minimum_client_version: null,
  sso_enabled: true,
  local_auth_enabled: false,
  setup_command: `sibyl setup ${SERVER}`,
  install: {
    macos: `brew install hyperb1iss/tap/sibyl && sibyl setup ${SERVER}`,
    linux: `uv tool install --upgrade sibyl-dev && sibyl setup ${SERVER}`,
    windows: `uv tool install --upgrade sibyl-dev; sibyl setup ${SERVER}`,
  },
};

function mockUserAgent(value: string) {
  vi.spyOn(window.navigator, 'userAgent', 'get').mockReturnValue(value);
}

describe('ConnectPanel', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    hooks.useConnectInfo.mockReturnValue({ data: CONNECT_INFO, isLoading: false, isError: false });
  });

  it('shows one install line for the visitor OS, ending in setup for this server', () => {
    mockUserAgent('Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0)');

    render(<ConnectPanel />);

    expect(screen.getByText(CONNECT_INFO.install.macos)).toBeInTheDocument();
    expect(screen.queryByText(CONNECT_INFO.install.linux)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'macOS' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('switches the line when another OS is picked', async () => {
    mockUserAgent('Mozilla/5.0 (Macintosh)');
    const user = userEvent.setup();

    render(<ConnectPanel />);
    await user.click(screen.getByRole('button', { name: 'Linux' }));

    expect(screen.getByText(CONNECT_INFO.install.linux)).toBeInTheDocument();
    expect(screen.queryByText(CONNECT_INFO.install.macos)).not.toBeInTheDocument();
  });

  it('hands the agent one sentence pointing at /agent on this origin', async () => {
    const user = userEvent.setup();

    render(<ConnectPanel />);
    await user.click(screen.getByRole('tab', { name: 'Agent' }));

    expect(
      await screen.findByText(
        `Set up Sibyl on this machine by following ${window.location.origin}/agent`
      )
    ).toBeInTheDocument();
  });

  it('copies the command to the clipboard', async () => {
    mockUserAgent('Mozilla/5.0 (X11; Linux x86_64)');
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue();

    render(<ConnectPanel />);
    await user.click(screen.getByRole('button', { name: 'Copy command' }));

    expect(writeText).toHaveBeenCalledWith(CONNECT_INFO.install.linux);
  });

  it('selects the text when the clipboard is unavailable', async () => {
    mockUserAgent('Mozilla/5.0 (Windows NT 10.0)');
    const user = userEvent.setup();
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('denied'));

    render(<ConnectPanel />);
    await user.click(screen.getByRole('button', { name: 'Copy command' }));

    expect(await screen.findByText(/press ctrl\+c or ⌘c to copy/i)).toBeInTheDocument();
    expect(window.getSelection()?.toString()).toBe(CONNECT_INFO.install.windows);
  });

  it('never mentions API keys or MCP config', () => {
    render(<ConnectPanel />);

    expect(screen.queryByText(/api key/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/mcp/i)).not.toBeInTheDocument();
  });
});

describe('detectOs', () => {
  it.each([
    ['Mozilla/5.0 (Macintosh; Intel Mac OS X 15_0)', 'macos'],
    ['Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'windows'],
    ['Mozilla/5.0 (X11; Linux x86_64)', 'linux'],
  ])('maps %s to %s', (agent, os) => {
    expect(detectOs(agent)).toBe(os);
  });
});

describe('agentSentence', () => {
  it('points at /agent on the given origin', () => {
    expect(agentSentence(SERVER)).toBe(
      'Set up Sibyl on this machine by following https://sibyl.example.com/agent'
    );
  });
});
