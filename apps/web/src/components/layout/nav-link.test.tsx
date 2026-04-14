import { beforeEach, describe, expect, it, vi } from 'vitest';
import { LayoutDashboard } from '@/components/ui/icons';
import { render, screen } from '@/test/utils';
import { NavLink } from './nav-link';

const navigationState = vi.hoisted(() => ({
  pathname: '/dashboard',
  searchParams: new URLSearchParams(),
}));

vi.mock('next/navigation', () => ({
  usePathname: () => navigationState.pathname,
  useSearchParams: () => navigationState.searchParams,
}));

describe('NavLink', () => {
  beforeEach(() => {
    navigationState.pathname = '/dashboard';
    navigationState.searchParams = new URLSearchParams();
  });

  it('renders an optional description block', () => {
    render(
      <NavLink href="/settings/profile" icon={LayoutDashboard} description="Personal settings">
        Profile
      </NavLink>
    );

    expect(screen.getByText('Profile')).toBeInTheDocument();
    expect(screen.getByText('Personal settings')).toBeInTheDocument();
  });

  it('preserves project context by default', () => {
    navigationState.searchParams = new URLSearchParams('projects=proj-a');

    render(
      <NavLink href="/tasks" icon={LayoutDashboard}>
        Tasks
      </NavLink>
    );

    expect(screen.getByRole('link', { name: 'Tasks' })).toHaveAttribute(
      'href',
      '/tasks?projects=proj-a'
    );
  });

  it('can opt out of project context preservation', () => {
    navigationState.searchParams = new URLSearchParams('projects=proj-a');

    render(
      <NavLink href="/settings/profile" icon={LayoutDashboard} preserveProjectsContext={false}>
        Profile
      </NavLink>
    );

    expect(screen.getByRole('link', { name: 'Profile' })).toHaveAttribute(
      'href',
      '/settings/profile'
    );
  });
});
