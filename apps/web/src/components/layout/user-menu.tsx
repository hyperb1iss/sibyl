'use client';

import { AnimatePresence, motion } from 'motion/react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, Folder, Settings, User, Users } from '@/components/ui/icons';
import { authApi } from '@/lib/api/auth';
import { useMe, useOrgs, useSwitchOrg } from '@/lib/hooks/auth';

export function UserMenu() {
  const router = useRouter();
  const { data: me } = useMe();
  const { data: orgsData } = useOrgs();
  const switchOrg = useSwitchOrg();
  const [isOpen, setIsOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  const user = me?.user;
  const currentOrg = me?.organization;

  const orgs = useMemo(() => orgsData?.orgs ?? [], [orgsData?.orgs]);

  // Close on click outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  // Close on escape
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setIsOpen(false);
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, []);

  const handleSignOut = useCallback(async () => {
    try {
      await authApi.logout();
      router.push('/login');
      router.refresh();
    } catch {
      // Still navigate to login on error
      router.push('/login');
    }
  }, [router]);

  const handleSwitchOrg = useCallback(
    async (slug: string) => {
      await switchOrg.mutateAsync(slug);
      setIsOpen(false);
      router.refresh();
    },
    [switchOrg, router]
  );

  if (!user) {
    return (
      <Link
        href="/login"
        className="px-3 py-1.5 text-sm font-medium text-sc-fg-muted hover:text-sc-fg-primary transition-colors"
      >
        Sign in
      </Link>
    );
  }

  return (
    <div ref={menuRef} className="relative">
      <button
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        aria-label="Open user menu"
        aria-haspopup="menu"
        aria-expanded={isOpen}
        className={`
          flex items-center gap-2.5 px-2.5 py-1.5 rounded-lg
          border transition-colors duration-200
          focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan
          focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base
          ${
            isOpen
              ? 'bg-sc-bg-highlight border-sc-purple/30 shadow-[0_0_16px_color-mix(in_oklch,var(--sc-purple)_15%,transparent)]'
              : 'border-transparent hover:bg-sc-bg-highlight/50 hover:border-sc-fg-subtle/20'
          }
        `}
      >
        {/* Avatar */}
        {user.avatar_url ? (
          <img
            src={user.avatar_url}
            alt={user.name || 'User'}
            className={`w-8 h-8 rounded-full border-2 transition-all duration-200 ${
              isOpen
                ? 'border-sc-purple/50 shadow-[0_0_12px_color-mix(in_oklch,var(--sc-purple)_30%,transparent)]'
                : 'border-sc-fg-subtle/20 hover:border-sc-purple/30'
            }`}
          />
        ) : (
          <div
            className={`w-8 h-8 rounded-full bg-gradient-to-br from-sc-purple/20 to-sc-magenta/20 border-2 flex items-center justify-center transition-all duration-200 ${
              isOpen
                ? 'border-sc-purple/50 shadow-[0_0_12px_color-mix(in_oklch,var(--sc-purple)_30%,transparent)]'
                : 'border-sc-fg-subtle/20'
            }`}
          >
            <User width={14} height={14} className="text-sc-purple" />
          </div>
        )}

        {/* Name (hidden on mobile) */}
        <span className="hidden md:block text-sm font-medium text-sc-fg-primary max-w-[120px] truncate">
          {user.name || user.email || 'User'}
        </span>

        <ChevronDown
          width={14}
          height={14}
          className={`text-sc-fg-muted transition-all duration-200 ${isOpen ? 'rotate-180 text-sc-purple' : ''}`}
        />
      </button>

      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ opacity: 0, y: -8, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.95 }}
            transition={{ duration: 0.15, ease: 'easeOut' }}
            role="menu"
            className="absolute right-0 top-full mt-2 w-60 rounded-xl bg-sc-bg-elevated border border-sc-purple/20 shadow-glow-purple overflow-hidden z-50"
          >
            {/* User info header */}
            <div className="px-4 py-3.5 border-b border-sc-fg-subtle/10 bg-gradient-to-r from-sc-purple/5 to-transparent">
              <p className="text-sm font-semibold text-sc-fg-primary truncate">
                {user.name || 'User'}
              </p>
              {user.email && (
                <p className="text-xs text-sc-fg-muted truncate mt-0.5">{user.email}</p>
              )}
            </div>

            {/* Menu items */}
            <div className="py-1.5">
              <Link
                href="/settings/profile"
                role="menuitem"
                onClick={() => setIsOpen(false)}
                className="flex items-center gap-3 px-4 py-2.5 text-sm text-sc-fg-muted hover:bg-sc-purple/10 hover:text-sc-fg-primary transition-colors duration-200 group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-inset"
              >
                <User
                  width={16}
                  height={16}
                  className="text-sc-cyan group-hover:text-sc-purple transition-colors"
                />
                Your Profile
              </Link>
              <Link
                href="/settings"
                role="menuitem"
                onClick={() => setIsOpen(false)}
                className="flex items-center gap-3 px-4 py-2.5 text-sm text-sc-fg-muted hover:bg-sc-purple/10 hover:text-sc-fg-primary transition-colors duration-200 group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-inset"
              >
                <Settings
                  width={16}
                  height={16}
                  className="text-sc-cyan group-hover:text-sc-purple transition-colors"
                />
                Settings
              </Link>
            </div>

            {/* Organization switcher */}
            {orgs.length > 0 && (
              <div className="border-t border-sc-fg-subtle/10 py-1.5">
                <div className="px-4 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-sc-fg-subtle">
                  Organizations
                </div>
                {orgs.map(org => (
                  <button
                    key={org.slug}
                    type="button"
                    role="menuitem"
                    onClick={() => handleSwitchOrg(org.slug)}
                    disabled={org.slug === currentOrg?.slug || switchOrg.isPending}
                    className={`w-full flex items-center gap-3 px-4 py-2 text-sm transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-inset ${
                      org.slug === currentOrg?.slug
                        ? 'text-sc-purple bg-sc-purple/10'
                        : 'text-sc-fg-muted hover:bg-sc-purple/10 hover:text-sc-fg-primary'
                    } disabled:cursor-default`}
                  >
                    {org.is_personal ? (
                      <Users width={14} height={14} className="text-sc-purple" />
                    ) : (
                      <Folder width={14} height={14} className="text-sc-cyan" />
                    )}
                    <span className="flex-1 text-left truncate">{org.name}</span>
                    {org.slug === currentOrg?.slug && (
                      <Check width={14} height={14} className="text-sc-green" />
                    )}
                  </button>
                ))}
              </div>
            )}

            {/* Sign out */}
            <div className="border-t border-sc-fg-subtle/10 py-1.5">
              <button
                type="button"
                role="menuitem"
                onClick={handleSignOut}
                className="w-full flex items-center gap-3 px-4 py-2.5 text-sm text-sc-fg-muted hover:text-sc-red hover:bg-sc-red/10 transition-colors duration-200 group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-red focus-visible:ring-inset"
              >
                <svg
                  width={16}
                  height={16}
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={2}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  aria-label="Sign out"
                  role="img"
                >
                  <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
                  <polyline points="16 17 21 12 16 7" />
                  <line x1="21" y1="12" x2="9" y2="12" />
                </svg>
                Sign out
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
