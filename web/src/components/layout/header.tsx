'use client';

import { useRouter } from 'next/navigation';
import { useState, useCallback, useEffect } from 'react';
import { useHealth } from '@/lib/hooks';

export function Header() {
  const router = useRouter();
  const { data: health } = useHealth();
  const [searchQuery, setSearchQuery] = useState('');

  const handleSearch = useCallback(() => {
    if (searchQuery.trim()) {
      router.push(`/search?q=${encodeURIComponent(searchQuery.trim())}`);
    }
  }, [router, searchQuery]);

  // Global keyboard shortcut
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        const input = document.getElementById('global-search') as HTMLInputElement;
        input?.focus();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  return (
    <header className="h-14 bg-sc-bg-base border-b border-sc-fg-subtle/20 flex items-center justify-between px-6">
      {/* Search */}
      <div className="flex-1 max-w-md">
        <div className="relative group">
          <label htmlFor="global-search" className="sr-only">
            Search knowledge base
          </label>
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sc-fg-muted group-focus-within:text-sc-purple transition-colors" aria-hidden="true">⌕</span>
          <input
            id="global-search"
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
            placeholder="Search knowledge... (⌘K)"
            className="w-full pl-10 pr-16 py-2 bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg text-sc-fg-primary placeholder:text-sc-fg-subtle focus:border-sc-purple focus:outline-none focus:ring-2 focus:ring-sc-purple/10 transition-all"
          />
          <kbd className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-sc-fg-subtle bg-sc-bg-base px-1.5 py-0.5 rounded border border-sc-fg-subtle/30 hidden md:block" aria-hidden="true">
            ⌘K
          </kbd>
        </div>
      </div>

      {/* Status */}
      <div className="flex items-center gap-4">
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-sc-bg-highlight/50">
          <span
            className={`w-2 h-2 rounded-full ${
              health?.status === 'healthy'
                ? 'bg-sc-green animate-pulse'
                : health?.status === 'unhealthy'
                  ? 'bg-sc-red'
                  : 'bg-sc-yellow animate-pulse'
            }`}
          />
          <span className="text-sm text-sc-fg-muted">
            {health?.status === 'healthy' ? 'Connected' : 'Disconnected'}
          </span>
        </div>
      </div>
    </header>
  );
}
