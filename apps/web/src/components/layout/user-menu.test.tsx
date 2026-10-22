import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';
import { UserMenu } from './user-menu';

const hooks = vi.hoisted(() => ({
  useMe: vi.fn(),
  useOrgs: vi.fn(),
  useSwitchOrg: vi.fn(),
}));

vi.mock('@/lib/hooks/auth', () => ({
  useMe: hooks.useMe,
  useOrgs: hooks.useOrgs,
  useSwitchOrg: hooks.useSwitchOrg,
}));

describe('UserMenu', () => {
  beforeEach(() => {
    hooks.useOrgs.mockReturnValue({ data: { orgs: [] } });
    hooks.useSwitchOrg.mockReturnValue({ mutateAsync: vi.fn(), isPending: false });
  });

  it('shows a placeholder, not a sign-in link, while the profile loads', () => {
    hooks.useMe.mockReturnValue({ data: undefined, isLoading: true });

    render(<UserMenu />);

    expect(screen.getByRole('status', { name: 'Loading account' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Sign in' })).not.toBeInTheDocument();
  });

  it('offers sign-in once the profile request finishes without a user', () => {
    hooks.useMe.mockReturnValue({ data: undefined, isLoading: false });

    render(<UserMenu />);

    expect(screen.getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('opens the account menu for a signed-in user', () => {
    hooks.useMe.mockReturnValue({
      data: {
        user: { id: 'user_1', name: 'Ada', email: 'ada@example.com', avatar_url: null },
        organization: null,
      },
      isLoading: false,
    });

    render(<UserMenu />);

    expect(screen.getByRole('button', { name: 'Open user menu' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Sign in' })).not.toBeInTheDocument();
  });
});
