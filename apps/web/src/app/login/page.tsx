'use client';

import Image from 'next/image';
import { useSearchParams } from 'next/navigation';
import { Suspense, useState } from 'react';
import { Spinner } from '@/components/ui/spinner';

type AuthMode = 'signin' | 'signup';

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
  const searchParams = useSearchParams();
  const rawNext = searchParams.get('next');
  const error = searchParams.get('error');
  const next = getSafeRedirect(rawNext);

  const [mode, setMode] = useState<AuthMode>('signin');

  return (
    <div className="min-h-dvh flex flex-col items-center justify-center px-4 py-12 bg-sc-bg-dark">
      {/* Logo + Branding */}
      <div className="mb-8 flex flex-col items-center animate-fade-in group">
        <div className="relative mb-3">
          <div className="absolute -inset-4 rounded-2xl bg-sc-purple/10 blur-2xl opacity-0 group-hover:opacity-100 transition-opacity duration-500" />
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
        <div className="bg-sc-bg-elevated rounded-2xl border border-sc-fg-subtle/20 shadow-card-elevated overflow-hidden">
          {/* Tab Switcher */}
          <div className="flex border-b border-sc-fg-subtle/10">
            <button
              type="button"
              onClick={() => setMode('signin')}
              className={`flex-1 py-3 text-sm font-medium transition-all duration-200 relative ${
                mode === 'signin'
                  ? 'text-sc-fg-primary'
                  : 'text-sc-fg-muted hover:text-sc-fg-secondary'
              }`}
            >
              Sign In
              <span
                className={`absolute bottom-0 left-0 right-0 h-0.5 bg-sc-purple transition-transform duration-300 origin-left ${
                  mode === 'signin' ? 'scale-x-100' : 'scale-x-0'
                }`}
              />
            </button>
            <button
              type="button"
              onClick={() => setMode('signup')}
              className={`flex-1 py-3 text-sm font-medium transition-all duration-200 relative ${
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

          {/* Form Content - Fixed height container to prevent layout shift */}
          <div className="p-6">
            {error && (
              <div className="mb-4 text-sm px-3 py-2 rounded-lg border border-sc-red/30 bg-sc-red/10 text-sc-red animate-shake">
                {error === 'invalid_credentials' ? 'Invalid email or password.' : error}
              </div>
            )}

            {/* Fixed height wrapper - sized for 3-field form */}
            <div className="relative h-[280px]">
              <div
                className={`absolute inset-0 transition-all duration-300 ${
                  mode === 'signin'
                    ? 'opacity-100 translate-x-0 pointer-events-auto'
                    : 'opacity-0 -translate-x-4 pointer-events-none'
                }`}
              >
                <SignInForm next={next} />
              </div>
              <div
                className={`absolute inset-0 transition-all duration-300 ${
                  mode === 'signup'
                    ? 'opacity-100 translate-x-0 pointer-events-auto'
                    : 'opacity-0 translate-x-4 pointer-events-none'
                }`}
              >
                <SignUpForm next={next} />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

const inputClasses =
  'w-full px-3 py-2.5 rounded-lg bg-sc-bg-base border border-sc-fg-subtle/20 text-sc-fg-primary placeholder:text-sc-fg-subtle/50 focus:outline-none focus:border-sc-purple/60 focus:ring-2 focus:ring-sc-purple/20 transition-all duration-200';

function SignInForm({ next }: { next: string | null }) {
  return (
    <form action="/api/auth/local/login" method="post" className="h-full relative pb-14">
      {next && <input type="hidden" name="redirect" value={next} />}

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

        <div className="flex items-center justify-between">
          <label className="flex items-center gap-2 cursor-pointer group">
            <input
              type="checkbox"
              name="remember"
              className="w-4 h-4 rounded border-sc-fg-subtle/30 bg-sc-bg-base text-sc-purple focus:ring-sc-purple/30 focus:ring-offset-0 cursor-pointer"
            />
            <span className="text-xs text-sc-fg-muted group-hover:text-sc-fg-secondary transition-colors">
              Remember me
            </span>
          </label>
          <button
            type="button"
            className="text-xs text-sc-fg-muted hover:text-sc-purple transition-colors"
            onClick={() => alert('Password reset coming soon!')}
          >
            Forgot password?
          </button>
        </div>
      </div>

      <button
        type="submit"
        className="absolute bottom-0 left-0 right-0 w-full py-2.5 px-4 rounded-lg bg-sc-purple text-white font-medium text-sm transition-all duration-200 hover:bg-sc-purple/90 hover:shadow-lg hover:shadow-sc-purple/25 active:scale-[0.98] focus:outline-none focus:ring-2 focus:ring-sc-purple/50 focus:ring-offset-2 focus:ring-offset-sc-bg-elevated"
      >
        Sign In
      </button>
    </form>
  );
}

function SignUpForm({ next }: { next: string | null }) {
  return (
    <form action="/api/auth/local/signup" method="post" className="h-full relative pb-14">
      {next && <input type="hidden" name="redirect" value={next} />}

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

      <button
        type="submit"
        className="absolute bottom-0 left-0 right-0 w-full py-2.5 px-4 rounded-lg bg-sc-purple text-white font-medium text-sm transition-all duration-200 hover:bg-sc-purple/90 hover:shadow-lg hover:shadow-sc-purple/25 active:scale-[0.98] focus:outline-none focus:ring-2 focus:ring-sc-purple/50 focus:ring-offset-2 focus:ring-offset-sc-bg-elevated"
      >
        Create Account
      </button>
    </form>
  );
}
