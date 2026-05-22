import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAuthProviders, useSetupStatus } from '@/lib/hooks';
import { render, screen } from '@/test/utils';
import LoginPage from './page';

const routerReplace = vi.fn();
let searchParams = new URLSearchParams();

vi.mock('next/navigation', () => ({
  useRouter: () => ({
    replace: routerReplace,
    push: vi.fn(),
    prefetch: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    refresh: vi.fn(),
  }),
  useSearchParams: () => searchParams,
}));

vi.mock('next/image', () => ({
  default: ({
    priority,
    ...props
  }: React.ImgHTMLAttributes<HTMLImageElement> & { priority?: boolean }) => {
    void priority;
    const { alt = '', ...imageProps } = props;
    return <img alt={alt} {...imageProps} />;
  },
}));

vi.mock('@/lib/hooks', () => ({
  useAuthProviders: vi.fn(),
  useSetupStatus: vi.fn(),
}));

const setupStatus = {
  needs_setup: false,
  has_users: true,
  has_orgs: true,
  setup_complete: true,
  public_signups_enabled: false,
  openai_configured: false,
  anthropic_configured: false,
  gemini_configured: false,
  openai_valid: null,
  anthropic_valid: null,
  gemini_valid: null,
};

function authProvidersResult(data: ReturnType<typeof useAuthProviders>['data']) {
  return { data, isLoading: false } as unknown as ReturnType<typeof useAuthProviders>;
}

describe('LoginPage', () => {
  beforeEach(() => {
    routerReplace.mockClear();
    searchParams = new URLSearchParams();
    vi.mocked(useSetupStatus).mockReturnValue({
      data: setupStatus,
      isLoading: false,
    } as ReturnType<typeof useSetupStatus>);
    vi.mocked(useAuthProviders).mockReturnValue(
      authProvidersResult({ local_auth_enabled: true, providers: [] })
    );
  });

  it('hides account creation when public signups are disabled', () => {
    render(<LoginPage />);

    expect(screen.getByRole('button', { name: 'Sign In' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Create Account' })).not.toBeInTheDocument();
  });

  it('shows account creation when public signups are enabled', () => {
    vi.mocked(useSetupStatus).mockReturnValue({
      data: { ...setupStatus, public_signups_enabled: true },
      isLoading: false,
    } as ReturnType<typeof useSetupStatus>);

    render(<LoginPage />);

    expect(screen.getAllByRole('button', { name: 'Create Account' }).length).toBeGreaterThan(0);
  });

  it('shows account creation for invitation links', async () => {
    searchParams = new URLSearchParams({ invite: 'invite-token' });
    const { container, user } = render(<LoginPage />);

    expect(screen.getAllByRole('button', { name: 'Create Account' }).length).toBeGreaterThan(0);
    expect(screen.getByText('Invitation ready.')).toBeInTheDocument();
    expect(
      container.querySelector('form[action="/api/auth/local/signup"] input[name="invite_token"]')
    ).toHaveAttribute('value', 'invite-token');

    const signInTab = screen.getAllByRole('button', { name: 'Sign In' })[0];
    await user.click(signInTab);

    expect(signInTab).toHaveClass('text-sc-fg-primary');
    expect(
      container.querySelector('form[action="/api/auth/local/login"] input[name="invite_token"]')
    ).toHaveAttribute('value', 'invite-token');
  });

  it('hides local forms when local auth is disabled', () => {
    vi.mocked(useAuthProviders).mockReturnValue(
      authProvidersResult({
        local_auth_enabled: false,
        providers: [{ name: 'entra', label: 'Entra', login_url: '/api/auth/oidc/entra/login' }],
      })
    );

    const { container } = render(<LoginPage />);

    expect(screen.getByRole('link', { name: 'Entra' })).toHaveAttribute(
      'href',
      '/api/auth/oidc/entra/login'
    );
    expect(container.querySelector('form[action="/api/auth/local/login"]')).not.toBeInTheDocument();
    expect(
      container.querySelector('form[action="/api/auth/local/signup"]')
    ).not.toBeInTheDocument();
  });

  it('preserves safe next redirects on OIDC links', () => {
    searchParams = new URLSearchParams({ next: '/studio' });
    vi.mocked(useAuthProviders).mockReturnValue(
      authProvidersResult({
        local_auth_enabled: false,
        providers: [{ name: 'entra', label: 'Entra', login_url: '/api/auth/oidc/entra/login' }],
      })
    );

    render(<LoginPage />);

    expect(screen.getByRole('link', { name: 'Entra' })).toHaveAttribute(
      'href',
      '/api/auth/oidc/entra/login?redirect=%2Fstudio'
    );
  });
});
