'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';

export default function NotFound() {
  const router = useRouter();
  return (
    <div className="min-h-screen bg-sc-bg-dark flex items-center justify-center p-6">
      <div className="text-center max-w-md">
        {/* Glitchy 404 */}
        <div className="relative mb-8">
          <div className="text-[120px] font-bold leading-none tracking-tighter text-sc-purple/20 select-none">
            404
          </div>
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="text-[120px] font-bold leading-none tracking-tighter bg-gradient-to-r from-sc-purple to-sc-cyan bg-clip-text text-transparent animate-pulse">
              404
            </div>
          </div>
        </div>

        {/* Message */}
        <h1 className="text-2xl font-semibold text-sc-fg-primary mb-3">Lost in the void</h1>
        <p className="text-sc-fg-muted mb-8 leading-relaxed">
          The oracle couldn&apos;t find what you&apos;re looking for.
          <br />
          Perhaps it was never meant to be... or perhaps you just mistyped.
        </p>

        {/* Actions */}
        <div className="flex flex-col sm:flex-row gap-3 justify-center">
          <Link
            href="/"
            className="inline-flex items-center justify-center px-6 py-3 rounded-xl bg-gradient-to-r from-sc-purple to-sc-purple/80 text-white font-medium transition-all hover:opacity-90 hover:scale-[0.98]"
          >
            Return to safety
          </Link>
          <button
            type="button"
            onClick={() => router.back()}
            className="inline-flex items-center justify-center px-6 py-3 rounded-xl bg-sc-bg-elevated border border-sc-bg-surface text-sc-fg-muted font-medium transition-all hover:bg-sc-bg-surface hover:text-sc-fg-primary"
          >
            Go back
          </button>
        </div>

        {/* Decorative element */}
        <div className="mt-12 flex justify-center gap-2">
          {[...Array(5)].map((_, i) => (
            <div
              key={i}
              className="w-2 h-2 rounded-full bg-sc-purple/30"
              style={{
                animation: `pulse 1.5s ease-in-out ${i * 0.2}s infinite`,
              }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
