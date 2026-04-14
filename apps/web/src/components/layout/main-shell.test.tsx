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

vi.mock('@/lib/hooks', () => hooks);
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
    });
    hooks.useSwitchOrg.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    });
  });

  it('shows a global capture launcher in the shell and opens the dialog', async () => {
    const { user } = render(
      <MobileNavProvider>
        <MainShell>
          <div>Shell content</div>
        </MainShell>
      </MobileNavProvider>
    );

    await user.click(screen.getByRole('button', { name: /capture memory/i }));

    expect(screen.getByRole('heading', { name: /capture memory/i })).toBeInTheDocument();
  });

  it('opens the capture dialog from the global command palette path', async () => {
    const { user } = render(
      <MobileNavProvider>
        <MainShell>
          <div>Shell content</div>
        </MainShell>
      </MobileNavProvider>
    );

    await user.keyboard('{Meta>}{Shift>}{k}{/Shift}{/Meta}');
    const palette = screen.getByRole('dialog', { name: /command palette/i });
    await user.click(within(palette).getByRole('button', { name: /capture memory/i }));

    expect(screen.getByRole('heading', { name: /capture memory/i })).toBeInTheDocument();
  });
});
