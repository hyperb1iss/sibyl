'use client';

import { AnimatePresence, motion } from 'motion/react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, Folder, Settings, User, Users } from '@/components/ui/icons';
import { useMe, useOrgs, useSwitchOrg } from '@/lib/hooks';

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
      await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' });
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
        className={`
          flex items-center gap-2 px-2 py-1.5 rounded-lg
          transition-all duration-200
          ${isOpen ? 'bg-sc-bg-highlight' : 'hover:bg-sc-bg-highlight/50'}
        `}
      >
        {/* Avatar */}
        {user.avatar_url ? (
          <img
            src={user.avatar_url}
            alt={user.name || 'User'}
            className="w-7 h-7 rounded-full border border-sc-fg-subtle/20"
          />
        ) : (
          <div className="w-7 h-7 rounded-full bg-sc-bg-highlight border border-sc-fg-subtle/20 flex items-center justify-center">
            <User width={14} height={14} className="text-sc-fg-muted" />
          </div>
        )}

        {/* Name (hidden on mobile) */}
        <span className="hidden md:block text-sm text-sc-fg-primary max-w-[120px] truncate">
          {user.name || user.email || 'User'}
        </span>

        <ChevronDown
          width={14}
          height={14}
          className={`text-sc-fg-muted transition-transform duration-200 ${isOpen ? 'rotate-180' : ''}`}
        />
      </button>

      <AnimatePresence>
        {isOpen && (
          <motion.div
            initial={{ opacity: 0, y: -8, scale: 0.95 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.95 }}
            transition={{ duration: 0.15 }}
            className="absolute right-0 top-full mt-2 w-56 rounded-lg bg-sc-bg-elevated border border-sc-fg-subtle/10 shadow-xl shadow-black/30 overflow-hidden z-50"
          >
            {/* User info header */}
            <div className="px-4 py-3 border-b border-sc-fg-subtle/10">
              <p className="text-sm font-medium text-sc-fg-primary truncate">
                {user.name || 'User'}
              </p>
              {user.email && <p className="text-xs text-sc-fg-muted truncate">{user.email}</p>}
            </div>

            {/* Menu items */}
            <div className="py-1">
              <Link
                href="/settings/profile"
                onClick={() => setIsOpen(false)}
                className="flex items-center gap-3 px-4 py-2.5 text-sm text-sc-fg-secondary hover:bg-sc-bg-highlight hover:text-sc-fg-primary transition-colors"
              >
                <User width={16} height={16} className="text-sc-cyan" />
                Your Profile
              </Link>
              <Link
                href="/settings"
                onClick={() => setIsOpen(false)}
                className="flex items-center gap-3 px-4 py-2.5 text-sm text-sc-fg-secondary hover:bg-sc-bg-highlight hover:text-sc-fg-primary transition-colors"
              >
                <Settings width={16} height={16} className="text-sc-cyan" />
                Settings
              </Link>
            </div>

            {/* Organization switcher */}
            {orgs.length > 0 && (
              <div className="border-t border-sc-fg-subtle/10 py-1">
                <div className="px-4 py-1.5 text-[10px] font-medium uppercase tracking-wider text-sc-fg-subtle">
                  Organizations
                </div>
                {orgs.map(org => (
                  <button
                    key={org.slug}
                    type="button"
                    onClick={() => handleSwitchOrg(org.slug)}
                    disabled={org.slug === currentOrg?.slug || switchOrg.isPending}
                    className={`w-full flex items-center gap-3 px-4 py-2 text-sm transition-colors ${
                      org.slug === currentOrg?.slug
                        ? 'text-sc-purple bg-sc-purple/5'
                        : 'text-sc-fg-secondary hover:bg-sc-bg-highlight hover:text-sc-fg-primary'
                    } disabled:opacity-50`}
                  >
                    {org.is_personal ? (
                      <Users width={14} height={14} className="text-sc-purple" />
                    ) : (
                      <Folder width={14} height={14} className="text-sc-cyan" />
                    )}
                    <span className="flex-1 text-left truncate">{org.name}</span>
                    {org.slug === currentOrg?.slug && (
                      <Check width={14} height={14} className="text-sc-purple" />
                    )}
                  </button>
                ))}
              </div>
            )}

            {/* Sign out */}
            <div className="border-t border-sc-fg-subtle/10 py-1">
              <button
                type="button"
                onClick={handleSignOut}
                className="w-full flex items-center gap-3 px-4 py-2.5 text-sm text-sc-red hover:bg-sc-red/10 transition-colors"
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
