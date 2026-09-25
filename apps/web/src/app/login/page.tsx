'use client';

import Image from 'next/image';
import { useRouter, useSearchParams } from 'next/navigation';
import { type FormEvent, Suspense, useEffect, useState } from 'react';
import { Button } from '@/components/ui';
import { Spinner } from '@/components/ui/spinner';
import { type AuthProvider, api } from '@/lib/api';
import { useAuthProviders, useSetupStatus } from '@/lib/hooks';

type AuthMode = 'signin' | 'signup' | 'reset';

const ERROR_MESSAGES: Record<string, string> = {
  account_conflict: 'Could not create that account.',
  authentication_failed: 'Authentication failed. Please try again.',
  invalid_credentials: 'Invalid email or password.',
  invalid_invitation: 'Invitation is not valid for this account.',
  local_auth_disabled: 'Use your organization sign-in provider.',
  break_glass_reason_required: 'Enter an incident reason for break-glass access.',
  break_glass_reason_too_long: 'Break-glass access reason must be 512 characters or fewer.',
  signup_disabled: 'Account creation requires an invitation.',
};

/**
 * Validate redirect URL to prevent open redirect attacks.
 * Only allows relative URLs (starting with /).
 */
function getSafeRedirect(url: string | null): string | null {
  if (!url) return null;
  if (url.startsWith('/') && !url.startsWith('//')) {
    return url;
  }
  return null;
}

function getProviderHref(provider: AuthProvider, next: string | null): string {
  if (!next) {
    return provider.login_url;
  }
  const params = new URLSearchParams({ redirect: next });
  return `${provider.login_url}?${params.toString()}`;
}

export default function LoginPage() {
  return (
    <Suspense fallback={<LoginSkeleton />}>
      <LoginContent />
    </Suspense>
  );
}

function LoginSkeleton() {
  return (
    <div className="min-h-dvh flex flex-col items-center justify-center px-4 py-12 bg-sc-bg-dark">
      <Spinner size="lg" color="purple" />
    </div>
  );
}

function LoginContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const rawNext = searchParams.get('next');
  const error = searchParams.get('error');
  const inviteToken = searchParams.get('invite');
  const resetComplete = searchParams.get('reset') === 'complete';
  const next = getSafeRedirect(rawNext);

  const [mode, setMode] = useState<AuthMode>('signin');

  // Check if setup is needed (no users exist)
  const { data: setupStatus, isLoading: isCheckingSetup } = useSetupStatus();
  const { data: authProviders, isLoading: isCheckingProviders } = useAuthProviders({
    enabled: setupStatus?.needs_setup !== true,
  });

  // Redirect to /setup if this is a fresh install
  useEffect(() => {
    if (setupStatus?.needs_setup) {
      router.replace('/setup');
    }
  }, [setupStatus, router]);

  const oidcProviders = authProviders?.providers ?? [];
  const localAuthEnabled = authProviders?.local_auth_enabled ?? true;
  const breakGlassEnabled = authProviders?.break_glass_enabled ?? false;
  const allowSignup =
    localAuthEnabled && Boolean(setupStatus?.public_signups_enabled || inviteToken);
  const errorMessage = error
    ? (ERROR_MESSAGES[error] ?? 'Something went wrong. Please try again.')
    : null;

  useEffect(() => {
    if (inviteToken) {
      setMode('signup');
    }
  }, [inviteToken]);

  useEffect(() => {
    if (!allowSignup && mode === 'signup') {
      setMode('signin');
    }
  }, [allowSignup, mode]);

  // Show loading while checking setup status
  if (isCheckingSetup || isCheckingProviders || setupStatus?.needs_setup) {
    return (
      <div className="min-h-dvh flex flex-col items-center justify-center px-4 py-12 bg-sc-bg-dark">
        <Spinner size="lg" color="purple" />
        <p className="mt-4 text-sc-fg-muted text-sm">
          {setupStatus?.needs_setup ? 'Redirecting to setup...' : 'Loading...'}
        </p>
      </div>
    );
  }

  return (
    <div className="min-h-dvh flex flex-col items-center justify-center px-4 py-12 bg-sc-bg-dark">
      {/* Logo + Branding */}
      <div className="mb-8 flex flex-col items-center animate-fade-in group">
        <div className="relative mb-3">
          <div className="absolute -inset-4 rounded-xl bg-sc-purple/10 blur-2xl opacity-0 group-hover:opacity-100 transition-opacity duration-500" />
          <Image
            src="/sibyl-logo.png"
            alt="Sibyl"
            width={200}
            height={58}
            className="h-14 w-auto relative z-10 animate-logo-glow"
            priority
          />
        </div>
        <p className="tagline text-[11px] uppercase tracking-[0.1em] font-medium">
          <span className="tagline-word">Collective</span>
          <span className="tagline-separator mx-1.5">·</span>
          <span className="tagline-word">Intelligence</span>
        </p>
      </div>

      {/* Auth Card */}
      <div className="w-full max-w-sm animate-slide-up">
        <div className="bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/20 shadow-card-elevated overflow-hidden">
          {/* Tab Switcher */}
          {allowSignup ? (
            <div className="flex border-b border-sc-fg-subtle/10">
              <button
                type="button"
                onClick={() => setMode('signin')}
                className={`flex-1 py-3 text-sm font-medium transition-colors duration-200 relative rounded-t-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-inset ${
                  mode === 'signin' || mode === 'reset'
                    ? 'text-sc-fg-primary'
                    : 'text-sc-fg-muted hover:text-sc-fg-secondary'
                }`}
              >
                Sign In
                <span
                  className={`absolute bottom-0 left-0 right-0 h-0.5 bg-sc-purple transition-transform duration-300 origin-left ${
                    mode === 'signin' || mode === 'reset' ? 'scale-x-100' : 'scale-x-0'
                  }`}
                />
              </button>
              <button
                type="button"
                onClick={() => setMode('signup')}
                className={`flex-1 py-3 text-sm font-medium transition-colors duration-200 relative rounded-t-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-inset ${
                  mode === 'signup'
                    ? 'text-sc-fg-primary'
                    : 'text-sc-fg-muted hover:text-sc-fg-secondary'
                }`}
              >
                Create Account
                <span
                  className={`absolute bottom-0 left-0 right-0 h-0.5 bg-sc-purple transition-transform duration-300 origin-right ${
                    mode === 'signup' ? 'scale-x-100' : 'scale-x-0'
                  }`}
                />
              </button>
            </div>
          ) : (
            <div className="border-b border-sc-fg-subtle/10 py-3 text-center text-sm font-medium text-sc-fg-primary">
              Sign In
            </div>
          )}

          {/* Form Content - Fixed height container to prevent layout shift */}
          <div className="p-6">
            {resetComplete && (
              <div className="mb-4 text-sm px-3 py-2 rounded-lg border border-sc-green/30 bg-sc-green/10 text-sc-green">
                Password updated. Sign in with your new password.
              </div>
            )}
            {errorMessage && (
              <div className="mb-4 text-sm px-3 py-2 rounded-lg border border-sc-red/30 bg-sc-red/10 text-sc-red animate-shake">
                {errorMessage}
              </div>
            )}
            {inviteToken && !errorMessage && (
              <div className="mb-4 text-sm px-3 py-2 rounded-lg border border-sc-cyan/30 bg-sc-cyan/10 text-sc-cyan">
                Invitation ready.
              </div>
            )}

            {oidcProviders.length > 0 && <OIDCProviderList providers={oidcProviders} next={next} />}

            {localAuthEnabled ? (
              <div className="relative h-[300px]">
                <div
                  className={`absolute inset-0 transition-all duration-300 ${
                    mode === 'signin'
                      ? 'opacity-100 translate-x-0 pointer-events-auto'
                      : 'opacity-0 -translate-x-4 pointer-events-none'
                  }`}
                >
                  <SignInForm
                    next={next}
                    inviteToken={inviteToken}
                    breakGlassEnabled={breakGlassEnabled}
                    onForgotPassword={() => setMode('reset')}
                  />
                </div>
                <div
                  className={`absolute inset-0 transition-all duration-300 ${
                    mode === 'reset'
                      ? 'opacity-100 translate-x-0 pointer-events-auto'
                      : 'opacity-0 translate-x-4 pointer-events-none'
                  }`}
                >
                  <PasswordResetRequestForm onBack={() => setMode('signin')} />
                </div>
                {allowSignup && (
                  <div
                    className={`absolute inset-0 transition-all duration-300 ${
                      mode === 'signup'
                        ? 'opacity-100 translate-x-0 pointer-events-auto'
                        : 'opacity-0 translate-x-4 pointer-events-none'
                    }`}
                  >
                    <SignUpForm next={next} inviteToken={inviteToken} />
                  </div>
                )}
              </div>
            ) : oidcProviders.length === 0 ? (
              <div className="text-sm text-sc-fg-muted">No sign-in providers are configured.</div>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}

const inputClasses =
  'w-full px-3 py-2.5 rounded-lg bg-sc-bg-highlight border border-sc-fg-subtle/20 text-sc-fg-primary placeholder:text-sc-fg-subtle/50 transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated';

function OIDCProviderList({ providers, next }: { providers: AuthProvider[]; next: string | null }) {
  return (
    <div className="mb-5 space-y-2">
      {providers.map(provider => (
        <a
          key={provider.name}
          href={getProviderHref(provider, next)}
          className="flex w-full items-center justify-center rounded-lg border border-sc-cyan/30 bg-sc-cyan/10 px-3 py-2.5 text-sm font-medium text-sc-cyan transition-colors duration-200 hover:border-sc-cyan/60 hover:bg-sc-cyan/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
        >
          {provider.label}
        </a>
      ))}
      <div className="pt-2 text-center text-[11px] uppercase tracking-[0.1em] text-sc-fg-subtle">
        Organization Sign In
      </div>
    </div>
  );
}

function SignInForm({
  next,
  inviteToken,
  breakGlassEnabled,
  onForgotPassword,
}: {
  next: string | null;
  inviteToken: string | null;
  breakGlassEnabled: boolean;
  onForgotPassword: () => void;
}) {
  return (
    <form action="/api/auth/local/login" method="post" className="h-full relative pb-14">
      <input type="hidden" name="redirect" value={next || '/'} />
      {inviteToken && <input type="hidden" name="invite_token" value={inviteToken} />}

      <div className="space-y-4">
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-sc-fg-muted" htmlFor="email">
            Email
          </label>
          <input
            id="email"
            name="email"
            type="email"
            autoComplete="email"
            required
            className={inputClasses}
            placeholder="you@example.com"
          />
        </div>

        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-sc-fg-muted" htmlFor="password">
            Password
          </label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            className={inputClasses}
            placeholder="Enter your password"
          />
        </div>

        {breakGlassEnabled && (
          <div className="space-y-1.5">
            <label
              className="block text-xs font-medium text-sc-fg-muted"
              htmlFor="break_glass_reason"
            >
              Incident reason
            </label>
            <textarea
              id="break_glass_reason"
              name="break_glass_reason"
              required
              maxLength={512}
              className={`${inputClasses} min-h-24 resize-y`}
              placeholder="Incident or change record for this emergency access"
            />
          </div>
        )}

        <div className="flex items-center justify-between">
          <label className="flex items-center gap-2 cursor-pointer group">
            <input
              type="checkbox"
              name="remember"
              className="w-4 h-4 rounded border-sc-fg-subtle/30 bg-sc-bg-highlight text-sc-purple cursor-pointer transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            />
            <span className="text-xs text-sc-fg-muted group-hover:text-sc-fg-secondary transition-colors duration-200">
              Remember me
            </span>
          </label>
          <button
            type="button"
            className="text-xs text-sc-fg-muted hover:text-sc-purple transition-colors duration-200 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
            onClick={onForgotPassword}
          >
            Forgot password?
          </button>
        </div>
      </div>

      <Button
        type="submit"
        variant="primary"
        className="absolute bottom-0 left-0 right-0 w-full focus-visible:ring-offset-sc-bg-elevated"
      >
        Sign In
      </Button>
    </form>
  );
}

function PasswordResetRequestForm({ onBack }: { onBack: () => void }) {
  const [email, setEmail] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setIsSubmitting(true);
    setMessage(null);
    setError(null);

    try {
      const response = await api.security.requestPasswordReset({ email });
      setMessage(response.message);
    } catch {
      setError('Could not send a reset email. Try again in a moment.');
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <form aria-label="Reset password" onSubmit={handleSubmit} className="h-full relative pb-20">
      <div className="space-y-4">
        <div>
          <h2 className="text-sm font-semibold text-sc-fg-primary">Reset Password</h2>
          <p className="mt-1 text-xs text-sc-fg-muted">
            We'll send a reset link if the account exists.
          </p>
        </div>

        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-sc-fg-muted" htmlFor="reset_email">
            Email
          </label>
          <input
            id="reset_email"
            name="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={event => setEmail(event.target.value)}
            className={inputClasses}
            placeholder="you@example.com"
          />
        </div>

        {message && (
          <div className="text-xs px-3 py-2 rounded-lg border border-sc-green/30 bg-sc-green/10 text-sc-green">
            {message}
          </div>
        )}
        {error && (
          <div className="text-xs px-3 py-2 rounded-lg border border-sc-red/30 bg-sc-red/10 text-sc-red">
            {error}
          </div>
        )}
      </div>

      <div className="absolute bottom-0 left-0 right-0 flex gap-2">
        <Button
          type="button"
          variant="secondary"
          className="flex-1 focus-visible:ring-offset-sc-bg-elevated"
          onClick={onBack}
        >
          Back
        </Button>
        <Button
          type="submit"
          variant="primary"
          loading={isSubmitting}
          className="flex-1 focus-visible:ring-offset-sc-bg-elevated"
        >
          Send Link
        </Button>
      </div>
    </form>
  );
}

function SignUpForm({ next, inviteToken }: { next: string | null; inviteToken: string | null }) {
  return (
    <form action="/api/auth/local/signup" method="post" className="h-full relative pb-14">
      <input type="hidden" name="redirect" value={next || '/'} />
      {inviteToken && <input type="hidden" name="invite_token" value={inviteToken} />}

      <div className="space-y-4">
        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-sc-fg-muted" htmlFor="signup_name">
            Name
          </label>
          <input
            id="signup_name"
            name="name"
            type="text"
            autoComplete="name"
            required
            className={inputClasses}
            placeholder="Your name"
          />
        </div>

        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-sc-fg-muted" htmlFor="signup_email">
            Email
          </label>
          <input
            id="signup_email"
            name="email"
            type="email"
            autoComplete="email"
            required
            className={inputClasses}
            placeholder="you@example.com"
          />
        </div>

        <div className="space-y-1.5">
          <label className="block text-xs font-medium text-sc-fg-muted" htmlFor="signup_password">
            Password
          </label>
          <input
            id="signup_password"
            name="password"
            type="password"
            autoComplete="new-password"
            minLength={8}
            required
            className={inputClasses}
            placeholder="At least 8 characters"
          />
        </div>
      </div>

      <Button
        type="submit"
        variant="primary"
        className="absolute bottom-0 left-0 right-0 w-full focus-visible:ring-offset-sc-bg-elevated"
      >
        Create Account
      </Button>
    </form>
  );
}
