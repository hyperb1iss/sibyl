'use client';

import Link from 'next/link';
import { Command, Database, Menu, Search } from '@/components/ui/icons';
import { ThemeToggleCompact } from '@/components/ui/theme-toggle';
import { useCommandPalette } from './command-palette-context';
import { useMobileNav } from './mobile-nav-context';
import { ProjectSelector } from './project-selector';
import { UserMenu } from './user-menu';

export function Header() {
  const { toggle } = useMobileNav();
  const { open: openCommandPalette } = useCommandPalette();

  return (
    <header className="h-14 bg-sc-bg-base border-b border-sc-fg-subtle/10 flex items-center justify-between px-3 md:px-6 gap-3 shadow-header z-40">
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
            <Database width={16} height={16} className="text-sc-on-accent" />
          </div>
          <span className="font-bold text-sc-fg-primary">Sibyl</span>
        </Link>
      </div>

      {/* Search launcher: looks like the old input, opens the omnibox */}
      <div className="flex-1 max-w-md hidden sm:block">
        <button
          type="button"
          onClick={openCommandPalette}
          aria-label="Search knowledge"
          aria-haspopup="dialog"
          aria-keyshortcuts="Meta+K Control+K"
          className={`
            group relative flex w-full items-center rounded-lg
            border border-sc-fg-subtle/20 bg-sc-bg-dark/80
            py-2.5 pl-10 pr-20 text-left text-sm text-sc-fg-muted/80
            transition-all duration-300
            hover:border-sc-purple/40 hover:text-sc-fg-muted
            hover:shadow-[0_0_12px_color-mix(in_oklch,var(--sc-purple)_12%,transparent)]
            focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan
            focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
          `}
        >
          <Search
            width={16}
            height={16}
            aria-hidden="true"
            className="absolute left-3 top-1/2 -translate-y-1/2 text-sc-fg-muted transition-all duration-300 group-hover:text-sc-purple group-hover:drop-shadow-[0_0_8px_color-mix(in_oklch,var(--sc-purple)_50%,transparent)]"
          />
          <span className="truncate">Search knowledge...</span>
          <span
            aria-hidden="true"
            className="absolute right-3 top-1/2 -translate-y-1/2 hidden md:flex items-center gap-1 rounded border border-sc-fg-subtle/20 bg-sc-bg-base/50 px-1.5 py-1 font-mono text-[10px] text-sc-fg-subtle transition-colors duration-300 group-hover:border-sc-purple/30 group-hover:bg-sc-purple/10 group-hover:text-sc-purple"
          >
            <Command width={10} height={10} />
            <span>K</span>
          </span>
        </button>
      </div>

      {/* Mobile Search Button */}
      <button
        type="button"
        onClick={openCommandPalette}
        className="sm:hidden p-2 rounded-lg text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
        aria-label="Search"
        aria-haspopup="dialog"
      >
        <Search width={20} height={20} />
      </button>

      {/* Right section: Project Selector + Theme + User Menu */}
      <div className="flex items-center gap-2 sm:gap-3">
        <ProjectSelector />
        <ThemeToggleCompact />
        <UserMenu />
      </div>
    </header>
  );
}
