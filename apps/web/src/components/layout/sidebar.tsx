'use client';

import { AnimatePresence, motion } from 'motion/react';
import Image from 'next/image';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect } from 'react';
import {
  BookOpen,
  Boxes,
  FolderKanban,
  type IconComponent,
  Layers,
  LayoutDashboard,
  ListTodo,
  Network,
  Search,
  X,
} from '@/components/ui/icons';
import { APP_CONFIG } from '@/lib/constants';
import { useMobileNav } from './mobile-nav-context';
import { NavLink } from './nav-link';

// Navigation with Iconoir icons
const NAVIGATION: Array<{ name: string; href: string; icon: IconComponent }> = [
  { name: 'Dashboard', href: '/', icon: LayoutDashboard },
  { name: 'Projects', href: '/projects', icon: FolderKanban },
  { name: 'Epics', href: '/epics', icon: Layers },
  { name: 'Tasks', href: '/tasks', icon: ListTodo },
  { name: 'Sources', href: '/sources', icon: BookOpen },
  { name: 'Graph', href: '/graph', icon: Network },
  { name: 'Entities', href: '/entities', icon: Boxes },
  { name: 'Search', href: '/search', icon: Search },
];

interface SidebarContentProps {
  onNavClick?: () => void;
}

function SidebarContent({ onNavClick }: SidebarContentProps) {
  return (
    <>
      {/* Logo */}
      <div className="py-4 pr-4 pl-0 md:py-6 md:pr-6 md:pl-0 border-b border-sc-fg-subtle/10">
        <Link href="/" className="block text-center" onClick={onNavClick}>
          <Image
            src="/sibyl-logo.png"
            alt="Sibyl"
            width={180}
            height={52}
            className="h-12 w-auto mx-auto animate-logo-glow"
            priority
          />
          <p className="text-[11px] text-sc-fg-muted uppercase tracking-widest mt-1.5 text-center">
            Knowledge Oracle
          </p>
        </Link>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-3 md:p-4 space-y-1 overflow-y-auto">
        {NAVIGATION.map(item => (
          <NavLink key={item.name} href={item.href} icon={item.icon} onClick={onNavClick}>
            {item.name}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="p-3 md:p-4 border-t border-sc-fg-subtle/10">
        <div className="flex items-center justify-center text-[10px] text-sc-fg-subtle">
          <span className="uppercase tracking-wider">
            {APP_CONFIG.NAME} v{APP_CONFIG.VERSION}
          </span>
        </div>
      </div>
    </>
  );
}

export function Sidebar() {
  const { isOpen, close } = useMobileNav();
  const _pathname = usePathname();
  // Close mobile nav on route change
  useEffect(() => {
    close();
  }, [close]);

  // Close on escape key
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) {
        close();
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isOpen, close]);

  return (
    <>
      {/* Desktop Sidebar - hidden on mobile */}
      <aside className="hidden md:flex w-64 bg-sc-bg-base border-r border-sc-fg-subtle/10 flex-col shadow-[4px_0_20px_rgba(0,0,0,0.04),8px_0_32px_rgba(0,0,0,0.025)] dark:shadow-[4px_0_24px_rgba(0,0,0,0.5),8px_0_40px_rgba(0,0,0,0.3)]">
        <SidebarContent />
      </aside>

      {/* Mobile Drawer */}
      <AnimatePresence>
        {isOpen && (
          <>
            {/* Backdrop */}
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 md:hidden"
              onClick={close}
              aria-hidden="true"
            />

            {/* Drawer */}
            <motion.aside
              initial={{ x: '-100%' }}
              animate={{ x: 0 }}
              exit={{ x: '-100%' }}
              transition={{ type: 'spring', damping: 25, stiffness: 300 }}
              className="fixed inset-y-0 left-0 w-72 bg-sc-bg-base border-r border-sc-fg-subtle/10 flex flex-col z-50 md:hidden shadow-2xl shadow-black/50"
            >
              {/* Close button */}
              <button
                type="button"
                onClick={close}
                className="absolute top-4 right-4 p-2 rounded-lg text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
                aria-label="Close navigation"
              >
                <X width={20} height={20} />
              </button>

              <SidebarContent onNavClick={close} />
            </motion.aside>
          </>
        )}
      </AnimatePresence>
    </>
  );
}
