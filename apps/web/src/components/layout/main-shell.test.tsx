import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@/test/utils';
import { MainShell } from './main-shell';
import { MobileNavProvider } from './mobile-nav-context';

const hooks = vi.hoisted(() => ({
  useCreateEntity: vi.fn(),
  useMe: vi.fn(),
  useOrgs: vi.fn(),
  useProjects: vi.fn(),
  useProjectContext: vi.fn(),
  useSwitchOrg: vi.fn(),
}));

vi.mock('@/lib/hooks/auth', () => ({
  useMe: hooks.useMe,
  useOrgs: hooks.useOrgs,
  useSwitchOrg: hooks.useSwitchOrg,
}));
vi.mock('@/lib/hooks/graph', () => ({ useCreateEntity: hooks.useCreateEntity }));
vi.mock('@/lib/hooks/work-items', () => ({ useProjects: hooks.useProjects }));
vi.mock('@/lib/project-context', () => ({
  useProjectContext: hooks.useProjectContext,
}));
vi.mock('@/components/error-boundary', () => ({
  AsyncBoundary: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock('@/components/onboarding', () => ({
  OnboardingGate: ({ children }: { children: ReactNode }) => <>{children}</>,
}));
vi.mock('./sidebar', () => ({
  Sidebar: () => <div data-testid="sidebar" />,
}));

describe('MainShell', () => {
  beforeEach(() => {
    hooks.useCreateEntity.mockReturnValue({
      mutateAsync: vi.fn().mockResolvedValue({ id: 'entity_123' }),
      isPending: false,
    });
    hooks.useMe.mockReturnValue({ data: null });
    hooks.useOrgs.mockReturnValue({ data: { orgs: [] } });
    hooks.useProjects.mockReturnValue({ data: { entities: [] } });
    hooks.useProjectContext.mockReturnValue({
      selectedProjects: [],
      isAll: true,
      toggleProject: vi.fn(),
      setProjects: vi.fn(),
      selectProject: vi.fn(),
      clearProjects: vi.fn(),
      contextEnabled: false,
      scopeReady: true,
    });
    hooks.useSwitchOrg.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    });
  });

  it('keeps capture launchers out of the shell chrome', () => {
    render(
      <MobileNavProvider>
        <MainShell>
          <div>Shell content</div>
        </MainShell>
      </MobileNavProvider>
    );

    expect(screen.queryByRole('button', { name: /capture memory/i })).not.toBeInTheDocument();
  });

  it('holds the page until a project scope exists, then renders it', () => {
    hooks.useProjectContext.mockReturnValue({
      selectedProjects: [],
      isAll: false,
      toggleProject: vi.fn(),
      setProjects: vi.fn(),
      selectProject: vi.fn(),
      clearProjects: vi.fn(),
      contextEnabled: true,
      scopeReady: false,
    });

    const { rerender } = render(
      <MobileNavProvider>
        <MainShell>
          <div>Shell content</div>
        </MainShell>
      </MobileNavProvider>
    );

    expect(screen.queryByText('Shell content')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Choosing a project')).toBeInTheDocument();

    hooks.useProjectContext.mockReturnValue({
      selectedProjects: ['project_123'],
      isAll: false,
      toggleProject: vi.fn(),
      setProjects: vi.fn(),
      selectProject: vi.fn(),
      clearProjects: vi.fn(),
      contextEnabled: true,
      scopeReady: true,
    });
    rerender(
      <MobileNavProvider>
        <MainShell>
          <div>Shell content</div>
        </MainShell>
      </MobileNavProvider>
    );

    expect(screen.getByText('Shell content')).toBeInTheDocument();
  });

  it('opens the omnibox from the header search control', async () => {
    const { user } = render(
      <MobileNavProvider>
        <MainShell>
          <div>Shell content</div>
        </MainShell>
      </MobileNavProvider>
    );

    await user.click(screen.getByRole('button', { name: /search knowledge/i }));

    expect(screen.getByRole('dialog', { name: /command palette/i })).toBeInTheDocument();
  });

  it('omits capture from the global command palette', async () => {
    const { user } = render(
      <MobileNavProvider>
        <MainShell>
          <div>Shell content</div>
        </MainShell>
      </MobileNavProvider>
    );

    await user.keyboard('{Meta>}{Shift>}{k}{/Shift}{/Meta}');
    const palette = screen.getByRole('dialog', { name: /command palette/i });

    expect(
      within(palette).queryByRole('button', { name: /capture memory/i })
    ).not.toBeInTheDocument();
    expect(
      within(palette).queryByRole('option', { name: /capture memory/i })
    ).not.toBeInTheDocument();
  });
});
