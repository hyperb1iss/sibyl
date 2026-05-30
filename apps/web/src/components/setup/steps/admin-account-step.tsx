'use client';

import { type FormEvent, useState } from 'react';
import { User } from '@/components/ui/icons';
import { Spinner } from '@/components/ui/spinner';

interface AdminAccountStepProps {
  onBack: () => void;
  onAccountCreated: () => void;
}

export function AdminAccountStep({ onBack, onAccountCreated }: AdminAccountStepProps) {
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setIsSubmitting(true);
    setError(null);

    const formData = new FormData(e.currentTarget);
    const name = formData.get('name') as string;
    const email = formData.get('email') as string;
    const password = formData.get('password') as string;
    const confirmPassword = formData.get('confirmPassword') as string;

    // Client-side validation
    if (password !== confirmPassword) {
      setError('Passwords do not match');
      setIsSubmitting(false);
      return;
    }

    if (password.length < 8) {
      setError('Password must be at least 8 characters');
      setIsSubmitting(false);
      return;
    }

    try {
      // Use fetch directly to hit the signup endpoint
      const response = await fetch('/api/auth/local/signup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, email, password }),
        credentials: 'include', // Include cookies
      });

      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `Signup failed: ${response.status}`);
      }

      // Account created successfully - tokens are now in cookies
      onAccountCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create account');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className="p-8">
      {/* Header */}
      <div className="text-center mb-8">
        <div className="w-14 h-14 mx-auto mb-4 rounded-xl bg-gradient-to-br from-sc-purple/20 to-sc-coral/20 flex items-center justify-center">
          <User aria-hidden="true" width={28} height={28} className="text-sc-purple" />
        </div>
        <h2 className="text-xl font-semibold text-sc-fg-primary mb-2">Create Admin Account</h2>
        <p className="text-sc-fg-muted text-sm max-w-md mx-auto">
          Set up the first administrator account. This account will have full access to Sibyl.
        </p>
      </div>

      {/* Error */}
      {error && (
        <div className="mb-6 p-3 rounded-lg bg-sc-red/10 border border-sc-red/20 text-sc-red text-sm">
          {error}
        </div>
      )}

      {/* Form */}
      <form onSubmit={handleSubmit} className="space-y-4 mb-6">
        <div className="space-y-1.5">
          <label htmlFor="name" className="block text-xs font-medium text-sc-fg-muted">
            Name
          </label>
          <input
            id="name"
            name="name"
            type="text"
            required
            autoComplete="name"
            placeholder="Your name"
            className="w-full px-3 py-2.5 rounded-lg bg-sc-bg-base border border-sc-fg-subtle/20 text-sc-fg-primary placeholder:text-sc-fg-subtle/50 focus:outline-none focus:border-sc-purple/60 focus:ring-2 focus:ring-sc-purple/20 transition-all"
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="email" className="block text-xs font-medium text-sc-fg-muted">
            Email
          </label>
          <input
            id="email"
            name="email"
            type="email"
            required
            autoComplete="email"
            placeholder="admin@example.com"
            className="w-full px-3 py-2.5 rounded-lg bg-sc-bg-base border border-sc-fg-subtle/20 text-sc-fg-primary placeholder:text-sc-fg-subtle/50 focus:outline-none focus:border-sc-purple/60 focus:ring-2 focus:ring-sc-purple/20 transition-all"
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="password" className="block text-xs font-medium text-sc-fg-muted">
            Password
          </label>
          <input
            id="password"
            name="password"
            type="password"
            required
            autoComplete="new-password"
            minLength={8}
            placeholder="At least 8 characters"
            className="w-full px-3 py-2.5 rounded-lg bg-sc-bg-base border border-sc-fg-subtle/20 text-sc-fg-primary placeholder:text-sc-fg-subtle/50 focus:outline-none focus:border-sc-purple/60 focus:ring-2 focus:ring-sc-purple/20 transition-all"
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="confirmPassword" className="block text-xs font-medium text-sc-fg-muted">
            Confirm Password
          </label>
          <input
            id="confirmPassword"
            name="confirmPassword"
            type="password"
            required
            autoComplete="new-password"
            minLength={8}
            placeholder="Repeat password"
            className="w-full px-3 py-2.5 rounded-lg bg-sc-bg-base border border-sc-fg-subtle/20 text-sc-fg-primary placeholder:text-sc-fg-subtle/50 focus:outline-none focus:border-sc-purple/60 focus:ring-2 focus:ring-sc-purple/20 transition-all"
          />
        </div>

        {/* Buttons */}
        <div className="flex gap-3 pt-2">
          <button
            type="button"
            onClick={onBack}
            disabled={isSubmitting}
            className="flex-1 py-2.5 px-4 rounded-lg border border-sc-fg-subtle/20 text-sc-fg-secondary font-medium text-sm transition-colors hover:bg-sc-bg-base disabled:opacity-50"
          >
            Back
          </button>
          <button
            type="submit"
            disabled={isSubmitting}
            className="flex-1 py-2.5 px-4 rounded-lg bg-sc-purple text-white font-medium text-sm transition-all hover:bg-sc-purple/90 hover:shadow-lg hover:shadow-sc-purple/25 disabled:opacity-50 disabled:cursor-not-allowed flex items-center justify-center gap-2"
          >
            {isSubmitting ? (
              <>
                <Spinner size="sm" />
                Creating...
              </>
            ) : (
              'Create Account'
            )}
          </button>
        </div>
      </form>

      {/* Note */}
      <p className="text-xs text-sc-fg-subtle text-center">
        This will be the first user and will have administrator privileges.
      </p>
    </div>
  );
}
