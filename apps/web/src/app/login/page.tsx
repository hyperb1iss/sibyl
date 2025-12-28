import { GradientButton } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { Github, Sparkles } from '@/components/ui/icons';

interface PageProps {
  searchParams: Promise<{ next?: string; error?: string }>;
}

export default async function LoginPage({ searchParams }: PageProps) {
  const { next, error } = await searchParams;

  return (
    <div className="min-h-dvh flex items-center justify-center px-4">
      <Card variant="elevated" className="w-full max-w-md">
        <CardHeader
          title="Sign in to Sibyl"
          description="Connect with GitHub to unlock your persistent memory."
          icon={<Sparkles width={22} height={22} />}
        />

        <div className="space-y-4">
          {error ? (
            <div className="text-sm px-3 py-2 rounded-lg border border-sc-red/30 bg-sc-red/10 text-sc-red">
              {error === 'invalid_credentials' ? 'Invalid email or password.' : error}
            </div>
          ) : null}

          <p className="text-sm text-sc-fg-muted">
            You’ll be redirected back here after authentication.
          </p>

          <form action="/api/auth/github" method="get">
            {next ? <input type="hidden" name="redirect" value={next} /> : null}
            <GradientButton
              type="submit"
              gradient="purple-cyan"
              size="lg"
              className="w-full"
              icon={<Github width={18} height={18} />}
              spark
            >
              Continue with GitHub
            </GradientButton>
          </form>

          <div className="flex items-center gap-3">
            <div className="h-px flex-1 bg-sc-fg-subtle/10" />
            <span className="text-[10px] font-medium tracking-wide uppercase text-sc-fg-subtle">
              Or
            </span>
            <div className="h-px flex-1 bg-sc-fg-subtle/10" />
          </div>

          <form action="/api/auth/local/login" method="post" className="space-y-3">
            {next ? <input type="hidden" name="redirect" value={next} /> : null}
            <div className="space-y-2">
              <label className="block text-xs font-medium text-sc-fg-muted" htmlFor="email">
                Email
              </label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                required
                className="w-full px-3 py-2 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10 text-sc-fg-primary placeholder:text-sc-fg-subtle/60 focus:outline-none focus:border-sc-purple/50"
                placeholder="you@domain.com"
              />
            </div>
            <div className="space-y-2">
              <label className="block text-xs font-medium text-sc-fg-muted" htmlFor="password">
                Password
              </label>
              <input
                id="password"
                name="password"
                type="password"
                autoComplete="current-password"
                required
                className="w-full px-3 py-2 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10 text-sc-fg-primary placeholder:text-sc-fg-subtle/60 focus:outline-none focus:border-sc-purple/50"
                placeholder="••••••••"
              />
            </div>
            <GradientButton
              type="submit"
              gradient="purple-coral"
              size="lg"
              className="w-full"
              spark
            >
              Sign in
            </GradientButton>
          </form>

          <details className="rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-highlight/20 p-3">
            <summary className="cursor-pointer text-xs font-medium text-sc-fg-muted select-none">
              Create a local account
            </summary>
            <form action="/api/auth/local/signup" method="post" className="mt-3 space-y-3">
              {next ? <input type="hidden" name="redirect" value={next} /> : null}
              <div className="space-y-2">
                <label className="block text-xs font-medium text-sc-fg-muted" htmlFor="name">
                  Name
                </label>
                <input
                  id="name"
                  name="name"
                  type="text"
                  autoComplete="name"
                  required
                  className="w-full px-3 py-2 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10 text-sc-fg-primary placeholder:text-sc-fg-subtle/60 focus:outline-none focus:border-sc-cyan/50"
                  placeholder="Bliss"
                />
              </div>
              <div className="space-y-2">
                <label
                  className="block text-xs font-medium text-sc-fg-muted"
                  htmlFor="signup_email"
                >
                  Email
                </label>
                <input
                  id="signup_email"
                  name="email"
                  type="email"
                  autoComplete="email"
                  required
                  className="w-full px-3 py-2 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10 text-sc-fg-primary placeholder:text-sc-fg-subtle/60 focus:outline-none focus:border-sc-cyan/50"
                  placeholder="you@domain.com"
                />
              </div>
              <div className="space-y-2">
                <label
                  className="block text-xs font-medium text-sc-fg-muted"
                  htmlFor="signup_password"
                >
                  Password
                </label>
                <input
                  id="signup_password"
                  name="password"
                  type="password"
                  autoComplete="new-password"
                  minLength={8}
                  required
                  className="w-full px-3 py-2 rounded-lg bg-sc-bg-highlight/50 border border-sc-fg-subtle/10 text-sc-fg-primary placeholder:text-sc-fg-subtle/60 focus:outline-none focus:border-sc-cyan/50"
                  placeholder="At least 8 characters"
                />
              </div>
              <GradientButton
                type="submit"
                gradient="cyan-coral"
                size="lg"
                className="w-full"
                spark
              >
                Create account
              </GradientButton>
            </form>
          </details>

          <div className="text-xs text-sc-fg-subtle/70 space-y-1">
            <p>Tip: You can also authenticate the CLI with an API key.</p>
            <p className="font-mono">sibyl auth api-key create</p>
          </div>
        </div>
      </Card>
    </div>
  );
}
