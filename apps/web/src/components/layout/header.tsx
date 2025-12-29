'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';
import { Command, Menu, Search, Sparkles } from '@/components/ui/icons';
import { useMobileNav } from './mobile-nav-context';
import { UserMenu } from './user-menu';

export function Header() {
  const router = useRouter();
  const { toggle } = useMobileNav();
  const [searchQuery, setSearchQuery] = useState('');
  const [isFocused, setIsFocused] = useState(false);

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
    <header className="h-14 bg-sc-bg-base border-b border-sc-fg-subtle/10 flex items-center justify-between px-3 md:px-6 gap-3 shadow-header z-10">
      {/* Mobile: Hamburger + Logo */}
      <div className="flex items-center gap-2 md:hidden">
        <button
          type="button"
          onClick={toggle}
          className="p-2 -ml-1 rounded-lg text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
          aria-label="Open navigation menu"
        >
          <Menu width={22} height={22} />
        </button>
        <Link href="/" className="flex items-center gap-2">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral flex items-center justify-center">
            <Sparkles width={16} height={16} className="text-white" />
          </div>
          <span className="font-bold text-sc-fg-primary">Sibyl</span>
        </Link>
      </div>

      {/* Search - responsive */}
      <div className="flex-1 max-w-md hidden sm:block">
        <div className={`relative transition-all duration-300 ${isFocused ? 'scale-[1.02]' : ''}`}>
          <label htmlFor="global-search" className="sr-only">
            Search knowledge base
          </label>

          <Search
            width={16}
            height={16}
            className={`absolute left-3 top-1/2 -translate-y-1/2 transition-all duration-300 ${
              isFocused
                ? 'text-sc-purple drop-shadow-[0_0_8px_rgba(225,53,255,0.5)]'
                : 'text-sc-fg-muted'
            }`}
          />

          <input
            id="global-search"
            type="search"
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && handleSearch()}
            onFocus={() => setIsFocused(true)}
            onBlur={() => setIsFocused(false)}
            placeholder="Search knowledge..."
            className={`
              w-full pl-10 pr-20 py-2 rounded-lg
              bg-sc-bg-highlight/50 border text-sc-fg-primary
              placeholder:text-sc-fg-subtle/60
              transition-all duration-300
              focus:outline-none
              ${
                isFocused
                  ? 'border-sc-purple/50 shadow-[0_0_20px_rgba(225,53,255,0.15),inset_0_0_20px_rgba(225,53,255,0.05)]'
                  : 'border-sc-fg-subtle/10 hover:border-sc-fg-subtle/20'
              }
            `}
          />

          {/* Keyboard hint */}
          <div
            className={`
              absolute right-3 top-1/2 -translate-y-1/2
              text-[10px] font-mono px-1.5 py-1 rounded
              border hidden md:flex items-center gap-1
              transition-all duration-300
              ${
                isFocused
                  ? 'bg-sc-purple/20 border-sc-purple/30 text-sc-purple'
                  : 'bg-sc-bg-base/50 border-sc-fg-subtle/20 text-sc-fg-subtle'
              }
            `}
          >
            <Command width={10} height={10} />
            <span>K</span>
          </div>
        </div>
      </div>

      {/* Mobile Search Button */}
      <button
        type="button"
        onClick={() => router.push('/search')}
        className="sm:hidden p-2 rounded-lg text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
        aria-label="Search"
      >
        <Search width={20} height={20} />
      </button>

      {/* User Menu */}
      <UserMenu />
    </header>
  );
}
