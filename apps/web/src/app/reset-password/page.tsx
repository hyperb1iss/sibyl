'use client';

import Image from 'next/image';
import { useSearchParams } from 'next/navigation';
import { type FormEvent, Suspense, useState } from 'react';
import { Button } from '@/components/ui';
import { Spinner } from '@/components/ui/spinner';
import { api } from '@/lib/api';

const inputClasses =
  'w-full px-3 py-2.5 rounded-lg bg-sc-bg-highlight border border-sc-fg-subtle/20 text-sc-fg-primary placeholder:text-sc-fg-subtle/50 transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated';

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<ResetPasswordSkeleton />}>
      <ResetPasswordContent />
    </Suspense>
  );
}

function ResetPasswordSkeleton() {
  return (
    <div className="min-h-dvh flex flex-col items-center justify-center px-4 py-12 bg-sc-bg-dark">
      <Spinner size="lg" color="purple" />
    </div>
  );
}

function ResetPasswordContent() {
  const searchParams = useSearchParams();
  const token = searchParams.get('token') ?? '';
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isComplete, setIsComplete] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if (password !== confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    setIsSubmitting(true);
    try {
      await api.security.confirmPasswordReset({ token, new_password: password });
      setIsComplete(true);
    } catch {
      setError('Could not update that password. Request a new reset link.');
    } finally {
      setIsSubmitting(false);
    }
  }

  return (
    <div className="min-h-dvh flex flex-col items-center justify-center px-4 py-12 bg-sc-bg-dark">
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

      <div className="w-full max-w-sm animate-slide-up">
        <div className="bg-sc-bg-elevated rounded-xl border border-sc-fg-subtle/20 shadow-card-elevated overflow-hidden">
          <div className="border-b border-sc-fg-subtle/10 py-3 text-center text-sm font-medium text-sc-fg-primary">
            Reset Password
          </div>

          <div className="p-6">
            {!token ? (
              <div className="space-y-5">
                <div className="text-sm px-3 py-2 rounded-lg border border-sc-red/30 bg-sc-red/10 text-sc-red">
                  Reset link is missing or expired.
                </div>
                <a
                  href="/login"
                  className="flex w-full items-center justify-center rounded-lg bg-sc-purple px-4 py-2 text-sm font-medium text-sc-on-accent shadow-lg shadow-sc-purple/20 transition-all duration-200 hover:bg-sc-purple/80 hover:shadow-xl hover:shadow-sc-purple/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
                >
                  Back to Sign In
                </a>
              </div>
            ) : isComplete ? (
              <div className="space-y-5">
                <div className="text-sm px-3 py-2 rounded-lg border border-sc-green/30 bg-sc-green/10 text-sc-green">
                  Password updated.
                </div>
                <a
                  href="/login?reset=complete"
                  className="flex w-full items-center justify-center rounded-lg bg-sc-purple px-4 py-2 text-sm font-medium text-sc-on-accent shadow-lg shadow-sc-purple/20 transition-all duration-200 hover:bg-sc-purple/80 hover:shadow-xl hover:shadow-sc-purple/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated"
                >
                  Sign In
                </a>
              </div>
            ) : (
              <form aria-label="Set new password" onSubmit={handleSubmit} className="space-y-4">
                {error && (
                  <div className="text-sm px-3 py-2 rounded-lg border border-sc-red/30 bg-sc-red/10 text-sc-red">
                    {error}
                  </div>
                )}

                <div className="space-y-1.5">
                  <label
                    className="block text-xs font-medium text-sc-fg-muted"
                    htmlFor="new_password"
                  >
                    New Password
                  </label>
                  <input
                    id="new_password"
                    name="new_password"
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    maxLength={128}
                    required
                    value={password}
                    onChange={event => setPassword(event.target.value)}
                    className={inputClasses}
                    placeholder="At least 8 characters"
                  />
                </div>

                <div className="space-y-1.5">
                  <label
                    className="block text-xs font-medium text-sc-fg-muted"
                    htmlFor="confirm_password"
                  >
                    Confirm Password
                  </label>
                  <input
                    id="confirm_password"
                    name="confirm_password"
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    maxLength={128}
                    required
                    value={confirmPassword}
                    onChange={event => setConfirmPassword(event.target.value)}
                    className={inputClasses}
                    placeholder="Repeat your password"
                  />
                </div>

                <Button
                  type="submit"
                  variant="primary"
                  loading={isSubmitting}
                  className="w-full focus-visible:ring-offset-sc-bg-elevated"
                >
                  Update Password
                </Button>
              </form>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
